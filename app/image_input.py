"""Image input (识图) bridge: client image parts -> Notion AI chat attachments.

NOTION_IMAGE_INPUT=on|off (default on). Parsing (app.schemas / responses) turns each image
part into an in-text marker ⟦n2a-img:<id>⟧ (id = sha256 of the image reference) and keeps the
reference here. Right before dispatch (NotionOpusAPI._stream_response_raw) each marker in a
user step is replaced by an `attachment` step placed before that user step, after uploading
the image to that account (Notion web flow):
  getUploadFileUrlForAssistantChatTranscriptUpload -> S3 POST (fields + file)
  -> processAgentAttachment (moderation / metadata) -> poll task_output.
Uploads are cached per (image id, account) in memory + data/image_uploads.json, so earlier
images of a conversation are never re-uploaded. All uploads of an account go to one
holder thread (created by the first upload) to avoid one stray Notion chat per image.
Any failure -> the marker becomes the old "could not be passed to you" text (logged).
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Optional

import requests

from app.logger import logger

PLACEHOLDER = ("\n[An image was attached here, but it could not be passed to you: "
               "you cannot see it. If the request depends on it, say so.]\n")
MARKER_RE = re.compile(r"\n?⟦n2a-img:([0-9a-f]{24})⟧\n?")
_ALLOWED = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}

_lock = threading.Lock()
_refs: "OrderedDict[str, str]" = OrderedDict()   # id -> data URL / http(s) URL
_uploads: dict = {}                               # "id|account" -> attachment info
_holders: dict = {}                               # account -> holder thread id
_upload_locks: dict = {}
_loaded = False


def enabled() -> bool:
    return os.getenv("NOTION_IMAGE_INPUT", "on").strip().lower() not in ("0", "off", "false", "no", "")


def _max_bytes() -> int:
    return int(float(os.getenv("NOTION_IMAGE_MAX_MB", "10") or 10) * 1024 * 1024)


def _store_path() -> str:
    return os.getenv("NOTION_IMAGE_STORE", "/app/data/image_uploads.json")


def _load():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_store_path()) as f:
            d = json.load(f)
        _uploads.update(d.get("uploads") or {})
        _holders.update(d.get("holders") or {})
    except Exception:
        pass


def _save():
    try:
        items = sorted(_uploads.items(), key=lambda kv: kv[1].get("ts", 0))[-5000:]
        tmp = _store_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"uploads": dict(items), "holders": _holders}, f)
        os.replace(tmp, _store_path())
    except Exception as exc:
        logger.warning("Image store save failed", extra={"request_info": {"event": "image_store_error", "error": type(exc).__name__}})


def _ref_of(part: dict) -> Optional[str]:
    t = part.get("type")
    if t in ("image_url", "input_image"):
        v = part.get("image_url")
        if isinstance(v, dict):
            v = v.get("url")
        return v if isinstance(v, str) and v else None
    if t == "image":  # Anthropic block
        src = part.get("source") or {}
        if src.get("type") == "base64" and src.get("data"):
            return "data:%s;base64,%s" % (src.get("media_type") or "image/png", src["data"])
        if src.get("type") == "url" and src.get("url"):
            return src["url"]
    return None


def part_to_text(part: dict) -> str:
    """Text standing in for an image part: a marker (feature on) or the placeholder."""
    if not enabled():
        return PLACEHOLDER
    ref = _ref_of(part)
    if not ref or not (ref.startswith("data:") or ref.startswith("http://") or ref.startswith("https://")):
        return PLACEHOLDER
    iid = hashlib.sha256(ref.encode()).hexdigest()[:24]
    with _lock:
        _refs[iid] = ref
        _refs.move_to_end(iid)
        while len(_refs) > 300:
            _refs.popitem(last=False)
    return "\n⟦n2a-img:%s⟧\n" % iid


def _sniff(b: bytes) -> Optional[str]:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "image/webp"
    return None


def _fetch(ref: str) -> tuple:
    cap = _max_bytes()
    if ref.startswith("data:"):
        head, _, data = ref.partition(",")
        if ";base64" not in head:
            raise ValueError("data URL not base64")
        if len(data) > cap * 4 // 3 + 16:
            raise ValueError("image too large")
        raw = base64.b64decode(data)
    else:
        with requests.get(ref, stream=True, timeout=20, headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            buf = bytearray()
            for chunk in r.iter_content(65536):
                buf += chunk
                if len(buf) > cap:
                    raise ValueError("image too large")
            raw = bytes(buf)
    if len(raw) > cap:
        raise ValueError("image too large")
    ct = _sniff(raw)
    if ct not in _ALLOWED:
        raise ValueError("unsupported image type")
    return raw, ct


def _poll_metadata(client, out_key: str, space_id: str) -> Optional[dict]:
    deadline = time.time() + float(os.getenv("NOTION_IMAGE_PROCESS_TIMEOUT", "20") or 20)
    while time.time() < deadline:
        time.sleep(1.0)
        code, b = client.api_post("syncRecordValuesSpace", {"requests": [
            {"pointer": {"table": "task_output", "id": out_key, "spaceId": space_id}, "version": -1}]}, timeout=15)
        v = (((b or {}).get("recordMap") or {}).get("task_output") or {}).get(out_key) or {}
        res = ((((v.get("value") or {}).get("value") or {}).get("value") or {}).get("result")) or {}
        if res.get("type") == "success":
            return (res.get("data") or {}).get("stepMetadata") or {}
        if res.get("type") == "error":
            raise RuntimeError("processing: %s" % ((res.get("data") or {}).get("code")))
    raise RuntimeError("processing timeout")


def _upload(client, iid: str, ref: str) -> dict:
    raw, ct = _fetch(ref)
    acct = getattr(client, "account_key", "")
    name = "image-%s.%s" % (iid[:12], _ALLOWED[ct])
    holder = _holders.get(acct)
    tid = holder or str(uuid.uuid4())
    ptr = {"table": "thread", "id": tid, "spaceId": client.space_id}
    code, r = client.api_post("getUploadFileUrlForAssistantChatTranscriptUpload", {
        "name": name, "contentType": ct, "assistantChatTranscriptSessionPointer": ptr,
        "contentLength": len(raw), "createThread": holder is None, "threadType": "workflow"}, timeout=20)
    if code != 200 or not isinstance(r, dict) or not r.get("url") or not r.get("signedUploadPostUrl"):
        if holder is not None:  # holder thread gone? start a new one next time
            _holders.pop(acct, None)
        raise RuntimeError("upload url HTTP %s" % code)
    up = requests.post(r["signedUploadPostUrl"], data=r.get("fields") or {},
                       files={"file": (name, raw, ct)}, timeout=60)
    if up.status_code not in (200, 201, 204):
        raise RuntimeError("s3 HTTP %s" % up.status_code)
    if holder is None:
        _holders[acct] = r.get("chatId") or tid
        ptr["id"] = _holders[acct]
    meta = {}
    try:
        code, p = client.api_post("processAgentAttachment", {
            "url": r["url"], "spaceId": client.space_id, "aiSessionPointer": ptr, "permissionRecord": ptr,
            "source": "user_upload", "clientVersion": os.getenv("NOTION_CLIENT_VERSION", "") or "23.13.0",
            "model": "apricot-sorbet-high"}, timeout=20)
        if code == 200 and isinstance(p, dict) and p.get("outputKey"):
            meta = _poll_metadata(client, p["outputKey"], p.get("spaceId") or client.space_id)
        else:
            raise RuntimeError("process HTTP %s" % code)
    except Exception as exc:
        logger.warning("Image processing failed; attaching without metadata", extra={"request_info": {
            "event": "image_process_error", "account": acct, "image": iid, "error": str(exc)[:200]}})
    if (meta.get("moderation") or {}).get("status") == "flagged":
        raise RuntimeError("image flagged by moderation")
    return {"url": r["url"], "name": name, "ct": ct, "ptr": ptr, "meta": meta, "bytes": len(raw), "ts": time.time()}


def get_attachment(client, iid: str) -> Optional[dict]:
    acct = getattr(client, "account_key", "")
    key = "%s|%s" % (iid, acct)
    with _lock:
        _load()
        hit = _uploads.get(key)
        if hit:
            return hit
        lk = _upload_locks.setdefault(key, threading.Lock())
        ref = _refs.get(iid)
    with lk:
        with _lock:
            hit = _uploads.get(key)
        if hit:
            return hit
        if not ref:
            raise RuntimeError("image reference not available")
        t0 = time.time()
        info = _upload(client, iid, ref)
        with _lock:
            _uploads[key] = info
            _save()
        logger.info("Image uploaded to Notion", extra={"request_info": {
            "event": "image_uploaded", "account": acct, "image": iid, "bytes": info["bytes"],
            "content_type": info["ct"], "ms": int((time.time() - t0) * 1000),
            "moderation": (info["meta"].get("moderation") or {}).get("status")}})
        return info


def _strip(v: Any) -> Any:
    if isinstance(v, str):
        return MARKER_RE.sub(lambda m: PLACEHOLDER, v)
    if isinstance(v, list):
        return [_strip(x) for x in v]
    if isinstance(v, dict):
        return {k: _strip(x) for k, x in v.items()}
    return v


def _resend_cap() -> int:
    try:
        return int(os.getenv("NOTION_RESEND_MAX_IMAGES", "4") or 0)
    except ValueError:
        return 4


OMITTED = "\n[earlier screenshot omitted]\n"


def expand_transcript(client, transcript: list, fresh_thread: bool = False) -> list:
    """Replace markers in user steps with attachment steps (before the user step)."""
    if "n2a-img:" not in json.dumps(transcript, ensure_ascii=False):
        return transcript
    out = []
    stats = {"attached": 0, "failed": 0, "omitted": 0}
    keep = None  # None = attach all
    cap = _resend_cap()
    if fresh_thread and cap > 0:
        # full-history resend: only the latest `cap` images (plus every image of the final
        # user turn) become attachments; older ones turn into a short text note
        order, last_user_ids = [], []
        last_ai = max([k for k, s in enumerate(transcript) if s.get("type") == "agent-inference"], default=-1)
        for k, s in enumerate(transcript):
            if s.get("type") == "user":
                ids = MARKER_RE.findall(json.dumps(s.get("value"), ensure_ascii=False))
                order += ids
                if k > last_ai:
                    last_user_ids += ids
        if len(dict.fromkeys(order)) > cap:
            keep = set(list(dict.fromkeys(reversed(order)))[:cap]) | set(last_user_ids)
    for step in transcript:
        if step.get("type") != "user" or not enabled():
            out.append(_strip(step) if step.get("type") != "config" else step)
            continue
        text = json.dumps(step.get("value"), ensure_ascii=False)
        ids = MARKER_RE.findall(text)
        if not ids:
            out.append(step)
            continue
        failed = set()
        omitted = set()
        for iid in dict.fromkeys(ids):
            if keep is not None and iid not in keep:
                omitted.add(iid)
                stats["omitted"] += 1
                continue
            try:
                a = get_attachment(client, iid)
                out.append({"id": str(uuid.uuid4()), "type": "attachment", "fileUrl": a["url"],
                            "fileName": a["name"], "contentType": a["ct"], "permissionRecord": a["ptr"],
                            "metadata": {**(a.get("meta") or {}), "attachmentSource": "user_upload"}})
                stats["attached"] += 1
            except Exception as exc:
                failed.add(iid)
                stats["failed"] += 1
                logger.warning("Image attach failed; using placeholder", extra={"request_info": {
                    "event": "image_attach_error", "account": getattr(client, "account_key", ""),
                    "image": iid, "error": str(exc)[:200]}})

        def sub(v):
            if isinstance(v, str):
                return MARKER_RE.sub(lambda m: OMITTED if m.group(1) in omitted else PLACEHOLDER if m.group(1) in failed else
                                     "\n[image: see attached file image-%s]\n" % m.group(1)[:12], v)
            if isinstance(v, list):
                return [sub(x) for x in v]
            return v
        out.append({**step, "value": sub(step.get("value"))})
    if stats["attached"] or stats["failed"] or stats["omitted"]:
        logger.info("Image attachments", extra={"request_info": {"event": "image_attachments", **stats,
                                                                 "account": getattr(client, "account_key", "")}})
    return out

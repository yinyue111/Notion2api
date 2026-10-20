"""Scoped API keys (in addition to the global API_KEY, which is unchanged).

SCOPED_API_KEYS_FILE (default /app/data/scoped_api_keys.json), managed from the admin UI
(#keys, /admin/api/keys) or by hand:
  {"keys": [{"id": "k_…", "name": "gpt-sol", "sha256": "<hex of the key>",
             "models": ["gpt-6.1-sol"], "all_models": false, "disabled": false,
             "hint": "sk-n2a-AbC…wXyZ", "note": "", "created": "…", "created_at": 0,
             "updated_at": 0, "expires_at": null}]}
Each entry stores SHA-256 (for auth) and, going forward, the plaintext in the `key`
field (file mode 0600) so the admin UI can reveal/copy it. Legacy entries may lack
`key` until rebound or regenerated. A request authenticated with a scoped key may only use the listed models
(aliases / effort suffixes resolve to the same Notion model; `*` globs like "claude-*" are
allowed); others get 403 model_not_allowed, and /v1/models lists only the allowed ones.
all_models=true lifts the restriction. Legacy entries (only name/sha256/models): an empty
or missing model list means all models, exactly as before; disabled or expired keys are
rejected like an unknown key (401).
"""
from __future__ import annotations

import copy
import fnmatch
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from typing import Optional

from app.logger import logger

_cache: dict = {"sig": None, "doc": {"keys": []}}
_lock = threading.RLock()
KEY_PREFIX = "sk-n2a-"
MIN_CUSTOM_LEN = 16


def _path() -> str:
    return os.getenv("SCOPED_API_KEYS_FILE", "/app/data/scoped_api_keys.json")


def _sig(p: str):
    try:
        st = os.stat(p)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _load() -> dict:
    """The whole document (cached by mtime/size/inode). Never raises."""
    p = _path()
    sig = _sig(p)
    if sig is None:
        return {"keys": []}
    with _lock:
        if _cache["sig"] != sig:
            try:
                with open(p, encoding="utf-8") as f:
                    d = json.load(f)
                if not isinstance(d, dict) or not isinstance(d.get("keys", []), list):
                    raise ValueError("bad shape")
                d.setdefault("keys", [])
            except Exception as exc:
                logger.warning(f"scoped api keys file unreadable: {exc}")
                d = {"keys": []}
            _cache.update(sig=sig, doc=d)
        return _cache["doc"]


def _save(doc: dict) -> None:
    p = _path()
    d = os.path.dirname(p) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".scoped_api_keys.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    with _lock:
        _cache.update(sig=_sig(p), doc=doc)


# ---------------- request path ----------------
def is_all_models(k: dict) -> bool:
    if "all_models" in k:
        return bool(k.get("all_models"))
    return not (k.get("models") or [])  # legacy: no list = unrestricted (unchanged)


def is_active(k: dict, now: Optional[float] = None) -> bool:
    if not isinstance(k, dict) or not k.get("sha256") or k.get("disabled"):
        return False
    exp = k.get("expires_at")
    if isinstance(exp, (int, float)) and exp > 0 and (now or time.time()) >= exp:
        return False
    return True


def _keys() -> list:
    return [k for k in _load().get("keys") or [] if is_active(k)]


def lookup(bearer: str) -> Optional[dict]:
    if not bearer:
        return None
    h = hashlib.sha256(bearer.encode()).hexdigest()
    for k in _keys():
        if hmac.compare_digest(h, str(k["sha256"]).lower()):
            return k
    return None


def allowed_for(k: dict) -> Optional[list]:
    """None = every model; otherwise the allow-list (possibly empty = nothing)."""
    return None if is_all_models(k) else list(k.get("models") or [])


def model_allowed(allowed, model: str) -> bool:
    if allowed is None:
        return True
    if not model:
        return False
    from app import workspaces as ws
    if model in allowed:
        return True
    nid = ws.notion_id(model)
    for a in allowed:
        a = str(a)
        if any(ch in a for ch in "*?["):
            if fnmatch.fnmatchcase(model, a):
                return True
            continue
        if nid and ws.notion_id(a) == nid:
            return True
    return False


# ---------------- management (admin UI) ----------------
def _new_id() -> str:
    return "k_" + secrets.token_hex(6)


def _hint(plain: str) -> str:
    return plain[:11] + "…" + plain[-4:] if len(plain) > 18 else plain[:4] + "…"


def _migrate(doc: dict) -> bool:
    """Give legacy entries an id / timestamps (hash, name and models untouched)."""
    changed = False
    seen = set()
    for k in doc.get("keys") or []:
        if not isinstance(k, dict):
            continue
        if not k.get("id") or k["id"] in seen:
            k["id"] = _new_id()
            changed = True
        seen.add(k["id"])
        if "all_models" not in k:
            k["all_models"] = is_all_models(k)
            changed = True
        if "disabled" not in k:
            k["disabled"] = False
            changed = True
    return changed


class KeyError_(ValueError):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def _public(k: dict) -> dict:
    out = {kk: copy.deepcopy(v) for kk, v in k.items() if kk not in ("sha256", "key")}
    out["enabled"] = not k.get("disabled")
    out["all_models"] = is_all_models(k)
    out["models"] = list(k.get("models") or [])
    out["legacy"] = not k.get("hint")
    out["has_plaintext"] = bool(k.get("key"))
    out["fingerprint"] = "sha256:" + str(k.get("sha256") or "")[:8]
    exp = k.get("expires_at")
    out["expired"] = bool(isinstance(exp, (int, float)) and exp > 0 and time.time() >= exp)
    return out


def list_keys() -> list:
    with _lock:
        doc = copy.deepcopy(_load())
        if _migrate(doc):
            _save(doc)
        return [_public(k) for k in doc.get("keys") or [] if isinstance(k, dict)]


def _clean_models(models) -> list:
    out = []
    for m in models or []:
        m = str(m).strip()
        if m and m not in out and len(m) <= 100:
            out.append(m)
    return out[:200]


def _check_name(doc: dict, name: str, exclude_id: Optional[str] = None) -> str:
    name = (name or "").strip()
    if not name or len(name) > 64:
        raise KeyError_("名称必填，最长 64 个字符")
    for k in doc.get("keys") or []:
        if k.get("id") != exclude_id and str(k.get("name") or "").strip().lower() == name.lower():
            raise KeyError_("名称已存在（请求日志按名称区分 Key）", 409)
    return name


def _check_plain(doc: dict, plain: str, master_keys=()) -> str:
    h = hashlib.sha256(plain.encode()).hexdigest()
    for k in doc.get("keys") or []:
        if hmac.compare_digest(h, str(k.get("sha256") or "").lower()):
            raise KeyError_("这个 Key 已存在", 409)
    for m in master_keys:
        if m and hmac.compare_digest(plain, m):
            raise KeyError_("不能与主 API_KEY 相同", 409)
    return h


def _gen_plain() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def create_key(name: str, models=None, all_models: bool = False, note: str = "", enabled: bool = True,
               expires_at: Optional[float] = None, custom_key: Optional[str] = None, master_keys=()) -> tuple:
    """-> (public entry, plaintext). Plaintext is stored in the file (mode 0600) for admin reveal."""
    with _lock:
        doc = copy.deepcopy(_load())
        _migrate(doc)
        name = _check_name(doc, name)
        if custom_key is not None and custom_key.strip():
            plain = custom_key.strip()
            if len(plain) < MIN_CUSTOM_LEN or len(plain) > 200 or any(c.isspace() for c in plain):
                raise KeyError_(f"自定义 Key 至少 {MIN_CUSTOM_LEN} 个字符且不含空白")
        else:
            plain = _gen_plain()
        h = _check_plain(doc, plain, master_keys)
        now = time.time()
        k = {"id": _new_id(), "name": name, "sha256": h, "key": plain, "models": _clean_models(models),
             "all_models": bool(all_models), "disabled": not enabled, "hint": _hint(plain),
             "note": (note or "").strip()[:500],
             "created": time.strftime("%Y-%m-%d %H:%M UTC+8", time.gmtime(now + 8 * 3600)),
             "created_at": int(now), "updated_at": int(now),
             "expires_at": int(expires_at) if expires_at else None}
        doc["keys"].append(k)
        _save(doc)
    logger.info("Scoped API key created", extra={"request_info": {"event": "api_key_created", "name": name,
                                                                   "all_models": bool(all_models), "models": len(k["models"])}})
    return _public(k), plain


def _find(doc: dict, key_id: str) -> dict:
    for k in doc.get("keys") or []:
        if k.get("id") == key_id:
            return k
    raise KeyError_("Key 不存在", 404)


_UNSET = object()


def update_key(key_id: str, *, name=None, models=None, all_models=None, note=None, enabled=None,
               expires_at=_UNSET) -> dict:
    with _lock:
        doc = copy.deepcopy(_load())
        _migrate(doc)
        k = _find(doc, key_id)
        if name is not None:
            k["name"] = _check_name(doc, name, exclude_id=key_id)
        if models is not None:
            k["models"] = _clean_models(models)
        if all_models is not None:
            k["all_models"] = bool(all_models)
        if note is not None:
            k["note"] = str(note).strip()[:500]
        if enabled is not None:
            k["disabled"] = not bool(enabled)
        if expires_at is not _UNSET:
            k["expires_at"] = int(expires_at) if expires_at else None
        k["updated_at"] = int(time.time())
        _save(doc)
    logger.info("Scoped API key updated", extra={"request_info": {"event": "api_key_updated", "id": key_id}})
    return _public(k)


def regenerate_key(key_id: str, master_keys=()) -> tuple:
    with _lock:
        doc = copy.deepcopy(_load())
        _migrate(doc)
        k = _find(doc, key_id)
        plain = _gen_plain()
        k["sha256"] = _check_plain(doc, plain, master_keys)
        k["key"] = plain
        k["hint"] = _hint(plain)
        k["updated_at"] = int(time.time())
        _save(doc)
    logger.info("Scoped API key regenerated", extra={"request_info": {"event": "api_key_regenerated", "id": key_id}})
    return _public(k), plain


def delete_key(key_id: str) -> None:
    with _lock:
        doc = copy.deepcopy(_load())
        _migrate(doc)
        k = _find(doc, key_id)
        doc["keys"] = [x for x in doc["keys"] if x is not k]
        _save(doc)
    logger.info("Scoped API key deleted", extra={"request_info": {"event": "api_key_deleted", "id": key_id}})


def get_plaintext(key_id: str) -> str:
    """Plaintext for admin reveal. Raises KeyError_ if missing / hash-only legacy."""
    with _lock:
        doc = copy.deepcopy(_load())
        _migrate(doc)
        k = _find(doc, key_id)
        plain = k.get("key")
        if not isinstance(plain, str) or not plain:
            raise KeyError_("仅保存了哈希，无法查看明文；请重新生成", 404)
        return plain


def bind_plaintext(key_id: str, plain: str) -> dict:
    """Attach plaintext to an existing entry after verifying it matches the stored SHA-256."""
    plain = (plain or "").strip()
    if not plain:
        raise KeyError_("明文为空")
    with _lock:
        doc = copy.deepcopy(_load())
        _migrate(doc)
        k = _find(doc, key_id)
        h = hashlib.sha256(plain.encode()).hexdigest()
        if not hmac.compare_digest(h, str(k.get("sha256") or "").lower()):
            raise KeyError_("明文与已存哈希不匹配", 400)
        k["key"] = plain
        k["hint"] = _hint(plain)
        k["updated_at"] = int(time.time())
        _save(doc)
    logger.info("Scoped API key plaintext bound", extra={"request_info": {
        "event": "api_key_plaintext_bound", "id": key_id, "name": k.get("name")}})
    return _public(k)


def bind_plaintext_by_name(name: str, plain: str) -> dict:
    with _lock:
        doc = copy.deepcopy(_load())
        _migrate(doc)
        target = None
        for k in doc.get("keys") or []:
            if str(k.get("name") or "").strip().lower() == (name or "").strip().lower():
                target = k
                break
        if not target:
            raise KeyError_("Key 不存在", 404)
        kid = target["id"]
    return bind_plaintext(kid, plain)


def model_catalog() -> list:
    """One entry per Notion model for the admin checkbox list: primary id + aliases."""
    from app import workspaces as ws
    from app.model_registry import DISPLAY_NAMES, list_available_models
    groups: dict = {}
    order: list = []
    for m in list_available_models():
        nid = ws.notion_id(m) or m
        if nid not in groups:
            groups[nid] = []
            order.append(nid)
        groups[nid].append(m)
    out = []
    for nid in order:
        ids = groups[nid]
        primary = next((i for i in ids if i in DISPLAY_NAMES), ids[0])
        fam = "claude" if primary.startswith(("claude", "opus", "sonnet", "haiku")) else \
            "gpt" if primary.startswith("gpt") else "gemini" if primary.startswith("gemini") else "other"
        out.append({"id": primary, "name": DISPLAY_NAMES.get(primary, primary),
                    "aliases": [i for i in ids if i != primary], "family": fam})
    rank = {"claude": 0, "gpt": 1, "gemini": 2, "other": 3}
    out.sort(key=lambda m: (rank[m["family"]], m["id"]))
    return out

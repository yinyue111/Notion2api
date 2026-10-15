"""Long generations on Notion (40k output-token cap per inference, hidden thinking included).

* plan-then-write: at xhigh/max a long deliverable (big SVG/HTML/program/essay) makes Opus
  spend the whole 40k budget thinking and answer nothing. For such requests we run the
  requested effort (real Max) on a *planning* prompt -> concise plan, shown to the client
  as reasoning, then write the deliverable at NOTION_PLAN_WRITE_EFFORT (default high) with
  the plan in context. Short prompts stay pure Max. NOTION_PLAN_WRITE=auto|on|off (default off: measured 2026-10-15, the Max planning pass is itself silent+capped, so it only added latency/cost over the hedge).
* auto-continue: an answer whose output hit the cap (finish=length) is continued on the
  same thread and stitched (NOTION_AUTO_CONTINUE, rounds NOTION_AUTO_CONTINUE_MAX).
"""
import os
import re
import time
import uuid

from app.logger import logger

PLAN_EFFORTS = ("xhigh", "max")
_LONG_RE = re.compile(
    r"(?ix)\b(svg|animat\w*|single[- ]file|"
    r"(full|complete|entire|whole)\s+(code|program|implementation|source|file|app|game|website|html|document)|"
    r"(write|build|create|make|generate|implement|code|design)\s+(?:[\w,-]+\s+){0,5}(app|application|game|website|web\s*page|"
    r"landing\s*page|html|dashboard|program|script|library|essay|story|novel|chapter|report|article|paper|book|"
    r"tutorial|guide|svg|animation)s?|"
    r"\d{3,}\s*(words|lines|字))\b"
    r"|长篇|完整的|动画|网页|小游戏|写一篇|写一个.{0,12}(程序|应用|游戏|网站|页面)")
_CONT_PROMPT = ("Your previous reply was cut off by the output limit. Continue EXACTLY where it stopped, "
                "with the very next characters. Do not repeat anything, do not add any preamble or commentary.")
_PLAN_INSTR = (
    "\n\n[Planning pass] Do NOT write the final deliverable in this reply. Think it through, then output "
    "ONLY a concise but specific plan/outline that another writer will follow to produce the complete "
    "answer: structure, components, concrete values/decisions, pitfalls to avoid. At most ~1200 words. "
    "Keep your internal reasoning brief - your total output budget is limited. Do not call tools in this reply.")
_WRITE_INSTR = (
    "\n\n[Plan from a prior deep-reasoning pass - follow it and now write the COMPLETE final answer "
    "(not a plan, no commentary about the plan). Keep reasoning brief and spend your budget on the answer.]\n")


def _env_on(name, default="1"):
    return os.getenv(name, default).strip().lower() not in ("0", "false", "off", "no")


def _user_text(block) -> str:
    v = block.get("value") if isinstance(block, dict) else None
    out = []
    if isinstance(v, list):
        for seg in v:
            if isinstance(seg, list) and seg and isinstance(seg[0], str):
                out.append(seg[0])
            elif isinstance(seg, str):
                out.append(seg)
    elif isinstance(v, str):
        out.append(v)
    return "".join(out)


def last_user_index(transcript):
    for i in range(len(transcript or []) - 1, -1, -1):
        b = transcript[i]
        if isinstance(b, dict) and b.get("type") == "user":
            return i
    return -1


def looks_long_generation(text: str) -> bool:
    t = (text or "")[-6000:]
    if not t.strip():
        return False
    # tool results / continuation turns are not fresh long-generation asks
    if "<tool_result" in t or "tool_call_id" in t:
        return False
    return bool(_LONG_RE.search(t[-3000:]))


def should_plan(transcript, thread_id, meta, effort) -> bool:
    mode = os.getenv("NOTION_PLAN_WRITE", "off").strip().lower()
    if mode in ("0", "off", "false", "no") or meta.get("_lg_inner") or meta.get("_is_hedge"):
        return False
    if effort not in PLAN_EFFORTS:
        return False
    base = meta.get("_hedge_transcript") or (transcript if thread_id is None else None)
    if not base:
        return False
    i = last_user_index(base)
    if i < 0:
        return False
    ask = meta.get("_user_ask")
    if ask is None:
        ask = _user_text(base[i])
    return mode == "on" or looks_long_generation(ask)


def _with_user_suffix(base, suffix, effort=None):
    out = []
    i = last_user_index(base)
    for k, b in enumerate(base):
        if isinstance(b, dict) and "id" in b:
            b = dict(b, id=str(uuid.uuid4()))
        if effort and isinstance(b, dict) and b.get("type") == "config" and isinstance(b.get("value"), dict):
            b = {**b, "value": {**b["value"], "reasoningEffort": effort}}
        if k == i:
            v = b.get("value")
            v = list(v) if isinstance(v, list) else [[str(v or "")]]
            b = {**b, "value": v + [[suffix]]}
        out.append(b)
    return out


def plan_transcript(base, effort):
    return _with_user_suffix(base, _PLAN_INSTR, effort)


def write_transcript(base, plan, effort):
    return _with_user_suffix(base, _WRITE_INSTR + plan.strip() + "\n", effort)


def continue_transcript(transcript, effort):
    cfg = next((b for b in transcript if isinstance(b, dict) and b.get("type") == "config"), None)
    out = []
    if cfg is not None:
        v = dict(cfg.get("value") or {})
        if effort:
            v["reasoningEffort"] = effort
        out.append({**cfg, "id": str(uuid.uuid4()), "value": v})
    out.append({"id": str(uuid.uuid4()), "type": "user", "value": [[_CONT_PROMPT]]})
    return out


def _sum_usage(total, u):
    for k, v in (u or {}).items():
        if isinstance(v, (int, float)):
            total[k] = total.get(k, 0) + v
        else:
            total.setdefault(k, v)


def trim_overlap(prev_tail: str, nxt: str, min_k: int = 16) -> str:
    """Drop a prefix of `nxt` that repeats the end of `prev_tail` (continuation overlap)."""
    best = 0
    for k in range(min(len(prev_tail), len(nxt)), min_k - 1, -1):
        if prev_tail.endswith(nxt[:k]):
            best = k
            break
    return nxt[best:]


def is_capped(out_tokens) -> bool:
    cap = float(os.getenv("NOTION_OUTPUT_CAP", "40000") or 40000)
    frac = float(os.getenv("NOTION_CAP_FRACTION", "0.97") or 0.97)
    return bool(out_tokens) and float(out_tokens) >= cap * frac


def _cont_effort(effort):
    return "high" if effort in ("xhigh", "max") else effort


def with_auto_continue(client, gen, transcript, meta):
    """Pass items through; absorb usage (summed, emitted once at the end); if the answer hit
    the output cap with partial text, continue it on the same thread and stitch."""
    usage = {}
    text = []
    for it in gen:
        if isinstance(it, dict) and it.get("type") == "usage":
            _sum_usage(usage, it.get("usage"))
            continue
        if isinstance(it, dict) and it.get("type") == "content":
            text.append(str(it.get("text") or ""))
        yield it
    rounds = 0
    max_rounds = int(os.getenv("NOTION_AUTO_CONTINUE_MAX", "2") or 0)
    last_out = usage.get("outputTokens")
    eff = client._transcript_effort(transcript)
    if eff in PLAN_EFFORTS and is_capped(last_out) and not meta.get("_is_hedge"):
        try:  # a xhigh/max answer hit the 40k cap: remember for this conversation
            from app import cap_memory
            cap_memory.mark(meta.get("_conv_keys"), reason="capped")
        except Exception:
            pass
    while (_env_on("NOTION_AUTO_CONTINUE") and rounds < max_rounds and is_capped(last_out)
           and "".join(text).strip() and "<tool_call" not in "".join(text)
           and meta.get("thread_id") and not meta.get("_cancelled")):
        rounds += 1
        ce = _cont_effort(eff)
        logger.info("Answer hit the output cap: auto-continuing", extra={"request_info": {
            "event": "auto_continue", "round": rounds, "output_tokens": last_out, "effort": ce,
            "chars_so_far": len("".join(text))}})
        cmeta = meta
        cmeta["_lg_inner"] = True
        tail = "".join(text)[-600:]
        head_buf, decided, u2 = "", False, {}
        try:
            for it in client._stream_guarded(continue_transcript(transcript, ce), meta.get("thread_id"), cmeta):
                if isinstance(it, dict) and it.get("type") == "usage":
                    _sum_usage(u2, it.get("usage"))
                    continue
                if isinstance(it, dict) and it.get("type") == "content":
                    t = str(it.get("text") or "")
                    if not decided:
                        head_buf += t
                        if len(head_buf) < 400:
                            continue
                        t, decided = trim_overlap(tail, head_buf), True
                    text.append(t)
                    yield {"type": "content", "text": t}
                    continue
                if isinstance(it, dict) and it.get("type") == "final_content":
                    continue  # the stitched text is the answer
                yield it
        except Exception as exc:
            logger.info("Auto-continue failed", extra={"request_info": {"event": "auto_continue_failed",
                                                                        "error": str(exc)[:160]}})
            break
        finally:
            meta.pop("_lg_inner", None)
        if not decided and head_buf:
            t = trim_overlap(tail, head_buf)
            text.append(t)
            yield {"type": "content", "text": t}
        _sum_usage(usage, u2)
        last_out = u2.get("outputTokens")
    if usage:
        yield {"type": "usage", "usage": usage}


def plan_then_write(client, transcript, meta):
    base = meta.get("_hedge_transcript") or transcript
    effort = client._transcript_effort(transcript)
    write_eff = os.getenv("NOTION_PLAN_WRITE_EFFORT", "high").strip() or "high"
    t0 = time.time()
    logger.info("Long generation at high effort: plan-then-write", extra={"request_info": {
        "event": "plan_write_start", "effort": effort, "write_effort": write_eff}})
    ptr = plan_transcript(base, effort)
    saved = meta.get("_hedge_transcript")
    meta["_hedge_transcript"] = ptr  # a capped plan call is hedged with the plan prompt
    meta.pop("thread_id", None)
    plan, usage = [], {}
    try:
        for it in client._stream_guarded(ptr, None, meta):
            if isinstance(it, dict) and it.get("type") == "usage":
                _sum_usage(usage, it.get("usage"))
                continue
            if isinstance(it, dict) and it.get("type") == "content":
                plan.append(str(it.get("text") or ""))
                continue
            if isinstance(it, dict) and it.get("type") == "final_content":
                continue
            yield it  # thinking etc.
    finally:
        meta["_hedge_transcript"] = saved
    plan_text = "".join(plan).strip()
    plan_out = usage.get("outputTokens")
    logger.info("Plan pass done", extra={"request_info": {
        "event": "plan_done", "chars": len(plan_text), "output_tokens": plan_out,
        "elapsed_s": round(time.time() - t0)}})
    if "<tool_call" in plan_text:
        # the model chose to call a tool: that is a complete (Max) answer for tool mode
        yield {"type": "content", "text": plan_text}
        if usage:
            yield {"type": "usage", "usage": usage}
        return
    if not plan_text:
        from app.notion_client import NotionUpstreamError
        raise NotionUpstreamError("Notion upstream returned an empty stream.", status_code=502, retriable=True)
    yield {"type": "thinking", "text": "Plan (" + effort + " effort):\n" + plan_text + "\n"}
    wtr = write_transcript(base, plan_text, write_eff)
    meta["_hedge_transcript"] = wtr
    meta.pop("thread_id", None)
    t1 = time.time()
    meta["_lg_inner"] = True
    wusage = {}
    chars = 0
    try:
        for it in with_auto_continue(client, client._stream_guarded(wtr, None, meta), wtr, meta):
            if isinstance(it, dict) and it.get("type") == "usage":
                _sum_usage(wusage, it.get("usage"))
                continue
            if isinstance(it, dict) and it.get("type") == "content":
                chars += len(str(it.get("text") or ""))
            yield it
    finally:
        meta.pop("_lg_inner", None)
        meta["_hedge_transcript"] = saved
    logger.info("Write pass done", extra={"request_info": {
        "event": "plan_write_done", "chars": chars, "plan_output_tokens": plan_out,
        "write_output_tokens": wusage.get("outputTokens"), "plan_s": round(t1 - t0),
        "write_s": round(time.time() - t1)}})
    _sum_usage(usage, wusage)
    if usage:
        yield {"type": "usage", "usage": usage}

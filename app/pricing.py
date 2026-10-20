"""Official list-price valuation of logged token usage (admin overview).

The table lives in data/official_prices.json (OFFICIAL_PRICES_FILE); it is seeded from
DEFAULT_PRICES on first use and can be edited in the admin UI or on disk (mtime-reloaded).
Prices are USD per 1M tokens.  One entry per model, keyed by the standard model id; any
alias / effort suffix / Notion codename resolves to the same entry via the Notion id.

Per request:  uncached*input + cache_read*cache_read + cache_write*cache_write + output*output
  * long_context: when the request's whole prompt (uncached + cache read + cache write)
    >= min_prompt_tokens, every token of that request uses the long-context prices.
  * offpeak: provider peak/off-peak schedule (e.g. DeepSeek); list prices are the peak
    prices and requests outside the peak hours are multiplied by `factor`.
  * an entry with "unpriced": true (or a model not in the table) is excluded from totals.
Token semantics follow request_log: prompt_tokens = uncached + cache read + cache write.
The model is the *requested* model name; Notion may serve another model.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Optional

ANTHROPIC = "https://platform.claude.com/docs/en/about-claude/pricing"
OPENAI = "https://platform.openai.com/docs/pricing"
GOOGLE = "https://ai.google.dev/gemini-api/docs/pricing"
GOOGLE_37 = "https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-3-7-flash/"
GOOGLE_35 = "https://ai.google.dev/gemini-api/docs/pricing?hl=id"
XAI = "https://docs.x.ai/developers/pricing"
DEEPSEEK = "https://api-docs.deepseek.com/quick_start/pricing"
KIMI = "https://platform.kimi.ai/docs/guide/context-caching"
ZAI = "https://docs.z.ai/guides/overview/pricing"
CHECKED = "2026-10-18"

_NO_CW_OPENAI = "OpenAI 该模型无单独缓存写入价，写入 token 按普通输入价计"
_NO_CW = "无单独缓存写入价，写入 token 按普通输入价计"


def _e(name, provider, i, o, cr, cw, source, lc=None, note="", **kw):
    d = {"name": name, "provider": provider, "input": i, "output": o, "cache_read": cr, "cache_write": cw,
         "long_context": lc, "source": source, "checked": CHECKED, "note": note}
    d.update(kw)
    return d


def _lc(n, i, o, cr, cw):
    return {"min_prompt_tokens": n, "input": i, "output": o, "cache_read": cr, "cache_write": cw}


_OAI_LC = 272001   # OpenAI: long context = > 272K input tokens
_DS_PEAK = {"factor": 0.5, "peak_utc_hours": [1, 2, 3, 6, 7, 8, 9], "peak_weekdays": [1, 2, 3, 4, 5]}

DEFAULT_PRICES: dict = {
    "version": 1,
    "currency": "USD",
    "unit": "per 1M tokens",
    "updated": CHECKED,
    "models": {
        # ---- Anthropic (cache_write = 5-minute cache write) ----
        "fable-5.1": _e("Claude Fable 5.1", "anthropic", 10, 50, 0.25, 12.5, ANTHROPIC),
        "fable-5": _e("Claude Fable 5", "anthropic", 10, 50, 1, 12.5, ANTHROPIC),
        "claude-opus5.5": _e("Claude Opus 5.5", "anthropic", 4, 20, 0.20, 5, ANTHROPIC),
        "claude-opus5": _e("Claude Opus 5", "anthropic", 5, 25, 0.50, 6.25, ANTHROPIC),
        "claude-opus4.8": _e("Claude Opus 4.8", "anthropic", 5, 25, 0.50, 6.25, ANTHROPIC),
        "claude-opus4.7": _e("Claude Opus 4.7", "anthropic", 5, 25, 0.50, 6.25, ANTHROPIC),
        "claude-opus4.6": _e("Claude Opus 4.6", "anthropic", 5, 25, 0.50, 6.25, ANTHROPIC),
        "claude-sonnet5.5": _e("Claude Sonnet 5.5", "anthropic", 2, 10, 0.10, 2.5, ANTHROPIC),
        "claude-sonnet5": _e("Claude Sonnet 5", "anthropic", 2, 10, 0.20, 2.5, ANTHROPIC),
        "claude-sonnet4.6": _e("Claude Sonnet 4.6", "anthropic", 3, 15, 0.30, 3.75, ANTHROPIC),
        "claude-haiku5.5": _e("Claude Haiku 5.5", "anthropic", 0.10, 0.50, 0.01, 0.125, ANTHROPIC,
                              lc=_lc(100001, 0.50, 2.50, 0.05, 0.625), note="prompt > 100,000 tokens 用高价"),
        "claude-haiku4.5": _e("Claude Haiku 4.5", "anthropic", 1, 5, 0.10, 1.25, ANTHROPIC),
        # ---- OpenAI (Standard tier) ----
        "gpt-6-astra": _e("GPT-6 Astra", "openai", 10, 50, 1, 12.5, OPENAI, lc=_lc(_OAI_LC, 20, 75, 2, 25)),
        "gpt-6.1-sol": _e("GPT-6.1 Sol", "openai", 2, 10, 0.10, 2.5, OPENAI, lc=_lc(_OAI_LC, 4, 15, 0.20, 5)),
        "gpt-6-luna": _e("GPT-6 Luna", "openai", 0.10, 0.50, 0.01, 0.125, OPENAI, lc=_lc(_OAI_LC, 0.20, 0.75, 0.02, 0.25)),
        "gpt-5.6-sol": _e("GPT-5.6 Sol", "openai", 4, 20, 0.40, 5, OPENAI, lc=_lc(_OAI_LC, 8, 30, 0.80, 10),
                          note="OpenAI 标注为促销价（至少到 2026-11-21）"),
        "gpt-5.6-terra": _e("GPT-5.6 Terra", "openai", 2, 12, 0.20, 2.5, OPENAI, lc=_lc(_OAI_LC, 4, 18, 0.40, 5)),
        "gpt-5.6-luna": _e("GPT-5.6 Luna", "openai", 0.20, 1.20, 0.02, 0.25, OPENAI, lc=_lc(_OAI_LC, 0.40, 1.80, 0.04, 0.50)),
        "gpt-5.5": _e("GPT-5.5", "openai", 5, 30, 0.50, 5, OPENAI, lc=_lc(_OAI_LC, 10, 45, 1, 10), note=_NO_CW_OPENAI),
        "gpt-5.4": _e("GPT-5.4", "openai", 2.5, 15, 0.25, 2.5, OPENAI, lc=_lc(_OAI_LC, 5, 22.5, 0.50, 5), note=_NO_CW_OPENAI),
        "gpt-5.4-mini": _e("GPT-5.4 mini", "openai", 0.75, 4.5, 0.075, 0.75, OPENAI, note=_NO_CW_OPENAI),
        "gpt-5.4-nano": _e("GPT-5.4 nano", "openai", 0.20, 1.25, 0.02, 0.20, OPENAI, note=_NO_CW_OPENAI),
        "gpt-5.2": _e("GPT-5.2", "openai", 1.75, 14, 0.175, 1.75, OPENAI, note=_NO_CW_OPENAI),
        # ---- Google (Gemini API paid tier, Standard) ----
        "gemini-3.1pro": _e("Gemini 3.1 Pro (preview)", "google", 2, 12, 0.20, 2, GOOGLE,
                            lc=_lc(200001, 4, 18, 0.40, 4), note="prompt > 200k 用高价；缓存存储费未计；" + _NO_CW),
        "gemini-3flash": _e("Gemini 3 Flash (preview)", "google", 0.50, 3, 0.05, 0.50, GOOGLE, note=_NO_CW),
        "gemini-3.6flash": _e("Gemini 3.6 Flash", "google", 0.75, 3.75, 0.075, 0.75, GOOGLE,
                              note="官方价至 2026-12-31，2027-01-01 起 $1.50/$7.50/$0.15；" + _NO_CW),
        "gemini-3.7flash": _e("Gemini 3.7 Flash", "google", 0.75, 3.75, 0.075, 0.75, GOOGLE_37,
                              note="已弃用（API 路由到 3.8 Flash，同价）；输入/输出取自 Google 发布文，缓存价取 3.8 Flash 官方价；" + _NO_CW),
        "gemini-3.5flash": _e("Gemini 3.5 Flash", "google", 1.50, 9, 0.15, 1.50, GOOGLE_35,
                              note="已弃用（API 路由到 3.6 Flash）；价格取自 Google 价格页（仍列出 3.5 Flash 的版本）；" + _NO_CW),
        # ---- xAI ----
        "grok-4.6": _e("Grok 4.6", "xai", 2, 6, 0.50, 2, XAI, lc=_lc(200000, 4, 12, 1, 4), note="prompt ≥ 200k 用高价；" + _NO_CW),
        "grok-4.3": _e("Grok 4.3", "xai", 1.25, 2.5, 0.20, 1.25, XAI, lc=_lc(200000, 2.5, 5, 0.40, 2.5), note="prompt ≥ 200k 用高价；" + _NO_CW),
        "grok-build0.1": _e("Grok Build 0.1", "xai", 1, 2, 0.20, 1, XAI, lc=_lc(200000, 2, 4, 0.40, 2), note="prompt ≥ 200k 用高价；" + _NO_CW),
        "spacexai-4.5": _e("SpaceXAI 4.5 (= grok-4.5)", "xai", 2, 6, 0.30, 2, XAI, lc=_lc(200000, 4, 12, 0.60, 4),
                           note="Notion 显示名 SpaceXAI 4.5，按 xAI grok-4.5 定价（映射为推断）；" + _NO_CW),
        # ---- DeepSeek (list = peak; off-peak 50%) ----
        "deepseek-v4pro": _e("DeepSeek V4 Pro", "deepseek", 1.32, 3.96, 0.044, 1.32, DEEPSEEK, offpeak=_DS_PEAK,
                             note="高峰价；UTC 01-04、06-10 点（周一至五）以外按 50%；" + _NO_CW),
        "deepseek-v4.1flash": _e("DeepSeek V4.1 Flash", "deepseek", 0.30, 1.20, 0.006, 0.30, DEEPSEEK, offpeak=_DS_PEAK,
                                 note="deepseek-flash 高峰价；非高峰 50%；" + _NO_CW),
        "deepseek-v4flash": _e("DeepSeek V4 Flash", "deepseek", 0.30, 1.20, 0.006, 0.30, DEEPSEEK, offpeak=_DS_PEAK,
                               note="已退役，官方按 V4.1 Flash 价计费；非高峰 50%；" + _NO_CW),
        # ---- Moonshot ----
        "kimi-k3": _e("Kimi K3", "moonshot", 3, 15, 0.30, 3, KIMI, note="缓存写入按 5m TTL 价"),
        # ---- Z.ai ----
        "glm-5.3": _e("GLM-5.3", "zai", 1.4, 4.4, 0.26, 1.4, ZAI, note=_NO_CW),
        "glm-5.2": _e("GLM-5.2", "zai", 1.4, 4.4, 0.26, 1.4, ZAI, note=_NO_CW),
        "glm-5.3flash": _e("GLM-5.3-Flash", "zai", 0.15, 0.50, 0.03, 0.15, ZAI, note=_NO_CW),
    },
}

PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")
_lock = threading.RLock()
_cache: dict = {"sig": None, "data": None, "index": None}


def path() -> str:
    return os.getenv("OFFICIAL_PRICES_FILE", "/app/data/official_prices.json")


class PriceError(ValueError):
    pass


def _num(v, where: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0 or v > 100000:
        raise PriceError(f"{where}: 需要 0–100000 的数字")
    return float(v)


def validate(data) -> dict:
    """Normalize + validate a full price table; raises PriceError."""
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        raise PriceError("格式错误：需要 {\"models\": {...}}")
    if len(data["models"]) > 500:
        raise PriceError("模型条目过多")
    out = {k: data[k] for k in ("version", "currency", "unit", "updated") if k in data}
    out.setdefault("currency", "USD")
    out.setdefault("unit", "per 1M tokens")
    models = {}
    for key, e in data["models"].items():
        key = str(key).strip()
        if not key or len(key) > 100 or not isinstance(e, dict):
            raise PriceError(f"条目 {key!r} 无效")
        n = {"name": str(e.get("name") or key)[:120], "provider": str(e.get("provider") or "")[:40],
             "source": str(e.get("source") or "")[:500], "checked": str(e.get("checked") or "")[:40],
             "note": str(e.get("note") or "")[:500]}
        if e.get("unpriced"):
            n["unpriced"] = True
        else:
            for f in PRICE_FIELDS:
                n[f] = _num(e.get(f), f"{key}.{f}")
            lc = e.get("long_context")
            if lc:
                if not isinstance(lc, dict):
                    raise PriceError(f"{key}.long_context 需要对象")
                t = lc.get("min_prompt_tokens")
                if isinstance(t, bool) or not isinstance(t, int) or t <= 0:
                    raise PriceError(f"{key}.long_context.min_prompt_tokens 需要正整数")
                n["long_context"] = {"min_prompt_tokens": t, **{f: _num(lc.get(f), f"{key}.long_context.{f}") for f in PRICE_FIELDS}}
            else:
                n["long_context"] = None
            op = e.get("offpeak")
            if op:
                if not isinstance(op, dict):
                    raise PriceError(f"{key}.offpeak 需要对象")
                fac = _num(op.get("factor"), f"{key}.offpeak.factor")
                hrs = op.get("peak_utc_hours") or []
                wds = op.get("peak_weekdays") or [1, 2, 3, 4, 5, 6, 7]
                if not all(isinstance(x, int) and 0 <= x <= 23 for x in hrs) or not all(isinstance(x, int) and 1 <= x <= 7 for x in wds):
                    raise PriceError(f"{key}.offpeak 小时需 0–23、星期需 1–7")
                n["offpeak"] = {"factor": fac, "peak_utc_hours": sorted(set(hrs)), "peak_weekdays": sorted(set(wds))}
        models[key] = n
    out["models"] = models
    return out


def _build_index(data: dict) -> dict:
    from app import workspaces as ws
    idx: dict = {}
    for key, e in data["models"].items():
        idx.setdefault(key, (key, e))
        nid = ws.notion_id(key)
        if nid:
            idx.setdefault("nid:" + nid, (key, e))
    return idx


def _write(data: dict) -> None:
    p = path()
    d = os.path.dirname(p) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".prices-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load() -> dict:
    """Current table (validated). Seeds the file with defaults if it does not exist."""
    with _lock:
        p = path()
        try:
            st = os.stat(p)
            sig = (p, st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            data = validate(copy.deepcopy(DEFAULT_PRICES))
            try:
                _write(data)
                st = os.stat(p)
                sig = (p, st.st_mtime_ns, st.st_size)
            except OSError:
                sig = (p, None, None)
            _cache.update(sig=sig, data=data, index=_build_index(data), error=None)
            return data
        if _cache["sig"] == sig and _cache["data"] is not None:
            return _cache["data"]
        try:
            with open(p, encoding="utf-8") as f:
                data = validate(json.load(f))
            err = None
        except (OSError, ValueError) as exc:  # broken file -> keep defaults, report error
            data, err = validate(copy.deepcopy(DEFAULT_PRICES)), f"{type(exc).__name__}: {exc}"[:300]
        _cache.update(sig=sig, data=data, index=_build_index(data), error=err)
        return data


def load_error() -> Optional[str]:
    load()
    return _cache.get("error")


def save(data) -> dict:
    v = validate(data)
    v["updated"] = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    with _lock:
        _write(v)
        _cache["sig"] = None
    return load()


def reset() -> dict:
    with _lock:
        _write(validate(copy.deepcopy(DEFAULT_PRICES)))
        _cache["sig"] = None
    return load()


def lookup(model: Optional[str]) -> tuple[Optional[str], Optional[dict]]:
    """Requested model name -> (price key, entry) or (None, None)."""
    if not model:
        return None, None
    load()
    idx = _cache["index"] or {}
    m = str(model).strip()
    if m in idx:
        return idx[m]
    from app import workspaces as ws
    from app.model_registry import resolve_model
    try:
        std, _ = resolve_model(m)
    except Exception:
        std = None
    if std and std in idx:
        return idx[std]
    nid = ws.notion_id(m)
    if nid and "nid:" + nid in idx:
        return idx["nid:" + nid]
    return None, None


def is_peak(entry: dict, ts: float) -> bool:
    op = entry.get("offpeak")
    if not op:
        return True
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return d.isoweekday() in op["peak_weekdays"] and d.hour in op["peak_utc_hours"]


def cost(entry: Optional[dict], uncached: float, cache_read: float, cache_write: float, output: float,
         ts: Optional[float] = None) -> Optional[dict]:
    """USD value of one request; None if the entry is missing / unpriced."""
    if not entry or entry.get("unpriced"):
        return None
    uncached, cache_read, cache_write, output = (max(0.0, float(x or 0)) for x in (uncached, cache_read, cache_write, output))
    rates = entry
    lc = entry.get("long_context")
    long = bool(lc and uncached + cache_read + cache_write >= lc["min_prompt_tokens"])
    if long:
        rates = lc
    f = 1.0
    if ts is not None and entry.get("offpeak") and not is_peak(entry, ts):
        f = entry["offpeak"]["factor"]
    parts = {"input_usd": uncached * rates["input"] * f / 1e6, "cache_read_usd": cache_read * rates["cache_read"] * f / 1e6,
             "cache_write_usd": cache_write * rates["cache_write"] * f / 1e6, "output_usd": output * rates["output"] * f / 1e6}
    parts["usd"] = sum(parts.values())
    parts["long"] = long
    return parts

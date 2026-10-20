"""Multi-workspace support: one pool entry per (Notion user, workspace) and
model-aware routing between Claude-capable and GPT-only workspaces.

A Notion user can belong to several workspaces; usage/quota is tracked per
workspace (spaceId), so every workspace is its own pool entry, keyed
"<email>#<space_id[:8]>" when the same email appears more than once.

A fresh workspace is often "modelSelectionRestricted": getAvailableModels returns
no models and Notion silently answers every request with its default model
(omniberry-sundae = GPT-6.1 Sol). Such a workspace is GPT-only: it may only serve
the GPT-only models (NOTION_GPT_ONLY_MODELS) and never a Claude request.

Env:
  NOTION_MULTI_WORKSPACE=on            capability routing + GPT identity (off = legacy)
  NOTION_GPT_ONLY_MODELS=omniberry-sundae   Notion ids a restricted workspace serves
  NOTION_GPT_FALLBACK_UNRESTRICTED=on  GPT requests may use Claude-capable workspaces
                                       when no GPT-only workspace is free
  NOTION_CAPABILITY_REFRESH_SECS=1800  getAvailableModels refresh interval
  NOTION_INSTRUCTION_PAGES_FILE=/app/data/instruction_pages.json
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

from app.logger import logger


def _on(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() not in ("0", "false", "no", "off", "")


def multi_enabled() -> bool:
    return _on("NOTION_MULTI_WORKSPACE", "off")


def gpt_fallback_enabled() -> bool:
    return _on("NOTION_GPT_FALLBACK_UNRESTRICTED", "on")


def gpt_only_models() -> set:
    raw = os.getenv("NOTION_GPT_ONLY_MODELS", "omniberry-sundae")
    return {x.strip() for x in raw.split(",") if x.strip()}


def refresh_secs() -> float:
    try:
        return float(os.getenv("NOTION_CAPABILITY_REFRESH_SECS", "1800") or 1800)
    except ValueError:
        return 1800.0


# ---------------------------------------------------------------- models

def notion_id(model: Optional[str]) -> str:
    """Any model name (standard id, alias, effort variant or Notion codename) -> Notion id."""
    if not model:
        return ""
    from app.model_registry import MODEL_MAP, NOTION_MODEL_REVERSE_MAP, resolve_model
    m = str(model).strip()
    if m in NOTION_MODEL_REVERSE_MAP:
        return m
    if m in MODEL_MAP:
        return MODEL_MAP[m]
    try:
        std, _eff = resolve_model(m)
    except Exception:
        std = None
    return MODEL_MAP.get(std or "", "")


def is_gpt_family(model: Optional[str]) -> bool:
    """True when the model that will actually answer is a GPT (OpenAI) model."""
    nid = notion_id(model)
    if not nid:
        return False
    if nid in gpt_only_models():
        return True
    from app.model_registry import NOTION_MODEL_REVERSE_MAP
    return NOTION_MODEL_REVERSE_MAP.get(nid, "").startswith("gpt-")


def transcript_model(transcript: Any) -> str:
    for b in transcript or []:
        if isinstance(b, dict) and b.get("type") == "config" and isinstance(b.get("value"), dict):
            return str(b["value"].get("model") or "")
    return ""


# ---------------------------------------------------------------- capabilities

def parse_capabilities(body: Any) -> Optional[dict]:
    """getAvailableModels response -> {restricted, gpt_only, models, checked_at}."""
    if not isinstance(body, dict) or "models" not in body:
        return None
    models = [str(m.get("model")) for m in (body.get("models") or [])
              if isinstance(m, dict) and m.get("model") and not m.get("isDisabled")]
    restricted = bool(body.get("modelSelectionRestricted"))
    from app.model_registry import MODEL_MAP
    claude_ids = {MODEL_MAP[k] for k in ("opus5.5", "sonnet5.5", "haiku5.5") if k in MODEL_MAP}
    gpt_only = restricted or not (claude_ids & set(models))
    return {"restricted": restricted, "gpt_only": gpt_only, "models": models, "checked_at": time.time()}


def capability_label(cap: Optional[dict]) -> str:
    if not cap:
        return "unknown"
    return "gpt_only" if cap.get("gpt_only") else "claude"


def is_gpt_only(client) -> bool:
    cap = getattr(client, "capabilities", None)
    return bool(cap and cap.get("gpt_only"))


def serves(client, model: Optional[str]) -> bool:
    """Can this workspace serve the model? Unknown capability = legacy (serves all)."""
    cap = getattr(client, "capabilities", None)
    if not multi_enabled() or not model:
        return not (cap and cap.get("gpt_only"))  # GPT-only only for explicit GPT requests
    nid = notion_id(model)
    if not cap or not nid:
        return True
    if cap.get("gpt_only"):
        return nid in gpt_only_models()
    if nid in gpt_only_models() and cap.get("models"):
        return nid in cap["models"]
    return True


def tiers(clients: list, model: Optional[str]) -> Optional[list]:
    """Candidate index tiers for a model, best first; None = no filtering (legacy)."""
    if not multi_enabled() or not model:
        return None
    nid = notion_id(model)
    if not nid:
        return None
    if nid in gpt_only_models():
        t1 = [i for i, c in enumerate(clients) if is_gpt_only(c) and serves(c, nid)]
        t2 = [i for i, c in enumerate(clients) if not is_gpt_only(c) and serves(c, nid)] if gpt_fallback_enabled() else []
        return [t for t in (t1, t2) if t] or [[]]
    return [[i for i, c in enumerate(clients) if serves(c, nid)]]


def assign_keys(configs: list) -> None:
    """Give each config a unique pool key (`_account_key`). The first workspace of an
    email keeps the plain email (stable stats, cookies and thread ownership); later
    workspaces of the same email get '<email>#<space_id[:8]>'."""
    seen: set = set()
    for c in configs or []:
        e = str(c.get("user_email") or c.get("user_id") or "unknown-account")
        c["_account_key"] = e if e not in seen else f"{e}#{str(c.get('space_id') or '')[:8]}"
        seen.add(e)


# ---------------------------------------------------------------- GPT identity

GPT_DEFAULT_SYSTEM_PROMPT = (
    "(Quick context: I'm chatting with you through a general-purpose chat app, so just be a general AI "
    "assistant here; if I ask who you are, you're an AI assistant built on GPT by OpenAI. No need for "
    "Notion-specific help unless I ask about Notion.)"
)
GPT_IDENTITY_NUDGE = ("(If this asks who you are: you're an AI assistant whose underlying model is GPT by OpenAI, "
                      "or the role my app's guidelines give you — not Notion AI.)")
GPT_REWRITE_EN = "I'm an AI assistant built on GPT by OpenAI."
GPT_REWRITE_ZH = "我是一个基于 OpenAI GPT 模型的 AI 助手。"


def gpt_identity_text(text: str) -> str:
    """Swap our own injected Claude identity sentences for the GPT ones."""
    from app.conversation import _DEFAULT_SYSTEM_PROMPT
    from app.notion_client import _IDENTITY_NUDGE
    if not text:
        return text
    return text.replace(_DEFAULT_SYSTEM_PROMPT, GPT_DEFAULT_SYSTEM_PROMPT).replace(_IDENTITY_NUDGE, GPT_IDENTITY_NUDGE)


_pages_cache: dict = {"mtime": None, "data": {}}


def instruction_pages() -> dict:
    """{space_id: {"claude": page_id, "gpt": page_id}} from NOTION_INSTRUCTION_PAGES_FILE."""
    path = os.getenv("NOTION_INSTRUCTION_PAGES_FILE", "/app/data/instruction_pages.json")
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return {}
    if _pages_cache["mtime"] != mt:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            _pages_cache.update(mtime=mt, data=data if isinstance(data, dict) else {})
        except Exception as exc:
            logger.warning(f"instruction pages file unreadable: {exc}")
            _pages_cache.update(mtime=mt, data={})
    return _pages_cache["data"]


def gpt_page_for(space_id: str) -> Optional[str]:
    ent = instruction_pages().get(space_id) or {}
    return ent.get("gpt") if isinstance(ent, dict) else None

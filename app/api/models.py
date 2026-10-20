import time
from typing import Dict, Any

from fastapi import APIRouter, Request

import os

from app.model_registry import get_display_name, get_model_icon, list_available_models, effort_variant_ids

router = APIRouter()


@router.get("/models", tags=["models"])
async def list_models(request: Request) -> Dict[str, Any]:
    """
    列出可用的模型，兼容 OpenAI 格式（同时带 Anthropic /v1/models 的 type / created_at /
    has_more / first_id / last_id 字段，两种客户端都能解析）。
    """
    created_at = int(time.time())
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at))
    ids = list_available_models() + (effort_variant_ids() if os.getenv("MODELS_LIST_EFFORT_VARIANTS", "0") == "1" else [])
    allowed = getattr(request.state, "allowed_models", None)
    if allowed is not None:  # scoped API key: only its models (None = all)
        from app.api_keys import model_allowed
        ids = [m for m in ids if model_allowed(allowed, m)]
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "type": "model",
                "created": created_at,
                "created_at": iso,
                "owned_by": "notion",
                "permission": [],
                "root": model_id,
                "parent": None,
                "display_name": get_display_name(model_id),
                "icon": get_model_icon(model_id),
            }
            for model_id in ids
        ],
        "has_more": False,
        "first_id": ids[0] if ids else None,
        "last_id": ids[-1] if ids else None,
    }

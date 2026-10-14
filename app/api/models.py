import time
from typing import Dict, Any

from fastapi import APIRouter

import os

from app.model_registry import get_display_name, get_model_icon, list_available_models, effort_variant_ids

router = APIRouter()


@router.get("/models", tags=["models"])
async def list_models() -> Dict[str, Any]:
    """
    列出可用的模型，兼容 OpenAI 格式。
    """
    created_at = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": created_at,
                "owned_by": "notion",
                "permission": [],
                "root": model_id,
                "parent": None,
                "display_name": get_display_name(model_id),
                "icon": get_model_icon(model_id),
            }
            for model_id in list_available_models()
            + (effort_variant_ids() if os.getenv("MODELS_LIST_EFFORT_VARIANTS", "0") == "1" else [])
        ],
    }

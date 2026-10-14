import os

from slowapi import Limiter
from slowapi.util import get_remote_address
from app.config import is_lite_mode, is_standard_mode

# DISABLE_RATE_LIMIT=True 时关闭限流（调试用）
_disable_flag = os.getenv("DISABLE_RATE_LIMIT", "").strip().lower()
_rate_limit_disabled = _disable_flag == "true"

# 根据 APP_MODE 动态设置速率限制
# Lite 模式：30/minute（单轮问答响应快，3-5秒/次）
# Standard 模式：25/minute（完整上下文，支持 thinking 和搜索）
# Heavy 模式：20/minute（需要数据库操作，相对慢一些）
if is_lite_mode():
    default_limit = "30/minute"
elif is_standard_mode():
    default_limit = "25/minute"
else:
    default_limit = "20/minute"

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[] if _rate_limit_disabled else [default_limit],
    enabled=not _rate_limit_disabled,
)

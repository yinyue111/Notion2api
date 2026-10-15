import os
import asyncio
import json
import re
import time
import uuid
from difflib import SequenceMatcher
from typing import Any, Dict, Generator, Iterable, List, Tuple

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool as _rtp

from app.config import is_lite_mode
from app.conversation import (
    build_lite_transcript,
    compress_round_if_needed,
    compress_sliding_window_round,
)
from app.logger import logger
from app.model_registry import is_supported_model, list_available_models
from app.notion_client import NotionUpstreamError
from app.request_log import annotate as _request_log_annotate
from app.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    ChatMessageResponseChoice,
)

router = APIRouter()


# ─── 上游真实 usage 透传 ─────────────────────────────────────────
class _UsageCollector:
    """包裹上游事件流：吸收 {"type": "usage"} 事件并记录，其余事件原样透传。

    只记录 Notion 实际返回的数字，绝不估算。
    """

    def __init__(self, inner: Iterable[Any], include_usage: bool = True, account: str = ""):
        self._inner = iter(inner)
        self.include_usage = include_usage
        self.raw: dict[str, int] | None = None
        self.error: str | None = None
        _request_log_annotate(_collector=self, account=account or None)

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            try:
                item = next(self._inner)
            except StopIteration:
                raise
            except BaseException as exc:
                self.error = getattr(exc, "notion_error_message", "") or type(exc).__name__
                raise
            if isinstance(item, dict) and item.get("type") == "usage":
                usage = item.get("usage")
                if isinstance(usage, dict):
                    self.raw = dict(usage)
                continue
            return item

    def close(self):
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()



def _apply_reasoning_effort(transcript, req_body):
    """Carry the client's requested effort into the config block (resolved/clamped
    per model in NotionOpusAPI._to_notion_transcript)."""
    eff = getattr(req_body, "reasoning_effort", None)
    if not eff or not isinstance(transcript, list):
        return transcript
    out = []
    for b in transcript:
        if isinstance(b, dict) and b.get("type") == "config" and isinstance(b.get("value"), dict):
            b = {**b, "value": {**b["value"], "reasoningEffort": eff}}
        out.append(b)
    return out

def _openai_usage_from_raw(raw: dict[str, int] | None) -> dict[str, Any]:
    """Notion usage → OpenAI usage。

    Notion 字段语义（实测）：inputTokens = 未缓存 + 缓存命中；cachedTokensCreated 另计。
    prompt_tokens = inputTokens + cachedTokensCreated；cached_tokens = cachedTokensRead。
    缺失字段不编造：没有 cachedTokensRead 就不输出 prompt_tokens_details。
    """
    if not raw:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = int(raw.get("inputTokens", 0)) + int(raw.get("cachedTokensCreated", 0))
    completion = int(raw.get("outputTokens", 0))
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if "cachedTokensRead" in raw:
        usage["prompt_tokens_details"] = {"cached_tokens": int(raw["cachedTokensRead"])}
    return usage


def _openai_usage(stream_gen: Any) -> dict[str, Any]:
    return _openai_usage_from_raw(getattr(stream_gen, "raw", None))


def _include_usage_flag(req_body: Any) -> bool:
    """与 Agent2API 一致：流式默认总发 usage 收尾帧；仅客户端显式 include_usage=false 时不发。"""
    opts = getattr(req_body, "stream_options", None)
    if isinstance(opts, dict) and opts.get("include_usage") is False:
        return False
    return True


def _build_usage_chunk(response_id: str, model: str, stream_gen: Any) -> str:
    if not getattr(stream_gen, "include_usage", False):
        return ""
    payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": _openai_usage(stream_gen),
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _stream_error_hint(exc: BaseException) -> str:
    msg = getattr(exc, "notion_error_message", "") if isinstance(exc, NotionUpstreamError) else ""
    if msg:
        return f"\n\n[Notion 上游错误：{msg}]"
    return "\n\n[上游连接中断，请稍后重试。]"


# ─── 结构化错误响应 ─────────────────────────────────────────────
def _classify_upstream_error(exc: NotionUpstreamError) -> dict[str, Any]:
    """将 NotionUpstreamError 分类为结构化错误信息，前端可直接展示。"""
    sc = exc.status_code

    notion_msg = getattr(exc, "notion_error_message", "")
    if notion_msg:
        return {
            "code": f"NOTION_STREAM_ERROR" + (f"_{sc}" if sc else ""),
            "type": "upstream_error",
            "message": f"Notion 上游错误：{notion_msg}",
            "suggestion": "",
            "status": sc or 502,
        }

    if sc == 401:
        return {
            "code": "NOTION_401",
            "type": "upstream_auth_error",
            "message": "Notion 认证失败 (HTTP 401)，token 可能已过期",
            "suggestion": "请重新获取 token_v2 并更新配置",
        }
    if sc == 403:
        return {
            "code": "NOTION_403",
            "type": "upstream_forbidden",
            "message": "Notion 拒绝访问 (HTTP 403)，可能被 Cloudflare 拦截或账号受限",
            "suggestion": "检查服务器网络环境，或稍后重试",
        }
    if sc == 429:
        return {
            "code": "NOTION_429",
            "type": "upstream_rate_limit",
            "message": "Notion 请求频率过高 (HTTP 429)",
            "suggestion": "等待几秒后重试，或配置多个账号分散请求",
        }
    if sc and sc >= 500:
        return {
            "code": f"NOTION_{sc}",
            "type": "upstream_server_error",
            "message": f"Notion 服务暂时不可用 (HTTP {sc})",
            "suggestion": "Notion 服务端故障，请稍后重试",
        }
    if "timed out" in str(exc).lower():
        return {
            "code": "NETWORK_TIMEOUT",
            "type": "network_timeout",
            "message": "连接 Notion 超时",
            "suggestion": "检查服务器到 notion.so 的网络连通性",
        }
    if "failed" in str(exc).lower() and not sc:
        return {
            "code": "NETWORK_ERROR",
            "type": "network_error",
            "message": "无法连接 Notion 服务",
            "suggestion": "检查服务器网络和 DNS 配置",
        }
    if "empty" in str(exc).lower():
        return {
            "code": "NOTION_EMPTY",
            "type": "upstream_empty_response",
            "message": "Notion 返回了空内容",
            "suggestion": "请重新发送消息",
        }
    # 兜底
    return {
        "code": "UPSTREAM_UNKNOWN",
        "type": "upstream_error",
        "message": str(exc),
        "suggestion": "请稍后重试",
    }


def _build_error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    error_type: str = "server_error",
    suggestion: str = "",
    detail: str = "",
) -> JSONResponse:
    """构建统一格式的错误 JSON 响应，前端可解析展示。"""
    content: dict[str, Any] = {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
        }
    }
    if suggestion:
        content["error"]["suggestion"] = suggestion
    if detail:
        content["error"]["detail"] = detail
    return JSONResponse(status_code=status_code, content=content)


def _upstream_error_response(exc: NotionUpstreamError) -> JSONResponse:
    """将 NotionUpstreamError 转为统一的 503 JSON 响应。"""
    info = _classify_upstream_error(exc)
    return _build_error_response(
        int(info.get("status") or 503),
        code=info["code"],
        message=info["message"],
        error_type=info["type"],
        suggestion=info.get("suggestion", ""),
        detail=exc.response_excerpt or "",
    )


RECALL_INTENT_KEYWORDS = [
    "之前",
    "上次",
    "以前",
    "你还记得",
    "我们之前",
    "earlier",
    "before",
    "recall",
    "remember",
    "之前说过",
    "历史记录",
    "找一下",
    "搜索记忆",
]


def _build_stream_chunk(
    response_id: str,
    model: str,
    *,
    content: str = "",
    thinking: str = "",
    role: str = "",
    finish_reason=None,
) -> str:
    delta: Dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content:
        delta["content"] = content
    if thinking:
        delta["reasoning_content"] = thinking

    payload = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _build_local_ui_chunk(
    response_id: str,
    model: str,
    event_type: str,
    **payload_fields: Any,
) -> str:
    payload: Dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
        "type": event_type,
    }
    payload.update(payload_fields)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _format_search_results_md(search_data: dict[str, Any]) -> str:
    """将搜索数据格式化为 Markdown 引用块，以便标准客户端显示。"""
    lines = []
    queries = search_data.get("queries", [])
    if queries:
        lines.append(f"> 🔍 **已搜索:** {', '.join(queries)}")

    # Only public web sources: drop Notion-internal refs (user://, thread://, view://,
    # notion.so / notion.com pages) and duplicates.
    sources, seen = [], set()
    for src in search_data.get("sources", []) or []:
        url = str((src or {}).get("url") or "")
        low = url.lower()
        if not low.startswith(("http://", "https://")) or "notion.so" in low or "notion.com" in low or "notion.site" in low:
            continue
        if url in seen:
            continue
        seen.add(url)
        sources.append(src)
    if sources:
        lines.append("> 🌐 **来源:**")
        for i, src in enumerate(sources[:5], 1):  # 最多显示5个来源，避免刷屏
            title = src.get("title") or src.get("url") or "未知来源"
            url = src.get("url")
            lines.append(f"> {i}. [{title}]({url})")

    if lines and not sources and not queries:
        return ""
    if lines:
        return "\n\n" + "\n".join(lines) + "\n\n"
    return ""


def _normalize_stream_item(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        return {"type": "content", "text": item}

    if isinstance(item, dict):
        item_type = str(item.get("type", "") or "").lower()
        if item_type == "content":
            return {"type": "content", "text": str(item.get("text", "") or "")}
        if item_type == "search":
            payload = item.get("data")
            return {
                "type": "search",
                "data": payload if isinstance(payload, dict) else {},
            }
        if item_type == "thinking":
            return {"type": "thinking", "text": str(item.get("text", "") or "")}
        if item_type == "final_content":
            return {
                "type": "final_content",
                "text": str(item.get("text", "") or ""),
                "source_type": str(item.get("source_type", "") or ""),
                "source_length": item.get("source_length"),
            }

    return {"type": "unknown"}


def _iter_stream_items(
    first_item: Any, stream_gen: Iterable[Any]
) -> Generator[Any, None, None]:
    if first_item is not None:
        yield first_item
    for item in stream_gen:
        yield item


def _compute_missing_suffix(current_text: str, final_text: str) -> str:
    if not final_text:
        return ""
    if not current_text:
        return final_text
    if final_text.startswith(current_text):
        return final_text[len(current_text) :]
    return ""


def _select_best_final_reply(
    streamed_text: str,
    final_text: str,
    final_source_type: str,
) -> tuple[str, str]:
    streamed = streamed_text or ""
    final = final_text or ""
    streamed_stripped = streamed.strip()
    final_stripped = final.strip()
    source = (final_source_type or "").strip().lower()

    if not final_stripped:
        return streamed, "streamed_only"
    if not streamed_stripped:
        return final, "final_only"
    if final.startswith(streamed):
        return final, "final_extends_streamed"
    if streamed.startswith(final):
        if source == "title" or len(final_stripped) <= max(
            32, int(len(streamed_stripped) * 0.35)
        ):
            return streamed, "streamed_beats_short_final"
        return final, "final_prefix_of_streamed"

    # Diverged content: usually prefer richer non-title final content.
    if source == "title" and len(final_stripped) < max(
        48, int(len(streamed_stripped) * 0.6)
    ):
        return streamed, "streamed_beats_title"
    # Diverged: Notion's record-map "final" can be an earlier assistant turn replayed
    # from the client's history (multi-turn), so a diverging final is not trusted
    # unless the streamed text is only a short fragment.
    if len(streamed_stripped) < 20 and len(final_stripped) >= 48 and source != "agent-inference":
        return final, "final_diverged_preferred"
    return streamed, "streamed_diverged_preferred"


def _normalize_overlap_text(text: str) -> str:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return ""
    normalized = re.sub(r"```.*?```", " ", normalized, flags=re.DOTALL)
    normalized = re.sub(r"\s+", "", normalized)
    return normalized


def _trim_redundant_thinking(
    thinking_text: str, final_reply: str
) -> tuple[str, str, float]:
    thinking = str(thinking_text or "").strip()
    final = str(final_reply or "").strip()
    if not thinking or not final:
        return thinking, "missing_text", 0.0

    normalized_thinking = _normalize_overlap_text(thinking)
    normalized_final = _normalize_overlap_text(final)
    if not normalized_thinking or not normalized_final:
        return thinking, "missing_normalized_text", 0.0

    overlap_ratio = SequenceMatcher(None, normalized_thinking, normalized_final).ratio()
    if normalized_thinking == normalized_final:
        return "", "identical", overlap_ratio

    if thinking.endswith(final):
        prefix = thinking[: -len(final)].rstrip()
        if len(_normalize_overlap_text(prefix)) >= 10:
            return prefix, "suffix_trimmed", overlap_ratio
        return "", "suffix_cleared", overlap_ratio

    if overlap_ratio >= 0.92 and (
        normalized_thinking in normalized_final
        or normalized_final in normalized_thinking
    ):
        return "", "high_overlap_cleared", overlap_ratio

    return thinking, "kept", overlap_ratio


def _build_thinking_replacement(
    streamed_content_text: str,
    thinking_text: str,
    final_reply: str,
    final_source_type: str,
) -> dict[str, Any] | None:
    source = str(final_source_type or "").strip().lower()

    # Relax constraint: Allow replacement for more source types to fix Sonnet thinking leakage
    # But still require minimal validation for non-inference sources
    if source not in ("agent-inference", "text", "markdown-chat", ""):
        # Only skip for clearly non-thinking source types
        return None

    normalized_final = _normalize_overlap_text(final_reply)
    normalized_streamed = _normalize_overlap_text(streamed_content_text)

    # Require at least some thinking content to process
    if not _normalize_overlap_text(thinking_text):
        return None

    # For non-agent-inference sources, be more conservative but still check for obvious duplication
    if source != "agent-inference":
        # Only process if there's clear overlap or thinking is redundant
        if not normalized_final:
            return None

        # Check for obvious duplication (thinking appears in final reply)
        if thinking_text.strip() in final_reply or final_reply in thinking_text:
            # Clear case of duplication - trim it
            replacement, decision, overlap_ratio = _trim_redundant_thinking(
                thinking_text, final_reply
            )
            if replacement != str(thinking_text or "").strip():
                logger.debug(
                    "Non-agent-inference thinking replacement applied",
                    extra={
                        "request_info": {
                            "event": "thinking_replacement_non_agent",
                            "source_type": source,
                            "overlap_ratio": round(overlap_ratio, 4),
                            "decision": f"{decision}_non_agent_inference",
                        }
                    },
                )
                return {
                    "thinking": replacement,
                    "decision": f"{decision}_non_agent_inference",
                    "overlap_ratio": round(overlap_ratio, 4),
                    "source_type": source,
                }
        return None

    # Original agent-inference logic continues
    if not normalized_final:
        return None

    # 只在几乎没有真实正文增量时做裁决，避免误伤复杂推理场景。
    if normalized_streamed and len(normalized_streamed) >= max(
        10, int(len(normalized_final) * 0.35)
    ):
        return None

    replacement, decision, overlap_ratio = _trim_redundant_thinking(
        thinking_text, final_reply
    )
    if replacement == str(thinking_text or "").strip():
        return None

    return {
        "thinking": replacement,
        "decision": decision,
        "overlap_ratio": round(overlap_ratio, 4),
        "source_type": source,
    }


def _contains_recall_intent(text: str) -> bool:
    lowered = text.lower()
    for keyword in RECALL_INTENT_KEYWORDS:
        if keyword.isascii():
            if keyword.lower() in lowered:
                return True
            continue
        if keyword in text:
            return True
    return False


def _extract_recall_query(text: str) -> str:
    cleaned = text
    for keyword in RECALL_INTENT_KEYWORDS:
        if keyword.isascii():
            cleaned = re.sub(
                rf"\b{re.escape(keyword)}\b", " ", cleaned, flags=re.IGNORECASE
            )
        else:
            cleaned = cleaned.replace(keyword, " ")
    cleaned = re.sub(r"[\s，。！？、,.!?;:：]+", " ", cleaned).strip()
    return cleaned or text.strip()


def _prepare_messages(
    req_body: ChatCompletionRequest,
) -> Tuple[str, List[Tuple[str, str, str]], str]:
    system_messages = []
    dialogue_messages = []

    for msg in req_body.messages:
        if msg.role == "system":
            if msg.content.strip():
                system_messages.append(msg.content.strip())
            continue
        dialogue_messages.append((msg.role, msg.content, msg.thinking or ""))

    if not dialogue_messages:
        raise HTTPException(
            status_code=400,
            detail="The messages list must contain at least one user message.",
        )

    last_role, user_prompt, _ = dialogue_messages[-1]
    raw_user_prompt = user_prompt
    history_messages = dialogue_messages[:-1]

    if last_role != "user":
        raise HTTPException(
            status_code=400, detail="The last message must be from role 'user'."
        )
    if not user_prompt.strip():
        raise HTTPException(
            status_code=400, detail="The last user message cannot be empty."
        )

    if system_messages:
        from app.conversation import reframe_system_prompt_for_notion

        merged_system_prompt = "\n".join(system_messages)
        reframed = reframe_system_prompt_for_notion(merged_system_prompt)
        if reframed:
            user_prompt = f"{reframed}\n\n{user_prompt}"

    return user_prompt, history_messages, raw_user_prompt


def _prepare_messages_lite(req_body: ChatCompletionRequest) -> str:
    """Lite 模式：只提取最后一条 user 消息，支持 system 指令合并"""
    system_messages = []
    user_prompt = ""

    for msg in req_body.messages:
        if msg.role == "system" and msg.content.strip():
            system_messages.append(msg.content.strip())
        elif msg.role == "user":
            user_prompt = msg.content

    if not user_prompt.strip():
        raise HTTPException(
            status_code=400,
            detail="The messages list must contain at least one user message.",
        )

    if system_messages:
        from app.conversation import reframe_system_prompt_for_notion

        reframed = reframe_system_prompt_for_notion(" ".join(system_messages))
        if reframed:
            user_prompt = f"{reframed}\n\n{user_prompt}"

    return user_prompt


def _create_lite_stream_generator(
    response_id: str,
    model_name: str,
    first_item: Any,
    stream_gen: Iterable[Any],
) -> Generator[str, None, None]:
    """Lite 模式流式生成器：只输出 content，忽略 thinking 和 search"""
    streamed_content_accumulator = ""
    authoritative_final_content = ""
    authoritative_final_source_type = ""
    assistant_started = False

    try:
        for raw_item in _iter_stream_items(first_item, stream_gen):
            item = _normalize_stream_item(raw_item)
            item_type = item.get("type")

            if item_type == "final_content":
                final_text = str(item.get("text", "") or "").strip()
                if final_text:
                    authoritative_final_content = final_text
                    authoritative_final_source_type = str(
                        item.get("source_type", "") or ""
                    )
                continue

            # Lite 模式忽略 thinking 和 search
            if item_type in ("thinking", "search"):
                continue

            if item_type != "content":
                continue

            chunk_text = item.get("text", "")
            if not chunk_text:
                continue

            streamed_content_accumulator += chunk_text
            if not assistant_started:
                assistant_started = True
                yield _build_stream_chunk(
                    response_id,
                    model_name,
                    role="assistant",
                    content=chunk_text,
                )
            else:
                yield _build_stream_chunk(response_id, model_name, content=chunk_text)
    except asyncio.CancelledError:
        logger.info(
            "Lite streaming cancelled by client",
            extra={"request_info": {"event": "lite_stream_cancelled"}},
        )
        raise
    except BaseException as exc:
        if _is_client_disconnect_error(exc):
            logger.info(
                "Lite streaming connection closed by client",
                extra={"request_info": {"event": "lite_stream_client_disconnected"}},
            )
            return
        logger.error(
            "Lite streaming interrupted",
            exc_info=True,
            extra={"request_info": {"event": "lite_stream_interrupted"}},
        )
        error_hint = _stream_error_hint(exc)
        streamed_content_accumulator += error_hint
        if not assistant_started:
            assistant_started = True
            yield _build_stream_chunk(
                response_id,
                model_name,
                role="assistant",
                content=error_hint,
            )
        else:
            yield _build_stream_chunk(response_id, model_name, content=error_hint)
    finally:
        # 选择最佳最终回复
        final_reply, _ = _select_best_final_reply(
            streamed_content_accumulator,
            authoritative_final_content,
            authoritative_final_source_type,
        )

        # 发送缺失的后缀（如果有）
        missing_suffix = _compute_missing_suffix(
            streamed_content_accumulator, final_reply
        )
        if missing_suffix:
            if not assistant_started:
                assistant_started = True
                yield _build_stream_chunk(
                    response_id,
                    model_name,
                    role="assistant",
                    content=missing_suffix,
                )
            else:
                yield _build_stream_chunk(
                    response_id, model_name, content=missing_suffix
                )
            streamed_content_accumulator += missing_suffix
        elif final_reply != streamed_content_accumulator:
            # 处理分叉内容（使用最终内容）
            if not streamed_content_accumulator and final_reply:
                if not assistant_started:
                    assistant_started = True
                    yield _build_stream_chunk(
                        response_id,
                        model_name,
                        role="assistant",
                        content=final_reply,
                    )
                else:
                    yield _build_stream_chunk(
                        response_id, model_name, content=final_reply
                    )
                streamed_content_accumulator = final_reply

        yield _build_stream_chunk(response_id, model_name, finish_reason="stop")
        _usage_chunk = _build_usage_chunk(response_id, model_name, stream_gen)
        if _usage_chunk:
            yield _usage_chunk
        yield "data: [DONE]\n\n"


def _create_standard_stream_generator(
    response_id: str,
    model_name: str,
    first_item: Any,
    stream_gen: Iterable[Any],
    client_type: str = "",
) -> Generator[str, None, None]:
    """
    Standard 模式流式生成器：使用前端定义的 SSE 事件类型

    前端协议：
    - thinking_chunk: 流式思考片段
    - thinking_replace: 完整思考替换
    - search_metadata: 搜索结果
    - choices[0].delta.content: 正文内容

    对非 web 客户端（如 opencode 等严格 OpenAI 兼容客户端），thinking 使用
    delta.reasoning_content；search 以 markdown 注入 content（对齐 Heavy），
    避免自定义 SSE 字段触发 strict validation，同时不丢搜索结果。
    """
    streamed_content_accumulator = ""
    streamed_thinking_accumulator = ""
    collected_search_sources = []
    collected_search_queries = []
    authoritative_final_content = ""
    authoritative_final_source_type = ""
    assistant_started = False
    is_web_client = client_type == "web"
    aborted = False  # client went away: emit nothing more, stop the upstream
    stream_exc: BaseException | None = None

    try:
        for raw_item in _iter_stream_items(first_item, stream_gen):
            item = _normalize_stream_item(raw_item)
            item_type = item.get("type")

            if item_type == "final_content":
                final_text = str(item.get("text", "") or "").strip()
                if final_text:
                    authoritative_final_content = final_text
                    authoritative_final_source_type = str(
                        item.get("source_type", "") or ""
                    )
                continue

            # Standard 模式：处理 thinking
            if item_type == "thinking":
                thinking_text = item.get("text", "")
                if thinking_text:
                    streamed_thinking_accumulator += thinking_text
                    if is_web_client:
                        # Web UI: 前端协议 thinking_chunk
                        yield f"data: {json.dumps({'type': 'thinking_chunk', 'text': thinking_text}, ensure_ascii=False)}\n\n"
                    else:
                        # 严格 OpenAI 客户端：reasoning_content；首包带 role
                        if not assistant_started:
                            assistant_started = True
                            yield _build_stream_chunk(
                                response_id,
                                model_name,
                                role="assistant",
                                thinking=thinking_text,
                            )
                        else:
                            yield _build_stream_chunk(
                                response_id, model_name, thinking=thinking_text
                            )
                continue

            # Standard 模式：处理 search（收集起来，最后输出）
            if item_type == "search":
                search_data = item.get("data", {})
                if isinstance(search_data, dict):
                    # 提取 queries 和 sources
                    queries = search_data.get("queries", [])
                    sources = search_data.get("sources", [])

                    if queries:
                        collected_search_queries.extend(queries)
                    if sources:
                        collected_search_sources.extend(sources)
                continue

            if item_type != "content":
                continue

            chunk_text = item.get("text", "")
            if not chunk_text:
                continue

            streamed_content_accumulator += chunk_text

            # 输出标准 OpenAI 格式的 delta
            if not assistant_started:
                assistant_started = True
                yield _build_stream_chunk(
                    response_id,
                    model_name,
                    role="assistant",
                    content=chunk_text,
                )
            else:
                yield _build_stream_chunk(response_id, model_name, content=chunk_text)
    except (asyncio.CancelledError, GeneratorExit):
        aborted = True
        logger.info(
            "Standard streaming cancelled by client",
            extra={"request_info": {"event": "standard_stream_cancelled"}},
        )
        raise
    except BaseException as exc:
        if _is_client_disconnect_error(exc):
            aborted = True
            logger.info(
                "Standard streaming connection closed by client",
                extra={
                    "request_info": {"event": "standard_stream_client_disconnected"}
                },
            )
            return
        logger.error(
            "Standard streaming interrupted",
            exc_info=True,
            extra={"request_info": {"event": "standard_stream_interrupted"}},
        )
        stream_exc = exc
        if not streamed_content_accumulator.strip() and not authoritative_final_content:
            # nothing usable was sent: a proper error event (below), not an error-text "answer"
            return
        error_hint = _stream_error_hint(exc)
        streamed_content_accumulator += error_hint
        if not assistant_started:
            assistant_started = True
            yield _build_stream_chunk(
                response_id,
                model_name,
                role="assistant",
                content=error_hint,
            )
        else:
            yield _build_stream_chunk(response_id, model_name, content=error_hint)
    finally:
      if aborted:
        try:
            stream_gen.close()  # stop the Notion stream so the account isn't kept busy
        except Exception:
            pass
      else:
          # 选择最佳最终回复
          final_reply, _ = _select_best_final_reply(
              streamed_content_accumulator,
              authoritative_final_content,
              authoritative_final_source_type,
          )

          # 发送缺失的后缀（如果有）
          missing_suffix = _compute_missing_suffix(
              streamed_content_accumulator, final_reply
          )
          if missing_suffix:
              if not assistant_started:
                  assistant_started = True
                  yield _build_stream_chunk(
                      response_id,
                      model_name,
                      role="assistant",
                      content=missing_suffix,
                  )
              else:
                  yield _build_stream_chunk(
                      response_id, model_name, content=missing_suffix
                  )
              streamed_content_accumulator += missing_suffix
          elif final_reply != streamed_content_accumulator:
              # 处理分叉内容（使用最终内容）
              if not streamed_content_accumulator and final_reply:
                  if not assistant_started:
                      assistant_started = True
                      yield _build_stream_chunk(
                          response_id,
                          model_name,
                          role="assistant",
                          content=final_reply,
                      )
                  else:
                      yield _build_stream_chunk(
                          response_id, model_name, content=final_reply
                      )
                  streamed_content_accumulator = final_reply

          # 输出搜索结果
          if collected_search_sources or collected_search_queries:
              search_payload = {
                  "queries": collected_search_queries,
                  "sources": collected_search_sources,
              }
              if is_web_client:
                  # Web UI：扩展 search_metadata（带 OpenAI chunk 外壳）
                  yield _build_local_ui_chunk(
                      response_id,
                      model_name,
                      "search_metadata",
                      searches=search_payload,
                  )
              else:
                  # 严格客户端：markdown 注入 content，不丢结果
                  search_md = _format_search_results_md(search_payload)
                  if search_md:
                      if not assistant_started:
                          assistant_started = True
                          yield _build_stream_chunk(
                              response_id,
                              model_name,
                              role="assistant",
                              content=search_md,
                          )
                      else:
                          yield _build_stream_chunk(
                              response_id, model_name, content=search_md
                          )

          if not streamed_content_accumulator.strip():
              # nothing to show (error before any text, or Notion closed the stream empty):
              # an OpenAI-style error event so clients/relays don't take "" as the answer
              if isinstance(stream_exc, NotionUpstreamError):
                  info = _classify_upstream_error(stream_exc)
                  err = {"message": info.get("message"), "type": info.get("type", "upstream_error"),
                         "code": info.get("code"), "status": info.get("status") or stream_exc.status_code or 502}
              elif stream_exc is not None:
                  err = {"message": "Upstream connection interrupted, please retry.", "type": "upstream_error",
                         "code": "UPSTREAM_INTERRUPTED", "status": 502}
              else:
                  err = {"message": "Notion upstream returned empty content, please retry.", "type": "upstream_error",
                         "code": "EMPTY_CONTENT", "status": 502}
              logger.warning("Standard stream ended without content", extra={"request_info": {
                  "event": "standard_stream_empty", "code": err["code"]}})
              _request_log_annotate(error_code=err["code"], error_message=str(err["message"])[:300])
              yield f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"
              yield "data: [DONE]\n\n"
              return
          yield _build_stream_chunk(response_id, model_name, finish_reason="stop")
          _usage_chunk = _build_usage_chunk(response_id, model_name, stream_gen)
          if _usage_chunk:
              yield _usage_chunk
          yield "data: [DONE]\n\n"


def _tool_call_chunks(response_id: str, model: str, calls: list, role_needed: bool) -> Generator[str, None, None]:
    """OpenAI-style delta.tool_calls: header chunk (index/id/type/name, empty args) then args."""
    for i, call in enumerate(calls):
        fn = call["function"]
        head = {"index": i, "id": call["id"], "type": "function",
                "function": {"name": fn["name"], "arguments": ""}}
        delta: Dict[str, Any] = {"tool_calls": [head]}
        if role_needed and i == 0:
            delta = {"role": "assistant", "content": None, "tool_calls": [head]}
        for d in (delta, {"tool_calls": [{"index": i, "function": {"arguments": fn["arguments"]}}]}):
            payload = {"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()),
                       "model": model, "choices": [{"index": 0, "delta": d, "finish_reason": None}]}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


_TOOL_REFUSAL_RE = re.compile(
    r"prompt[ -]?injection|提示注入|指令注入|injected instructions|embedded instructions|"
    r"I'?m Notion AI|我是 ?Notion AI|作为 ?Notion AI|not able to follow|won'?t (?:be )?follow|"
    r"can(?:no|')t follow|不会(?:执行|遵循)|无法(?:执行|遵循)",
    re.I,
)
TOOL_MODE_MAX_ATTEMPTS = 3
_TICK = object()


_URL_SCHEME_RE = re.compile(r"(?i)\b(https?):(?=//)")


def _defang_urls(obj):
    if isinstance(obj, str):
        return _URL_SCHEME_RE.sub(lambda m: m.group(1) + ":\u200b", obj)
    if isinstance(obj, list):
        return [_defang_urls(x) for x in obj]
    if isinstance(obj, dict):
        return {k: (v if k in ("id", "type") else _defang_urls(v)) for k, v in obj.items()}
    return obj


def _last_user_ask(messages) -> str:
    """Text of the client's last message when it is a user turn ("" after tool results)."""
    try:
        last = messages[-1] if messages else None
        role = last.get("role") if isinstance(last, dict) else getattr(last, "role", None)
        if role != "user":
            return ""
        c = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
        if isinstance(c, str):
            return c
        out = []
        for part in c or []:
            p = part if isinstance(part, dict) else getattr(part, "__dict__", {})
            if p.get("type") in ("text", "input_text") and isinstance(p.get("text"), str):
                out.append(p["text"])
        return "\n".join(out)
    except Exception:
        return ""


def _tick_iter(it, every: float = 5.0, on_stop=None):
    """Yield items from `it`, plus _TICK whenever nothing arrived for `every` seconds.
    The upstream is consumed in a worker thread so ticks flow even while Notion is
    silent (long tool_call generation, backoff sleeps). Closing this generator (client
    disconnect) tells the worker to close the upstream at its next item."""
    import queue as _q
    import threading as _th
    q: "_q.Queue" = _q.Queue(maxsize=256)
    stop = _th.Event()
    _END = object()

    def work():
        try:
            for x in it:
                if stop.is_set():
                    break
                q.put(("i", x))
            q.put(("e", _END))
        except BaseException as exc:  # forwarded to the consumer
            q.put(("x", exc))
        finally:
            if stop.is_set():
                close = getattr(it, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:
                        pass

    t = _th.Thread(target=work, name="tool-upstream", daemon=True)
    t.start()
    last = time.monotonic()
    try:
        while True:
            try:
                kind, x = q.get(timeout=max(0.05, every - (time.monotonic() - last)))
            except _q.Empty:
                last = time.monotonic()
                yield _TICK
                continue
            if time.monotonic() - last >= every:
                last = time.monotonic()
                yield _TICK
            if kind == "i":
                yield x
            elif kind == "x":
                raise x
            else:
                return
    except GeneratorExit:
        # consumer gone (client disconnected): don't wait for Notion's next item to notice
        stop.set()
        if on_stop is not None:
            try:
                on_stop()
            except Exception:
                pass
        raise
    finally:
        stop.set()


def _lowered_effort(transcript):
    """Config block with high/xhigh/max reasoning effort lowered to medium."""
    out = []
    for b in transcript:
        if isinstance(b, dict) and b.get("type") == "config" and isinstance(b.get("value"), dict) \
                and str(b["value"].get("reasoningEffort") or "").lower() in ("high", "xhigh", "max"):
            logger.info("Retrying with lower reasoning effort", extra={"request_info": {
                "event": "effort_lowered", "from": b["value"].get("reasoningEffort"), "to": "medium"}})
            b = {**b, "value": {**b["value"], "reasoningEffort": "medium"}}
        out.append(b)
    return out


def _abort_upstream(meta) -> None:
    """Client went away: flag the request cancelled (stops retries / thread polling) and
    shut down the in-flight Notion socket so the blocked reader returns now instead of
    at Notion's next item (which can be 30-60s away while a tool call is generated)."""
    if not isinstance(meta, dict):
        return
    meta["_cancelled"] = True
    resp = meta.get("_http_response")
    if resp is None:
        return
    import socket as _socket
    try:
        fp = getattr(getattr(resp, "raw", None), "_fp", None)
        sock = getattr(getattr(getattr(fp, "fp", None), "raw", None), "_sock", None)
        if sock is not None:
            sock.shutdown(_socket.SHUT_RDWR)
    except Exception:
        pass
    logger.info("Client gone: aborted in-flight Notion stream", extra={"request_info": {"event": "upstream_aborted"}})


def _sum_usage(a: dict | None, b: dict | None) -> dict | None:
    if not a:
        return dict(b) if b else None
    if not b:
        return a
    return {k: int(a.get(k, 0)) + int(b.get(k, 0)) for k in set(a) | set(b)}


def _run_tool_attempts(client, transcript, *, include_usage, account, tools, parallel, mode,
                       thread_id=None, fallback_transcript=None, meta=None, pool=None):
    """Run the emulated-tools request, retrying (fresh thread) when Notion's persona
    refuses the tool protocol or a forced tool call is missing. Yields "tick" while
    waiting (for SSE keepalives) and finally ("done", result)."""
    from app.tool_emulation import parse_tool_calls

    total_raw = None
    collector = None
    result = None
    budget = float(os.getenv("NOTION_REQUEST_BUDGET", "330") or 0)
    deadline = time.time() + budget if budget > 0 else None
    if meta is None:
        meta = {}
    meta["_deadline"] = deadline
    lower_effort = False
    tried = {getattr(client, "account_key", "")}
    for attempt in range(1, TOOL_MODE_MAX_ATTEMPTS + 1):
        if meta.get("_cancelled"):
            return  # client gone: no result
        if attempt > 1 and deadline and time.time() > deadline - 60:
            logger.info("Tool mode: request budget exhausted, not retrying", extra={"request_info": {
                "event": "tool_mode_budget_exhausted", "attempt": attempt, "budget_s": budget}})
            break
        if deadline:  # leave room for one failover retry after the first attempt
            meta["_deadline"] = deadline - 150 if attempt == 1 else deadline
        # fresh block ids per attempt: Notion rejects re-sent ids (400 ValidationError / 502)
        # Same-thread 502 short-retry keeps the continued transcript; other retries use full.
        _same_keep = bool(meta.get("_same_thread_502_keep"))
        base = transcript if (attempt == 1 or _same_keep) else (fallback_transcript or transcript)
        attempt_transcript = [dict(b, id=str(uuid.uuid4())) if isinstance(b, dict) and "id" in b else b
                              for b in base] if attempt > 1 else base
        if lower_effort:
            attempt_transcript = _lowered_effort(attempt_transcript)
        if attempt > 1 and thread_id and meta is not None and not _same_keep:
            meta["reuse_fallback"] = "retry"
            from app.request_log import current_log as _cl, annotate as _rl_annotate
            if not str((_cl.get() or {}).get("thread_reuse") or "").startswith("hit>"):
                _rl_annotate(thread_reuse="hit>fallback:retry")  # refusal / empty answer -> fresh thread
        # Keep the Notion thread across a single 502 short-retry; later fallbacks clear it.
        _keep_tid = thread_id if (attempt == 1 or meta.get("_same_thread_502_keep")) else None
        if attempt > 1 and meta.get("_same_thread_502_keep"):
            meta.pop("_same_thread_502_keep", None)  # consumed for this one retry
        collector = _UsageCollector(client.stream_response(attempt_transcript, thread_id=_keep_tid,
                                                           meta=meta),
                                    include_usage=include_usage, account=account)
        content, thinking, final_text, final_src = [], [], "", ""
        failed = False
        got_items = 0
        try:
          try:
              for raw_item in _tick_iter(collector, on_stop=lambda m=meta: _abort_upstream(m)):
                  if raw_item is _TICK:
                      yield "tick"
                      continue
                  got_items += 1
                  item = _normalize_stream_item(raw_item)
                  typ = item.get("type")
                  if typ == "content" and item.get("text"):
                      content.append(item["text"])
                  elif typ == "thinking" and item.get("text"):
                      thinking.append(item["text"])
                  elif typ == "final_content":
                      ft = str(item.get("text", "") or "").strip()
                      if ft:
                          final_text, final_src = ft, str(item.get("source_type", "") or "")
          except NotionUpstreamError as exc:
              # Empty-stream 502 on a continued thread: short-retry SAME thread once before
              # opening a new/fallback thread (preserves Notion prompt cache).
              same_tid_retry = (
                  exc.retriable and (exc.status_code or 0) == 502 and thread_id
                  and not meta.get("_same_thread_502_retried") and not got_items
                  and not meta.get("_cancelled")
              )
              if same_tid_retry:
                  meta["_same_thread_502_retried"] = True
                  meta["_same_thread_502_keep"] = True
                  from app.request_log import annotate as _rl_annotate
                  _rl_annotate(thread_reuse="hit>retry502")
                  logger.info("Tool mode: 502 on continued thread, short-retry same thread",
                              extra={"request_info": {"event": "tool_mode_same_thread_502_retry",
                                                      "thread_id": thread_id, "attempt": attempt}})
                  failed = True
              else:
                  if thread_id and meta is not None and (
                          attempt == 1 or meta.get("_same_thread_502_retried")):
                      from app.request_log import annotate as _rl_annotate
                      from app import thread_reuse as _tr
                      # the continued thread failed: report the real outcome, and if Notion
                      # rejected the turn (rate limit) keep the entry for the client's retry
                      _rl_annotate(thread_reuse=f"hit>fallback:{exc.status_code or 'err'}")
                      if exc.status_code in (429, 503) and not got_items:
                          _tr.STORE.restore(meta.get("_reuse_entry"))
                  meta.pop("_same_thread_502_keep", None)
                  # never re-hit a fair-use rate limit on the same account: fail over to another
                  # free account (fresh full thread) like standard mode, else surface it
                  # Any retriable failure (fair-use 429/503, empty stream 502, dead token 401):
                  # rest this account and fail over to another free one. Retrying the same
                  # account only works for one-off glitches, so do that only when alone.
                  can_failover = (exc.retriable and pool is not None and attempt < TOOL_MODE_MAX_ATTEMPTS
                                  and _other_account_free(pool, client))
                  if exc.status_code in (429, 503) and not can_failover:
                      raise
                  if can_failover:
                      rl = exc.status_code in (429, 503)
                      pool.mark_failed(client, cooldown_seconds=int(os.getenv("NOTION_RATELIMIT_FAILOVER_COOLDOWN", "45") or 45) if rl else 3)
                      new_client = pool.get_client(True, tried)
                      logger.info("Rate-limited: failing over to another account", extra={"request_info": {
                          "event": "ratelimit_failover", "mode": "tool", "from": getattr(client, "account_key", ""),
                          "to": getattr(new_client, "account_key", ""), "status": exc.status_code}})
                      client = new_client
                      tried.add(getattr(client, "account_key", ""))
                      account = getattr(client, "account_label", "") or getattr(client, "account_key", "")
                      thread_id = None
                  elif not (exc.retriable and attempt < TOOL_MODE_MAX_ATTEMPTS):
                      raise
                  logger.info("Tool mode: upstream error, retrying",
                              extra={"request_info": {"event": "tool_mode_retry_upstream", "attempt": attempt}})
                  failed = True
        finally:
            # client gone (GeneratorExit) or error: _tick_iter's worker closes the Notion
            # stream at its next item, which ends generation and frees the account
            pass
        if failed:
            total_raw = _sum_usage(total_raw, collector.raw)
            continue
        total_raw = _sum_usage(total_raw, collector.raw)
        streamed = "".join(content)
        # Notion's record-map "final" may be an *earlier* assistant turn replayed from
        # history (e.g. the previous tool call); only trust it when it continues what
        # was streamed (streamed deltas occasionally lose fragments).
        cands = [streamed] if streamed.strip() else []
        if final_text.strip() and final_src != "title" and (
                not streamed.strip() or final_text.startswith(streamed.strip()[:40])):
            cands.append(final_text)
        best = streamed if streamed.strip() else (final_text if final_src != "title" else "")
        visible, calls = "", []
        for cand in cands:
            visible, calls = parse_tool_calls(cand, tools, parallel)
            if calls:
                break
        text = visible if calls else (streamed if streamed.strip() else best)
        from app.meta_commentary import strip_meta_commentary
        text = strip_meta_commentary(text)
        result = {"text": text, "calls": calls, "thinking": "".join(thinking), "attempts": attempt}
        if calls:
            break
        if not text.strip() and attempt < TOOL_MODE_MAX_ATTEMPTS:
            # an empty answer usually means the account's inference stalled: retry on
            # another free account (fresh thread) instead of the same one
            if pool is not None and _other_account_free(pool, client):
                try:
                    pool.mark_failed(client)  # counts toward the account's failure streak
                    new_client = pool.get_client(True, tried)
                    if new_client is not client:
                        logger.info("Empty answer: failing over to another account", extra={"request_info": {
                            "event": "empty_failover", "from": getattr(client, "account_key", ""),
                            "to": getattr(new_client, "account_key", "")}})
                        client = new_client
                        tried.add(getattr(client, "account_key", ""))
                        account = getattr(client, "account_label", "") or getattr(client, "account_key", "")
                        thread_id = None
                except Exception:
                    pass
            # e.g. Notion's built-in URL fetch / confirmation failed mid-answer: nothing usable.
            # High effort thinks silently the longest (Notion drops the stream after ~60s idle):
            # retry at medium effort (TOOL_MODE_EMPTY_LOWER_EFFORT=0 disables)
            lower_effort = os.getenv("TOOL_MODE_EMPTY_LOWER_EFFORT", "1").strip().lower() not in ("0", "false", "off", "no")
            logger.info("Tool mode: empty answer, retrying",
                        extra={"request_info": {"event": "tool_mode_retry_empty", "attempt": attempt}})
            continue
        refused = bool(_TOOL_REFUSAL_RE.search(text[:1500]))
        if not (refused or mode in ("required", "specific")):
            break
        logger.info("Tool mode: no tool call (refusal=%s), retrying" % refused,
                    extra={"request_info": {"event": "tool_mode_retry", "attempt": attempt, "refused": refused}})
    if collector is not None:
        collector.raw = total_raw  # report the sum of all attempts
    result["collector"] = collector
    yield ("done", result)


class _ClientGone(Exception):
    pass


def _tool_mode_non_stream(gen, response_id, model, cancel=None):
    result = None
    for ev in gen:
        if isinstance(ev, tuple):
            result = ev[1]
        elif cancel is not None and cancel.is_set():
            # client disconnected: stop retries / thread polling and close the Notion stream
            try:
                gen.close()
            except Exception:
                pass
            raise _ClientGone()
    if result is None:
        raise _ClientGone()
    calls = result["calls"]
    if not calls and not result["text"].strip():
        raise NotionUpstreamError("Notion upstream returned empty content.", retriable=True)
    msg: Dict[str, Any] = {"role": "assistant", "content": (result["text"] or None) if calls else result["text"]}
    if calls:
        msg["tool_calls"] = calls
    if result["thinking"]:
        msg["reasoning_content"] = result["thinking"]
    return JSONResponse({
        "id": response_id, "object": "chat.completion", "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop", "logprobs": None}],
        "usage": _openai_usage(result["collector"]),
    })


async def _client_disconnected(request) -> bool:
    raw = request.scope.get("n2a_raw_receive")
    if raw is None:
        return await request.is_disconnected()
    import anyio
    msg = None
    with anyio.move_on_after(0.01):
        msg = await raw()  # body is already consumed: only http.disconnect can arrive
    return bool(msg) and msg.get("type") == "http.disconnect"


def _collect_items_cancellable(first_item, stream_gen, cancel=None):
    out = []
    for x in _iter_stream_items(first_item, stream_gen):
        if cancel is not None and cancel.is_set():
            try:
                stream_gen.close()
            except Exception:
                pass
            raise _ClientGone()
        out.append(x)
    return out


async def _run_tool_non_stream_cancellable(request, gen, response_id, model):
    return await _run_cancellable(request, _tool_mode_non_stream, gen, response_id, model)


async def _run_cancellable(request, fn, *args):
    """Run fn(*args, cancel) in a worker thread; if the client disconnects meanwhile, set
    `cancel` so the upstream work (up to NOTION_REQUEST_BUDGET) stops burning quota.
    Returns a 499 Response when it was cancelled."""
    import threading as _th
    cancel = _th.Event()
    task = asyncio.ensure_future(_rtp(fn, *args, cancel))
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=2.0)
            if done:
                break
            try:
                if await _client_disconnected(request):
                    cancel.set()
                    logger.info("Non-stream: client disconnected, stopping upstream",
                                extra={"request_info": {"event": "nonstream_client_disconnected"}})
                    _request_log_annotate(error_code="CLIENT_DISCONNECTED")
            except Exception:
                pass
        return task.result()
    except _ClientGone:
        return Response(status_code=499)
    except asyncio.CancelledError:
        cancel.set()
        raise


_GEN_END = object()


def _next_or_end(gen):
    try:
        return next(gen)
    except StopIteration:
        return _GEN_END


def _close_when_idle(gen, limit: float = 120.0) -> None:
    """Close a sync generator that may still be executing in a worker thread."""
    end = time.time() + limit
    while time.time() < end:
        try:
            gen.close()
            return
        except ValueError:  # "generator already executing": wait for the in-flight next()
            time.sleep(0.1)
        except Exception:
            return


async def _guarded_stream(request, gen, meta=None):
    """Serve a blocking SSE generator from a worker thread while watching the client.
    Starlette neither closes a sync body iterator on disconnect nor (behind
    BaseHTTPMiddleware) reports http.disconnect to it, so a dropped client kept Notion
    generating until the answer ended (tens of seconds, holding the account). Here the
    raw ASGI receive is polled; on disconnect the in-flight Notion read is aborted
    (meta) and the generator closed, which ends retries / thread polling."""
    import anyio
    import threading as _th
    stop = _th.Event()

    async def watch():
        while not stop.is_set():
            await asyncio.sleep(1.0)
            try:
                if await _client_disconnected(request):
                    stop.set()
                    logger.info("Stream: client disconnected, stopping upstream",
                                extra={"request_info": {"event": "stream_client_gone"}})
                    _request_log_annotate(error_code="CLIENT_DISCONNECTED")
                    await anyio.to_thread.run_sync(_abort_upstream, meta)
                    return
            except Exception:
                return

    watcher = asyncio.ensure_future(watch())
    finished = False
    try:
        while not stop.is_set():
            chunk = await anyio.to_thread.run_sync(_next_or_end, gen, abandon_on_cancel=True)
            if chunk is _GEN_END:
                finished = True
                return
            if stop.is_set():
                return
            yield chunk
    finally:
        stop.set()
        watcher.cancel()
        if isinstance(meta, dict) and not finished:
            meta["_cancelled"] = True
        _th.Thread(target=_close_when_idle, args=(gen,), name="close-stream", daemon=True).start()


def _prepull_tool_events(gen):
    """Pull tool-mode events until the first keepalive tick or the final result.
    Returns (events, exception)."""
    held = []
    try:
        for ev in gen:
            held.append(ev)
            if ev == "tick" or isinstance(ev, tuple):
                break
    except Exception as exc:  # noqa: BLE001 - re-raised by the caller
        return held, exc
    return held, None


def _chain_events(held, gen):
    yield from held
    if held and isinstance(held[-1], tuple):
        return
    yield from gen


def _tool_mode_stream(gen, response_id, model, client_type=""):
    """Tool mode streams after the (possibly retried) attempt completes, so a refused
    attempt is never shown; SSE comments keep the connection alive meanwhile."""
    result = None
    # Send real SSE data immediately and as keepalive: relays (new-api) drop ": comment"
    # lines, so clients (WorkBuddy) saw no bytes for >60s and aborted ("用户已取消").
    # Data keepalives also let a client disconnect propagate back here quickly.
    _ka = "data: " + json.dumps({"id": response_id, "object": "chat.completion.chunk", "created": int(time.time()),
                                 "model": model, "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]},
                                ensure_ascii=False) + "\n\n"
    yield _build_stream_chunk(response_id, model, role="assistant", content="")
    try:
        for ev in gen:
            if isinstance(ev, tuple):
                result = ev[1]
            else:
                yield _ka
    except Exception as exc:
        if _is_client_disconnect_error(exc):
            return
        logger.error("Tool streaming failed", exc_info=True, extra={"request_info": {"event": "tool_stream_failed"}})
        # nothing has been streamed yet: send an OpenAI-style error event so SDKs raise
        # (an agent must not mistake an upstream failure for the assistant's answer)
        if isinstance(exc, NotionUpstreamError):
            info = _classify_upstream_error(exc)
            err = {"message": info.get("message"), "type": info.get("type", "upstream_error"),
                   "code": info.get("code"), "status": info.get("status") or exc.status_code or 502}
        else:
            err = {"message": "Upstream connection interrupted, please retry.", "type": "upstream_error",
                   "code": "UPSTREAM_INTERRUPTED", "status": 502}
        yield f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return
    started = True  # role chunk already sent
    if result is None:  # cancelled (client gone)
        return
    if not result["calls"] and not (result["text"] or "").strip():
        err = {"message": "Notion upstream returned empty content, please retry.", "type": "upstream_error",
               "code": "EMPTY_CONTENT", "status": 502}
        yield f"data: {json.dumps({'error': err}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        return
    if result["thinking"] and client_type != "web":
        yield _build_stream_chunk(response_id, model, role="assistant", thinking=result["thinking"])
        started = True
    if result["text"]:
        yield _build_stream_chunk(response_id, model, role="" if started else "assistant", content=result["text"])
        started = True
    if result["calls"]:
        yield from _tool_call_chunks(response_id, model, result["calls"], role_needed=not started)
    elif not started:
        yield _build_stream_chunk(response_id, model, role="assistant", content="")
    yield _build_stream_chunk(response_id, model, finish_reason="tool_calls" if result["calls"] else "stop")
    usage_chunk = _build_usage_chunk(response_id, model, result["collector"])
    if usage_chunk:
        yield usage_chunk
    yield "data: [DONE]\n\n"


def _create_tool_stream_generator(
    response_id: str,
    model_name: str,
    first_item: Any,
    stream_gen: Iterable[Any],
    tools: list,
    parallel: bool,
    client_type: str = "",
) -> Generator[str, None, None]:
    """Streaming with emulated function calling: plain text is streamed live, anything
    from the first <tool_call> on is buffered, parsed and emitted as delta.tool_calls."""
    from app.tool_emulation import ToolStreamSplitter

    splitter = ToolStreamSplitter()
    raw = ""
    final_text = ""
    final_source = ""
    started = False
    calls: list = []
    try:
        for raw_item in _iter_stream_items(first_item, stream_gen):
            item = _normalize_stream_item(raw_item)
            typ = item.get("type")
            if typ == "final_content":
                ft = str(item.get("text", "") or "").strip()
                if ft:
                    final_text, final_source = ft, str(item.get("source_type", "") or "")
                continue
            if typ == "thinking" and client_type != "web":
                t = item.get("text", "")
                if t:
                    yield _build_stream_chunk(response_id, model_name, role="" if started else "assistant", thinking=t)
                    started = True
                continue
            if typ != "content":
                continue
            t = item.get("text", "")
            if not t:
                continue
            raw += t
            out = splitter.feed(t)
            if out:
                yield _build_stream_chunk(response_id, model_name, role="" if started else "assistant", content=out)
                started = True
    except asyncio.CancelledError:
        raise
    except BaseException as exc:
        if _is_client_disconnect_error(exc):
            return
        logger.error("Tool streaming interrupted", exc_info=True,
                     extra={"request_info": {"event": "tool_stream_interrupted"}})
        hint = _stream_error_hint(exc)
        yield _build_stream_chunk(response_id, model_name, role="" if started else "assistant", content=hint)
        started = True
    best, _ = _select_best_final_reply(raw, final_text, final_source)
    tail, calls = splitter.finish(tools, parallel)
    if not calls and splitter.capturing and best != raw:
        # streamed deltas truncated inside a tool block: retry with Notion's final text
        tail, calls = splitter.finish(tools, parallel, final_text=best)
    if tail:
        yield _build_stream_chunk(response_id, model_name, role="" if started else "assistant", content=tail)
        started = True
    if calls:
        yield from _tool_call_chunks(response_id, model_name, calls, role_needed=not started)
    elif not started:
        yield _build_stream_chunk(response_id, model_name, role="assistant", content="")
    yield _build_stream_chunk(response_id, model_name, finish_reason="tool_calls" if calls else "stop")
    _usage_chunk = _build_usage_chunk(response_id, model_name, stream_gen)
    if _usage_chunk:
        yield _usage_chunk
    yield "data: [DONE]\n\n"


def _persist_round(
    manager,
    background_tasks: BackgroundTasks,
    conversation_id: str,
    user_prompt: str,
    assistant_reply: str,
    assistant_thinking: str = "",
) -> None:
    """
    持久化一轮对话并触发异步预压缩。

    预压缩逻辑：
    - 当 round >= WINDOW_ROUNDS//2 时，提前压缩滑出窗口的轮次
    - 使用 BackgroundTasks 确保不阻塞当前对话
    """
    round_index = manager.persist_round(
        conversation_id,
        user_prompt,
        assistant_reply,
        assistant_thinking=assistant_thinking,
    )

    # 异步预压缩：当窗口快满时提前压缩
    WINDOW_ROUNDS = 8  # 与 conversation.py 保持一致
    PRECOMPRESS_THRESHOLD = WINDOW_ROUNDS // 2  # 在第 4 轮时开始预压缩

    if round_index >= PRECOMPRESS_THRESHOLD:
        # 计算需要压缩的轮次（滑出窗口的轮次）
        round_to_compress = round_index - WINDOW_ROUNDS + 1
        if round_to_compress >= 0:
            background_tasks.add_task(
                compress_sliding_window_round,
                manager=manager,
                conversation_id=conversation_id,
                round_number=round_to_compress,
            )
            logger.info(
                "Triggered async pre-compression",
                extra={
                    "request_info": {
                        "event": "async_precompress_triggered",
                        "conversation_id": conversation_id,
                        "current_round": round_index,
                        "compress_round": round_to_compress,
                    }
                },
            )

    # 保留原有的压缩逻辑作为兜底
    background_tasks.add_task(
        compress_round_if_needed,
        manager=manager,
        conversation_id=conversation_id,
    )


def _persist_history_messages(
    manager, conversation_id: str, history_messages: List[Tuple[str, str, str]]
) -> None:
    for role, content, thinking in history_messages:
        manager.add_message(conversation_id, role, content, thinking)


def _is_client_disconnect_error(exc: BaseException) -> bool:
    if isinstance(exc, asyncio.CancelledError):
        return True
    if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
        return True
    if isinstance(exc, OSError):
        return exc.errno in {32, 54, 104, 10053, 10054}
    return False


async def _handle_lite_request(
    request: Request,
    req_body: ChatCompletionRequest,
    response: Response,
) -> JSONResponse | StreamingResponse | ChatCompletionResponse:
    """处理 Lite 模式请求（无记忆，单轮问答）"""
    pool = request.app.state.account_pool

    # 提取用户问题
    user_prompt = _prepare_messages_lite(req_body)

    # 验证模型
    if not is_supported_model(req_body.model):
        available_models = list_available_models()
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported model '{req_body.model}'. Available models: {', '.join(available_models)}",
        )

    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    max_retries = max(3, len(pool.clients))

    for attempt in range(1, max_retries + 1):
        client = None
        try:
            client = await _rtp(pool.get_client)

            # 构建 Lite transcript（无历史记忆）
            transcript = _apply_reasoning_effort(build_lite_transcript(user_prompt, req_body.model), req_body)

            # 调用 Notion API（不使用 thread_id）
            stream_gen = _UsageCollector(client.stream_response(transcript, thread_id=None), include_usage=_include_usage_flag(req_body), account=getattr(client, "account_label", "") or getattr(client, "account_key", ""))
            first_item = await _rtp(next, stream_gen, None)

            if first_item is None:
                raise NotionUpstreamError(
                    "Notion upstream returned empty content.", retriable=True
                )

            # 流式响应
            if req_body.stream:
                stream_headers = {
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                }
                return StreamingResponse(
                    _create_lite_stream_generator(
                        response_id,
                        req_body.model,
                        first_item,
                        stream_gen,
                    ),
                    media_type="text/event-stream",
                    headers=stream_headers,
                )

            # 非流式响应
            content_parts: list[str] = []
            authoritative_final_content = ""
            authoritative_final_source_type = ""

            _items = await _rtp(lambda: list(_iter_stream_items(first_item, stream_gen)))
            for raw_item in _items:
                item = _normalize_stream_item(raw_item)
                item_type = item.get("type")

                if item_type == "final_content":
                    final_text = str(item.get("text", "") or "").strip()
                    if final_text:
                        authoritative_final_content = final_text
                        authoritative_final_source_type = str(
                            item.get("source_type", "") or ""
                        )
                    continue

                # Lite 模式忽略 thinking 和 search
                if item_type in ("thinking", "search"):
                    continue

                if item_type != "content":
                    continue

                chunk_text = item.get("text", "")
                if chunk_text:
                    content_parts.append(chunk_text)

            full_text, _ = _select_best_final_reply(
                "".join(content_parts),
                authoritative_final_content,
                authoritative_final_source_type,
            )

            if not full_text.strip():
                raise NotionUpstreamError(
                    "Notion upstream returned empty content.", retriable=True
                )

            response_text = (
                full_text if full_text.strip() else "[assistant_no_visible_content]"
            )
            return ChatCompletionResponse(
                id=response_id,
                usage=_openai_usage(stream_gen),
                model=req_body.model,
                choices=[
                    ChatMessageResponseChoice(
                        message=ChatMessage(role="assistant", content=response_text)
                    )
                ],
            )

        except NotionUpstreamError as exc:
            if client is not None and exc.retriable:
                pool.mark_failed(client)
            logger.warning(
                "Lite mode: Notion upstream failed",
                extra={
                    "request_info": {
                        "event": "lite_notion_upstream_failed",
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "status_code": exc.status_code,
                        "retriable": exc.retriable,
                        "response_excerpt": exc.response_excerpt,
                    }
                },
            )
            if attempt == max_retries or not exc.retriable:
                return _upstream_error_response(exc)
        except RuntimeError as exc:
            logger.error(
                "Lite mode: No available client in account pool",
                extra={
                    "request_info": {
                        "event": "lite_account_pool_unavailable",
                        "detail": str(exc),
                    }
                },
            )
            return _build_error_response(
                503,
                code="POOL_COOLING",
                message=str(exc),
                error_type="account_pool_cooling",
                suggestion="所有账号暂时冷却中，请等待几秒后重试",
            )
        except HTTPException:
            raise
        except Exception:
            if client is not None:
                pool.mark_failed(client)
            logger.error(
                "Lite mode: Unhandled error",
                exc_info=True,
                extra={
                    "request_info": {
                        "event": "lite_unhandled_exception",
                        "attempt": attempt,
                    }
                },
            )
            if attempt == max_retries:
                return _build_error_response(
                    500,
                    code="INTERNAL_ERROR",
                    message="服务内部异常",
                    error_type="internal_error",
                    suggestion="请稍后重试，如持续出现请联系管理员",
                )

    return _build_error_response(
        503,
        code="RETRIES_EXHAUSTED",
        message="所有重试均已失败",
        error_type="upstream_error",
        suggestion="Notion 上游服务暂时不可用，请稍后重试",
    )


def _tr_capture_stream(gen, on_reply):
    """Pass SSE chunks through; when the stream ends normally, rebuild the assistant
    message the client received and hand it to on_reply (thread-reuse bookkeeping)."""
    from app.thread_reuse import reply_from_sse
    chunks: list[str] = []
    completed = False
    try:
        for ch in gen:
            if isinstance(ch, str):
                chunks.append(ch)
            yield ch
        completed = True
    finally:
        if completed:
            try:
                msg = reply_from_sse(chunks)
                if msg:
                    on_reply(msg)
            except Exception:
                logger.warning("thread reuse: capture failed", exc_info=True)


def _tr_reply_from_response(resp) -> dict | None:
    try:
        if isinstance(resp, JSONResponse):
            body = json.loads(resp.body)
            if body.get("error"):
                return None
            m = body["choices"][0]["message"]
        else:
            m = resp.choices[0].message
            m = m.dict() if hasattr(m, "dict") else dict(m)
        out = {"role": "assistant", "content": m.get("content") or ""}
        if m.get("tool_calls"):
            out["tool_calls"] = m["tool_calls"]
        return out
    except Exception:
        return None


def _other_account_free(pool, client) -> bool:
    from app.account_pool import is_exhausted as _is_exhausted
    now = time.time()
    try:
        with pool._lock:
            return any(c is not client and cd <= now and not _is_exhausted(c, now)
                       for c, cd in zip(pool.clients, pool.cooldown_until))
    except Exception:
        return False


async def _handle_standard_request(
    request: Request,
    req_body: ChatCompletionRequest,
    response: Response,
) -> JSONResponse | StreamingResponse | ChatCompletionResponse:
    """
    处理 Standard 模式请求（完整上下文，支持 thinking 和搜索）

    类似 Lite 模式，但：
    1. 发送完整 messages 历史
    2. 保留 thinking 输出
    3. 保留搜索结果输出
    """
    from app.conversation import build_standard_transcript

    pool = request.app.state.account_pool

    # 验证模型
    if not is_supported_model(req_body.model):
        available_models = list_available_models()
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported model '{req_body.model}'. Available models: {', '.join(available_models)}",
        )

    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    max_retries = max(3, len(pool.clients))

    # Emulated OpenAI function calling (Notion has no client-defined tools)
    from app.tool_emulation import normalize_tools, resolve_tool_choice, build_tool_prompt, parse_tool_calls
    tools = normalize_tools(req_body.tools or [{"type": "function", "function": f} for f in (req_body.functions or [])])
    tool_mode, forced_tool = resolve_tool_choice(req_body.tool_choice, tools)
    parallel_tools = req_body.parallel_tool_calls is not False
    # Emulated tool calling is gated until reliable: ENABLE_TOOL_EMULATION=1
    tools_active = bool(tools) and tool_mode != "none" and os.getenv("ENABLE_TOOL_EMULATION", "0").strip().lower() in ("1", "true", "yes", "on")
    tool_prompt = build_tool_prompt(tools, tool_mode, forced_tool, parallel_tools) if tools_active else ""
    tool_reminder = ""
    if tools_active:
        tool_reminder = "(If one of the operations is needed, reply with <tool_call> action block(s) as described above.)"
        if tool_mode == "specific":
            tool_reminder = f"(Reply with a <tool_call> action block for `{forced_tool}`.)"
        elif tool_mode == "required":
            tool_reminder = "(Reply with at least one <tool_call> action block.)"
    _non_sys = [m for m in (req_body.messages or []) if m.role not in ("system", "developer")]
    last_role = _non_sys[-1].role if _non_sys else ""
    if last_role in ("tool", "function"):
        from app.tool_emulation import TOOL_RESULT_REMINDER
        tool_reminder = TOOL_RESULT_REMINDER

    from app import thread_reuse as _tr
    from app.request_log import annotate as _rl_annotate
    messages_all = [msg.dict() for msg in req_body.messages]
    reuse_on = _tr.enabled()
    tr_ident, tr_ident_src = _tr.identity(request.headers, req_body.user) if reuse_on else ("", [])
    tr_ns = _tr.namespace(req_body.model, tool_prompt, tr_ident) if reuse_on else ""
    reuse_plan = None
    reuse_status = "off"
    tr_diag = None
    if reuse_on:
        tr_norm = _tr.normalize_history(messages_all)

        def _tr_accept(k, e=None, _n=tr_norm):
            new_msgs = _n[k:]
            # side request (title / summary / compaction fork without tools): reuse the main
            # conversation thread (was veto side:no_tools -> cold start). Entry is restored
            # after take so the main agent loop can still continue.
            if e is not None and e.get("tp") and not tool_prompt:
                return None  # accept as side reuse
            if not new_msgs:
                return "shape:empty"
            # agent clients (e.g. Claude Code) append a system/developer note after the
            # latest user/tool turn; judge the shape by the last non-system message
            convo = [m for m in new_msgs if m.get("role") not in ("system", "developer")]
            if not convo:
                return "shape:only_system"
            if convo[-1].get("role") not in ("user", "tool", "function"):
                return "shape:last_" + str(convo[-1].get("role"))
            return None

        # sqlite-backed (admin.db, busy timeout): keep it off the event loop
        entry, k, reuse_status = await _rtp(_tr.STORE.take, tr_ns, tr_norm, accept=_tr_accept)
        if entry is not None:
            new_msgs = list(tr_norm[k:])
            # system lines (prefix part) the continued thread hasn't seen: a dynamic date /
            # memory / project context is sent as a small update; trailing system notes in
            # new_msgs are sent anyway
            delta = _tr.sys_delta(tr_norm[:k], entry.get("sh") or [])
            tools_changed = bool(tool_prompt) and _tr.tp_hash(tool_prompt) != (entry.get("tp") or "")
            side_no_tools = bool(entry.get("tp")) and not tool_prompt
            if delta:
                new_msgs.insert(0, {"role": "system", "content": "Updated context (changed since earlier in this conversation):\n" + delta})
            if side_no_tools:
                # Reuse main thread for this side request, but put the entry back so the
                # main agent turn can still take it (do not clobber tp with empty tools).
                await _rtp(_tr.STORE.restore, entry)
                reuse_status = "hit:side"
            if entry.get("pt") is not None:
                try:
                    _rl_annotate(prev_prompt_tokens=int(entry["pt"]))
                except (TypeError, ValueError):
                    pass
            reuse_plan = {"entry": entry, "new": new_msgs, "k": k, "norm": tr_norm,
                          "sys_delta_lines": delta.count("\n") + 1 if delta else 0, "tools_changed": tools_changed,
                          "side_no_tools": side_no_tools}
        elif reuse_status not in ("short",):
            try:
                tr_diag = await _rtp(_tr.STORE.diagnose, tr_ns, tr_norm)
            except Exception:
                tr_diag = None

    # effort telemetry + conversation cap memory (app.cap_memory)
    from app import cap_memory as _cm
    from app.request_log import current_log as _rl_cur
    conv_keys: list = []
    try:
        from app.model_registry import clamp_effort as _ce, normalize_effort as _ne, get_notion_model as _gnm
        from app.long_gen import looks_long_generation as _llg
        _nm = _gnm(req_body.model)
        eff_req = _ce(_nm, _ne(req_body.reasoning_effort)) or ""
        _ask = _last_user_ask(messages_all)
        _long = bool(_llg(_ask))
        _cmt = req_body.max_completion_tokens or req_body.max_tokens
        _wt = _cm.has_write_tool(tools)
        _ident = tr_ident or (_tr.identity(request.headers, req_body.user)[0])
        _ck = _cm.conv_key(_ident, messages_all)
        conv_keys = [k for k in (_ck, ("t:" + reuse_plan["entry"]["thread_id"]) if reuse_plan else "") if k]
        eff_mapped = _cm.map_max(eff_req)
        eff_final, mem_status = _cm.decide(conv_keys, eff_mapped)
        _wd = _cm.would_downgrade(eff_req, _long, _cmt, _wt)
        route = _cm.autoroute_mode()
        if mem_status is None and _wd and route == "on":
            eff_final, mem_status = "high", "autoroute"
        if mem_status is None and eff_mapped != eff_req:
            mem_status = "max_mapped"
        if eff_final != eff_req:
            req_body.reasoning_effort = eff_final
            logger.info("Effort lowered for this conversation", extra={"request_info": {
                "event": "cap_memory_applied", "from": eff_req, "to": eff_final, "why": mem_status,
                "conv_key": _ck, "state": _cm.state(conv_keys)}})
        _rl_annotate(effort_req=eff_req or None, effort_final=eff_final or None, client_max_tokens=_cmt,
                     has_tools=bool(tools), n_tools=len(tools or []), ask_chars=len(_ask or ""),
                     long_gen_match=_long, conv_key=_ck or None, cap_memory=mem_status,
                     would_downgrade=(_wd if route != "off" else None), _conv_keys=conv_keys)
    except Exception as _exc:
        logger.warning(f"effort telemetry failed: {_exc}")

    def _finish_transcript(t):
        t = _apply_reasoning_effort(t, req_body)
        if tools_active and os.getenv("TOOL_MODE_NOTION_WEB", "0") != "1":
            for b in t:
                if isinstance(b, dict) and b.get("type") == "config" and isinstance(b.get("value"), dict):
                    b["value"]["useWebSearch"] = False
                    b["value"]["internetAccess"] = False
        if tools_active and os.getenv("TOOL_MODE_DEFANG_URLS", "1") == "1":
            t = [_defang_urls(b) if isinstance(b, dict) and b.get("type") not in ("config", "context") else b for b in t]
        return t

    # whole-request budget (non-tool path; tool mode has its own in _run_tool_attempts):
    # bounds thread-recover polling and the retry loop so a request can't run ~15 min
    _budget = float(os.getenv("NOTION_REQUEST_BUDGET", "330") or 0)
    req_deadline = time.time() + _budget if _budget > 0 else None
    last_exc: NotionUpstreamError | None = None
    tried_accounts: set = set()
    for attempt in range(1, max_retries + 1):
        if attempt > 1 and req_deadline and time.time() > req_deadline - 60:
            logger.info("Standard mode: request budget exhausted, not retrying", extra={"request_info": {
                "event": "standard_budget_exhausted", "attempt": attempt, "budget_s": _budget}})
            if last_exc is not None:
                return _upstream_error_response(last_exc)
            break
        client = None
        tr_meta: dict = {"_deadline": req_deadline, "_conv_keys": conv_keys, "_rl": _rl_cur.get()}
        reuse_tid = None
        tr_started = True  # until the continued request is dispatched
        try:
            if reuse_plan is not None:
                client = pool.get_client_by_key(reuse_plan["entry"]["account"])
                if client is None and hasattr(pool, "key_cooldown_left"):
                    # the thread's account is only briefly cooling: wait for it rather than
                    # lose the conversation cache on a fresh thread elsewhere
                    left = pool.key_cooldown_left(reuse_plan["entry"]["account"])
                    if 0 < left <= float(os.getenv("THREAD_REUSE_WAIT_COOLDOWN", "8") or 0):
                        await asyncio.sleep(left + 0.05)
                        client = pool.get_client_by_key(reuse_plan["entry"]["account"])
                        if client is not None:
                            reuse_plan["waited"] = round(left, 1)
                if client is None:
                    reuse_status, reuse_plan = "account_unavailable", None
                else:
                    reuse_tid = reuse_plan["entry"]["thread_id"]
            if client is None:
                # may sleep for a cooling account: never on the event loop
                client = await _rtp(pool.get_client, True, tried_accounts)
            tried_accounts.add(getattr(client, "account_key", ""))

            # 构建 Standard transcript（完整上下文）
            # 从 client 提取账号信息
            account = {
                "user_id": client.user_id,
                "space_id": client.space_id,
            }
            messages = messages_all
            # Tool mode: Notion's built-in web is disabled (client brings its own tools; the
            # urlSafety confirm continuation 502s) and URLs get a zero-width space after the
            # scheme so Notion doesn't auto-"view" them (context_urls); see _finish_transcript.
            transcript = _finish_transcript(build_standard_transcript(
                messages, req_body.model, account,
                tool_prompt=tool_prompt, tool_reminder=tool_reminder,
            ))
            full_transcript = transcript
            tr_meta["_hedge_transcript"] = full_transcript  # fresh-thread transcript for an effort hedge
            tr_meta["_user_ask"] = _last_user_ask(messages)  # long-generation heuristic (app.long_gen)
            if reuse_tid:
                # continue the stored Notion thread: only the new messages, no instructions /
                # tool preamble (already in the thread), reminder only after tool results
                transcript = _finish_transcript(build_standard_transcript(
                    reuse_plan["new"], req_body.model, account,
                    tool_prompt=tool_prompt if reuse_plan.get("tools_changed") else "",
                    tool_reminder=tool_reminder if last_role in ("tool", "function") else "",
                    name_context=reuse_plan["norm"][:reuse_plan["k"]],
                    inject_default_system=False,
                ))
            logger.info("Thread reuse " + ("hit" if reuse_tid else "miss"), extra={"request_info": {
                "event": "thread_reuse_hit" if reuse_tid else "thread_reuse_miss",
                "reason": reuse_status, "thread_id": reuse_tid, "prefix_msgs": (reuse_plan or {}).get("k"),
                "new_msgs": len((reuse_plan or {}).get("new") or []), "account": client.account_key,
                "sys_delta_lines": (reuse_plan or {}).get("sys_delta_lines"), "tools_changed": (reuse_plan or {}).get("tools_changed"),
                "waited_s": (reuse_plan or {}).get("waited"), "identity_sources": tr_ident_src, **({"diag": tr_diag} if tr_diag and not reuse_tid else {})}})
            if reuse_on:
                _rl_annotate(thread_reuse=(reuse_status if reuse_tid else f"miss:{reuse_status}"))
            if reuse_tid:
                try:
                    _cm.alias(conv_keys, "t:" + reuse_tid)
                    if "t:" + reuse_tid not in conv_keys:
                        conv_keys.append("t:" + reuse_tid)
                        tr_meta["_conv_keys"] = conv_keys
                except Exception:
                    pass

            def _tr_store(reply, _meta=tr_meta, _msgs=messages):
                if not reuse_on or not reply:
                    return
                if (reuse_plan or {}).get("side_no_tools"):
                    return  # side fork: entry already restored; do not overwrite main tp
                pt = None
                try:
                    rec = _meta.get("_rl") if isinstance(_meta, dict) else None
                    col = (rec or {}).get("_collector")
                    raw = getattr(col, "raw", None) if col is not None else None
                    if raw:
                        pt = int(raw.get("inputTokens", 0) or 0) + int(raw.get("cachedTokensCreated", 0) or 0)
                except Exception:
                    pt = None
                _tr.STORE.put(tr_ns, _msgs, reply, _meta.get("thread_id"), _meta.get("account"),
                              tool_prompt=tool_prompt, prompt_tokens=pt)

            if tools_active:
                if reuse_tid:
                    tr_meta["_reuse_entry"] = reuse_plan["entry"]
                tool_gen = _run_tool_attempts(
                    client, transcript, include_usage=_include_usage_flag(req_body),
                    account=getattr(client, "account_label", "") or getattr(client, "account_key", ""),
                    tools=tools, parallel=parallel_tools, mode=tool_mode,
                    thread_id=reuse_tid, fallback_transcript=full_transcript,
                    meta=tr_meta, pool=pool,
                )
                if req_body.stream:
                    # Hold the first byte until the first keepalive tick (~5s) or the result: an
                    # upstream failure inside that window (fair-use 503, dead account, every
                    # failover exhausted) then gets a real HTTP error status. Once streaming has
                    # started it can only be an in-stream error event, which new-api's
                    # OpenAI->Anthropic conversion drops (client sees an empty end_turn).
                    held, early_exc = await _rtp(_prepull_tool_events, tool_gen)
                    if early_exc is not None:
                        raise early_exc  # handled below like the non-stream path (failover / JSON error)
                    return StreamingResponse(
                        _guarded_stream(request, _tr_capture_stream(_tool_mode_stream(_chain_events(held, tool_gen), response_id, req_body.model,
                                          request.headers.get("X-Client-Type", "").lower()), _tr_store), tr_meta),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
                    )
                # blocking (Notion stream + thread polling + retries): never on the event loop,
                # or one slow request freezes every other request and /health
                resp = await _run_tool_non_stream_cancellable(request, tool_gen, response_id, req_body.model)
                await _rtp(_tr_store, _tr_reply_from_response(resp))
                return resp

            tr_started = False
            stream_gen = _UsageCollector(client.stream_response(transcript, thread_id=reuse_tid, meta=tr_meta), include_usage=_include_usage_flag(req_body), account=getattr(client, "account_label", "") or getattr(client, "account_key", ""))
            first_item = await _rtp(next, stream_gen, None)
            tr_started = first_item is not None

            if first_item is None:
                raise NotionUpstreamError(
                    "Notion upstream returned empty content.", retriable=True
                )

            # 流式响应
            if req_body.stream:
                stream_headers = {
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                }
                client_type = request.headers.get("X-Client-Type", "").lower()
                if tools_active:
                    return StreamingResponse(
                        _guarded_stream(request, _create_tool_stream_generator(
                            response_id, req_body.model, first_item, stream_gen,
                            tools, parallel_tools, client_type=client_type,
                        ), tr_meta),
                        media_type="text/event-stream",
                        headers=stream_headers,
                    )
                return StreamingResponse(
                    _guarded_stream(request, _tr_capture_stream(_create_standard_stream_generator(
                        response_id,
                        req_body.model,
                        first_item,
                        stream_gen,
                        client_type=client_type,
                    ), _tr_store), tr_meta),
                    media_type="text/event-stream",
                    headers=stream_headers,
                )

            # 非流式响应
            content_parts: list[str] = []
            thinking_parts: list[str] = []
            search_results: list[dict] = []
            authoritative_final_content = ""
            authoritative_final_source_type = ""

            # the rest of the Notion stream is blocking network reads: collect it in a worker
            # thread (iterating here froze the event loop and /health for the whole answer)
            _items = await _run_cancellable(request, _collect_items_cancellable, first_item, stream_gen)
            if isinstance(_items, Response):
                return _items  # client disconnected
            for raw_item in _items:
                item = _normalize_stream_item(raw_item)
                item_type = item.get("type")

                if item_type == "final_content":
                    final_text = str(item.get("text", "") or "").strip()
                    if final_text:
                        authoritative_final_content = final_text
                        authoritative_final_source_type = str(
                            item.get("source_type", "") or ""
                        )
                    continue

                # Standard 模式：处理 thinking
                if item_type == "thinking":
                    thinking_text = item.get("text", "")
                    if thinking_text:
                        thinking_parts.append(thinking_text)
                    continue

                # Standard 模式：处理 search
                if item_type == "search":
                    search_data = item.get("data", {})
                    if search_data:
                        search_results.append(search_data)
                    continue

                if item_type != "content":
                    continue

                chunk_text = item.get("text", "")
                if chunk_text:
                    content_parts.append(chunk_text)

            full_text, _ = _select_best_final_reply(
                "".join(content_parts),
                authoritative_final_content,
                authoritative_final_source_type,
            )

            if not full_text.strip():
                raise NotionUpstreamError(
                    "Notion upstream returned empty content.", retriable=True
                )

            response_text = (
                full_text if full_text.strip() else "[assistant_no_visible_content]"
            )

            if tools_active:
                # Prefer the streamed text: Notion's record-map "final" can be an earlier
                # assistant turn (e.g. the previous tool call replayed from history).
                # Streamed deltas can also be truncated, so try both and keep the one that parses.
                streamed_text = "".join(content_parts)
                visible, calls = "", []
                for cand in ([streamed_text] if streamed_text.strip() else []) + [full_text]:
                    visible, calls = parse_tool_calls(cand, tools, parallel_tools)
                    if calls:
                        break
                if not calls and streamed_text.strip():
                    full_text = streamed_text
                if calls:
                    msg: Dict[str, Any] = {"role": "assistant", "content": visible or None, "tool_calls": calls}
                    if thinking_parts:
                        msg["reasoning_content"] = "".join(thinking_parts)
                    resp = JSONResponse({
                        "id": response_id, "object": "chat.completion", "created": int(time.time()),
                        "model": req_body.model,
                        "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls", "logprobs": None}],
                        "usage": _openai_usage(stream_gen),
                    })
                    await _rtp(_tr_store, _tr_reply_from_response(resp))
                    return resp

                response_text = full_text

            # 构建响应
            from app.meta_commentary import strip_meta_commentary
            response_message = ChatMessage(role="assistant", content=strip_meta_commentary(response_text))

            # 如果有 thinking，添加到扩展字段（前端会读取）
            if thinking_parts:
                response_message.thinking = "".join(thinking_parts)
                response_message.reasoning_content = response_message.thinking  # OpenAI-compat field (new-api -> Anthropic thinking)

            # 构建响应
            response_obj = ChatCompletionResponse(
                id=response_id,
                usage=_openai_usage(stream_gen),
                model=req_body.model,
                choices=[ChatMessageResponseChoice(message=response_message)],
            )

            # 如果有搜索结果，添加到扩展字段（前端会读取）
            if search_results:
                # 提取 queries 和 sources
                all_queries = []
                all_sources = []
                for result in search_results:
                    if isinstance(result, dict):
                        all_queries.extend(result.get("queries", []))
                        all_sources.extend(result.get("sources", []))

                if all_queries or all_sources:
                    # 添加到自定义字段
                    response_obj.search_metadata = {
                        "queries": all_queries,
                        "sources": all_sources,
                    }

            await _rtp(_tr_store, _tr_reply_from_response(response_obj))
            return response_obj

        except NotionUpstreamError as exc:
            last_exc = exc
            # 502 on a continued thread: short-retry same Notion thread once before fallback.
            if (reuse_plan is not None and exc.retriable and (exc.status_code or 0) == 502
                    and not tr_started and not tr_meta.get("_same_thread_502_retried")
                    and not tr_meta.get("_cancelled") and attempt < max_retries):
                tr_meta["_same_thread_502_retried"] = True
                from app.request_log import annotate as _rl_annotate
                _rl_annotate(thread_reuse="hit>retry502")
                logger.info("Standard mode: 502 on continued thread, short-retry same thread",
                            extra={"request_info": {"event": "standard_same_thread_502_retry",
                                                    "attempt": attempt,
                                                    "thread_id": (reuse_plan.get("entry") or {}).get("thread_id")}})
                continue  # reuse_plan kept; same account/thread
            if reuse_plan is not None:
                if exc.status_code in (429, 503) and not tr_started:
                    await _rtp(_tr.STORE.restore, reuse_plan["entry"])  # rejected: thread unchanged
                reuse_status, reuse_plan = "upstream_error", None
            if client is not None and exc.retriable:
                pool.mark_failed(client)
            logger.warning(
                "Standard mode: Notion upstream failed",
                extra={
                    "request_info": {
                        "event": "standard_notion_upstream_failed",
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "status_code": exc.status_code,
                        "retriable": exc.retriable,
                        "response_excerpt": exc.response_excerpt,
                    }
                },
            )
            # 429/503 were already backed off inside the client; retrying here only
            # deepens Notion's fair-use penalty
            from app.account_pool import is_exhausted as _is_exhausted
            if client is not None and _is_exhausted(client) and attempt < max_retries:
                continue  # out of credits: fail over to the next account now
            if (exc.status_code in (429, 503) and client is not None and attempt < max_retries
                    and _other_account_free(pool, client)):
                # this account is fair-use limited even after backoff: rest it and fail over
                pool.mark_failed(client, cooldown_seconds=int(os.getenv("NOTION_RATELIMIT_FAILOVER_COOLDOWN", "45") or 45))
                reuse_plan = None
                logger.info("Rate-limited: failing over to another account", extra={"request_info": {
                    "event": "ratelimit_failover", "from": getattr(client, "account_key", ""), "status": exc.status_code}})
                continue
            if attempt == max_retries or not exc.retriable or exc.status_code in (429, 503):
                return _upstream_error_response(exc)
        except RuntimeError as exc:
            logger.error(
                "Standard mode: No available client in account pool",
                extra={
                    "request_info": {
                        "event": "standard_account_pool_unavailable",
                        "detail": str(exc),
                    }
                },
            )
            return _build_error_response(
                503,
                code="POOL_COOLING",
                message=str(exc),
                error_type="account_pool_cooling",
                suggestion="所有账号暂时冷却中，请等待几秒后重试",
            )
        except HTTPException:
            raise
        except Exception:
            reuse_plan = None
            if client is not None:
                pool.mark_failed(client)
            logger.error(
                "Standard mode: Unhandled error",
                exc_info=True,
                extra={
                    "request_info": {
                        "event": "standard_unhandled_exception",
                        "attempt": attempt,
                    }
                },
            )
            if attempt == max_retries:
                return _build_error_response(
                    500,
                    code="INTERNAL_ERROR",
                    message="服务内部异常",
                    error_type="internal_error",
                    suggestion="请稍后重试，如持续出现请联系管理员",
                )

    return _build_error_response(
        503,
        code="RETRIES_EXHAUSTED",
        message="所有重试均已失败",
        error_type="upstream_error",
        suggestion="Notion 上游服务暂时不可用，请稍后重试",
    )


@router.post("/chat/completions", tags=["chat"])
async def create_chat_completion(
    request: Request,
    req_body: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    response: Response,
):
    """OpenAI-compatible entry: validation + output controls (stop / max_tokens /
    response_format) around the generation pipeline."""
    from app.output_controls import parse_controls, json_instruction, apply_json_body, wrap_stream
    from app.schemas import ChatMessage
    if not req_body.messages:
        return _build_error_response(400, code="invalid_value", error_type="invalid_request_error",
                                     message="'messages' must contain at least one message.")
    if not is_supported_model(req_body.model):
        return JSONResponse(status_code=404, content={"error": {
            "message": f"The model '{req_body.requested_model or req_body.model}' does not exist. Available models: "
                       + ", ".join(list_available_models()),
            "type": "invalid_request_error", "param": "model", "code": "model_not_found"}})
    if req_body.n not in (None, 1):
        return _build_error_response(400, code="unsupported_value", error_type="invalid_request_error",
                                     message="Only n=1 is supported.")
    ctl = parse_controls(req_body.model_dump())
    instr = json_instruction(ctl["format"])
    if instr:
        msgs = list(req_body.messages)
        last_sys = max([i for i, m in enumerate(msgs) if m.role in ("system", "developer")] or [-1])
        msgs.insert(last_sys + 1, ChatMessage(role="system", content=instr))
        req_body.messages = msgs
    result = await _create_chat_completion_impl(request, req_body, background_tasks, response)
    if not (ctl["stop"] or ctl["max_tokens"] or ctl["format"]):
        return result
    if isinstance(result, StreamingResponse):
        result.body_iterator = wrap_stream(result.body_iterator, ctl)
        return result
    if isinstance(result, JSONResponse):
        if result.status_code != 200:
            return result
        data = apply_json_body(json.loads(result.body), ctl)
        return JSONResponse(status_code=200, content=data, headers={k: v for k, v in result.headers.items()
                                                                     if k.lower() not in ("content-length", "content-type")})
    if hasattr(result, "model_dump"):
        return JSONResponse(status_code=200, content=apply_json_body(result.model_dump(), ctl))
    if isinstance(result, dict):
        return apply_json_body(result, ctl)
    return result


async def _create_chat_completion_impl(
    request: Request,
    req_body: ChatCompletionRequest,
    background_tasks: BackgroundTasks,
    response: Response,
):
    """
    创建聊天请求，严格兼容 OpenAI API。

    速率限制：
    - Lite 模式：30/分钟（适合单轮问答）
    - Standard 模式：25/分钟（完整上下文，支持 thinking 和搜索）
    - Heavy 模式：20/分钟（包含会话管理）
    """
    from app.config import is_standard_mode

    _request_log_annotate(model=req_body.model, stream=bool(req_body.stream))

    # Lite 模式：单轮问答，无记忆
    if is_lite_mode():
        return await _handle_lite_request(request, req_body, response)

    # Standard 模式：完整上下文，支持 thinking 和搜索
    if is_standard_mode():
        return await _handle_standard_request(request, req_body, response)

    # Heavy 模式：完整会话管理
    pool = request.app.state.account_pool
    manager = request.app.state.conversation_manager

    user_prompt, history_messages, raw_user_prompt = _prepare_messages(req_body)
    recall_query = (
        _extract_recall_query(raw_user_prompt)
        if _contains_recall_intent(raw_user_prompt)
        else None
    )

    if not is_supported_model(req_body.model):
        available_models = list_available_models()
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported model '{req_body.model}'. Available models: {', '.join(available_models)}",
        )

    conversation_id = (
        req_body.conversation_id.strip() if req_body.conversation_id else ""
    )
    restore_history = False
    if not conversation_id:
        conversation_id = manager.new_conversation()
        restore_history = True
    elif not manager.conversation_exists(conversation_id):
        logger.warning(
            "Conversation id not found, creating a fresh conversation",
            extra={
                "request_info": {
                    "event": "conversation_id_not_found",
                    "provided_conversation_id": conversation_id,
                }
            },
        )
        conversation_id = manager.new_conversation()
        restore_history = True

    # 关键修复：总是持久化客户端发送的历史消息，避免上下文丢失
    # 即使 conversation_id 已存在，也需要同步客户端发送的完整历史
    if history_messages:
        # 检查是否需要持久化（避免重复）
        with manager._get_conn() as conn:
            existing_count = manager._count_messages(conn, conversation_id)
            history_count = len(history_messages)

            # 只有当客户端发送的历史消息多于数据库中的消息时才持久化
            # 这样可以：
            # 1. 避免重复持久化相同的历史
            # 2. 确保客户端发送的完整历史被保存
            # 3. 解决"滑动窗口缺失 AI 回复"的 bug
            if history_count > existing_count:
                _persist_history_messages(manager, conversation_id, history_messages)
                restored_user_count = sum(
                    1 for role, *_ in history_messages if role == "user"
                )
                restored_assistant_count = sum(
                    1 for role, *_ in history_messages if role == "assistant"
                )

                logger.info(
                    "Restored history into conversation",
                    extra={
                        "request_info": {
                            "event": "conversation_history_restored",
                            "conversation_id": conversation_id,
                            "restore_history_flag": restore_history,
                            "existing_count": existing_count,
                            "history_count": history_count,
                            "restored_total": len(history_messages),
                            "restored_user_count": restored_user_count,
                            "restored_assistant_count": restored_assistant_count,
                        }
                    },
                )

    response_id = f"chatcmpl-{uuid.uuid4().hex}"
    max_retries = max(3, len(pool.clients))

    for attempt in range(1, max_retries + 1):
        client = None
        try:
            client = await _rtp(pool.get_client)
            transcript_payload = manager.get_transcript_payload(
                notion_client=client,
                conversation_id=conversation_id,
                new_prompt=user_prompt,
                model_name=req_body.model,
                recall_query=recall_query,
            )
            transcript = _apply_reasoning_effort(transcript_payload["transcript"], req_body)
            memory_degraded = bool(transcript_payload.get("memory_degraded"))
            memory_headers = {"X-Memory-Status": "degraded"} if memory_degraded else {}

            # 获取或创建 thread_id 以保持对话上下文
            thread_id = manager.get_conversation_thread_id(conversation_id)

            # 检测用户是否在对话中途切换了模型。Notion 的 thread 会把 config 中的 model
            # 粘在服务端 thread 对象上，复用同一 thread 即使 transcript 里写了新 model，
            # 上游实际仍按原始模型执行。必须丢掉旧 thread 让 Notion 按新 model 新建，
            # 对话上下文由我们自己的滑动窗口 + 压缩摘要重建，不会失忆。
            if thread_id:
                bound_model = manager.get_conversation_thread_model(conversation_id)
                # bound_model 为 None 表示升级前的遗留对话，没记录过模型绑定，
                # 无法判断 thread 当初绑的是什么模型。为避免继续踩老 bug，
                # 一律当作"可能不匹配"处理，丢弃老 thread 重新开。
                if not bound_model or bound_model != req_body.model:
                    logger.info(
                        "Recreating Notion thread: model changed or legacy binding",
                        extra={
                            "request_info": {
                                "event": "thread_model_switched",
                                "conversation_id": conversation_id,
                                "old_model": bound_model,
                                "new_model": req_body.model,
                                "reason": "model_mismatch" if bound_model else "legacy_no_binding",
                            }
                        },
                    )
                    manager.clear_conversation_thread(conversation_id)
                    thread_id = None

            stream_gen = _UsageCollector(client.stream_response(transcript, thread_id=thread_id), include_usage=_include_usage_flag(req_body), account=getattr(client, "account_label", "") or getattr(client, "account_key", ""))
            first_item = next(stream_gen, None)

            # 保存 thread_id（如果是新对话或刚切换模型）
            if not thread_id and hasattr(client, "current_thread_id"):
                manager.set_conversation_thread_id(
                    conversation_id,
                    client.current_thread_id,
                    model_name=req_body.model,
                )

            if first_item is None:
                raise NotionUpstreamError(
                    "Notion upstream returned empty content.", retriable=True
                )

            def openai_stream_generator() -> Generator[str, None, None]:
                streamed_content_accumulator = ""
                thinking_accumulator = ""
                authoritative_final_content = ""
                authoritative_final_source_type = ""
                assistant_started = False
                pending_search_md = ""
                client_type = request.headers.get("X-Client-Type", "").lower()
                recent_thinking_buffer: list[str] = []

                try:
                    for raw_item in _iter_stream_items(first_item, stream_gen):
                        item = _normalize_stream_item(raw_item)
                        item_type = item.get("type")

                        if item_type == "search":
                            search_data = item.get("data")
                            if isinstance(search_data, dict) and search_data:
                                pending_search_md += _format_search_results_md(
                                    search_data
                                )
                                if client_type == "web":
                                    yield _build_local_ui_chunk(
                                        response_id,
                                        req_body.model,
                                        "search_metadata",
                                        searches=search_data,
                                    )
                            continue

                        if item_type == "final_content":
                            final_text = str(item.get("text", "") or "").strip()
                            if final_text:
                                authoritative_final_content = final_text
                                authoritative_final_source_type = str(
                                    item.get("source_type", "") or ""
                                )
                            continue

                        if item_type == "thinking":
                            thinking_text = item.get("text", "")
                            if thinking_text:
                                thinking_accumulator += thinking_text
                                # Track recent thinking for overlap detection
                                recent_thinking_buffer.append(thinking_text)
                                # Keep buffer manageable (max 40 recent chunks)
                                if len(recent_thinking_buffer) > 40:
                                    recent_thinking_buffer.pop(0)

                                if not assistant_started:
                                    assistant_started = True
                                    yield _build_stream_chunk(
                                        response_id,
                                        req_body.model,
                                        role="assistant",
                                        thinking=thinking_text,
                                    )
                                else:
                                    yield _build_stream_chunk(
                                        response_id,
                                        req_body.model,
                                        thinking=thinking_text,
                                    )
                            continue

                        if item_type != "content":
                            continue

                        chunk_text = item.get("text", "")
                        if not chunk_text and not pending_search_md:
                            continue

                        # Check if content overlaps with recent thinking (prevents thinking leakage)
                        if recent_thinking_buffer and chunk_text.strip():
                            combined_recent_thinking = "".join(recent_thinking_buffer)
                            chunk_normalized = chunk_text.strip()

                            # Use normalized text without spaces for robust comparison
                            combined_norm = re.sub(r"\s+", "", combined_recent_thinking)
                            chunk_norm = re.sub(r"\s+", "", chunk_normalized)

                            # Check for significant overlap - skip duplicate content
                            # We only skip if a sufficiently long chunk matches to avoid swallowing short common characters.
                            if (
                                chunk_norm
                                and len(chunk_norm) > 3
                                and (
                                    chunk_norm in combined_norm
                                    or (
                                        len(chunk_norm) > 10
                                        and chunk_norm[:10] in combined_norm
                                    )
                                )
                            ):
                                # Skip this chunk as it's likely duplicated thinking content
                                logger.debug(
                                    "Skipping duplicate content chunk that overlaps with thinking",
                                    extra={
                                        "request_info": {
                                            "event": "content_overlap_with_thinking",
                                            "chunk_length": len(chunk_text),
                                            "overlap_detected": True,
                                        }
                                    },
                                )
                                continue

                        # 在第一个正文内容发出前，把积攒的搜索信息拼上去
                        if pending_search_md and client_type != "web":
                            chunk_text = pending_search_md + chunk_text

                        if pending_search_md:
                            pending_search_md = ""

                        streamed_content_accumulator += chunk_text
                        if not assistant_started:
                            assistant_started = True
                            yield _build_stream_chunk(
                                response_id,
                                req_body.model,
                                role="assistant",
                                content=chunk_text,
                            )
                        else:
                            yield _build_stream_chunk(
                                response_id, req_body.model, content=chunk_text
                            )
                except asyncio.CancelledError:
                    logger.info(
                        "Streaming response cancelled by downstream client",
                        extra={
                            "request_info": {
                                "event": "stream_cancelled_by_client",
                                "conversation_id": conversation_id,
                                "attempt": attempt,
                            }
                        },
                    )
                    raise
                except BaseException as exc:
                    if _is_client_disconnect_error(exc):
                        logger.info(
                            "Streaming connection closed by downstream client",
                            extra={
                                "request_info": {
                                    "event": "stream_client_disconnected",
                                    "conversation_id": conversation_id,
                                    "attempt": attempt,
                                }
                            },
                        )
                        return
                    if (
                        isinstance(exc, NotionUpstreamError)
                        and client is not None
                        and exc.retriable
                    ):
                        pool.mark_failed(client)
                    log_method = (
                        logger.warning
                        if isinstance(exc, NotionUpstreamError)
                        else logger.error
                    )
                    log_method(
                        "Streaming response interrupted",
                        exc_info=True,
                        extra={
                            "request_info": {
                                "event": "stream_interrupted",
                                "conversation_id": conversation_id,
                                "attempt": attempt,
                                "is_upstream_error": isinstance(
                                    exc, NotionUpstreamError
                                ),
                            }
                        },
                    )
                    error_hint = _stream_error_hint(exc)
                    streamed_content_accumulator += error_hint
                    if not assistant_started:
                        assistant_started = True
                        yield _build_stream_chunk(
                            response_id,
                            req_body.model,
                            role="assistant",
                            content=error_hint,
                        )
                    else:
                        yield _build_stream_chunk(
                            response_id, req_body.model, content=error_hint
                        )
                finally:
                    final_reply, reply_decision = _select_best_final_reply(
                        streamed_content_accumulator,
                        authoritative_final_content,
                        authoritative_final_source_type,
                    )

                    missing_suffix = _compute_missing_suffix(
                        streamed_content_accumulator, final_reply
                    )
                    if missing_suffix:
                        suffix_to_emit = missing_suffix
                        if (
                            pending_search_md
                            and client_type != "web"
                            and not streamed_content_accumulator
                        ):
                            suffix_to_emit = pending_search_md + suffix_to_emit
                            pending_search_md = ""
                        if not assistant_started:
                            assistant_started = True
                            yield _build_stream_chunk(
                                response_id,
                                req_body.model,
                                role="assistant",
                                content=suffix_to_emit,
                            )
                        else:
                            yield _build_stream_chunk(
                                response_id, req_body.model, content=suffix_to_emit
                            )
                        streamed_content_accumulator += suffix_to_emit
                    elif final_reply != streamed_content_accumulator:
                        # Diverged bodies cannot be safely "patched" in plain OpenAI deltas.
                        # Web client supports replace event to keep rendered body aligned with persisted final reply.
                        if client_type == "web":
                            yield _build_local_ui_chunk(
                                response_id,
                                req_body.model,
                                "content_replace",
                                content=final_reply,
                                source_type=authoritative_final_source_type,
                                decision=reply_decision,
                            )
                            streamed_content_accumulator = final_reply
                        elif not streamed_content_accumulator and final_reply:
                            # Non-web fallback when nothing has been shown yet.
                            emit_text = final_reply
                            if pending_search_md and client_type != "web":
                                emit_text = pending_search_md + emit_text
                                pending_search_md = ""
                            if not assistant_started:
                                assistant_started = True
                                yield _build_stream_chunk(
                                    response_id,
                                    req_body.model,
                                    role="assistant",
                                    content=emit_text,
                                )
                            else:
                                yield _build_stream_chunk(
                                    response_id, req_body.model, content=emit_text
                                )
                            streamed_content_accumulator = final_reply

                    thinking_replacement = _build_thinking_replacement(
                        streamed_content_accumulator,
                        thinking_accumulator,
                        final_reply,
                        authoritative_final_source_type,
                    )
                    if client_type == "web" and thinking_replacement is not None:
                        yield _build_local_ui_chunk(
                            response_id,
                            req_body.model,
                            "thinking_replace",
                            thinking=thinking_replacement["thinking"],
                            decision=thinking_replacement["decision"],
                            overlap_ratio=thinking_replacement["overlap_ratio"],
                            source_type=thinking_replacement["source_type"],
                            reply_decision=reply_decision,
                        )

                    persisted_thinking = (
                        str(thinking_replacement["thinking"])
                        if thinking_replacement is not None
                        else thinking_accumulator
                    )
                    if final_reply.strip() or persisted_thinking.strip():
                        try:
                            _persist_round(
                                manager,
                                background_tasks,
                                conversation_id,
                                user_prompt,
                                final_reply,
                                persisted_thinking,
                            )
                        except Exception:
                            logger.error(
                                "Failed to persist conversation round",
                                exc_info=True,
                                extra={
                                    "request_info": {
                                        "event": "conversation_persist_failed",
                                        "conversation_id": conversation_id,
                                    }
                                },
                            )
                    yield _build_stream_chunk(
                        response_id, req_body.model, finish_reason="stop"
                    )
                    _usage_chunk = _build_usage_chunk(response_id, req_body.model, stream_gen)
                    if _usage_chunk:
                        yield _usage_chunk
                    yield "data: [DONE]\n\n"

            if req_body.stream:
                stream_headers = {
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    "X-Conversation-Id": conversation_id,
                    **memory_headers,
                }
                return StreamingResponse(
                    openai_stream_generator(),
                    media_type="text/event-stream",
                    headers=stream_headers,
                )

            content_parts: list[str] = []
            thinking_parts: list[str] = []
            authoritative_final_content = ""
            authoritative_final_source_type = ""
            for raw_item in _iter_stream_items(first_item, stream_gen):
                item = _normalize_stream_item(raw_item)
                item_type = item.get("type")
                if item_type == "final_content":
                    final_text = str(item.get("text", "") or "").strip()
                    if final_text:
                        authoritative_final_content = final_text
                        authoritative_final_source_type = str(
                            item.get("source_type", "") or ""
                        )
                    continue
                if item_type == "thinking":
                    thinking_text = str(item.get("text", "") or "")
                    if thinking_text:
                        thinking_parts.append(thinking_text)
                    continue
                if item_type != "content":
                    continue
                chunk_text = item.get("text", "")
                if chunk_text:
                    content_parts.append(chunk_text)

            full_text, _ = _select_best_final_reply(
                "".join(content_parts),
                authoritative_final_content,
                authoritative_final_source_type,
            )
            merged_thinking = "".join(thinking_parts).strip()
            if not full_text.strip() and not merged_thinking:
                raise NotionUpstreamError(
                    "Notion upstream returned empty content.", retriable=True
                )

            _persist_round(
                manager,
                background_tasks,
                conversation_id,
                user_prompt,
                full_text,
                merged_thinking,
            )
            response.headers["X-Conversation-Id"] = conversation_id
            if memory_degraded:
                response.headers["X-Memory-Status"] = "degraded"

            response_text = (
                full_text if full_text.strip() else "[assistant_no_visible_content]"
            )
            return ChatCompletionResponse(
                id=response_id,
                usage=_openai_usage(stream_gen),
                model=req_body.model,
                choices=[
                    ChatMessageResponseChoice(
                        message=ChatMessage(role="assistant", content=response_text)
                    )
                ],
            )
        except NotionUpstreamError as exc:
            if client is not None and exc.retriable:
                pool.mark_failed(client)
            logger.warning(
                "Notion upstream failed",
                extra={
                    "request_info": {
                        "event": "notion_upstream_failed",
                        "attempt": attempt,
                        "max_retries": max_retries,
                        "conversation_id": conversation_id,
                        "status_code": exc.status_code,
                        "retriable": exc.retriable,
                        "response_excerpt": exc.response_excerpt,
                    }
                },
            )
            if attempt == max_retries or not exc.retriable:
                return _upstream_error_response(exc)
        except RuntimeError as exc:
            logger.error(
                "No available client in account pool",
                extra={
                    "request_info": {
                        "event": "account_pool_unavailable",
                        "detail": str(exc),
                    }
                },
            )
            return _build_error_response(
                503,
                code="POOL_COOLING",
                message=str(exc),
                error_type="account_pool_cooling",
                suggestion="所有账号暂时冷却中，请等待几秒后重试",
            )
        except HTTPException:
            raise
        except Exception:
            if client is not None:
                pool.mark_failed(client)
            logger.error(
                "Unhandled chat completion error",
                exc_info=True,
                extra={
                    "request_info": {
                        "event": "chat_completion_unhandled_exception",
                        "attempt": attempt,
                        "conversation_id": conversation_id,
                    }
                },
            )
            if attempt == max_retries:
                return _build_error_response(
                    500,
                    code="INTERNAL_ERROR",
                    message="服务内部异常",
                    error_type="internal_error",
                    suggestion="请稍后重试，如持续出现请联系管理员",
                )

    return _build_error_response(
        503,
        code="RETRIES_EXHAUSTED",
        message="所有重试均已失败",
        error_type="upstream_error",
        suggestion="Notion 上游服务暂时不可用，请稍后重试",
    )


@router.delete("/conversations/{conversation_id}", tags=["chat"])
async def delete_conversation(conversation_id: str, request: Request):
    manager = request.app.state.conversation_manager
    deleted = manager.delete_conversation(conversation_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"id": conversation_id, "deleted": True}

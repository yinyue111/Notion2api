import time
import os
import json
import re
from typing import Any, Generator

import requests

from app.logger import logger

# 清理 Notion 内部语言/格式标记的正则集合
# 1. 完整的 <lang ...>...</lang> 块，替换时只保留内部文本（_strip_lang_tags 的兜底）
_RE_LANG_FULL = re.compile(r"<lang\s+primary\b[^>]*>(.*?)</lang>", re.DOTALL)
# 2. 残留的 <lang ...> 孤立开标签（无对应内容或截断后留下的）
_RE_LANG_OPEN = re.compile(r"<lang\s+primary\b[^>]*>")
# 3. 残留的 </lang> 闭标签
_RE_LANG_CLOSE = re.compile(r"</lang>")
# 4. Notion 语言属性残片：如 primary="zh-CN" 或 primary="zh" 或 primary="en"
#    可能带或不带引号，出现在文本开头或任意位置
_RE_PRIMARY_ATTR = re.compile(r'\bprimary="[a-zA-Z\-]{1,15}"\s*')
# 5. 剩余的 > 或 "> 残片（由截断的开标签留下的属性尾巴，如 -CN"> ）
_RE_ATTR_TAIL = re.compile(r'^-?[a-zA-Z]{0,4}"\s*>\s*')
_RE_PRIMARY_START = re.compile(r"\bprimary\b", re.IGNORECASE)
_TOOL_IO_PATH_RE = re.compile(r"^/?s/\d+/(input|result|structuredContent)(/|$)")

SEARCH_PATH_KEYWORDS = ("search", "web", "query", "source", "citation", "tool")
SEARCH_TYPE_KEYWORDS = ("search", "web", "tool", "citation")
LINE_DEBUG_KEYWORDS = ("queries", "category", "sources", "citations", "questions")
SEARCH_VALUE_KEYS = (
    "queries",
    "query",
    "questions",
    "category",
    "sources",
    "citations",
    "results",
    "url",
    "urls",
    "href",
    "search",
    "web",
    "tool",
    "toolname",
    "tooltype",
    "internal",
)

# ---- 段落分类：根据 Notion o:"a" patch 的 v.type 判断 ----
# 思考类 type 关键词
_THINKING_TYPES = ("agent-inference", "thinking", "reasoning", "inference")
# 工具/搜索类 type 关键词
_TOOL_TYPES = ("agent-tool-result", "tool_use", "tool", "search", "web", "citation")

# 段落角色常量
SEG_THINKING = "thinking"
SEG_TOOL = "tool"
SEG_CONTENT = "content"
SEG_META = "meta"

FINAL_STEP_PRIORITIES: dict[str, int] = {
    "markdown-chat": 400,
    "text": 350,
    "agent-inference": 300,
    "title": 50,
}


def _is_notion_lang_open(rest: str) -> bool:
    """`rest` = text after "<lang". True for Notion's `<lang primary=...` opener or a
    chunk-final prefix of it ("<lang", "<lang pri"); False for <language>, <lang>, <lang x=..>."""
    if re.match(r"\s+primary\b", rest):
        return True
    if ">" in rest or "\n" in rest:
        return False
    if rest == "":
        return True
    if not rest[0].isspace():
        return False
    r = rest.lstrip()
    return r == "" or "primary".startswith(r)


def _strip_lang_tags(text: str, in_tag: list[bool]) -> str:
    """Strip Notion internal <lang ...> tags, including cross-chunk broken tags."""
    result = []
    i = 0
    while i < len(text):
        if in_tag[0]:
            end = text.find(">", i)
            if end == -1:
                break
            in_tag[0] = False
            i = end + 1
            continue

        lang_start = text.find("<lang", i)
        # only Notion's <lang ...> / <lang>, never <language>, <langs> etc.
        # only Notion's <lang primary=...> (or a chunk-final prefix of it), never <language>, <lang>
        while lang_start != -1 and not _is_notion_lang_open(text[lang_start + 5:]):
            lang_start = text.find("<lang", lang_start + 1)
        close_start = -1  # </lang> is left to app.lang_sanitizer (code-fence aware)
        candidates = [(pos, typ) for pos, typ in [(lang_start, "open"), (close_start, "close")] if pos != -1]
        if not candidates:
            result.append(text[i:])
            break

        next_pos, typ = min(candidates, key=lambda x: x[0])
        result.append(text[i:next_pos])

        if typ == "close":
            i = next_pos + len("</lang>")
            continue

        end = text.find(">", next_pos)
        if end == -1:
            if re.match(r"\s+primary\b", text[next_pos + 5:]):
                in_tag[0] = True  # certainly Notion's opener: swallow up to the next ">"
            else:
                # bare "<lang" / "<lang pri" at chunk end: could be `<lang>` or <language> in prose;
                # app.lang_sanitizer holds it statefully and decides once it resolves
                result.append(text[next_pos:])
            break
        i = end + 1

    return "".join(result)


def _clean_notion_markup(text: str) -> str:
    """
    二次清理：移除 _strip_lang_tags 可能遗漏的 Notion 内部标记残片。

    处理以下场景：
    1. 完整 <lang ...>内容</lang>（_strip_lang_tags 兜底，保留内容只剥标签）
    2. 孤立的 </lang> 闭标签
    3. 孤立的 <lang ...> 开标签（内容已被 _strip_lang_tags 保留，只剩空壳）
    4. primary="zh-CN" 属性残片（跨块截断后留下的属性文本）
    5. 属性尾部残片，如 -CN"> 或 en"> 出现在文本开头
    """
    # 完整 <lang ...>内容</lang>：保留内容，剥掉标签
    text = _RE_LANG_FULL.sub(r"\1", text)
    # 移除孤立的 </lang>
    # </lang> is removed (outside code only) by app.lang_sanitizer
    # 移除孤立的 <lang ...> 开标签
    text = _RE_LANG_OPEN.sub("", text)
    # 移除 primary="zh-CN" 之类的属性残片
    text = _RE_PRIMARY_ATTR.sub("", text)
    # NOTE: _RE_ATTR_TAIL used to be applied here to every chunk, which deleted
    # legitimate leading `">` from streamed HTML/JSON (e.g. `<html lang="zh` + `">\n`).
    # Cross-chunk primary/lang fragments are already handled statefully.
    return text



def _strip_primary_attr_fragments(text: str, in_primary_attr: list[bool]) -> str:
    """Strip fragmented `primary=...` attribute pieces leaked by stream chunks."""
    out: list[str] = []
    i = 0

    while i < len(text):
        if in_primary_attr[0]:
            ch = text[i]
            if ch in ">\r\n":
                in_primary_attr[0] = False
                i += 1
                continue
            if ch.isalpha() or ch in '-_="\'/: ':
                i += 1
                continue
            in_primary_attr[0] = False
            continue

        m = _RE_PRIMARY_START.search(text, i)
        if not m:
            out.append(text[i:])
            break

        start = m.start()
        out.append(text[i:start])

        j = m.end()
        while j < len(text) and text[j].isspace():
            j += 1

        if j < len(text) and text[j] not in ("=", "\"", "'"):
            out.append(text[start:m.end()])
            i = m.end()
            continue

        if j < len(text) and text[j] == "=":
            j += 1
            while j < len(text) and text[j].isspace():
                j += 1

        if j < len(text) and text[j] in ("\"", "'"):
            quote = text[j]
            j += 1
            while j < len(text) and (text[j].isalpha() or text[j] in "-_"):
                j += 1
            if j < len(text) and text[j] == quote:
                j += 1
        else:
            while j < len(text) and (text[j].isalpha() or text[j] in "-_"):
                j += 1

        while j < len(text) and text[j] in " />":
            j += 1

        if j >= len(text):
            in_primary_attr[0] = True
            break

        i = j

    return "".join(out)

def _normalize_path(patch: dict[str, Any]) -> str:
    for key in ("path", "p", "pointer", "at"):
        if key not in patch:
            continue
        raw = patch.get(key)
        if isinstance(raw, (list, tuple)):
            return "/".join(str(part) for part in raw)
        return str(raw)
    return ""


def _extract_segment_index(path: str) -> int | None:
    parts = [part for part in path.split("/") if part]
    if len(parts) < 2 or parts[0] != "s":
        return None
    try:
        return int(parts[1])
    except Exception:
        return None


def _extract_value_index(path: str) -> int | None:
    """从 /s/N/value/M/... 形式的 path 中提取 value block 序号 M。"""
    parts = [p for p in path.split("/") if p]
    for i, part in enumerate(parts):
        if part == "value" and i + 1 < len(parts):
            try:
                return int(parts[i + 1])
            except ValueError:
                return None
    return None


def _extract_value_add_index(path: str) -> int | None:
    """
    从 `o:"a"` 的 `/s/N/value/<idx|->` 路径中提取新 value block 序号。
    仅匹配 value block 本身，不匹配 `/content` 等子路径。
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) != 4:
        return None
    if parts[0] != "s" or parts[2] != "value":
        return None
    idx_raw = parts[3]
    if idx_raw == "-":
        return -1
    try:
        return int(idx_raw)
    except ValueError:
        return None


def _truncate_json(value: Any, max_len: int = 2000) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False)
    except Exception:
        raw = str(value)
    if len(raw) <= max_len:
        return raw
    return raw[:max_len] + "...(truncated)"


def _contains_search_keys(value: Any) -> bool:
    if isinstance(value, dict):
        for key, val in value.items():
            key_lower = str(key).lower()
            if any(token in key_lower for token in SEARCH_VALUE_KEYS):
                return True
            if _contains_search_keys(val):
                return True
        return False

    if isinstance(value, list):
        return any(_contains_search_keys(item) for item in value)

    return False


def _append_query(out: dict[str, Any], query: str) -> None:
    q = query.strip()
    if q:
        out.setdefault("queries", []).append(q)


def _append_source(out: dict[str, Any], source: dict[str, Any]) -> None:
    title = str(source.get("title", "") or "").strip()
    url = str(source.get("url", "") or "").strip()
    snippet = str(source.get("snippet", "") or "").strip()
    if not title and not url:
        return

    entry: dict[str, str] = {}
    if title:
        entry["title"] = title
    if url:
        entry["url"] = url
    if snippet:
        entry["snippet"] = snippet
    out.setdefault("sources", []).append(entry)


def _collect_search_metadata(value: Any, out: dict[str, Any]) -> None:
    if isinstance(value, dict):
        lowered = {str(k).lower(): v for k, v in value.items()}

        if isinstance(lowered.get("queries"), list):
            for item in lowered["queries"]:
                if isinstance(item, str):
                    _append_query(out, item)

        if isinstance(lowered.get("questions"), list):
            for item in lowered["questions"]:
                if isinstance(item, str):
                    _append_query(out, item)

        for single_query_key in ("query", "search_query", "searchquery"):
            query_val = lowered.get(single_query_key)
            if isinstance(query_val, str):
                _append_query(out, query_val)

        category = lowered.get("category")
        if isinstance(category, str) and category.strip():
            out.setdefault("categories", []).append(category.strip())

        for source_key in ("sources", "citations", "results"):
            source_items = lowered.get(source_key)
            if not isinstance(source_items, list):
                continue
            for item in source_items:
                if isinstance(item, dict):
                    _append_source(
                        out,
                        {
                            "title": item.get("title") or item.get("name") or item.get("sourceTitle") or "",
                            "url": item.get("url") or item.get("href") or item.get("link") or item.get("sourceUrl") or "",
                            "snippet": item.get("snippet") or item.get("summary") or item.get("description") or "",
                        },
                    )
                elif isinstance(item, str):
                    _append_source(out, {"title": item, "url": item})

        if isinstance(lowered.get("urls"), list):
            for url_item in lowered["urls"]:
                if isinstance(url_item, str) and url_item.strip():
                    _append_source(out, {"title": url_item.strip(), "url": url_item.strip()})

        url_val = lowered.get("url") or lowered.get("href") or lowered.get("link")
        if isinstance(url_val, str):
            _append_source(
                out,
                {
                    "title": lowered.get("title") or lowered.get("name") or url_val,
                    "url": url_val,
                    "snippet": lowered.get("snippet") or lowered.get("summary") or "",
                },
            )

        for nested in value.values():
            _collect_search_metadata(nested, out)
        return

    if isinstance(value, list):
        for item in value:
            _collect_search_metadata(item, out)


def _dedupe_search_data(data: dict[str, Any]) -> dict[str, Any]:
    queries = data.get("queries", [])
    sources = data.get("sources", [])
    categories = data.get("categories", [])

    deduped_queries: list[str] = []
    for query in queries:
        if query not in deduped_queries:
            deduped_queries.append(query)

    deduped_sources: list[dict[str, str]] = []
    seen_sources: set[tuple[str, str]] = set()
    for source in sources:
        title = str(source.get("title", "") or "")
        url = str(source.get("url", "") or "")
        key = (title, url)
        if key in seen_sources:
            continue
        seen_sources.add(key)
        deduped_sources.append(source)

    deduped_categories: list[str] = []
    for category in categories:
        if category not in deduped_categories:
            deduped_categories.append(category)

    out: dict[str, Any] = {}
    if deduped_queries:
        out["queries"] = deduped_queries
    if deduped_sources:
        out["sources"] = deduped_sources
    if deduped_categories:
        out["categories"] = deduped_categories
    return out


def _looks_like_search_patch(patch: dict[str, Any]) -> bool:
    patch_type = str(patch.get("type", "") or "").lower()
    patch_v = patch.get("v")
    nested_type = ""
    if isinstance(patch_v, dict):
        nested_type = str(patch_v.get("type", "") or "").lower()

    effective_type = patch_type or nested_type
    if effective_type and effective_type != "text" and any(token in effective_type for token in SEARCH_TYPE_KEYWORDS):
        return True

    path = _normalize_path(patch).lower()
    if any(token in path for token in SEARCH_PATH_KEYWORDS):
        return True

    if _contains_search_keys(patch.get("v")):
        return True

    return False


def _extract_search_data_from_patch(patch: dict[str, Any]) -> dict[str, Any]:
    extracted: dict[str, Any] = {"queries": [], "sources": [], "categories": []}
    _collect_search_metadata(patch, extracted)
    return _dedupe_search_data(extracted)


def _extract_text_from_patch(patch: dict[str, Any]) -> str:
    content = ""
    patch_op = patch.get("o")

    if patch_op == "a":
        patch_v = patch.get("v", {})
        if isinstance(patch_v, dict) and "value" in patch_v:
            val = patch_v["value"]
            if isinstance(val, list):
                text_parts = []
                for item in val:
                    if isinstance(item, dict) and isinstance(item.get("content"), str):
                        text_parts.append(str(item.get("content", "")))
                content = "".join(text_parts)
        elif isinstance(patch_v, dict) and "content" in patch_v:
            # 子 value block 创建：o:"a" /s/N/value/- → {type:"text", content:"..."}
            raw = patch_v.get("content")
            if isinstance(raw, str):
                content = raw

    elif patch_op == "x" and "v" in patch:
        content = patch["v"] if isinstance(patch["v"], str) else ""
    
    elif patch_op == "p" and "v" in patch:
        # 路径替换有时也携带文本内容
        path = _normalize_path(patch)
        if "/content" in path or "/text" in path:
            content = patch["v"] if isinstance(patch["v"], str) else ""

    return content


def _looks_like_search_json_fragment(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped.startswith("{"):
        return False

    if '"default"' in stripped and ('"questions"' in stripped or '"queries"' in stripped):
        return True

    return (
        '"queries"' in stripped
        or '"web"' in stripped
        or '"sources"' in stripped
        or '"citations"' in stripped
        or '"internal"' in stripped
        or '"questions"' in stripped
        or '"urls"' in stripped
    )


def _extract_search_data_from_json_text(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except Exception:
        return {}

    extracted: dict[str, Any] = {"queries": [], "sources": [], "categories": []}
    _collect_search_metadata(parsed, extracted)
    return _dedupe_search_data(extracted)


def _clean_extracted_text(text: str) -> str:
    if not text:
        return ""
    in_lang_tag = [False]
    in_primary_attr = [False]
    cleaned = _strip_lang_tags(text, in_lang_tag)
    cleaned = _strip_primary_attr_fragments(cleaned, in_primary_attr)
    cleaned = _clean_notion_markup(cleaned)
    return cleaned.strip()


def _extract_text_from_value_items(value_items: Any) -> str:
    if not isinstance(value_items, list):
        return ""

    parts: list[str] = []
    for item in value_items:
        if not isinstance(item, dict):
            continue
        if str(item.get("type", "") or "").lower() != "text":
            continue
        content = item.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
    return "".join(parts)


def _extract_markdown_chat_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type", "") or "").lower()
            if item_type == "text" and isinstance(item.get("content"), str):
                parts.append(str(item.get("content", "")))
                continue
            nested_value = item.get("value")
            if nested_value is not None:
                nested_text = _extract_markdown_chat_text(nested_value)
                if nested_text:
                    parts.append(nested_text)
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("value", "content", "text"):
            if key in value:
                nested_text = _extract_markdown_chat_text(value.get(key))
                if nested_text:
                    return nested_text
    return ""


def _extract_markdown_chat_patch_text(patch: dict[str, Any]) -> tuple[str, str] | None:
    patch_op = str(patch.get("o", "") or "")
    patch_v = patch.get("v")

    if (
        patch_op == "a"
        and isinstance(patch_v, dict)
        and str(patch_v.get("type", "") or "").lower() == "markdown-chat"
    ):
        cleaned = _clean_extracted_text(_extract_markdown_chat_text(patch_v.get("value")))
        if cleaned:
            return ("final_content", cleaned)

    return None


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _extract_final_content_from_record_map(data: dict[str, Any]) -> dict[str, Any] | None:
    record_map = data.get("recordMap")
    if not isinstance(record_map, dict):
        return None

    thread_messages = record_map.get("thread_message")
    if not isinstance(thread_messages, dict):
        return None

    candidates: list[dict[str, Any]] = []

    for msg_id, msg_data in thread_messages.items():
        if not isinstance(msg_data, dict):
            continue

        outer_value = msg_data.get("value")
        if not isinstance(outer_value, dict):
            continue
        inner_value = outer_value.get("value")
        if not isinstance(inner_value, dict):
            continue
        step = inner_value.get("step")
        if not isinstance(step, dict):
            continue

        step_type = str(step.get("type", "") or "").lower()
        content = ""

        if step_type == "markdown-chat":
            content = _extract_markdown_chat_text(step.get("value"))
        elif step_type == "agent-inference":
            content = _extract_text_from_value_items(step.get("value"))
        elif step_type in {"text", "title"}:
            raw_value = step.get("value")
            if isinstance(raw_value, str):
                content = raw_value

        cleaned = _clean_extracted_text(content)
        if cleaned:
            candidates.append(
                {
                    "message_id": str(msg_id),
                    "step_type": step_type or "unknown",
                    "priority": FINAL_STEP_PRIORITIES.get(step_type, 100),
                    "created_at": _safe_int(outer_value.get("created_time")),
                    "edited_at": _safe_int(outer_value.get("last_edited_time")),
                    "length": len(cleaned),
                    "text": cleaned,
                }
            )

    # 智能过滤：如果同时存在高优先级的 text/markdown-chat，则忽略 agent-inference
    # 这可以避免 Opus/GPT 模型将所有内容都放在 agent-inference 中导致的重复显示问题
    high_priority_types = {"text", "markdown-chat"}
    has_high_priority = any(c["step_type"] in high_priority_types for c in candidates)

    if has_high_priority:
        original_count = len(candidates)
        candidates = [c for c in candidates if c["step_type"] in high_priority_types]
        logger.debug(
            "Final content filtered",
            extra={
                "request_info": {
                    "event": "final_content_filtered",
                    "original_count": original_count,
                    "filtered_count": len(candidates),
                    "removed_types": [c["step_type"] for c in candidates if c["step_type"] not in high_priority_types],
                }
            },
        )

    if not candidates:
        return None

    best = max(
        candidates,
        key=lambda candidate: (
            int(candidate.get("priority", 0)),
            int(candidate.get("edited_at", 0)),
            int(candidate.get("created_at", 0)),
            int(candidate.get("length", 0)),
        ),
    )

    logger.debug(
        "Final content selected",
        extra={
            "request_info": {
                "event": "final_content_selected",
                "step_type": best.get("step_type", "unknown"),
                "priority": best.get("priority", 0),
                "length": len(best.get("text", "")),
                "message_id": best.get("message_id", ""),
            }
        },
    )

    return {
        "text": str(best.get("text", "") or ""),
        "source_type": str(best.get("step_type", "") or "unknown"),
        "source_message_id": str(best.get("message_id", "") or ""),
        "source_length": int(best.get("length", 0)),
    }


def _classify_segment_type(effective_type: str) -> str:
    """
    根据 o:"a" patch 的 type 字段判断新段落的角色。
    这是整个分类逻辑的唯一入口——只依赖 Notion 自己标注的 type。
    """
    if not effective_type:
        return SEG_CONTENT
    if effective_type == "text":
        return SEG_CONTENT
    if effective_type == "title":
        return SEG_META
    if any(kw in effective_type for kw in _THINKING_TYPES):
        return SEG_THINKING
    if any(kw in effective_type for kw in _TOOL_TYPES):
        return SEG_TOOL
    # 未知类型默认归正文，保证不丢内容
    return SEG_CONTENT


def _bind_pending_segment(
    notion_idx: int,
    pending: list[dict],
    segment_types: dict[int, str],
    value_types: dict[tuple[int, int], str],
    next_val_id: dict[int, int],
    patch_path: str,
) -> None:
    """
    从 pending 列表中找到最匹配的段落绑定到 Notion 的真实 index。

    策略：优先绑定 agent-inference（thinking）段落，因为它是唯一需要
    精确分类的段落。其他段落（tool、meta、content）即使绑错也不影响输出，
    因为它们的文本要么被跳过（meta/tool）要么本来就是 content。
    """
    if not pending:
        return

    # 从 patch_path 提取 value index，用于匹配
    val_idx = _extract_value_index(patch_path)

    # 策略：如果 path 引用了 value/0/content，优先找有 thinking value[0] 的段落
    best_idx = 0  # 默认取第一个
    if val_idx is not None:
        for i, seg in enumerate(pending):
            vt = seg.get("value_types", {})
            if vt.get(val_idx) == SEG_THINKING:
                best_idx = i
                break

    chosen = pending.pop(best_idx)
    segment_types[notion_idx] = chosen["seg_class"]
    for vi, vc in chosen["value_types"].items():
        value_types[(notion_idx, vi)] = vc
    next_val_id[notion_idx] = chosen["next_val_id"]

    logger.debug(
        "Pending segment bound to Notion index",
        extra={
            "request_info": {
                "event": "segment_bound",
                "notion_idx": notion_idx,
                "seg_class": chosen["seg_class"],
                "value_types": {str(k): v for k, v in chosen["value_types"].items()},
                "remaining_pending": len(pending),
                "patch_path": patch_path,
            }
        },
    )


# ---- 上游真实 usage 透传（不做估算） ----
USAGE_FIELDS = ("inputTokens", "outputTokens", "cachedTokensRead", "cachedTokensCreated")
_USAGE_PATH_RE = re.compile(r"^/?s/(\d+)/(inputTokens|outputTokens|cachedTokensRead|cachedTokensCreated)$")


class UsageTracker:
    """按 step 收集 Notion 上游返回的 token 用量。

    来源优先级：record-map 中本次响应出现过的 step（按 step id 去重）> patch 流中的
    /s/N/<field>。多步（多次 agent-inference）求和。只汇报上游给出的字段，缺失即缺失。
    """

    def __init__(self) -> None:
        self.patch_steps: dict[str, dict[str, int]] = {}
        self.record_steps: dict[str, dict[str, int]] = {}
        self.seen_step_ids: set[str] = set()

    @staticmethod
    def _pick(obj: dict[str, Any]) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in USAGE_FIELDS:
            v = obj.get(f)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                out[f] = int(v)
        return out

    def note_step_id(self, step_id: Any) -> None:
        if isinstance(step_id, str) and step_id:
            self.seen_step_ids.add(step_id)

    def observe_new_step(self, step: dict[str, Any]) -> None:
        self.note_step_id(step.get("id"))
        picked = self._pick(step)
        if picked:
            self.patch_steps.setdefault("id:" + str(step.get("id")), {}).update(picked)

    def observe_patch(self, path: str, value: Any) -> bool:
        m = _USAGE_PATH_RE.match(path or "")
        if not m:
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.patch_steps.setdefault("idx:" + m.group(1), {})[m.group(2)] = int(value)
        return True

    def observe_record_map(self, data: dict[str, Any]) -> None:
        record_map = data.get("recordMap")
        if not isinstance(record_map, dict):
            return
        msgs = record_map.get("thread_message")
        if not isinstance(msgs, dict):
            return
        for msg_id, msg in msgs.items():
            try:
                step = msg["value"]["value"]["step"]
            except (KeyError, TypeError):
                continue
            if not isinstance(step, dict):
                continue
            sid = str(step.get("id") or msg_id)
            # 线程复用时 record-map 可能含历史轮次，只统计本次响应中出现过的 step
            if self.seen_step_ids and sid not in self.seen_step_ids and str(msg_id) not in self.seen_step_ids:
                continue
            picked = self._pick(step)
            if picked:
                self.record_steps[sid] = picked

    def totals(self) -> dict[str, int] | None:
        steps = self.record_steps or self.patch_steps
        if not steps:
            return None
        out: dict[str, int] = {}
        for vals in steps.values():
            for k, v in vals.items():
                out[k] = out.get(k, 0) + v
        return out


def _extract_notion_error(data: dict[str, Any]) -> dict[str, Any] | None:
    """识别 Notion NDJSON 中的错误事件。"""
    data_type = str(data.get("type", "") or "").lower()
    if data_type not in {"error", "inference-error", "agent-error"}:
        return None
    err = data.get("error") if isinstance(data.get("error"), (dict, str)) else data
    message = ""
    code: Any = None
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("msg") or err.get("name") or "")
        code = err.get("code") or err.get("status") or err.get("statusCode")
    elif isinstance(err, str):
        message = err
    if not message:
        message = str(data.get("message") or "")
    return {"type": "upstream_error", "message": message or "Notion upstream error", "code": code,
            "raw": _truncate_json(data, 500)}


def _step_error(step: Any) -> dict[str, Any] | None:
    """Notion reports fair-use rate limits / outages as a transcript step of type
    "error" (inside patch-start or an /s/- append); without this they surfaced as a
    misleading "empty stream"."""
    if not isinstance(step, dict) or str(step.get("type", "")).lower() != "error":
        return None
    msg = str(step.get("message") or "Notion upstream error")
    blob = f"{msg} {step.get('stack', '')} {step.get('subType', '')}".lower()
    if "ratelimit" in blob or "rate limit" in blob or "rate-limit" in blob:
        code = 429
        msg = "Notion AI fair-use rate limit reached for this account; please retry later."
    elif "temporarily-unavailable" in blob or "unavailable" in blob:
        code = 503
    else:
        code = 502
    return {"type": "upstream_error", "message": msg, "code": code,
            "raw": _truncate_json({k: step.get(k) for k in ("type", "message", "subType", "isRetryable")}, 300)}


def _pending_url_confirmation(step: Any) -> dict[str, Any] | None:
    """Web-page fetches by Notion's `view` tool stop at state "confirmation:requested"
    (urlSafety) and wait for the user to click "continue" in the web app. We auto-approve
    those (and only those) by re-running with confirmToolStepIds."""
    if not isinstance(step, dict) or step.get("type") != "agent-tool-result":
        return None
    if step.get("state") != "confirmation:requested" or not step.get("id"):
        return None
    confs = step.get("pendingConfirmations") or []
    if not confs or any(not isinstance(c, dict) or c.get("type") not in ("urlSafety", "internetAccess") for c in confs):
        return None
    return {"type": "pending_confirmation", "step_id": step["id"], "tool": step.get("toolName")}


def parse_stream(response: requests.Response) -> Generator[dict[str, Any], None, None]:
    """
    解析 Notion NDJSON 流，输出三种结构化事件：
      - {"type": "content",  "text": "..."}   正文
      - {"type": "search",   "data": {...}}    搜索元数据
      - {"type": "thinking", "text": "..."}    思考过程

    核心机制——段落注册表（Segment Registry）：
      Notion 流中每个 o:"a" + path="/s/-" 的 patch 创建一个新的顶层段落，
      此时 v.type 明确标注了类型（agent-inference / agent-tool-result / text 等）。
      我们在此刻分配递增序号并记录类型。
      后续 o:"x" + path="/s/N/..." 只是往已有段落追加文本，查表即可知归属。
      不需要关键词猜测、不需要状态机。
    """
    in_lang_tag: list[bool] = [False]
    in_primary_attr: list[bool] = [False]
    search_json_buffer = ""
    search_json_depth = 0

    # ---- 段落注册表 ----
    # Notion 的 /s/N 中的 N 是全局 index（包括 config/context/user 等隐含段落），
    # 但 o:"a" /s/- 不告诉我们真实 index。
    # 策略：o:"a" /s/- 时记录到 pending dict。
    # 当 o:"x" /s/N/... 或 o:"a" /s/N/... 首次引用未知 N 时，
    # 从 pending 中找最匹配的段落绑定（优先匹配 agent-inference/thinking 类型）。
    segment_types: dict[int, str] = {}            # notion_index → SEG_THINKING / SEG_TOOL / SEG_CONTENT
    value_types: dict[tuple[int, int], str] = {}  # (notion_index, val_index) → 类型
    next_val_id: dict[int, int] = {}              # notion_index → 下一个 value block 序号

    # pending：o:"a" /s/- 注册但尚未绑定到 Notion 真实 index 的段落
    _pending_segments: list[dict] = []  # [{seg_class, value_types_local, next_val_id_local}]
    usage_tracker = UsageTracker()
    reported_models: list[str] = []  # Notion-reported model per agent-inference step
    step_kinds: list[str] = []  # non-inference steps (Notion built-in tools), names only

    _dump = None
    if os.getenv("NOTION_RAW_DUMP_DIR"):  # debug only (test containers)
        _dump = open(os.path.join(os.getenv("NOTION_RAW_DUMP_DIR"), "raw-%d.ndjson" % int(time.time() * 1000)), "w")
    for line in response.iter_lines(decode_unicode=True):
        if _dump is not None and line:
            _dump.write((line if isinstance(line, str) else line.decode()) + "\n"); _dump.flush()
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="ignore")

        # 调试日志：含搜索关键词的行
        lowered_line = line.lower()
        if any(token in lowered_line for token in LINE_DEBUG_KEYWORDS):
            logger.debug(
                "NDJSON debug line",
                extra={"request_info": {"event": "notion_ndjson_debug_line", "line": line[:4000]}},
            )

        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(data, dict):
            continue
        data_type = str(data.get("type", "") or "").lower()

        notion_error = _extract_notion_error(data)
        if notion_error is not None:
            yield notion_error
            continue

        if data_type == "patch-start":
            steps = (data.get("data") or {}).get("s") if isinstance(data.get("data"), dict) else None
            if isinstance(steps, list):
                for st in steps:
                    err = _step_error(st)
                    if err is not None:
                        yield err
                    if isinstance(st, dict):
                        usage_tracker.observe_new_step(st)
            continue

        if data_type == "record-map":
            usage_tracker.observe_record_map(data)
            final_payload = _extract_final_content_from_record_map(data)
            if final_payload and final_payload.get("text"):
                yield {"type": "final_content", **final_payload}
            continue

        if data_type == "markdown-chat":
            cleaned = _clean_extracted_text(_extract_markdown_chat_text(data.get("value")))
            if cleaned:
                yield {"type": "final_content", "text": cleaned, "source_type": "markdown-chat-event"}
            continue

        if data_type != "patch":
            continue

        patches = data.get("v", [])
        if not isinstance(patches, list):
            continue

        for patch in patches:
            if not isinstance(patch, dict):
                continue

            patch_op = str(patch.get("o", "") or "")
            patch_v = patch.get("v")
            patch_path = _normalize_path(patch)
            patch_seg = _extract_segment_index(patch_path)

            if isinstance(patch_v, str) and re.match(r"^/?s/\d+/model$", patch_path or ""):
                reported_models.append(patch_v)
            if usage_tracker.observe_patch(patch_path, patch_v):
                continue
            if patch_op == "a" and patch_path.strip("/") == "s/-" and isinstance(patch_v, dict):
                err = _step_error(patch_v)
                if err is not None:
                    yield err
                pend = _pending_url_confirmation(patch_v)
                if pend is not None:
                    yield pend
                usage_tracker.observe_new_step(patch_v)
                _st = str(patch_v.get("type") or "")
                if _st and _st != "agent-inference":
                    step_kinds.append(_st + (":" + str(patch_v.get("toolName")) if patch_v.get("toolName") else "")
                                      + (":" + str(patch_v.get("state")) if patch_v.get("state") else ""))

            markdown_chat_patch = _extract_markdown_chat_patch_text(patch)
            if markdown_chat_patch is not None:
                event_type, event_text = markdown_chat_patch
                if event_type == "final_content":
                    yield {"type": event_type, "text": event_text, "source_type": "markdown-chat-patch"}
                else:
                    yield {"type": event_type, "text": event_text}
                continue

            # 提取 effective type（优先 patch.type，其次 v.type）
            patch_type = str(patch.get("type", "") or "").lower()
            nested_type = ""
            if isinstance(patch_v, dict):
                nested_type = str(patch_v.get("type", "") or "").lower()
            effective_type = patch_type or nested_type

            # ========== 段落注册：o:"a" 创建新段落 ==========
            path_stripped = patch_path.strip("/")
            is_new_toplevel_segment = (patch_op == "a" and path_stripped == "s/-")

            # 本 patch 的角色（注册时确定，用于当前 patch 的分类）
            patch_role: str | None = None

            if is_new_toplevel_segment:
                seg_class = _classify_segment_type(effective_type)

                # 收集 value item 类型
                local_value_types: dict[int, str] = {}
                local_next_val_id = 0
                if isinstance(patch_v, dict) and "value" in patch_v:
                    value_array = patch_v.get("value")
                    if isinstance(value_array, list):
                        for idx, item in enumerate(value_array):
                            if isinstance(item, dict):
                                item_type = str(item.get("type", "") or "").lower()
                                item_class = _classify_segment_type(item_type)
                                local_value_types[idx] = item_class
                                local_next_val_id = idx + 1

                if 0 not in local_value_types:
                    local_value_types[0] = seg_class
                    local_next_val_id = max(local_next_val_id, 1)

                # 加入 pending 队列，等待 o:"x" /s/N 绑定真实 index
                _pending_segments.append({
                    "seg_class": seg_class,
                    "value_types": local_value_types,
                    "next_val_id": local_next_val_id,
                })

                # 初始 patch 的文本用 value[0] 的类型
                patch_role = local_value_types.get(0, seg_class)
                # patch_seg 暂时设为 None，因为还不知道真实 index
                patch_seg = None

                logger.debug(
                    "Segment registered (pending)",
                    extra={
                        "request_info": {
                            "event": "segment_registered",
                            "pending_idx": len(_pending_segments) - 1,
                            "seg_class": seg_class,
                            "effective_type": effective_type,
                            "value_types": local_value_types,
                            "patch_role": patch_role,
                        }
                    },
                )

            elif patch_op == "a" and patch_seg is not None:
                # o:"a" 但 path 不是 /s/-（如 /s/2/value/-），属于已有段落的子追加
                # 先尝试绑定 pending 段落
                if patch_seg not in segment_types and _pending_segments:
                    _bind_pending_segment(patch_seg, _pending_segments, segment_types, value_types, next_val_id, patch_path)

                if patch_seg not in segment_types:
                    segment_types[patch_seg] = _classify_segment_type(effective_type)

                # 检测 /s/N/value/<idx|-> 子块追加
                value_add_idx = _extract_value_add_index(patch_path)
                if value_add_idx is not None:
                    vid = next_val_id.get(patch_seg, 0) if value_add_idx < 0 else value_add_idx
                    next_val_id[patch_seg] = max(next_val_id.get(patch_seg, 0), vid + 1)
                    val_class = _classify_segment_type(effective_type)
                    value_types[(patch_seg, vid)] = val_class
                    patch_role = val_class
                    in_lang_tag[0] = False
                    in_primary_attr[0] = False

            # ========== 绑定 pending 段落（o:"x" 首次引用时） ==========
            if patch_seg is not None and patch_seg not in segment_types and _pending_segments:
                _bind_pending_segment(patch_seg, _pending_segments, segment_types, value_types, next_val_id, patch_path)

            # ========== 确定当前 patch 所属的角色 ==========
            if patch_role is not None:
                # 刚注册的 patch，直接用注册时的角色
                seg_owner = patch_role
            else:
                # o:"x" 追加文本：先查 value block 表，再查 segment 表
                val_idx = _extract_value_index(patch_path)
                if val_idx is not None and patch_seg is not None and (patch_seg, val_idx) in value_types:
                    seg_owner = value_types[(patch_seg, val_idx)]
                    logger.debug(
                        "Patch role from value_types",
                        extra={
                            "request_info": {
                                "event": "patch_role_value_lookup",
                                "patch_op": patch_op,
                                "patch_seg": patch_seg,
                                "val_idx": val_idx,
                                "seg_owner": seg_owner,
                                "patch_path": patch_path,
                            }
                        },
                    )
                elif patch_seg is not None and patch_seg in segment_types:
                    seg_owner = segment_types[patch_seg]
                    logger.debug(
                        "Patch role from segment_types fallback",
                        extra={
                            "request_info": {
                                "event": "patch_role_segment_fallback",
                                "patch_op": patch_op,
                                "patch_seg": patch_seg,
                                "val_idx": val_idx,
                                "seg_owner": seg_owner,
                                "value_types_missing": (patch_seg, val_idx) not in value_types if val_idx is not None else None,
                                "patch_path": patch_path,
                            }
                        },
                    )
                else:
                    seg_owner = SEG_CONTENT
                    logger.debug(
                        "Patch role defaulting to content",
                        extra={
                            "request_info": {
                                "event": "patch_role_default_content",
                                "patch_op": patch_op,
                                "patch_seg": patch_seg,
                                "val_idx": val_idx,
                                "patch_path": patch_path,
                            }
                        },
                    )

            # ========== 搜索元数据（与段落角色无关，始终提取） ==========
            is_search_patch = _looks_like_search_patch(patch)
            if is_search_patch:
                search_data = _extract_search_data_from_patch(patch)
                if search_data:
                    yield {"type": "search", "data": search_data}

            # ========== 提取文本 ==========
            # /content replace patch can finalize broken lang attributes.
            # Reset state here to avoid swallowing following normal text.
            # A "p" op REPLACES the whole content string: Notion streams a provisional
            # "<lang primary=..." prefix, then replaces it with the real text (e.g. "<tool_"
            # or "None of ..."). The partial tag is gone, so always reset; otherwise the
            # stripper swallows the new text up to the next ">" (ate "<tool_call>" openers).
            if patch_op == "p" and "/content" in patch_path and isinstance(patch_v, str):
                in_lang_tag[0] = False
                in_primary_attr[0] = False

            # Notion tool I/O (e.g. create-pages input, view results) is never answer text.
            if _TOOL_IO_PATH_RE.search(patch_path or ""):
                continue

            content = _extract_text_from_patch(patch)
            if not content:
                continue

            cleaned = _strip_lang_tags(content, in_lang_tag)
            cleaned = _strip_primary_attr_fragments(cleaned, in_primary_attr)
            cleaned = _clean_notion_markup(cleaned)
            if not cleaned:
                continue

            # ========== 搜索 JSON 片段检测 ==========
            stripped = cleaned.strip()
            if stripped and (search_json_depth > 0 or _looks_like_search_json_fragment(stripped)):
                search_json_buffer += cleaned
                search_json_depth += stripped.count("{") - stripped.count("}")
                if search_json_depth <= 0:
                    sd = _extract_search_data_from_json_text(search_json_buffer)
                    if sd:
                        yield {"type": "search", "data": sd}
                    search_json_buffer = ""
                    search_json_depth = 0
                continue

            # 已被 search patch 处理的结构化数据不重复输出
            if is_search_patch:
                continue

            # ========== 按段落角色输出 ==========
            if seg_owner == SEG_META:
                continue
            if seg_owner in (SEG_THINKING, SEG_TOOL):
                yield {"type": "thinking", "text": cleaned}
            else:
                yield {"type": "content", "text": cleaned}

    if reported_models:
        logger.info("Notion-reported step models", extra={"request_info": {
            "event": "notion_step_models", "models": reported_models, "steps": step_kinds[:20]}})
    totals = usage_tracker.totals()
    if totals:
        yield {"type": "usage", "usage": totals}

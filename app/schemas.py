import time
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator

# ================================
# 请求相关 Schema (Chat Completion)
# ================================

class ChatMessage(BaseModel):
    """单条对话消息"""
    role: Literal["user", "assistant", "system", "developer", "tool", "function"]
    content: str = ""
    thinking: Optional[str] = None
    reasoning_content: Optional[str] = None
    # OpenAI function-calling fields (emulated, see app/tool_emulation.py)
    tool_calls: Optional[List[Dict[str, Any]]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    @field_validator("content", mode="before")
    @classmethod
    def _coerce_content_parts(cls, v):
        # OpenAI-style content parts (отправляют omp и др. клиенты) → plain text.
        # Не-текстовые части (image_url и т.п.) мост не поддерживает и молча отбрасывает.
        if v is None:
            return ""
        if isinstance(v, str):
            return v
        if isinstance(v, list):
            texts = []
            for part in v:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text") or "")
                elif isinstance(part, str):
                    texts.append(part)
                elif isinstance(part, dict) and part.get("type") in ("image_url", "input_image", "image"):
                    # images can't be forwarded; say so instead of silently dropping them
                    # (otherwise the model guesses at an image it never saw)
                    from app import image_input as _ii
                    texts.append(_ii.part_to_text(part))
                elif isinstance(part, dict) and part.get("type") in ("input_text", "output_text"):
                    texts.append(part.get("text") or "")
                elif isinstance(part, dict) and part.get("type") == "tool_result":
                    # Anthropic-style tool_result block (text + image content) nested in a message
                    from app import image_input as _ii
                    _c = part.get("content")
                    if isinstance(_c, list):
                        inner = "".join((p.get("text") or "") if isinstance(p, dict) and p.get("type") in ("text", "input_text", "output_text")
                                        else _ii.part_to_text(p) if isinstance(p, dict) and p.get("type") in ("image_url", "input_image", "image")
                                        else (p if isinstance(p, str) else "") for p in _c)
                    else:
                        inner = "" if _c is None else str(_c)
                    tid = part.get("tool_use_id") or ""
                    texts.append(f"\n<tool_result tool_call_id=\"{tid}\">\n{inner}\n</tool_result>\n" if tid else inner)
            return "".join(texts)
        return str(v)

class ChatCompletionRequest(BaseModel):
    """
    OpenAI-Compatible 发起完成请求的 Payload。
    保留 `conversation_id` 作为特定的扩展字段，若缺失则视为独立请求。
    """
    model: str = Field(default="claude-opus4.6", description="Requested model.")
    messages: List[ChatMessage]
    stream: bool = Field(default=False, description="Whether to stream the response as SSE.")
    temperature: Optional[float] = Field(default=None, description="Sampling temperature.")
    conversation_id: Optional[str] = Field(default=None, description="Extension for stateful conversation tracking.")
    stream_options: Optional[Dict[str, Any]] = Field(default=None, description="OpenAI stream_options (include_usage).")
    tools: Optional[List[Dict[str, Any]]] = Field(default=None, description="OpenAI tools (emulated).")
    tool_choice: Optional[Any] = Field(default=None)
    parallel_tool_calls: Optional[bool] = Field(default=None)
    functions: Optional[List[Dict[str, Any]]] = Field(default=None, description="Legacy OpenAI functions.")
    # Reasoning effort: OpenAI `reasoning_effort`, Responses-style `reasoning.effort`,
    # Anthropic-style `thinking: {type, budget_tokens}`, or a model suffix (-high, -thinking).
    reasoning_effort: Optional[str] = Field(default=None)
    reasoning: Optional[Dict[str, Any]] = Field(default=None)
    thinking: Optional[Dict[str, Any]] = Field(default=None)
    user: Optional[str] = Field(default=None, description="End-user id (OpenAI `user`); scopes thread reuse.")
    # Output controls (enforced after generation, see app/output_controls.py)
    max_tokens: Optional[int] = Field(default=None)
    max_completion_tokens: Optional[int] = Field(default=None)
    stop: Optional[Any] = Field(default=None)
    response_format: Optional[Dict[str, Any]] = Field(default=None)
    n: Optional[int] = Field(default=None)
    requested_model: Optional[str] = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _normalize_model_and_effort(self):
        from app.model_registry import resolve_model, normalize_effort, effort_from_budget
        self.requested_model = self.requested_model or self.model
        std, suffix_effort = resolve_model(self.model)
        if std:
            self.model = std
        eff = normalize_effort(self.reasoning_effort)
        if eff is None and isinstance(self.reasoning, dict):
            eff = normalize_effort(self.reasoning.get("effort"))
        if eff is None and isinstance(self.thinking, dict):
            t = str(self.thinking.get("type") or "").lower()
            if t == "disabled":
                eff = "none"
            elif self.thinking.get("budget_tokens") is not None:
                eff = effort_from_budget(self.thinking.get("budget_tokens"))
            elif t == "enabled":
                eff = "high"
        self.reasoning_effort = eff or suffix_effort
        return self

# ================================
# 非流式返回 Schema
# ================================

class ChatMessageResponseChoice(BaseModel):
    """非流式响应的选项"""
    index: int = 0
    message: ChatMessage
    finish_reason: str = "stop"

class ChatCompletionResponse(BaseModel):
    """
    OpenAI-Compatible 完整返回 Payload。
    """
    id: str
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatMessageResponseChoice]
    usage: Dict[str, Any] = Field(
        default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    )
    # Standard 模式扩展字段
    search_metadata: Optional[Dict[str, Any]] = Field(default=None)

# ================================
# 流式返回 Schema (供内部组织)
# ================================

class ChatCompletionChunkDelta(BaseModel):
    """SSE Delta Block"""
    content: Optional[str] = None
    role: Optional[str] = None

class ChatCompletionChunkChoice(BaseModel):
    """SSE Choice Block"""
    index: int = 0
    delta: ChatCompletionChunkDelta
    finish_reason: Optional[str] = None

class ChatCompletionChunk(BaseModel):
    """
    OpenAI-Compatible 流式 Chunk
    """
    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: List[ChatCompletionChunkChoice]

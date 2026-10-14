import os
from typing import Optional

# Claude Opus 5.5: codename from Notion's /api/v3/getAvailableModels (2026-10-13).
# Override with NOTION_MODEL_OPUS55 if Notion renames it.
OPUS55_NOTION_MODEL = os.getenv("NOTION_MODEL_OPUS55", "").strip() or "albuquerque-quinn"

MODEL_MAP: dict[str, str] = {
    "gpt-5.6-sol": "orange-mousse",
    "gpt-5.6-terra": "orchid-muffin",
    "gpt-5.6-luna": "olive-jellyroll",
    "gpt-5.4": "oval-kumquat-medium",
    "gpt-5.5": "opal-quince-medium",
    "gemini-3.5flash": "vertex-gemini-3.5-flash",
    "claude-sonnet4.6": "almond-croissant-low",
    "claude-sonnet5": "angel-cake-high",
    "claude-opus4.6": "avocado-froyo-medium",
    "claude-opus4.7": "apricot-sorbet-high",
    "claude-opus4.8": "ambrosia-tart-high",
    "claude-opus5": "agave-flan",
    "opus5.5": OPUS55_NOTION_MODEL,
    "claude-opus5.5": OPUS55_NOTION_MODEL,
    "kimi-2.7": "fireworks-kimi-k3",
    "deepseek-v4pro": "baseten-deepseek-v4-pro",
    "glm-5.2": "baseten-glm-5.2",
    "grok-4.3": "xigua-mochi-medium",
    "spacexai-4.5": "strawberry-whoopiepie",
    "grok-build0.1": "xinomavro-cake",
    "gemini-3.1pro": "galette-medium-thinking",
    "claude-haiku4.5": "anthropic-haiku-4.5",
    "fable-5": "acai-budino-high",
    "gemini-3flash": "gingerbread",
    "gpt-6-astra": "orlando-quinn",
    "gpt-6.1-sol": "omniberry-sundae",
    "gpt-6-luna": "omodi",
    "gpt-5.4-mini": "oregon-grape-medium",
    "gpt-5.4-nano": "otaheite-apple-medium",
    "gpt-5.2": "oatmeal-cookie",
    "gemini-3.7flash": "grapefruit-zeppole",
    "gemini-3.6flash": "vertex-gemini-3.6-flash",
    "claude-sonnet5.5": "achira-donut",
    "claude-haiku5.5": "amla-jam",
    "kimi-k3": "fireworks-kimi-k3",
    "deepseek-v4.1flash": "baseten-deepseek-v4.1-flash",
    "deepseek-v4flash": "baseten-deepseek-v4-flash",
    "glm-5.3flash": "baseten-glm-5.3-flash",
    "glm-5.3": "baseten-glm-5.3",
    "grok-4.6": "soursop-shortcake",
    "fable-5.1": "assam-chai",
    "sonnet5.5": "achira-donut",
    "haiku5.5": "amla-jam",
}

NOTION_MODEL_REVERSE_MAP: dict[str, str] = {value: key for key, value in MODEL_MAP.items()}

DISPLAY_NAMES: dict[str, str] = {
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.4": "GPT-5.4",
    "gpt-5.5": "GPT-5.5",
    "gemini-3.5flash": "Gemini 3.5 Flash",
    "claude-sonnet4.6": "Sonnet 4.6",
    "claude-sonnet5": "Sonnet 5",
    "claude-opus4.6": "Opus 4.6",
    "claude-opus4.7": "Opus 4.7",
    "claude-opus4.8": "Opus 4.8",
    "claude-opus5": "Opus 5",
    "opus5.5": "Opus 5.5",
    "claude-opus5.5": "Opus 5.5",
    "kimi-2.7": "Kimi K3",
    "deepseek-v4pro": "DeepSeek V4 Pro",
    "glm-5.2": "GLM 5.2",
    "grok-4.3": "Grok 4.3",
    "spacexai-4.5": "SpaceXAI 4.5",
    "grok-build0.1": "Grok Build 0.1",
    "gemini-3.1pro": "Gemini 3.1 Pro",
    "claude-haiku4.5": "Haiku 4.5",
    "fable-5": "Fable 5",
    "gemini-3flash": "Gemini 3 Flash",
    "gpt-6-astra": "GPT-6 Astra",
    "gpt-6.1-sol": "GPT-6.1 Sol",
    "gpt-6-luna": "GPT-6 Luna",
    "gpt-5.4-mini": "GPT-5.4 Mini",
    "gpt-5.4-nano": "GPT-5.4 Nano",
    "gpt-5.2": "GPT-5.2",
    "gemini-3.7flash": "Gemini 3.7 Flash",
    "gemini-3.6flash": "Gemini 3.6 Flash",
    "claude-sonnet5.5": "Sonnet 5.5",
    "claude-haiku5.5": "Haiku 5.5",
    "kimi-k3": "Kimi K3",
    "deepseek-v4.1flash": "DeepSeek V4.1 Flash",
    "deepseek-v4flash": "DeepSeek V4 Flash",
    "glm-5.3flash": "GLM 5.3 Flash",
    "glm-5.3": "GLM 5.3",
    "grok-4.6": "Grok 4.6",
    "fable-5.1": "Fable 5.1",
    "sonnet5.5": "Sonnet 5.5",
    "haiku5.5": "Haiku 5.5",
}

MODEL_ICONS: dict[str, str] = {
    "gpt-5.6-sol": "⚙",
    "gpt-5.6-terra": "⚙",
    "gpt-5.6-luna": "⚙",
    "gpt-5.4": "⚙",
    "gpt-5.5": "⚙",
    "gemini-3.5flash": "✦",
    "claude-sonnet4.6": "✳️",
    "claude-sonnet5": "✳️",
    "claude-opus4.6": "✳️",
    "claude-opus4.7": "✳️",
    "claude-opus4.8": "✳️",
    "claude-opus5": "✳️",
    "opus5.5": "✳️",
    "claude-opus5.5": "✳️",
    "kimi-2.7": "🌙",
    "deepseek-v4pro": "🐋",
    "glm-5.2": "◆",
    "grok-4.3": "⚡",
    "spacexai-4.5": "⚡",
    "grok-build0.1": "⚡",
    "gemini-3.1pro": "✦",
    "claude-haiku4.5": "✳️",
    "fable-5": "◇",
    "gemini-3flash": "✦",
    "gpt-6-astra": "⚙",
    "gpt-6.1-sol": "⚙",
    "gpt-6-luna": "⚙",
    "gpt-5.4-mini": "⚙",
    "gpt-5.4-nano": "⚙",
    "gpt-5.2": "⚙",
    "gemini-3.7flash": "✦",
    "gemini-3.6flash": "✦",
    "claude-sonnet5.5": "✳️",
    "claude-haiku5.5": "✳️",
    "kimi-k3": "🌙",
    "deepseek-v4.1flash": "🐋",
    "deepseek-v4flash": "🐋",
    "glm-5.3flash": "◆",
    "glm-5.3": "◆",
    "grok-4.6": "⚡",
    "fable-5.1": "◇",
    "sonnet5.5": "✳️",
    "haiku5.5": "✳️",
}

# 默认使用 Sonnet 4.6（速度和质量的最佳平衡）
DEFAULT_MODEL = "claude-sonnet4.6"


def get_notion_model(model_name: str) -> str:
    """Standard id -> Notion codename. Idempotent: a value that already is a Notion
    codename is returned unchanged. (Before 2026-10-14 the transcript builders mapped
    to the codename and NotionOpusAPI._to_notion_transcript mapped *again*; the codename
    is not a MODEL_MAP key, so every request silently fell back to the default
    almond-croissant-low = Sonnet 4.6.)"""
    if model_name in MODEL_MAP:
        return MODEL_MAP[model_name]
    if model_name in NOTION_MODEL_REVERSE_MAP:
        return model_name
    return MODEL_MAP[DEFAULT_MODEL]


# 需要走 markdown-chat 的 Notion 内部代号（vertex- 前缀的模型）
# Gemini 3.1 Pro (galette-medium-thinking) 已改为 workflow，不再走 markdown-chat
MARKDOWN_CHAT_MODELS: set[str] = {
    "vertex-gemini-3.5-flash",
    "vertex-gemini-3.6-flash",
}


def is_gemini_model(model_name: str) -> bool:
    """判断是否为 Gemini 系列模型（用于 config block 构建等）"""
    standard_name = get_standard_model(model_name)
    if standard_name.startswith("gemini-"):
        return True
    notion_model = get_notion_model(standard_name)
    return notion_model.startswith("vertex-") or notion_model.startswith("galette-")


def get_thread_type(model_name: str) -> str:
    """
    根据模型确定 Notion thread type。
    只有 vertex- 前缀的模型走 markdown-chat，其余全部走 workflow。
    """
    standard_name = get_standard_model(model_name)
    notion_model = get_notion_model(standard_name)
    if notion_model in MARKDOWN_CHAT_MODELS:
        return "markdown-chat"
    return "workflow"


def get_standard_model(model_name: str) -> str:
    if model_name in MODEL_MAP:
        return model_name
    return NOTION_MODEL_REVERSE_MAP.get(model_name, DEFAULT_MODEL)


def list_available_models() -> list[str]:
    return list(MODEL_MAP.keys())


def is_supported_model(model_name: str) -> bool:
    return model_name in MODEL_MAP


def get_display_name(model_name: str) -> str:
    standard_name = get_standard_model(model_name)
    return DISPLAY_NAMES.get(standard_name, standard_name)


def get_model_icon(model_name: str) -> str:
    standard_name = get_standard_model(model_name)
    return MODEL_ICONS.get(standard_name, "")


# Snapshot of /api/v3/getAvailableModels modelConfiguration (2026-10-14).
REASONING_EFFORTS: dict[str, tuple[tuple[str, ...], str]] = {
    'orlando-quinn': (('low', 'medium', 'high', 'xhigh', 'max'), 'medium'),
    'omniberry-sundae': (('none', 'low', 'medium', 'high', 'xhigh', 'max'), 'medium'),
    'orchid-muffin': (('none', 'low', 'medium', 'high', 'xhigh', 'max'), 'medium'),
    'omodi': (('none', 'low', 'medium', 'high', 'xhigh', 'max'), 'high'),
    'albuquerque-quinn': (('low', 'medium', 'high', 'xhigh', 'max'), 'medium'),
    'soursop-shortcake': (('low', 'medium', 'high', 'xhigh'), 'high'),
    'olive-jellyroll': (('none', 'low', 'medium', 'high', 'xhigh', 'max'), 'high'),
    'orange-mousse': (('none', 'low', 'medium', 'high', 'xhigh', 'max'), 'medium'),
    'oatmeal-cookie': (('medium', 'high'), 'medium'),
    'oval-kumquat-medium': (('medium', 'high'), 'medium'),
    'opal-quince-medium': (('medium', 'high'), 'medium'),
    'vertex-gemini-3.5-flash': (('low', 'medium', 'high'), 'low'),
    'vertex-gemini-3.6-flash': (('minimal', 'low', 'medium', 'high'), 'medium'),
    'grapefruit-zeppole': (('low', 'medium', 'high'), 'medium'),
    'almond-croissant-low': (('low', 'medium', 'high', 'max'), 'low'),
    'angel-cake-high': (('none', 'low', 'medium', 'high'), 'high'),
    'achira-donut': (('low', 'medium', 'high', 'xhigh', 'max'), 'high'),
    'amla-jam': (('none', 'low', 'medium', 'high', 'xhigh', 'max'), 'medium'),
    'avocado-froyo-medium': (('medium',), 'medium'),
    'apricot-sorbet-high': (('high',), 'high'),
    'ambrosia-tart-high': (('low', 'medium', 'high', 'max'), 'high'),
    'agave-flan': (('low', 'medium', 'high', 'max'), 'medium'),
    'oregon-grape-medium': (('medium',), 'medium'),
    'otaheite-apple-medium': (('medium',), 'medium'),
    'fireworks-kimi-k3': (('low', 'high', 'max'), 'high'),
    'baseten-deepseek-v4-pro': (('none', 'minimal', 'low', 'medium', 'high', 'max', 'xhigh'), 'high'),
    'baseten-deepseek-v4-flash': (('none', 'minimal', 'low', 'medium', 'high', 'xhigh'), 'high'),
    'baseten-deepseek-v4.1-flash': (('none', 'low', 'high', 'max'), 'high'),
    'baseten-glm-5.2': (('none', 'high', 'max'), 'high'),
    'baseten-glm-5.3': (('low', 'high', 'max'), 'high'),
    'baseten-glm-5.3-flash': (('low', 'high', 'max'), 'high'),
    'xigua-mochi-medium': (('low', 'medium', 'high'), 'medium'),
    'strawberry-whoopiepie': (('low', 'medium', 'high'), 'medium'),
    'assam-chai': (('low', 'medium', 'high', 'max'), 'high'),
    'galette-medium-thinking': (('low', 'medium'), 'medium'),
    'acai-budino-high': (('low', 'medium', 'high', 'max'), 'high'),
}

EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_EFFORT_ALIASES = {"thinking": "high", "think": "high", "extended": "high", "default": None,
                   "auto": None, "off": "none", "disabled": "none", "maximum": "max", "ultra": "max",
                   "extra-high": "xhigh", "extrahigh": "xhigh", "x-high": "xhigh", "med": "medium"}


def normalize_effort(value) -> Optional[str]:
    if value is None:
        return None
    v = str(value).strip().lower()
    if not v:
        return None
    if v in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[v]
    return v if v in EFFORT_ORDER else None


def clamp_effort(notion_model: str, effort: Optional[str]) -> Optional[str]:
    """Map a requested effort onto what this Notion model supports (nearest level).
    None -> Notion's own default for the model (what the web picker uses)."""
    conf = REASONING_EFFORTS.get(notion_model)
    if not conf:
        return None
    supported, default = conf
    if not effort:
        return default
    if effort in supported:
        return effort
    idx = EFFORT_ORDER.index(effort)
    return min(supported, key=lambda e: (abs(EFFORT_ORDER.index(e) - idx), -EFFORT_ORDER.index(e)))


def effort_from_budget(budget) -> Optional[str]:
    try:
        b = int(budget)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return "none"
    for limit, eff in ((2048, "low"), (8192, "medium"), (24576, "high"), (65536, "xhigh")):
        if b <= limit:
            return eff
    return "max"


_SUFFIXES = ("-xhigh", "-minimal", "-medium", "-high", "-none", "-low", "-max", "-thinking", "-think")


def _candidates(name: str):
    import re
    yield name
    dotted = re.sub(r"(?<=\d)-(?=\d)", ".", name)          # opus-5-5 -> opus-5.5
    yield dotted
    joined = re.sub(r"-(?=\d)", "", dotted)                # claude-opus-5.5 -> claude-opus5.5
    yield joined
    for n in (dotted, joined):
        yield "claude-" + n if not n.startswith("claude-") else n[len("claude-"):]


def resolve_model(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Client model id -> (standard id or None, effort from suffix or None).
    Accepts aliases like claude-opus-5-5, anthropic/claude-opus-5.5, opus5.5-high,
    claude-haiku-5-5-thinking, or a raw Notion codename."""
    name = str(raw or "").strip()
    if name in MODEL_MAP:
        return name, None
    low = name.lower()
    if "/" in low:
        low = low.rsplit("/", 1)[1]
    effort = None
    for suf in _SUFFIXES:
        if low.endswith(suf) and low[: -len(suf)]:
            base = low[: -len(suf)]
            for cand in _candidates(base):
                if cand in MODEL_MAP or cand in NOTION_MODEL_REVERSE_MAP:
                    return (cand if cand in MODEL_MAP else NOTION_MODEL_REVERSE_MAP[cand]), normalize_effort(suf[1:])
    for cand in _candidates(low):
        if cand in MODEL_MAP:
            return cand, effort
        if cand in NOTION_MODEL_REVERSE_MAP:
            return NOTION_MODEL_REVERSE_MAP[cand], effort
    return None, None


def effort_variant_ids() -> list[str]:
    """Optional /v1/models entries like claude-opus5.5-high (MODELS_LIST_EFFORT_VARIANTS=1)."""
    out = []
    for std, cod in MODEL_MAP.items():
        for e in (REASONING_EFFORTS.get(cod) or ((), ""))[0]:
            out.append(f"{std}-{e}")
    return out

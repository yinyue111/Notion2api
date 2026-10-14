# 新增模型清单

> 加模型只改下面这些地方。**不必全库搜索。**  
> 样例：`claude-opus5` → Notion 代号 `agave-flan`，显示名 `Opus 5`

需要先确认：**外部 API 名**、**Notion 内部代号**、**显示名**、**厂商分组**（Anthropic / OpenAI / …）。

---

## 必改（6 个文件）

### 1. `app/model_registry.py`（真相源）

三个 dict 各加一行，key 均为外部名：

| Dict | 值 |
|------|-----|
| `MODEL_MAP` | Notion 内部代号 |
| `DISPLAY_NAMES` | 显示名 |
| `MODEL_ICONS` | 图标（同厂商抄现有） |

`NOTION_MODEL_REVERSE_MAP` 不用手写。  
`/v1/models` 与请求校验自动读这里，**不要**在 `chat.py` / `models.py` 再抄列表。

仅当新模型必须走 `markdown-chat` 时，才改 `MARKDOWN_CHAT_MODELS`（绝大多数保持默认 `workflow`）。

### 2. `frontend/index.html`（线上 UI 实际用这个）

| 位置 | 内容 |
|------|------|
| `MODEL_GROUPS` | 对应分组；可加 `badge:"New"` |
| `MODELS` | `{id, label}` |
| `MODEL_DISPLAY_NAMES` | id → 显示名 |
| `MODEL_PROVIDERS` | id → 厂商名 |

### 3. `frontend/js/core/constants.js`（与 html 双写，id 集合必须一致）

| 位置 | 内容 |
|------|------|
| `MODEL_GROUPS` | 含 `icon`，可选 `badge` |
| `MODELS` | `{id, label}` |
| `MODEL_DISPLAY_NAMES` | id → 显示名 |
| `MODEL_ICONS` | id → 图标 |

### 4–6. 文档

| 文件 | 改什么 |
|------|--------|
| `README.md` | 模型数量 N→N+1；「支持的模型」表加一行；顶部同步说明（保留指向本文的链接） |
| `README_EG.md` | 同上（英文） |
| `docs/PROJECT_PROGRESS.md` | 数量；§4.6 表（外部名 / 代号 / thread / 说明）；日期 |

---

## 不要改

默认值 / 样例里的旧模型名可保留：

- `app/schemas.py`、`main.py`、`scripts/manage.sh`（默认或 test 用的 model）
- `app/api/chat.py`、`app/api/models.py`（动态读 registry）
- `conversation.py` / `notion_client.py`（无硬编码名单）

除非任务明确要求「改默认模型」。

---

## 勾选

```
[ ] model_registry.py     MODEL_MAP / DISPLAY_NAMES / MODEL_ICONS
[ ] frontend/index.html   GROUPS / MODELS / DISPLAY / PROVIDERS
[ ] frontend/js/core/constants.js  GROUPS / MODELS / DISPLAY / ICONS
[ ] README.md + README_EG.md + docs/PROJECT_PROGRESS.md
[ ] python -c "from app.model_registry import *; assert is_supported_model('…'); assert get_notion_model('…')=='…'"
```

新厂商：两边 `MODEL_GROUPS` 都加分组；`index.html` 的 `MODEL_PROVIDERS` 写上厂商名。  
下线模型：上述列表对称删除，数量 N→N-1。

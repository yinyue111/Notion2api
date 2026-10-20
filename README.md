# notion2api

把 **Notion AI** 封装成 **OpenAI 兼容 API** 的网关（Python / FastAPI），自带管理后台。
可直接接入支持 OpenAI 协议的客户端与 Agent 工具（Cherry Studio、Codex、Claude Code 中转、new-api / sub2api 上游、自建 Agent 等）。

> An OpenAI-compatible API gateway for Notion AI, with a multi-account pool, thread reuse, tool-calling emulation and an admin UI.

---

## 功能概览

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/chat/completions` | Chat Completions，支持 SSE 流式与非流式 |
| `POST` | `/v1/responses` | OpenAI Responses API（需 `ENABLE_RESPONSES_API=1`）；无状态：不支持 `previous_response_id`，`store` 恒为 false，内置托管工具会被忽略 |
| `GET` | `/v1/models` | 模型列表（使用受限 Key 时只返回该 Key 允许的模型） |
| `DELETE` | `/v1/conversations/{id}` | 删除会话 |
| `GET` | `/health` | 健康检查（含进行中请求数，不含账号信息） |
| — | `/`、`/chat` | 管理后台、内嵌对话页 |
| — | `/admin/api/*` | 管理后台 API（会话 + CSRF 保护） |

### 模型与推理强度

- 内置约 40 个友好模型名 → Notion 内部代号的映射（Claude Opus / Sonnet / Haiku、GPT、Gemini、Grok、DeepSeek、Kimi、GLM 等），见 [`app/model_registry.py`](app/model_registry.py)，新增方法见 [`docs/ADD_MODEL.md`](docs/ADD_MODEL.md)。
- 推理强度后缀：`-none` / `-minimal` / `-low` / `-medium` / `-high` / `-xhigh` / `-max`（如 `opus5.5-high`），自动夹到该模型支持的档位；`NOTION_EFFORT_FLOOR` 可为指定模型设最低档。
- **Max → xhigh**：`NOTION_MAP_MAX_TO`（默认 `xhigh`，可设 `high` / `off`）。
- `MODELS_LIST_EFFORT_VARIANTS=1` 时 `/v1/models` 额外列出带强度后缀的变体。

### 协议兼容

- **工具调用模拟**（`ENABLE_TOOL_EMULATION=1`）：把客户端 `tools` 描述进提示词，解析模型输出的 `<tool_call>` 为标准 `tool_calls`（流式 / 非流式），历史中的 tool 调用与结果按同一协议回放；工具参数被截断时自动修复 / 重试（`NOTION_TOOL_TRUNC_*`）。
- **识图**：消息中的图片（data URL / http(s) URL）上传为 Notion 对话附件后再推理（`NOTION_IMAGE_INPUT`，默认开），同一图片按账号缓存不重复上传。
- **OpenAI 输出控制**：`stop`、`max_tokens` / `max_completion_tokens`（超限 `finish_reason=length`）、`response_format`（`json_object` / `json_schema`）。
- **真实用量**：返回上游 token 用量，含 `cached_tokens` / `cache_write_tokens`。
- 错误统一为 OpenAI 风格 `{"error": {...}}`；流式期间定期心跳，避免中转 / 客户端超时断流；客户端断开时中断上游推理（`NOTION_INTERRUPT_ON_ABANDON`）。

### 性能与可靠性

- **线程复用（Prompt 缓存）**：`ENABLE_THREAD_REUSE=1` 时，多轮对话续用同一 Notion 线程、只发送增量消息，提高上游缓存命中、降低延迟。只存哈希与线程 id，不存消息正文；按调用方身份隔离。
- **Effort Hedge**（`NOTION_EFFORT_HEDGE`，默认开）：`xhigh` / `max` 推理被 Notion 静默关流时，在同一线程按 `NOTION_HEDGE_EFFORT`（默认 `high,medium`）并行对冲，先到的有效答案胜出。
- **Cap Memory**（`NOTION_CAP_MEMORY`，默认开）：某会话的 xhigh/max 撞到 Notion 单次 40k 输出上限后，后续轮次自动降为 `high`，每隔 `NOTION_CAP_MEMORY_PROBE` 轮试探一次原强度，`NOTION_CAP_MEMORY_TTL` 后过期。
- **长生成**：输出撞上限时可在同线程自动续写并拼接（`NOTION_AUTO_CONTINUE*`）；可选 plan-then-write（`NOTION_PLAN_WRITE`，默认关）。
- **静默 / 空答恢复**：上游静默关流后从线程记录中取回推理结果；空答、过短答复自动核对与重试（`NOTION_SHORT_RECONCILE*`、`NOTION_DEAD_RETRY*`）。
- **Ghost-run 熔断**（`NOTION_GHOST_GUARD`）：同一会话连续出现“思考到上限却不输出”的空跑时快速失败（502 `EMPTY_STREAM_BREAKER`），避免反复烧额度。
- **重发去重**（`NOTION_RESEND_DEDUPE`，默认开）：客户端超时后原样重发同一请求时，中止仍在运行的旧副本；并行子任务与短请求不受影响。
- **线程清理**：后台把网关创建、已无法复用的 Notion AI 线程软删除（`NOTION_THREAD_CLEANUP_*`，默认 48h）。
- 长用户消息自动切分（`NOTION_SPLIT_LONG_USER_CHARS`）。

### 账号池与调度

- **多账号池**：账号来自 `NOTION_ACCOUNTS` / `accounts.json` 或管理后台（SQLite，加密存储），后台改动热更新。
- **调度策略**：`ACCOUNT_STRATEGY=round_robin`（默认）或 `priority`（主号优先，`ACCOUNT_PRIORITY` 指定顺序）；新会话按额度余量（月度剩余 % / 距重置天数）打分。
- **Spread**（`POOL_SPREAD=p2c`，默认）：新会话在得分靠前的账号中“二选一取负载较低者”，分散并发；续用线程的请求不换号，不破坏缓存。`ACCOUNT_SCORE_WEIGHTS` 可按账号调权重。
- **故障转移**：额度耗尽、鉴权失败、429 / 503、空流时自动切换账号，失败账号指数退避并定期复查。
- **用量监控**：后台定时调用 Notion 只读接口 `getCreditRateLimitStatus` 获取 6h 滚动窗口与月度 / 账单周期用量（含新版 credit-bucket 方案的 flex 额度），达到 `USAGE_DEMOTE_PCT`（默认 95%）提前降级；**从不调用**一次性免费重置接口。
- **Limit Guard**（`NOTION_LIMIT_GUARD`，默认开）：上次查询显示已限流（或滚动用量 ≥ `POOL_ROLLING_HARD_PCT`）的账号在所有派发路径（新会话、续用、hedge、重试、故障转移）上都视为不可用，直到重置时间。
- **多工作区**（`NOTION_MULTI_WORKSPACE`，默认关）：同一 Notion 用户的每个工作区作为独立池条目，按模型能力路由（受限工作区只服务 GPT 类模型）。

### 安全与输出治理

- **身份脱敏**：发往 Notion 的上下文不含账号身份；返回内容中的账号姓名、邮箱、用户提及会被过滤（`REDACT_ACCOUNT_IDENTITY`）。
- **人设守护**：回复自称 Notion AI 时自动重试，仍失败则改写（`NOTION_PERSONA_GUARD`）。
- 清理 Notion 的 `<lang>` 标签、提及标签、引用脚注，以及模型对网关提示脚手架的“旁白”。
- 工具模式下可关闭 Notion 内置联网（`TOOL_MODE_NOTION_WEB`）、URL defang（`TOOL_MODE_DEFANG_URLS`）。

### 管理后台

浏览器访问服务根路径 `/`。需设置 `ADMIN_USERNAME` + `ADMIN_PASSWORD`（或 bcrypt 哈希 `ADMIN_PASSWORD_HASH`）以及 `TOKEN_ENC_KEY`（Fernet 密钥）。未设置时后台登录处于禁用状态。

- **登录安全**：bcrypt 密码、会话 + CSRF、按 IP 及全局的失败锁定（`ADMIN_LOCKOUT_*`、`ADMIN_GLOBAL_*`），浅色 / 深色主题。
- **概览**：按时间范围的请求量、tokens、缓存命中、错误趋势；按模型 / 账号 / API Key 汇总；按官方 API 价格估算用量价值（价格表可在后台编辑，存于 `data/official_prices.json`）。
- **账号**：
  - **Notion 邮箱验证码登录**：输入邮箱 → Notion 发送登录码 → 填码后网关在服务端完成登录并捕获 `token_v2`，加密入库，不回显到浏览器；已存在的账号会刷新 token。
  - 手动添加 / 编辑 `token_v2`（只写不读，仅显示末 4 位），启用 / 停用、删除、连通性测试、手动标记耗尽。
  - **额度 %**：每个账号的 6h 滚动与月度用量条、重置时间，以及基于请求日志的剩余额度 $ 估算（只读查询，不消耗模型）。
- **API Key**：除全局 `API_KEY` 外可创建多个受限 Key（`sk-n2a-…`），可限定允许的模型（支持 `claude-*` 通配）、停用、过期时间、重新生成、查看 / 复制；越权模型返回 403。
- **请求与用量**：请求日志（仅元数据：模型、强度、账号、Key、IP、输入 / 缓存 / 输出 tokens、首字与总耗时、错误码；默认保留 30 天），按状态 / 模型筛选分页；按模型 / 账号 / 天统计。
- **对话**：内嵌 Chat UI（`/chat`）。

---

## 快速开始

### Docker Compose（推荐）

```bash
git clone https://github.com/yinyue111/notion2api.git
cd notion2api
cp .env.example .env      # 至少设置 API_KEY；使用后台再设置 ADMIN_* 与 TOKEN_ENC_KEY
docker compose up -d --build
```

- 默认只监听 `127.0.0.1:${HOST_PORT:-8095}`，对外请放在反向代理（Caddy / Nginx）后面。
- 数据（SQLite、价格表、受限 Key 等）在 `./data`，日志在 `./logs`。
- 打开 `http://127.0.0.1:8095/` 登录后台，用“登录 Notion 账号”（邮箱验证码）或手动粘贴 `token_v2` 添加账号。

生成 `TOKEN_ENC_KEY`：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 本地运行

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py            # 或: uvicorn app.server:app --host 0.0.0.0 --port 8000
```

不用后台时，也可以通过 `NOTION_ACCOUNTS`（JSON 数组，需 `token_v2`、`space_id`、`user_id`）或 `accounts.json` 提供账号；`python login.py` 可启动浏览器在本地捕获 Notion 会话并写入配置。

### 调用示例

```bash
curl http://127.0.0.1:8095/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "opus5.5-high", "stream": true,
       "messages": [{"role": "user", "content": "你好"}]}'

curl http://127.0.0.1:8095/v1/models -H "Authorization: Bearer $API_KEY"
```

---

## 配置（环境变量）

完整列表与注释见 [`.env.example`](.env.example)。所有开关都有默认值，常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `API_KEY` | 空 | 全局 Bearer Token（空 = 不鉴权，生产务必设置） |
| `NOTION_ACCOUNTS` | 空 | 账号 JSON 数组；也可只用后台管理 |
| `TOKEN_ENC_KEY` | 空 | Fernet 密钥，加密后台存储的 `token_v2` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_PASSWORD_HASH` | 空 | 后台登录 |
| `ADMIN_DB_PATH` | `/app/data/admin.db` | 后台数据库 |
| `APP_MODE` | `heavy` | `heavy` / `standard` / `lite` 上下文模式 |
| `ENABLE_TOOL_EMULATION` | `0` | 工具调用模拟 |
| `ENABLE_RESPONSES_API` | `0` | 启用 `/v1/responses` |
| `ENABLE_THREAD_REUSE` / `THREAD_REUSE_*` | `0` | 线程复用 |
| `ACCOUNT_STRATEGY` / `ACCOUNT_PRIORITY` | `round_robin` | 账号调度 |
| `POOL_SPREAD` / `POOL_SPREAD_TOPK` | `p2c` / `4` | 新会话分散 |
| `NOTION_LIMIT_GUARD` / `POOL_ROLLING_HARD_PCT` | `on` / `100` | 限流账号全路径屏蔽 |
| `USAGE_MONITOR` / `USAGE_POLL_SECONDS` / `USAGE_DEMOTE_PCT` | `1` / `600` / `95` | 用量监控与提前降级 |
| `NOTION_MAP_MAX_TO` | `xhigh` | `max` 强度映射 |
| `NOTION_EFFORT_HEDGE` / `NOTION_HEDGE_EFFORT` / `NOTION_HEDGE_WAIT` | `1` / `high,medium` / `600` | Effort 对冲 |
| `NOTION_CAP_MEMORY` / `NOTION_CAP_MEMORY_PROBE` / `NOTION_CAP_MEMORY_TTL` | `on` / `3` / `1800` | 容量记忆 |
| `NOTION_GHOST_GUARD` | `on` | 空跑熔断 |
| `NOTION_RESEND_DEDUPE` | `on` | 重发去重 |
| `NOTION_IMAGE_INPUT` | `on` | 识图 |
| `NOTION_MULTI_WORKSPACE` | `off` | 多工作区路由 |
| `NOTION_THREAD_CLEANUP*` | 开 / 48h | 旧线程清理 |
| `REQUEST_LOG_RETENTION_DAYS` | 30 | 请求日志保留天数 |
| `SCOPED_API_KEYS_FILE` | `/app/data/scoped_api_keys.json` | 受限 Key 存储 |

## 关于 “Notion AI 指令” 页面

服务可读取账号在 Notion 中的个人 “AI 指令（AI instructions）” 页面作为系统级上下文。可通过 `NOTION_INSTRUCTIONS_PAGE_ID` 指定，或设 `NOTION_USE_INSTRUCTIONS_PAGE=0` 关闭。示例文案见 [`docs/notion_instructions_page.txt`](docs/notion_instructions_page.txt)。

## 测试

```bash
pip install pytest
pytest -q
```

## 目录结构

```
app/            网关核心（server、api/、admin/、账号池、Notion 客户端、各类守护模块）
frontend/admin  管理后台（静态页面）
frontend/chat   内嵌对话页
tests/          单元测试
docs/ scripts/  文档与部署脚本
login.py        本地浏览器捕获 Notion 会话的辅助脚本
```

## 免责声明

本项目仅供学习研究，与 Notion 官方无关。请遵守 Notion 服务条款与当地法律法规，风险自负。

## License

见 [LICENSE](LICENSE)。

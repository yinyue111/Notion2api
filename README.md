# notion2api

基于 Notion AI 的 **OpenAI 兼容 API 网关**。把 Notion AI 封装成标准的 `/v1/chat/completions` 与 `/v1/responses` 接口，可直接接入各类支持 OpenAI 协议的客户端 / Agent 工具（Cherry Studio、Claude Desktop / Agent SDK、Codex、自建 Agent 等）。

## 功能特性

### 协议与兼容

- **OpenAI 兼容接口**：`POST /v1/chat/completions`、`POST /v1/responses`、`GET /v1/models`；支持流式（SSE）与非流式。
- **工具调用模拟（Tool Calling）**：解析模型输出中的 `<tool_call>` 为原生 `tool_calls`；流式友好；上游静默时周期性发送 keepalive，避免中转/客户端断流；客户端断开时主动取消上游请求。
- **模型映射**：`opus5.5` / `sonnet5.5` / `haiku5.5`（及 `claude-*` 前缀）、GPT 系列等友好名称 → Notion 内部模型；支持推理强度后缀 `-low` / `-medium` / `-high` / `-xhigh` / `-max`。
- **Max → xhigh**：`NOTION_MAP_MAX_TO`（默认 `xhigh`）将客户端 `max` 努力映射到 Notion 支持的档位；请求日志记录 `effort_req` / `effort_final`。
- **OpenAI 细节**：`stop`、`max_tokens` / `max_completion_tokens`（超限 `finish_reason=length`）、`response_format`（`json_object` / `json_schema`）；错误返回 OpenAI 标准 `{"error": {...}}`。
- **真实用量透传**：返回上游真实 token 用量（含缓存命中字段）。

### 性能与稳定性

- **线程复用（Prompt 缓存）**：`ENABLE_THREAD_REUSE=1` 时多轮对话续用同一 Notion 线程，只发送增量消息，提高上游缓存命中、降低延迟；密钥按规范化历史 + 身份隔离，避免串话。支持 Claude Desktop / new-api 等对 system / billing header / tools 的特殊形状。
- **Effort Hedge**：可选对冲（`NOTION_EFFORT_HEDGE`）：并行/等待备用努力档，日志可汇总 billing；降低偶发空答或弱答。
- **Cap Memory**：按账号/模型记忆输出容量（`NOTION_CAP_MEMORY_*`），避免反复踩上限。
- **长生成 / 静默恢复**：上游静默关流时从线程记录恢复推理；长输出与空答重试（可降努力档）。
- **线程清理**：后台归档过旧自建线程（`NOTION_THREAD_CLEANUP_*`）。
- **长消息切分**：超过 `NOTION_SPLIT_LONG_USER_CHARS` 的用户消息自动分段发送。

### 账号池与调度

- **多账号池**：轮询或 `ACCOUNT_STRATEGY=priority` 主账号优先；额度耗尽 / 鉴权失败 / 持续限流时故障转移，定期复查恢复。
- **节流与退避**：`NOTION_MIN_REQUEST_INTERVAL`、`NOTION_RATE_LIMIT_BACKOFF`；失败冷却指数退避，优先未试账号。

### 安全与输出治理

- **人设守护**：检测回复自称 Notion AI 时自动重试 / 改写（`NOTION_PERSONA_GUARD`）。
- **语言标签与元评论清理**：剥离 `<lang …>` 等泄漏；去掉关于指令页的旁白。
- **身份脱敏**：不向客户端泄露 Notion 账号信息与网关内部配置。
- **工具模式**：可关闭 Notion 内置联网；URL defang；thinking 脚手架清理。

### 管理后台

访问服务根路径 `/`（需配置 `ADMIN_USERNAME` + `ADMIN_PASSWORD` / `ADMIN_PASSWORD_HASH`，以及 `TOKEN_ENC_KEY`）。

- **登录**：失败锁定 / 全局限流；支持浅色 / 深色主题。
- **邮箱验证码登录 Notion**：网关代发验证码并在服务端捕获 `token_v2`（加密入库，不回显）。
- **手动粘贴 token**：只写不读，仅显示末 4 位；改后账号池热更新。
- **额度与预估**：6h 滚动 / 月度用量条；按 Opus 官方价估算池容量与剩余（只读刷新，不耗模型）。
- **请求日志**：模型、账号、鉴权、IP、输入 / 缓存 / 输出 tokens、首字耗时、总耗时、错误码；支持按状态 / 模型筛选与分页。统计含缓存命中列与按模型 / 账号 / 天汇总。
- **内嵌对话页**：侧栏可打开 Chat UI。

## 部署（Docker Compose）

```bash
cp .env.example .env   # 按需填写
docker compose up -d --build
```

默认映射到 `127.0.0.1:${HOST_PORT:-8095}`，数据在 `./data`、日志在 `./logs`。管理后台：浏览器打开映射端口根路径。

本地运行：

```bash
pip install -r requirements.txt
python main.py
```

也可用 `python login.py` 启动浏览器辅助捕获 Notion 会话（见 `.env.example` 说明）。

## 环境变量

完整列表与注释见 [`.env.example`](.env.example)。常用项：

| 变量 | 说明 |
|---|---|
| `NOTION_ACCOUNTS` | 账号 JSON 数组；也可用后台管理 |
| `API_KEY` | 客户端调用 `/v1/*` 的 Bearer Token |
| `HOST` / `PORT` / `HOST_PORT` | 监听与宿主机映射端口 |
| `TOKEN_ENC_KEY` | 凭据加密密钥（启用后台必填） |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD`（或 `ADMIN_PASSWORD_HASH`） | 后台登录 |
| `ENABLE_TOOL_EMULATION` / `ENABLE_RESPONSES_API` | 工具模拟 / Responses API |
| `ENABLE_THREAD_REUSE` / `THREAD_REUSE_*` | 线程复用 |
| `ACCOUNT_STRATEGY` / `ACCOUNT_PRIORITY` / `ACCOUNT_EXHAUSTED_RECHECK_SECONDS` | 账号调度 |
| `NOTION_MAP_MAX_TO` | `max` → `xhigh` / `high` / `off` |
| `NOTION_EFFORT_HEDGE` / `NOTION_HEDGE_*` | Effort 对冲 |
| `NOTION_CAP_MEMORY_*` / `NOTION_OUTPUT_CAP` | 输出容量记忆 |
| `NOTION_MIN_REQUEST_INTERVAL` / `NOTION_RATE_LIMIT_BACKOFF` | 节流 |
| `NOTION_DEFAULT_SYSTEM_PROMPT` / `NOTION_PERSONA_GUARD` | 默认系统提示 / 人设守护 |
| `NOTION_USE_INSTRUCTIONS_PAGE` / `NOTION_INSTRUCTIONS_PAGE_ID` | AI 指令页 |
| `NOTION_THREAD_CLEANUP_*` | 旧线程清理 |
| `REQUEST_LOG_RETENTION_DAYS` | 请求日志保留天数 |

## 调用示例

```bash
curl http://127.0.0.1:8095/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opus5.5-high",
    "stream": true,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

```bash
curl http://127.0.0.1:8095/v1/models \
  -H "Authorization: Bearer $API_KEY"
```

## 关于 “Notion AI 指令” 页面

服务可读取账号在 Notion 中的个人 “AI 指令（AI instructions）” 页面作为系统级上下文（缓存约 10 分钟）。可在 Notion AI 设置中创建该页面；也可通过 `NOTION_INSTRUCTIONS_PAGE_ID` 指定，或设 `NOTION_USE_INSTRUCTIONS_PAGE=0` 关闭。示例文案见 [`docs/notion_instructions_page.txt`](docs/notion_instructions_page.txt)。

## 免责声明

本项目仅供学习研究，请遵守 Notion 服务条款与当地法律法规，风险自负。

## License

见 [LICENSE](LICENSE)。

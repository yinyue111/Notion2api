# notion2api

基于 Notion AI 的 **OpenAI 兼容 API 网关**。把 Notion AI 封装成标准的 `/v1/chat/completions` 与 `/v1/responses` 接口，可直接接入各类支持 OpenAI 协议的客户端 / Agent 工具。

## 功能特性

- **OpenAI 兼容接口**：`POST /v1/chat/completions`、`POST /v1/responses`、`GET /v1/models`，支持流式（SSE）与非流式。
- **工具调用模拟（Tool Calling）**：将模型输出中的 `<tool_call>` 解析为原生 `tool_calls`；支持流式输出；上游静默时每 5 秒发送 keepalive 数据块，避免中转断流；客户端断开时主动取消上游请求。
- **模型映射**：`opus5.5`、`sonnet5.5`、`haiku5.5`（及 `claude-*` 前缀）、GPT 系列等友好名称映射到 Notion 内部模型；支持推理强度后缀 `-low` / `-medium` / `-high` / `-xhigh` / `-max`。
- **多账号池**：多个 Notion 账号轮询，带请求节流（pacing）与限流退避（backoff）。
- **身份脱敏**：不泄露 Notion 账号信息，也不暴露 Notion AI 自身人设。
- **工具模式稳定性**：工具模式下关闭 Notion 内置联网工具；上游流无输出关闭时，从会话线程记录中恢复推理结果。
- **真实用量透传**：返回上游真实的 token 用量（usage）。
- **管理后台**：登录（失败锁定）、账号管理（增删、测试、额度查询）、请求日志与统计（按模型统计请求数、token 数、缓存命中）。

## 部署（Docker Compose）

```bash
cp .env.example .env   # 按需填写
docker compose up -d --build
```

默认映射到 `127.0.0.1:${HOST_PORT}`，数据持久化在 `./data`、日志在 `./logs`。管理后台访问根路径 `/`。

也可本地运行：

```bash
pip install -r requirements.txt
python main.py
```

## 环境变量

完整列表见 `.env.example`，主要项：

| 变量 | 说明 |
|---|---|
| `NOTION_ACCOUNTS` | Notion 账号 JSON 数组（`token_v2`、`space_id`、`user_id` 等），也可通过后台管理 |
| `API_KEY` | 客户端调用 `/v1/*` 时使用的 Bearer Token |
| `HOST` / `PORT` / `HOST_PORT` | 监听地址与端口 |
| `ALLOWED_ORIGINS` | CORS 白名单 |
| `APP_MODE` | `standard`（默认）/ `lite` / `heavy` |
| `TOKEN_ENC_KEY` | 账号凭据加密密钥，启用管理后台必填 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD`（或 `ADMIN_PASSWORD_HASH`） | 后台登录凭据 |
| `ADMIN_LOCKOUT_*` / `ADMIN_GLOBAL_*` | 登录失败锁定策略 |
| `REQUEST_LOG_RETENTION_DAYS` | 请求日志保留天数 |
| `ENABLE_TOOL_EMULATION` / `ENABLE_RESPONSES_API` | 工具调用模拟 / Responses 接口开关 |
| `TOOL_MODE_NOTION_WEB` / `TOOL_MODE_DEFANG_URLS` | 工具模式下 Notion 联网与 URL 处理 |
| `REDACT_ACCOUNT_IDENTITY` | 身份脱敏开关 |
| `NOTION_MIN_REQUEST_INTERVAL` / `NOTION_RATE_LIMIT_BACKOFF` | 节流与退避 |
| `NOTION_USE_INSTRUCTIONS_PAGE` / `NOTION_INSTRUCTIONS_PAGE_ID` | AI 指令页（见下） |

## 调用示例

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "opus5.5-high",
    "stream": true,
    "messages": [{"role": "user", "content": "你好"}]
  }'
```

## 关于 “Notion AI 指令” 页面

服务默认会读取账号在 Notion 中设置的个人 “AI 指令（AI instructions）” 页面，作为系统级指令上下文（结果缓存 10 分钟）。可在 Notion AI 设置中为账号创建该页面并写入通用指令；也可通过 `NOTION_INSTRUCTIONS_PAGE_ID` 手动指定页面，或设置 `NOTION_USE_INSTRUCTIONS_PAGE=0` 关闭此功能。

## 免责声明

本项目仅供学习研究，请遵守 Notion 服务条款，风险自负。

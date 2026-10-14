import time
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from contextlib import asynccontextmanager
from app.config import ACCOUNTS, API_KEY, ALLOWED_ORIGINS, ADMIN_DB_PATH, TOKEN_ENC_KEY, is_lite_mode, is_standard_mode
from fastapi.responses import RedirectResponse
from app.admin import security as admin_sec
from app.admin.routes import router as admin_router
from app.request_log import RequestLogMiddleware, annotate as _rl_annotate
from app.account_pool import AccountPool
from app.conversation import ConversationManager
from app.api.chat import router as chat_router
from app.api.responses import router as responses_router
from app.api.models import router as models_router
from app.logger import logger
from app.limiter import limiter

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时初始化状态
    app.state.admin_creds = admin_sec.AdminCredentials()
    app.state.admin_lockout = admin_sec.LoginLockout()
    app.state.admin_store = None
    pool_accounts = ACCOUNTS
    if TOKEN_ENC_KEY:
        from app.admin.store import AdminStore
        store = AdminStore(ADMIN_DB_PATH, TOKEN_ENC_KEY)
        imported = store.import_env_accounts(ACCOUNTS)
        app.state.admin_store = store
        pool_accounts = store.enabled_account_configs()
        logger.info("Admin store ready", extra={"request_info": {"event": "admin_store_ready", "imported_env_accounts": imported, "db_accounts": len(pool_accounts)}})
    else:
        logger.warning("TOKEN_ENC_KEY not set: admin panel disabled, using env accounts", extra={"request_info": {"event": "admin_store_disabled"}})
    if not app.state.admin_creds.configured:
        logger.warning("ADMIN_USERNAME/ADMIN_PASSWORD_HASH not set: admin login disabled", extra={"request_info": {"event": "admin_login_disabled"}})
    app.state.account_pool = AccountPool(pool_accounts)

    # 确定运行模式
    if is_lite_mode():
        mode = "lite"
        logger.info("Service starting up in LITE mode", extra={"request_info": {"event": "startup", "accounts": len(pool_accounts), "mode": "lite"}})
    elif is_standard_mode():
        mode = "standard"
        logger.info("Service starting up in STANDARD mode", extra={"request_info": {"event": "startup", "accounts": len(pool_accounts), "mode": "standard"}})
    else:
        mode = "heavy"
        app.state.conversation_manager = ConversationManager()
        logger.info("Service starting up in HEAVY mode", extra={"request_info": {"event": "startup", "accounts": len(pool_accounts), "mode": "heavy"}})

    app.state.start_time = time.time()
    app.state.mode = mode
    yield
    # 关闭时清理
    logger.info("Service shutting down", extra={"request_info": {"event": "shutdown"}})

app = FastAPI(
    title="Notion Opus API",
    description="A FastAPI wrapper providing an OpenAI-compatible interface for Notion's Claude Opus backend.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# 允许跨域（配合本地前端）
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    # 管理面板依赖 cookie；跨域请求一律不带凭据（/v1 用 Bearer，不受影响）
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注入 Limiter
app.state.limiter = limiter

# 自定义 429 速率限制响应
def custom_rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"error": "Too many requests, please try again later"}
    )
app.add_exception_handler(RateLimitExceeded, custom_rate_limit_exceeded_handler)


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled application exception",
        exc_info=True,
        extra={
            "request_info": {
                "event": "unhandled_exception",
                "method": request.method,
                "path": request.url.path,
            }
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "Internal server error",
                "type": "server_error",
            }
        },
    )

# 结构化日志中间件
@app.middleware("http")
async def log_requests_middleware(request: Request, call_next):
    start_time = time.time()
    
    # 跳过高频且不重要的日志打印，避免刷屏
    skip_logging = request.url.path in ["/health", "/favicon.ico"]
    
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        status_code = 500
        logger.error(f"Unhandled exception: {str(e)}", exc_info=True)
        raise
    finally:
        process_time = time.time() - start_time
        client_ip = request.client.host if request.client else "unknown"
        
        if not skip_logging:
            log_level = logger.error if status_code >= 400 else logger.info
            log_level(
                "Request processed",
                extra={
                    "request_info": {
                        "method": request.method,
                        "path": request.url.path,
                        "ip": client_ip,
                        "status_code": status_code,
                        "duration_ms": round(process_time * 1000, 2)
                    }
                }
            )
            
    return response

# 鉴权中间件：/v1 用 API Key（常量时间比较）或管理员会话；/chat 需要管理员会话
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

def _v1_unauthorized():
    return JSONResponse(
        status_code=401,
        content={"error": {"message": "Error: API KEY doesn't match.", "type": "invalid_request_error", "code": "invalid_api_key"}},
    )

@app.middleware("http")
async def api_key_auth(request: Request, call_next):
    path = request.url.path
    if path.startswith("/v1") and request.method != "OPTIONS":
        auth_header = request.headers.get("Authorization", "")
        bearer = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        if bearer:
            if API_KEY and not admin_sec.const_eq(bearer, API_KEY):
                return _v1_unauthorized()
            request.state.auth = "api_key"
            _rl_annotate(auth="api_key")
        elif API_KEY:
            sess = admin_sec.get_session(request)
            if not sess:
                return _v1_unauthorized()
            if request.method in _UNSAFE and not (
                admin_sec.same_origin(request)
                and admin_sec.const_eq(request.headers.get("x-csrf-token", ""), sess["csrf"])
            ):
                return _v1_unauthorized()
            request.state.auth = "admin_session"
            _rl_annotate(auth="admin_session")
    elif path == "/chat" or path.startswith("/chat/"):
        if not admin_sec.get_session(request):
            return RedirectResponse("/#login", status_code=302)
    response = await call_next(request)
    if not path.startswith("/v1"):
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'self'")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if path.startswith("/admin") or path == "/" or path.startswith("/chat"):
            response.headers.setdefault("Cache-Control", "no-store")
    return response

# 挂载路由，前缀统一为 /v1
app.include_router(chat_router, prefix="/v1")
if os.getenv("ENABLE_RESPONSES_API", "0").strip().lower() in ("1", "true", "yes", "on"):
    app.include_router(responses_router, prefix="/v1")
app.include_router(models_router, prefix="/v1")
app.include_router(admin_router, prefix="/admin/api")

# 挂载健康检查
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=b"", media_type="image/x-icon", status_code=204)

@app.get("/health", tags=["system"])
def health_check():
    return {"status": "ok"}

# 挂载静态前端到根目录
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
chat_dir = os.path.join(frontend_dir, "chat")
admin_dir = os.path.join(frontend_dir, "admin")
if os.path.exists(chat_dir):
    app.mount("/chat", StaticFiles(directory=chat_dir, html=True), name="chat")
if os.path.exists(admin_dir):
    app.mount("/", StaticFiles(directory=admin_dir, html=True), name="admin")

# 请求日志（纯 ASGI，最外层；只记录元数据）
app.add_middleware(
    RequestLogMiddleware,
    get_store=lambda: getattr(app.state, "admin_store", None),
    get_ip=admin_sec.client_ip,
)

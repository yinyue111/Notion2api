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

def _start_loop_watchdog() -> None:
    """Docker only marks the container unhealthy, it never restarts it. If the event loop
    stops answering for LOOP_WATCHDOG_SECS (a blocking call slipped onto it), exit so the
    restart policy brings the service back instead of hanging every request."""
    import asyncio as _aio
    import threading as _th
    limit = float(os.getenv("LOOP_WATCHDOG_SECS", "120") or 0)
    if limit <= 0:
        return
    loop = _aio.get_running_loop()
    state = {"last": time.monotonic()}

    def _beat():
        state["last"] = time.monotonic()

    def _watch():
        warned = False
        while True:
            time.sleep(5)
            try:
                loop.call_soon_threadsafe(_beat)
            except RuntimeError:
                return  # loop closed (shutdown)
            lag = time.monotonic() - state["last"]
            if lag > 15 and not warned:
                warned = True
                logger.warning("Event loop stalled", extra={"request_info": {"event": "loop_stall", "lag_s": round(lag, 1)}})
            elif lag <= 15:
                warned = False
            if lag > limit:
                logger.error("Event loop unresponsive, exiting for restart", extra={"request_info": {
                    "event": "loop_watchdog_exit", "lag_s": round(lag, 1)}})
                os._exit(3)

    _th.Thread(target=_watch, name="loop-watchdog", daemon=True).start()


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
    _start_loop_watchdog()
    from app import thread_janitor as _tj
    _tj.start(lambda: getattr(app.state, "account_pool", None))
    from app import usage_monitor as _um
    _st = app.state.admin_store

    def _persist_usage(client, st, _st=_st):
        aid = getattr(client, "admin_id", None)
        if _st is None or aid is None:
            return
        cur = next((a.get("credits") for a in _st.list_accounts() if a.get("id") == aid), None) or {}
        cur = dict(cur) if isinstance(cur, dict) else {}
        cur["rateLimit"] = st
        _st.set_credits(aid, cur)
    app.state.account_pool.on_usage_status = _persist_usage
    _um.start(lambda: getattr(app.state, "account_pool", None))
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
        content={"error": {"message": "Too many requests, please try again later", "type": "rate_limit_error",
                           "param": None, "code": "rate_limit_exceeded"}}
    )
app.add_exception_handler(RateLimitExceeded, custom_rate_limit_exceeded_handler)


from fastapi import HTTPException as _HTTPException
from fastapi.exceptions import RequestValidationError as _RVE


@app.exception_handler(_HTTPException)
async def openai_http_exception_handler(request: Request, exc: _HTTPException):
    """OpenAI-style error body for API routes (admin keeps FastAPI's {"detail": ...})."""
    if not request.url.path.startswith("/v1"):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=getattr(exc, "headers", None))
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        content = exc.detail
    else:
        code = {400: "invalid_request", 401: "invalid_api_key", 403: "permission_denied", 404: "not_found",
                429: "rate_limit_exceeded"}.get(exc.status_code, "error")
        typ = "invalid_request_error" if exc.status_code < 500 else "server_error"
        content = {"error": {"message": str(exc.detail), "type": typ, "param": None, "code": code}}
    return JSONResponse(status_code=exc.status_code, content=content, headers=getattr(exc, "headers", None))


@app.exception_handler(_RVE)
async def openai_validation_handler(request: Request, exc: _RVE):
    if not request.url.path.startswith("/v1"):
        from fastapi.exception_handlers import request_validation_exception_handler
        return await request_validation_exception_handler(request, exc)
    errs = exc.errors()
    first = errs[0] if errs else {}
    loc = [str(x) for x in (first.get("loc") or []) if x != "body"]
    msg = first.get("msg") or "Invalid request"
    if first.get("type") == "json_invalid":
        msg = "We could not parse the JSON body of your request."
    return JSONResponse(status_code=400, content={"error": {
        "message": (f"{'.'.join(loc)}: " if loc and first.get("type") != "json_invalid" else "") + msg,
        "type": "invalid_request_error", "param": ".".join(loc) or None,
        "code": "invalid_json" if first.get("type") == "json_invalid" else "invalid_value"}})


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
async def health_check():
    # async on purpose: a sync handler needs a free threadpool worker, so /health would
    # hang (and the container go unhealthy) exactly when Notion calls saturate the pool
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

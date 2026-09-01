"""Application factory.

Router auto-discovery: every module in `app.api.v1` that defines a module-level
`router` is included automatically (pkgutil scan). Feature agents add a file to
that package and never touch this file.

Middleware (outermost first): CORS -> request id -> access log -> rate limit ->
API key. The API-key middleware guards mutating /api/v1 routes with the
X-API-Key header; demo and detection triggers are exempt when APP_ENV != prod
so the demo UI works without a key. Never edit this ordering casually.

Lifespan: the in-process worker (app.services.worker, docs/worker.md) starts
with the app when WORKER_ENABLED=true and stops cleanly on shutdown. It is
OFF by default so the test suite and one-shot scripts never spawn the loop.
"""

import importlib
import hmac
import pkgutil
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

from app import __version__, api
from app.config import settings
from app.db import SessionLocal
from app.logging import configure_logging, get_logger, request_id_ctx
from app.services.razorpay.factory import get_gateway
from app.services.worker import start_worker

logger = get_logger("app.main")

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Paths exempt from the API key when APP_ENV != prod (judge/demo convenience).
API_KEY_EXEMPT_PREFIXES = ("/api/v1/demo", "/api/v1/detection")
RATE_LIMITS = {"webhooks": (120, 60.0), "mutating": (60, 60.0)}  # (count, window_s)


def _error(status: int, code: str, message: str, request_id: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "request_id": request_id}},
    )


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = request_id_ctx.set(rid)
        try:
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            return response
        finally:
            request_id_ctx.reset(token)


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.info(
            "http_request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "client": request.client.host if request.client else None,
            },
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple in-memory sliding-window limiter for /webhooks and mutating
    /api/v1 routes. Per-process only — fine for a single-node demo."""

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)

    def _bucket(self, request: Request) -> str | None:
        path = request.url.path
        if path.startswith("/webhooks"):
            return "webhooks"
        if request.method in MUTATING_METHODS and path.startswith("/api/v1"):
            return "mutating"
        return None

    async def dispatch(self, request: Request, call_next: Callable):
        bucket = self._bucket(request)
        if bucket is None:
            return await call_next(request)
        limit, window = RATE_LIMITS[bucket]
        key = (bucket, request.client.host if request.client else "unknown")
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) >= limit:
            return _error(
                429, "rate_limited", f"Too many requests ({bucket}).", request_id_ctx.get()
            )
        hits.append(now)
        return await call_next(request)


class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable):
        path = request.url.path
        if request.method in MUTATING_METHODS and path.startswith("/api/v1"):
            expected = settings.API_KEY.encode("utf-8")
            if not expected:
                # Fail CLOSED on misconfiguration: an empty API_KEY would
                # otherwise compare equal to a missing/empty header and leave
                # every mutating route unauthenticated.
                logger.error("api key middleware: API_KEY is not configured; refusing mutating request")
                return _error(
                    503,
                    "auth_not_configured",
                    "API key is not configured on the server; mutating routes are disabled.",
                    request_id_ctx.get(),
                )
            exempt = settings.APP_ENV != "prod" and path.startswith(API_KEY_EXEMPT_PREFIXES)
            provided = request.headers.get("x-api-key", "").encode("utf-8")
            if not exempt and not hmac.compare_digest(provided, expected):
                return _error(
                    401,
                    "unauthorized",
                    "Missing or invalid X-API-Key header.",
                    request_id_ctx.get(),
                )
        return await call_next(request)


def _include_discovered_routers(app: FastAPI) -> None:
    from app.api import v1 as v1_pkg

    for mod_info in pkgutil.iter_modules(v1_pkg.__path__):
        module = importlib.import_module(f"{v1_pkg.__name__}.{mod_info.name}")
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)
            logger.debug("router_registered", extra={"module": mod_info.name})


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start/stop the in-process worker with the app (docs/worker.md).

    WORKER_ENABLED defaults to false: the test suite, the evaluation harness,
    and one-shot scripts build apps that never spawn the tick loop."""
    supervisor = None
    if settings.WORKER_ENABLED:
        supervisor = await start_worker(
            settings, session_factory=SessionLocal, gateway=get_gateway(settings)
        )
    try:
        yield
    finally:
        if supervisor is not None:
            await supervisor.stop()


def create_app() -> FastAPI:
    configure_logging(settings.LOG_LEVEL)
    app = FastAPI(
        title="PulseRecover API",
        version=__version__,
        description=(
            "AI Payment Reliability & Revenue Recovery Engine. "
            "Probabilistic AI proposes, deterministic policy decides, "
            "payment infrastructure executes, verification proves. "
            "All money amounts are integer paise (INR)."
        ),
        lifespan=_lifespan,
    )

    # Middleware: added bottom-up; the last added is outermost. Final order
    # (outermost first): CORS, RequestId, AccessLog, RateLimit, ApiKey.
    app.add_middleware(ApiKeyMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    _include_discovered_routers(app)

    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(request: Request, exc: StarletteHTTPException):
        return _error(
            exc.status_code,
            code=str(exc.detail).lower().replace(" ", "_") if exc.detail else "http_error",
            message=str(exc.detail) if exc.detail else "HTTP error",
            request_id=request_id_ctx.get(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exc_handler(request: Request, exc: RequestValidationError):
        return _error(
            422,
            code="validation_error",
            message="Request validation failed.",
            request_id=request_id_ctx.get(),
        )

    @app.exception_handler(Exception)
    async def unhandled_exc_handler(request: Request, exc: Exception):
        logger.exception("unhandled_error", extra={"path": request.url.path})
        # No stack traces or internals leak to clients.
        return _error(500, "internal_error", "Internal server error.", request_id_ctx.get())

    return app


app = create_app()

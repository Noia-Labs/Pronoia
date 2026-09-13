"""Pronoia backend entry: FastAPI app (CORS, routes, static SPA mount)."""
from __future__ import annotations

import base64
import binascii
import secrets
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from . import config, db
from .log_bus import publish
from .model_lab import event_experiments
from .routes import (
    admin,
    arena,
    arena_model_comparison,
    arena_workspace,
    backtest,
    cases,
    chat,
    external_service,
    logic,
    meta,
    model_lab,
    model_registry,
    prediction_import,
    prospective,
    simulations,
)

app = FastAPI(title="Pronoia", version="3.12.1", docs_url="/api/docs")


@app.exception_handler(RequestValidationError)
async def _safe_model_lab_validation_error(request: Request, exc: RequestValidationError):
    """Do not reflect a mistakenly pasted API key in Model Lab 422 JSON."""
    if request.url.path.startswith(("/api/model-lab", "/api/bt/external-service", "/api/bt/models")) or request.url.path == "/api/bt/event-experiments":
        errors = []
        for item in exc.errors():
            # FastAPI normally includes ``input`` and sometimes a ValueError
            # object in ``ctx``. Neither is needed by the form and both can
            # contain the credential the user meant to reference indirectly.
            errors.append({
                "type": item.get("type"),
                "loc": item.get("loc"),
                "msg": item.get("msg"),
            })
        return JSONResponse(status_code=422, content={"detail": errors})
    return await request_validation_exception_handler(request, exc)


def _basic_credentials(authorization: str) -> tuple[str, str]:
    """Decode one UTF-8 HTTP Basic header, returning empty credentials on error."""
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "basic" or not token.strip():
        return "", ""
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return "", ""
    if ":" not in decoded:
        return "", ""
    username, password = decoded.split(":", 1)
    return username, password


class ShareAuthMiddleware(BaseHTTPMiddleware):
    """Protect the complete shared UI/API while leaving health checks public."""

    async def dispatch(self, request: Request, call_next):
        config.validate_share_auth_config()
        if request.url.path == "/api/health" or not config.share_auth_enabled():
            return await call_next(request)

        supplied_user, supplied_password = _basic_credentials(
            request.headers.get("authorization", "")
        )
        # Compare byte strings so non-ASCII credentials remain supported. Both
        # comparisons always run; do not short-circuit after a username miss.
        user_ok = secrets.compare_digest(
            supplied_user.encode("utf-8"), config.SHARE_USER.encode("utf-8")
        )
        password_ok = secrets.compare_digest(
            supplied_password.encode("utf-8"), config.SHARE_PASSWORD.encode("utf-8")
        )
        if not (user_ok & password_ok):
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={
                    "WWW-Authenticate": 'Basic realm="Pronoia", charset="UTF-8"',
                    "Cache-Control": "no-store",
                },
            )
        return await call_next(request)


class TimingMiddleware(BaseHTTPMiddleware):
    """记录每个请求的耗时，慢请求打标签（SLOW >3s / VSLOW >10s）。

    用途：诊断前端卡住时，从日志一眼看到哪个后端请求慢。
    """

    async def dispatch(self, request: Request, call_next):
        start = time.time()
        try:
            response = await call_next(request)
        except Exception:
            dur = time.time() - start
            path = request.url.path
            print(
                f"TIMING {request.client.host if request.client else '-'} "
                f'"{request.method} {path}" ERR - {dur:.2f}s',
                flush=True,
            )
            publish(
                f"TIMING {request.client.host if request.client else '-'} "
                f'"{request.method} {path}" ERR - {dur:.2f}s'
            )
            raise

        dur = time.time() - start
        tag = ""
        if dur > 10:
            tag = " [VSLOW]"
        elif dur > 3:
            tag = " [SLOW]"
        path = request.url.path
        # 不记 /api/health（太频繁，会刷屏）
        if path != "/api/health":
            print(
                f"TIMING {request.client.host if request.client else '-'}:{request.client.port if request.client else '-'} "
                f'"{request.method} {path}" {response.status_code} - {dur:.2f}s{tag}',
                flush=True,
            )
            publish(
                f"TIMING {request.client.host if request.client else '-'}:{request.client.port if request.client else '-'} "
                f'"{request.method} {path}" {response.status_code} - {dur:.2f}s{tag}'
            )
        return response


app.add_middleware(TimingMiddleware)

app.add_middleware(ShareAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(meta.router)
app.include_router(cases.router)
app.include_router(chat.router)
app.include_router(logic.router)
app.include_router(backtest.router)
app.include_router(model_registry.router)
app.include_router(arena_model_comparison.router)
app.include_router(arena_workspace.router)
app.include_router(arena.router)
app.include_router(admin.router)
app.include_router(prospective.router)
app.include_router(simulations.router)
app.include_router(model_lab.router)
app.include_router(event_experiments.router)
app.include_router(external_service.router)
app.include_router(prediction_import.router)


@app.on_event("startup")
def _startup() -> None:
    # Fail before opening the database or starting background work when only
    # half of the sharing credentials were supplied.
    config.validate_share_auth_config()
    db.init_db()
    from .event_backtest.arena_workspace import recover_interrupted as recover_arena_workspace
    recover_arena_workspace()
    from .model_lab.repository import seed_builtin_question_set

    seed_builtin_question_set()
    # 自动扫描 <PROJECT_ROOT>/backtesting 目录，把 events/labels JSONL 对注册进 bt_datasets，
    # 保证「创建回测」的数据源始终来自 backtesting 目录，而不是数据库里残留的旧绝对路径。
    from .event_backtest.application import discover_backtesting_datasets

    try:
        datasets = discover_backtesting_datasets()
        print(f"[startup] Discovered {len(datasets)} backtesting dataset(s): {list(datasets.keys())}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] discover_backtesting_datasets failed: {exc}", flush=True)

    # Model Lab workers are daemon threads, so process termination can leave
    # durable rows marked running even though no executor survives.  Resume
    # only those orphaned batches after datasets have been rediscovered;
    # deliberately unstarted pending batches remain pending.
    from .model_lab.service import recover_interrupted_batches

    try:
        resumed = recover_interrupted_batches()
        if resumed:
            print(f"[startup] Resumed {len(resumed)} interrupted Model Lab batch(es)", flush=True)
    except Exception as exc:  # noqa: BLE001
        # One malformed historical row must not prevent the rest of Pronoia
        # from starting. The batch stays interrupted and can be resumed later.
        print(f"[startup] Model Lab recovery failed: {type(exc).__name__}: {exc}", flush=True)

    try:
        recovered_predictions = event_experiments.recover_event_predictions()
        if recovered_predictions:
            print(f"[startup] Resumed {len(recovered_predictions)} event experiment prediction(s)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] Event experiment recovery failed: {type(exc).__name__}", flush=True)


class SPAStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback: unknown non-/api paths -> index.html."""

    async def get_response(self, path: str, scope):  # type: ignore[override]
        from starlette.exceptions import HTTPException as StarletteHTTPException

        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            request_path = str(scope.get("path") or "")
            is_api_path = (
                request_path == "/api"
                or request_path.startswith("/api/")
                or path == "api"
                or path.startswith("api/")
            )
            if exc.status_code == 404 and not is_api_path:
                index = Path(self.directory) / "index.html"
                if index.exists():
                    return FileResponse(index)
            raise


if config.FRONTEND_DIST.exists():
    app.mount("/", SPAStaticFiles(directory=str(config.FRONTEND_DIST), html=True), name="spa")

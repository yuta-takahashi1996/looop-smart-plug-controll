"""FastAPI アプリケーションのエントリポイント。

Web UI とスケジューラーは同一プロセスで動作させる（スケジューラーの二重起動防止）。
"""

import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.api import auth, dashboard, forecast, history, schedule
from app.api import settings as settings_api
from app.config import get_settings
from app.repositories.database import get_session
from app.templating import templates

logger = logging.getLogger(__name__)

SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger.info("starting (env=%s, dry_run=%s)", settings.app_env, settings.dry_run)
    if not settings.auth_configured:
        logger.warning("WEB_PASSWORD_HASH / SESSION_SECRET が未設定のため、Web UI にログインできません")
    yield
    logger.info("shutting down")


settings = get_settings()

app = FastAPI(title="Looop Smart Plug Controller", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.add_middleware(
    SessionMiddleware,
    # 未設定時はプロセスごとの乱数を使う（この場合 auth_configured=False でログイン自体を拒否する）
    secret_key=settings.session_secret.get_secret_value() or secrets.token_urlsafe(32),
    session_cookie="looop_session",
    max_age=SESSION_MAX_AGE_SECONDS,
    same_site="lax",
)


@app.middleware("http")
async def restrict_client_network(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """ALLOWED_NETWORKS 外からの接続を拒否する。"""
    host = request.client.host if request.client else None
    try:
        allowed = host is not None and any(
            ip_address(host) in network for network in settings.allowed_networks
        )
    except ValueError:
        allowed = False
    if not allowed:
        logger.warning("rejected request from %s", host)
        return PlainTextResponse("Forbidden", status_code=status.HTTP_403_FORBIDDEN)
    return await call_next(request)


@app.exception_handler(auth.LoginRequired)
async def login_required_handler(request: Request, exc: auth.LoginRequired) -> Response:
    return auth.login_redirect_response(request, exc)


templates.env.globals["csrf_token"] = auth.get_csrf_token
templates.env.globals["dry_run"] = settings.dry_run

app.include_router(auth.router)
for module in (dashboard, forecast, schedule, history, settings_api):
    app.include_router(module.router, dependencies=[Depends(auth.require_login)])


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}

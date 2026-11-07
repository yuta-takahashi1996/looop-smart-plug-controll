"""FastAPI アプリケーションのエントリポイント。

Web UI とスケジューラーは同一プロセスで動作させる（スケジューラーの二重起動防止）。
"""

import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.api import auth, control, dashboard, devices, forecast, history, schedule
from app.api import settings as settings_api
from app.clients.jackery import JackeryClient
from app.clients.switchbot import SwitchBotClient
from app.config import Settings, get_settings
from app.models import device as device_models
from app.models.forecast import ADJUSTMENT_CATEGORY_LABELS
from app.providers.looop import LooopForecastProvider
from app.repositories.database import SessionLocal, get_session
from app.services.controller import BatteryMonitor, ControlService, PlugMonitor
from app.services.scheduler import Jobs, create_scheduler
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

    # 画面とスケジューラーで共有する外部連携。テストでは app.state を差し替える
    app.state.forecast_provider = LooopForecastProvider()
    switchbot = SwitchBotClient(
        settings.switchbot_token.get_secret_value(),
        settings.switchbot_secret.get_secret_value(),
        "",  # 制御する機器は DB の devices から設定する（bind_devices。SWITCHBOT_DEVICE_ID は移行にだけ使う）
    )
    app.state.switchbot = switchbot
    app.state.switchbot_devices = devices.DeviceListCache()
    app.state.plug_monitor = PlugMonitor(switchbot, SessionLocal)
    jackery = JackeryClient(settings.jackery_account, settings.jackery_password.get_secret_value())
    # 取得するポータブル電源は機器の登録（bind_devices）で決める
    battery = BatteryMonitor(jackery, None, SessionLocal)
    app.state.control = ControlService(settings, SessionLocal, switchbot=switchbot, battery=battery)
    _setup_devices(settings, switchbot, jackery, app.state.control)
    if not app.state.plug_monitor.configured:
        logger.warning("SwitchBot の認証情報または制御対象の機器が未設定のため、プラグ状態を確認できません")
    if not app.state.control.dry_run:
        logger.warning("実制御が有効です（SwitchBot へコマンドを送信します）")

    scheduler = None
    if settings.app_env != "test":
        app.state.control.initialize()
        jobs = Jobs(settings, app.state.forecast_provider, app.state.plug_monitor, SessionLocal, app.state.control)
        scheduler = create_scheduler(settings, jobs)
        scheduler.start()
    yield
    if scheduler is not None:
        scheduler.shutdown(wait=True)
        app.state.control.shutdown()
    logger.info("shutting down")


def _setup_devices(settings: Settings, switchbot: SwitchBotClient, jackery: JackeryClient, control: ControlService) -> None:
    """種別マスターの補完、既存の1台の移行（docs/17 §17.4.7）、制御する機器の設定。失敗しても Web UI は起動させる。"""

    def switchbot_name(device_id: str) -> str | None:
        if not switchbot.credentials_configured:
            return None
        found = next((d for d in switchbot.list_devices() if d.get("deviceId") == device_id), None)
        return str(found["deviceName"]) if found and found.get("deviceName") else None

    def jackery_device() -> tuple[str, str | None] | None:
        if not jackery.configured:
            return None
        listed = jackery.list_devices()
        if not listed:
            return None
        return str(listed[0].get("devId")), listed[0].get("devName") or None

    try:
        with SessionLocal() as session:
            device_models.seed_device_types(session)
            if not device_models.migrate_legacy_devices(
                session, settings, switchbot_name=switchbot_name, jackery_device=jackery_device
            ):
                logger.warning("ポータブル電源の一覧を取得できなかったため、機器の移行の一部を次回の起動時にやり直します")
            control.bind_devices(session)
    except Exception:
        logger.exception("failed to set up devices")


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
# 実制御は DRY_RUN=false かつ画面で有効にした場合のみ（実行中に切り替わるため、表示のたびに判定する）
templates.env.globals["is_dry_run"] = lambda request: request.app.state.control.dry_run
templates.env.globals["adjustment_labels"] = ADJUSTMENT_CATEGORY_LABELS
# DB の日時（UTC）を表示用のタイムゾーンへ変換する
templates.env.filters["local"] = lambda value, fmt: value.astimezone(ZoneInfo(settings.tz)).strftime(fmt)

app.include_router(auth.router)
for module in (dashboard, control, forecast, schedule, history, devices, settings_api):
    app.include_router(module.router, dependencies=[Depends(auth.require_login)])


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}

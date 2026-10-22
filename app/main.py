"""FastAPI アプリケーションのエントリポイント。

Web UI とスケジューラーは同一プロセスで動作させる（スケジューラーの二重起動防止）。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api import dashboard
from app.config import get_settings
from app.repositories.database import get_session

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger.info("starting (env=%s, dry_run=%s)", settings.app_env, settings.dry_run)
    yield
    logger.info("shutting down")


app = FastAPI(title="Looop Smart Plug Controller", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(dashboard.router)


@app.get("/healthz")
def healthz(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}

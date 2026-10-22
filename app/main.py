"""FastAPI アプリケーションのエントリポイント。

Web UI とスケジューラーは同一プロセスで動作させる（スケジューラーの二重起動防止）。
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings

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


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}

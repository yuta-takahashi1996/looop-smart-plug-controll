"""メトリクス画面（docs/18-host-metrics.md）。ホスト（Raspberry Pi）の現在の負荷をグラフで表示する。"""

from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.services.host_metrics import HISTORY_SECONDS, HostMetricsCollector
from app.templating import templates

router = APIRouter()


def collector(request: Request) -> HostMetricsCollector:
    return request.app.state.host_metrics


@router.get("/metrics", response_class=HTMLResponse, name="metrics")
def metrics_page(request: Request) -> HTMLResponse:
    c = collector(request)
    return templates.TemplateResponse(
        request,
        "metrics.html",
        {"latest": c.latest(), "interval": c.interval, "history_seconds": HISTORY_SECONDS},
    )


@router.get("/metrics/data", name="metrics_data")
def metrics_data(request: Request, since: Annotated[float | None, Query()] = None) -> dict:
    """since より新しい値（未指定なら保持している直近60秒の分）。画面から2秒ごとに取得する。"""
    c = collector(request)
    return {
        "interval": c.interval,
        "history_seconds": HISTORY_SECONDS,
        "samples": [s.to_dict() for s in c.samples(since)],
    }

"""定期ジョブ（docs/06 §6.3、docs/09-scheduling.md）。

Web UI と同一プロセスで 1 つだけ起動する（二重起動防止）。現時点のジョブ：
- 料金予報の取得：起動時、毎日 16:15（翌日分）、未取得の間は 15 分ごとに再取得。取得後に制御スケジュールを再生成する
  （取得に失敗しても、スケジュールが未生成の日は任意スケジュールを展開する。docs/07 §7.9）
- プラグ状態の確認：5 分ごと（SwitchBot API の上限は 1 日 10,000 回）
- 単価に関わる Looop のお知らせの確認：起動時と毎日 1 回
- 制御サイクル（有効なスケジュールの実行。Dry Run では操作を記録するだけ）：毎分
- 日次集計（前日分）：毎日 0:05、起動時（直近の未集計分も）
- バックアップと古いデータの削除：毎日 3:30
"""

import logging
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import Session

from app.config import Settings
from app.providers.base import ForecastFetchError, ForecastProvider
from app.services import forecast as forecast_service
from app.services import maintenance, optimizer
from app.services.controller import ControlService, PlugMonitor

logger = logging.getLogger(__name__)

TOMORROW_FETCH_TIME = time(16, 15)
FORECAST_RETRY_INTERVAL = timedelta(minutes=15)
PLUG_STATUS_INTERVAL = timedelta(minutes=5)
NOTICE_CHECK_TIME = time(9, 30)


class Jobs:
    def __init__(
        self,
        settings: Settings,
        provider: ForecastProvider,
        monitor: PlugMonitor,
        session_factory: Callable[[], Session],
        control: ControlService | None = None,
    ) -> None:
        self._settings = settings
        self._provider = provider
        self._monitor = monitor
        self._control = control
        self._session_factory = session_factory
        self._tz = ZoneInfo(settings.tz)

    def fetch_forecast(self) -> None:
        with self._session_factory() as session:
            try:
                saved = forecast_service.fetch_and_store(
                    session,
                    self._provider,
                    self._settings.looop_area,
                    self._tz,
                    retry_count=self._settings.forecast_retry_count,
                    retry_wait_seconds=5,
                )
                optimizer.regenerate_days(session, self._settings, saved)
            except ForecastFetchError:
                # イベントは fetch_and_store で記録済み。次回の再取得ジョブで再試行する
                self._expand_pending(session)
            except Exception:
                logger.exception("unexpected error while fetching forecast")

    def _target_days(self) -> list[date]:
        now = datetime.now(self._tz)
        return [now.date()] + ([now.date() + timedelta(days=1)] if now.time() >= TOMORROW_FETCH_TIME else [])

    def _expand_pending(self, session: Session) -> None:
        """スケジュールが未生成の日に任意スケジュールを展開する（予報を取得できない間も実行するため）。"""
        try:
            pending = [d for d in self._target_days() if not optimizer.schedules_for(session, d)]
            if pending:
                optimizer.regenerate_days(session, self._settings, pending)
        except Exception:
            logger.exception("unexpected error while expanding custom schedules")

    def ensure_forecast(self) -> None:
        """当日分（16:15 以降は翌日分も）が揃っていなければ取得する。"""
        now = datetime.now(self._tz)
        area = self._settings.looop_area
        with self._session_factory() as session:
            missing = not forecast_service.has_forecast(session, area, now.date(), self._tz)
            if now.time() >= TOMORROW_FETCH_TIME:
                missing = missing or not forecast_service.has_forecast(
                    session, area, now.date() + timedelta(days=1), self._tz
                )
        if missing:
            self.fetch_forecast()
            return
        # 予報はあるがスケジュールが未生成の日（初回起動時など）は生成する
        with self._session_factory() as session:
            self._expand_pending(session)

    def refresh_plug_status(self) -> None:
        try:
            self._monitor.refresh()
        except Exception:
            logger.exception("unexpected error while checking plug status")

    def run_control_cycle(self) -> None:
        if self._control is None:
            return
        try:
            self._control.run_cycle()
        except Exception:
            logger.exception("unexpected error in control cycle")

    def summarize_daily(self) -> None:
        with self._session_factory() as session:
            try:
                days = maintenance.summarize_pending(session, self._settings, datetime.now(self._tz).date())
                if days:
                    logger.info("daily summaries updated: %s", ", ".join(map(str, days)))
            except Exception:
                logger.exception("unexpected error while summarizing daily results")

    def run_maintenance(self) -> None:
        now = datetime.now(self._tz)
        try:
            maintenance.backup_database(self._settings, now.date())
        except Exception:
            logger.exception("database backup failed")
        with self._session_factory() as session:
            try:
                maintenance.cleanup_old_data(session, self._settings, now)
            except Exception:
                logger.exception("old data cleanup failed")

    def check_price_notices(self) -> None:
        with self._session_factory() as session:
            try:
                created = forecast_service.check_price_notices(session, self._provider)
                if created:
                    logger.info("found %d new price notice(s)", created)
            except ForecastFetchError as exc:
                logger.warning("price notice check failed: %s", exc)
            except Exception:
                logger.exception("unexpected error while checking price notices")


def create_scheduler(settings: Settings, jobs: Jobs) -> BackgroundScheduler:
    tz = ZoneInfo(settings.tz)
    scheduler = BackgroundScheduler(
        timezone=tz,
        # スリープ等で予定時刻を過ぎても 1 時間以内なら実行する（既定の 1 秒では取りこぼす）
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
    )
    now = datetime.now(tz)
    scheduler.add_job(
        jobs.ensure_forecast,
        "interval",
        seconds=FORECAST_RETRY_INTERVAL.total_seconds(),
        next_run_time=now,
        id="ensure_forecast",
    )
    scheduler.add_job(
        jobs.fetch_forecast,
        "cron",
        hour=TOMORROW_FETCH_TIME.hour,
        minute=TOMORROW_FETCH_TIME.minute,
        id="fetch_tomorrow_forecast",
    )
    scheduler.add_job(
        jobs.refresh_plug_status,
        "interval",
        seconds=PLUG_STATUS_INTERVAL.total_seconds(),
        next_run_time=now,
        id="refresh_plug_status",
    )
    scheduler.add_job(
        jobs.check_price_notices,
        "cron",
        hour=NOTICE_CHECK_TIME.hour,
        minute=NOTICE_CHECK_TIME.minute,
        id="check_price_notices",
    )
    scheduler.add_job(
        jobs.check_price_notices, "date", run_date=now + timedelta(seconds=30), id="check_price_notices_on_startup"
    )
    # スケジュールの境界（:00 / :30）の直後に実行されるよう、毎分 5 秒に実行する
    scheduler.add_job(jobs.run_control_cycle, "cron", second=5, id="run_control_cycle")
    scheduler.add_job(jobs.summarize_daily, "cron", hour=0, minute=5, id="summarize_daily")
    scheduler.add_job(jobs.summarize_daily, "date", run_date=now + timedelta(seconds=45), id="summarize_daily_on_startup")
    scheduler.add_job(jobs.run_maintenance, "cron", hour=3, minute=30, id="run_maintenance")
    return scheduler

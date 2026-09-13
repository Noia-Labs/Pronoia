from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from .. import db
from . import service

log = logging.getLogger(__name__)


def _parse(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def process_once() -> int:
    now = dt.datetime.now(dt.timezone.utc).astimezone()
    processed = 0
    for run in db.list_prospective_runs(limit=500):
        try:
            if run["status"] == "scheduled" and _parse(run["capture_at"]) <= now:
                asyncio.run(service.capture_run(run["id"]))
                processed += 1
            elif (
                run["status"] == "failed"
                and str(run.get("error_message") or "").startswith("公告数据源抓取失败")
                and _parse(run["updated_at"]) <= now - dt.timedelta(minutes=5)
            ):
                asyncio.run(service.capture_run(run["id"]))
                processed += 1
            refreshed = db.get_prospective_run(run["id"])
            # Older versions incorrectly left empty captures in waiting state.
            if refreshed and refreshed.get("status") == "waiting" and int(refreshed.get("total_items") or 0) == 0:
                db.update_prospective_run(
                    run["id"],
                    status="failed",
                    error_message="抓取完成，但没有找到符合条件的事件",
                )
                continue
            check_at = (refreshed or {}).get("next_check_at") or (refreshed or {}).get("settle_at")
            if refreshed and refreshed.get("status") in {"waiting", "partial"} and check_at and _parse(check_at) <= now:
                service.settle_run(run["id"])
                processed += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("prospective run %s failed", run.get("id"))
            db.update_prospective_run(run["id"], status="failed", error_message=f"worker: {type(exc).__name__}: {exc}")
    return processed


def run_forever(interval_seconds: int = 30) -> None:
    while True:
        process_once()
        time.sleep(max(5, int(interval_seconds)))

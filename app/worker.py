"""Serial background worker (concurrency=1) + Telegram long-polling runner."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
from typing import Any

from app.config import Settings, get_settings
from app.db import db_session, init_db, utc_now_iso
from app.services.scheduler import (
    claim_due_publish_jobs,
    claim_next_pending_job,
    dispatch_publish_job,
    list_reconciling_jobs,
    plan_autopilot_jobs,
    process_render_job,
    reconcile_job,
)
from app.services.temp_manager import count_active_temp_dirs, sweep_orphan_temp_dirs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tiktok-worker")


def record_heartbeat(db_path: str, service_name: str, details: dict[str, Any]) -> None:
    """Persist service heartbeat timestamp and metadata."""
    with db_session(db_path) as conn:
        conn.execute(
            """
            INSERT INTO system_heartbeats (service_name, last_seen_utc, details_json)
            VALUES (?, ?, ?)
            ON CONFLICT(service_name) DO UPDATE SET
                last_seen_utc = excluded.last_seen_utc,
                details_json = excluded.details_json
            """,
            (service_name, utc_now_iso(), json.dumps(details)),
        )


def run_worker_tick(
    settings: Settings,
    notify_callback: Any = None,
) -> dict[str, int]:
    """Execute one deterministic worker tick (plan, render max 1, reconcile, dispatch)."""
    swept = sweep_orphan_temp_dirs(settings.temp_root, settings.temp_orphan_max_age_seconds)
    planned = plan_autopilot_jobs(settings.database_path, settings)

    rendered = 0
    pending_job = claim_next_pending_job(settings.database_path)
    if pending_job:
        try:
            process_render_job(
                pending_job,
                db_path=settings.database_path,
                settings=settings,
                notify_callback=notify_callback,
            )
            rendered = 1
        except Exception as exc:
            logger.error("Render job %s failed: %s", pending_job["id"], exc)

    reconciled = 0
    for rec_job in list_reconciling_jobs(settings.database_path):
        reconcile_job(rec_job, db_path=settings.database_path, settings=settings)
        reconciled += 1

    dispatched = 0
    for pub_job in claim_due_publish_jobs(settings.database_path):
        dispatch_publish_job(int(pub_job["id"]), db_path=settings.database_path, settings=settings)
        dispatched += 1

    record_heartbeat(
        settings.database_path,
        "worker",
        {
            "swept_temp_dirs": swept,
            "active_temp_dirs": count_active_temp_dirs(settings.temp_root),
            "planned": planned,
            "rendered": rendered,
            "reconciled": reconciled,
            "dispatched": dispatched,
        },
    )
    return {
        "swept": swept,
        "planned": planned,
        "rendered": rendered,
        "reconciled": reconciled,
        "dispatched": dispatched,
    }


async def main_loop() -> None:
    """Run worker loop and optional Telegram long-polling bot concurrently."""
    settings = get_settings()
    init_db(settings.database_path)
    sweep_orphan_temp_dirs(settings.temp_root, max_age_seconds=0)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    telegram_app = None
    notify_cb = None
    if settings.telegram_bot_token:
        from app.telegram_bot import build_telegram_application, send_job_ready_notification

        telegram_app = build_telegram_application(settings)
        await telegram_app.initialize()
        await telegram_app.start()
        if telegram_app.updater:
            await telegram_app.updater.start_polling(drop_pending_updates=True)

        def _notify(job_row: dict[str, Any]) -> None:
            asyncio.run_coroutine_threadsafe(
                send_job_ready_notification(telegram_app, settings, job_row),
                loop,
            )

        notify_cb = _notify
        logger.info("Telegram bot polling started")

    logger.info("Worker started (poll=%ss, dry_run=%s)", settings.worker_poll_seconds, settings.dry_run)
    try:
        while not stop_event.is_set():
            await asyncio.to_thread(run_worker_tick, settings, notify_cb)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(3, settings.worker_poll_seconds))
            except asyncio.TimeoutError:
                pass
    finally:
        if telegram_app is not None:
            if telegram_app.updater:
                await telegram_app.updater.stop()
            await telegram_app.stop()
            await telegram_app.shutdown()


if __name__ == "__main__":
    asyncio.run(main_loop())

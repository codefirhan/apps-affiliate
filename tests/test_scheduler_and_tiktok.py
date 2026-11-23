"""Tests for timezone conversion, idempotent scheduling, isolation, and reconciliation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import Settings
from app.db import db_session, init_db, jakarta_to_utc_iso, utc_now_iso, utc_to_jakarta_str
from app.security import TokenCipher
from app.services.scheduler import (
    claim_due_publish_jobs,
    dispatch_publish_job,
    list_reconciling_jobs,
    plan_autopilot_jobs,
    reconcile_job,
)
from app.services.tiktok_connector import TikTokConnector


def _seed_account(db_path: str, settings: Settings, open_id: str, name: str) -> int:
    cipher = TokenCipher(settings.app_secret_key)
    now = utc_now_iso()
    with db_session(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO tiktok_accounts (
                display_name, open_id, publish_mode, status,
                access_token_enc, refresh_token_enc, scopes,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, 'draft_only', 'connected', ?, ?, 'video.upload', ?, ?)
            """,
            (
                name,
                open_id,
                cipher.encrypt(f"secret_tok_{open_id}"),
                cipher.encrypt(f"secret_ref_{open_id}"),
                now,
                now,
            ),
        )
        return int(cur.lastrowid)


def test_jakarta_utc_roundtrip() -> None:
    utc_iso = jakarta_to_utc_iso("2026-11-23T19:30", "Asia/Jakarta")
    assert utc_iso == "2026-11-23T12:30:00Z"
    assert utc_to_jakarta_str(utc_iso, "Asia/Jakarta") == "2026-11-23 19:30"


def test_autopilot_planner_and_atomic_claim_idempotency(tmp_path: Path) -> None:
    db_file = str(tmp_path / "sched.db")
    settings = Settings(database_path=db_file, temp_root=str(tmp_path / "tmp"), dry_run=True)
    init_db(db_file)
    acc_id = _seed_account(db_file, settings, "open_a", "Akun A")

    with db_session(db_file) as conn:
        conn.execute(
            """
            INSERT INTO campaigns (
                account_id, name, mode, niche, keywords, language,
                schedule_times_jakarta, is_active, auto_pilot, created_at_utc
            ) VALUES (?, 'Camp 1', 'faceless_ai', 'ai', 'tips', 'id', ?, 1, 1, ?)
            """,
            (acc_id, json.dumps(["12:00", "19:00"]), utc_now_iso()),
        )

    fixed_now = datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)  # 09:00 WIB
    first_run = plan_autopilot_jobs(db_file, settings, now_utc=fixed_now)
    second_run = plan_autopilot_jobs(db_file, settings, now_utc=fixed_now)

    assert first_run == 4  # today 12:00, 19:00 + tomorrow 12:00, 19:00 WIB
    assert second_run == 0  # idempotent on duplicate tick

    # Mark one job scheduled in the past and verify atomic claim runs once.
    with db_session(db_file) as conn:
        conn.execute(
            "UPDATE content_jobs SET status = 'scheduled', scheduled_at_utc = '2026-11-23T01:00:00Z' WHERE id = 1"
        )

    claimed_1 = claim_due_publish_jobs(db_file, now_utc=fixed_now)
    claimed_2 = claim_due_publish_jobs(db_file, now_utc=fixed_now)
    assert len(claimed_1) == 1
    assert claimed_1[0]["id"] == 1
    assert claimed_2 == []


def test_ambiguous_timeout_reconciles_without_blind_retry_and_isolates_accounts(tmp_path: Path) -> None:
    db_file = str(tmp_path / "reconcile.db")
    settings = Settings(
        database_path=db_file,
        temp_root=str(tmp_path / "tmp"),
        dry_run=False,
        r2_auto_delete_after_publish=True,
    )
    init_db(db_file)
    acc1 = _seed_account(db_file, settings, "open_1", "Akun Satu")
    acc2 = _seed_account(db_file, settings, "open_2", "Akun Dua")
    now = utc_now_iso()

    with db_session(db_file) as conn:
        conn.execute(
            """
            INSERT INTO content_jobs (
                account_id, idempotency_key, mode, caption, r2_object_key, r2_video_url,
                status, scheduled_at_utc, timezone, tiktok_publish_id, created_at_utc, updated_at_utc
            ) VALUES (?, 'k1', 'faceless_ai', 'Job 1', 'jobs/1.mp4', 'https://media.example.com/1.mp4',
                      'queued', ?, 'Asia/Jakarta', 'pub_timeout_1', ?, ?)
            """,
            (acc1, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO content_jobs (
                account_id, idempotency_key, mode, caption, r2_object_key, r2_video_url,
                status, scheduled_at_utc, timezone, created_at_utc, updated_at_utc
            ) VALUES (?, 'k2', 'faceless_ai', 'Job 2', 'jobs/2.mp4', 'https://media.example.com/2.mp4',
                      'queued', ?, 'Asia/Jakarta', ?, ?)
            """,
            (acc2, now, now, now),
        )

    def timeout_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Timed out waiting for TikTok response", request=request)

    def success_transport(request: httpx.Request) -> httpx.Response:
        if "status/fetch" in str(request.url):
            return httpx.Response(200, json={"error": {"code": "ok"}, "data": {"status": "SEND_TO_USER_INBOX"}})
        return httpx.Response(200, json={"error": {"code": "ok"}, "data": {"publish_id": "pub_ok_2"}})

    connector = TikTokConnector(settings)
    with httpx.Client(transport=httpx.MockTransport(timeout_transport)) as client_timeout:
        res1 = dispatch_publish_job(1, db_file, settings, connector=connector, http_client=client_timeout)
    assert res1 == "reconciling"

    # Job 2 on Account 2 still publishes independently.
    with httpx.Client(transport=httpx.MockTransport(success_transport)) as client_ok:
        res2 = dispatch_publish_job(2, db_file, settings, connector=connector, http_client=client_ok)
    assert res2 == "published"

    # Ensure Job 1 is NOT claimed by due publish dispatcher while reconciling.
    assert claim_due_publish_jobs(db_file) == []
    reconciling = list_reconciling_jobs(db_file)
    assert len(reconciling) == 1

    # Reconciliation checks status/fetch and marks published when TikTok confirms.
    with httpx.Client(transport=httpx.MockTransport(success_transport)) as client_ok:
        rec_outcome = reconcile_job(reconciling[0], db_file, settings, connector=connector, http_client=client_ok)
    assert rec_outcome == "published"


def test_auth_401_isolates_failed_account(tmp_path: Path) -> None:
    db_file = str(tmp_path / "auth_iso.db")
    settings = Settings(database_path=db_file, temp_root=str(tmp_path / "tmp"), dry_run=False)
    init_db(db_file)
    acc1 = _seed_account(db_file, settings, "open_bad", "Akun Bad")
    acc2 = _seed_account(db_file, settings, "open_good", "Akun Good")
    now = utc_now_iso()

    with db_session(db_file) as conn:
        conn.execute(
            """
            INSERT INTO content_jobs (
                account_id, idempotency_key, mode, r2_video_url, status,
                scheduled_at_utc, timezone, created_at_utc, updated_at_utc
            ) VALUES (?, 'bad1', 'faceless_ai', 'https://media.example.com/a.mp4', 'queued', ?, 'Asia/Jakarta', ?, ?)
            """,
            (acc1, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO content_jobs (
                account_id, idempotency_key, mode, r2_video_url, status,
                scheduled_at_utc, timezone, created_at_utc, updated_at_utc
            ) VALUES (?, 'good1', 'faceless_ai', 'https://media.example.com/b.mp4', 'queued', ?, 'Asia/Jakarta', ?, ?)
            """,
            (acc2, now, now, now),
        )

    connector = TikTokConnector(settings)
    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(401, json={"error": {"code": "access_token_invalid"}}))) as c401:
        assert dispatch_publish_job(1, db_file, settings, connector=connector, http_client=c401) == "needs_reconnect"

    with httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"error": {"code": "ok"}, "data": {"publish_id": "pub_good"}}))) as c200:
        assert dispatch_publish_job(2, db_file, settings, connector=connector, http_client=c200) == "published"

    with db_session(db_file) as conn:
        row1 = conn.execute("SELECT status FROM tiktok_accounts WHERE id = ?", (acc1,)).fetchone()
        row2 = conn.execute("SELECT status FROM tiktok_accounts WHERE id = ?", (acc2,)).fetchone()
        job1 = conn.execute("SELECT last_error FROM content_jobs WHERE id = 1").fetchone()
    assert row1["status"] == "needs_reconnect"
    assert row2["status"] == "connected"
    assert "secret_tok_open_bad" not in job1["last_error"]

"""End-to-end FastAPI dashboard tests and FFmpeg pipeline smoke tests."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import db_session, init_db
from app.main import create_app
from app.services.scheduler import claim_next_pending_job, process_render_job
from app.services.temp_manager import count_active_temp_dirs


def test_dashboard_oauth_campaigns_and_job_preview(tmp_path: Path) -> None:
    db_file = str(tmp_path / "web.db")
    temp_root = str(tmp_path / "tmp")
    settings = Settings(database_path=db_file, temp_root=temp_root, dry_run=True)
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        health = client.get("/health").json()
        assert health["status"] == "ok"
        assert health["active_temp_dirs"] == 0

        # Connect demo account via simulated OAuth PKCE callback.
        oauth_resp = client.get(
            "/auth/tiktok/start?display_name=Akun+Utama&publish_mode=draft_only",
            follow_redirects=True,
        )
        assert oauth_resp.status_code == 200
        assert "Akun Utama" in oauth_resp.text

        with db_session(db_file) as conn:
            acc = conn.execute("SELECT * FROM tiktok_accounts").fetchone()
            assert acc is not None
            assert acc["access_token_enc"] != "dryrun_access_token"

        # Create campaign.
        camp_resp = client.post(
            "/campaigns",
            data={
                "account_id": str(acc["id"]),
                "name": "Clip Podcast Tech",
                "mode": "youtube_clipper",
                "niche": "AI",
                "keywords": "startup",
                "language": "en",
                "translate_to_language": "id",
                "tts_voice": "id-ID-ArdiNeural",
                "schedule_slots": "12:00,19:00",
                "auto_pilot": "1",
            },
            follow_redirects=True,
        )
        assert camp_resp.status_code == 200
        assert "Clip Podcast Tech" in camp_resp.text

        # Trigger manual run-now job.
        run_resp = client.post("/campaigns/1/run-now", data={"source_url": ""}, follow_redirects=True)
        assert run_resp.status_code == 200
        assert "Preview Job #1" in run_resp.text

        # Edit job metadata & schedule in Asia/Jakarta.
        upd_resp = client.post(
            "/jobs/1/update",
            data={
                "title_overlay": "Judul Baru Overlay",
                "caption": "Caption edited",
                "hashtags": "#fyp #ai",
                "scheduled_at_jakarta": "2026-11-24T20:00",
            },
            follow_redirects=True,
        )
        assert upd_resp.status_code == 200
        assert "Judul Baru Overlay" in upd_resp.text

        # Cancel job.
        cancel_resp = client.post("/jobs/1/cancel", follow_redirects=True)
        assert cancel_resp.status_code == 200
        assert "cancelled" in cancel_resp.text


def test_ffmpeg_pipeline_smoke_renders_and_leaves_zero_local_files(tmp_path: Path) -> None:
    db_file = str(tmp_path / "pipe.db")
    temp_root = str(tmp_path / "tmp")
    settings = Settings(database_path=db_file, temp_root=temp_root, dry_run=True)
    init_db(db_file)

    with db_session(db_file) as conn:
        conn.execute(
            """
            INSERT INTO tiktok_accounts (
                display_name, open_id, publish_mode, status,
                created_at_utc, updated_at_utc
            ) VALUES ('Pipe Acc', 'open_pipe', 'draft_only', 'connected', '2026-11-23T00:00:00Z', '2026-11-23T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO campaigns (
                account_id, name, mode, niche, keywords, language,
                translate_to_language, tts_voice, schedule_times_jakarta,
                is_active, auto_pilot, created_at_utc
            ) VALUES
                (1, 'Faceless ID', 'faceless_ai', 'keuangan', 'investasi', 'id', NULL, 'id-ID-ArdiNeural', '["12:00"]', 1, 0, '2026-11-23T00:00:00Z'),
                (1, 'Clipper EN->ID', 'youtube_clipper', 'ai', 'llm', 'en', 'id', 'id-ID-ArdiNeural', '["19:00"]', 1, 0, '2026-11-23T00:00:00Z')
            """
        )
        conn.execute(
            """
            INSERT INTO content_jobs (
                campaign_id, account_id, idempotency_key, mode, status,
                scheduled_at_utc, timezone, created_at_utc, updated_at_utc
            ) VALUES
                (1, 1, 'pipe_face', 'faceless_ai', 'pending_research', '2026-11-23T05:00:00Z', 'Asia/Jakarta', '2026-11-23T00:00:00Z', '2026-11-23T00:00:00Z'),
                (2, 1, 'pipe_clip', 'youtube_clipper', 'pending_research', '2026-11-23T12:00:00Z', 'Asia/Jakarta', '2026-11-23T00:00:00Z', '2026-11-23T00:00:00Z')
            """
        )

    notifications: list[int] = []
    for _ in range(2):
        job = claim_next_pending_job(db_file)
        assert job is not None
        result = process_render_job(
            job,
            db_path=db_file,
            settings=settings,
            notify_callback=lambda row: notifications.append(int(row["id"])),
        )
        assert result["status"] == "scheduled"
        assert result["r2_video_url"].startswith("https://")
        assert result["thumbnail_url"].startswith("https://")
        # Crucial guarantee: zero persistent local media files remain after each job.
        assert count_active_temp_dirs(temp_root) == 0

    assert notifications == [1, 2]

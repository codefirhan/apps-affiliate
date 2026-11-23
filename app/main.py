"""FastAPI Web Dashboard, OAuth callbacks, R2 preview, and health endpoint."""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.db import (
    db_session,
    init_db,
    jakarta_to_utc_iso,
    parse_utc_iso,
    utc_now,
    utc_now_iso,
    utc_to_jakarta_str,
)
from app.services.storage import CloudflareR2Service
from app.services.temp_manager import count_active_temp_dirs
from app.services.tiktok_connector import TikTokConnector, generate_pkce_pair

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def create_app(settings_override: Settings | None = None) -> FastAPI:
    """Create configured FastAPI application instance."""
    settings = settings_override or get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(settings.database_path)
        yield

    application = FastAPI(title=settings.app_name, lifespan=lifespan)
    application.state.settings = settings
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["to_jakarta"] = lambda val: utc_to_jakarta_str(val, settings.timezone)

    def _get_stats() -> dict[str, Any]:
        with db_session(settings.database_path) as conn:
            acc_count = conn.execute(
                "SELECT COUNT(*) AS c FROM tiktok_accounts WHERE status = 'connected'"
            ).fetchone()["c"]
            job_counts = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('pending_research', 'rendering') THEN 1 ELSE 0 END) AS rendering,
                    SUM(CASE WHEN status IN ('scheduled', 'queued', 'retry_wait') THEN 1 ELSE 0 END) AS scheduled,
                    SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END) AS published,
                    SUM(CASE WHEN status IN ('reconciling', 'needs_reconnect', 'failed_permanent') THEN 1 ELSE 0 END) AS attention
                FROM content_jobs
                """
            ).fetchone()
            hb = conn.execute(
                "SELECT * FROM system_heartbeats WHERE service_name = 'worker'"
            ).fetchone()
        return {
            "connected_accounts": acc_count or 0,
            "rendering": (job_counts["rendering"] or 0) if job_counts else 0,
            "scheduled": (job_counts["scheduled"] or 0) if job_counts else 0,
            "published": (job_counts["published"] or 0) if job_counts else 0,
            "attention": (job_counts["attention"] or 0) if job_counts else 0,
            "active_temp_dirs": count_active_temp_dirs(settings.temp_root),
            "worker_last_seen": hb["last_seen_utc"] if hb else None,
        }

    def _list_jobs(limit: int = 30) -> list[dict[str, Any]]:
        with db_session(settings.database_path) as conn:
            rows = conn.execute(
                """
                SELECT j.*, a.display_name AS account_name, a.publish_mode AS account_publish_mode,
                       c.name AS campaign_name
                FROM content_jobs j
                LEFT JOIN tiktok_accounts a ON a.id = j.account_id
                LEFT JOIN campaigns c ON c.id = j.campaign_id
                ORDER BY j.id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    @application.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        with db_session(settings.database_path) as conn:
            campaigns = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, name, mode, language FROM campaigns WHERE is_active = 1 ORDER BY id DESC"
                ).fetchall()
            ]
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "settings": settings,
                "stats": _get_stats(),
                "jobs": _list_jobs(),
                "campaigns": campaigns,
                "active_tab": "dashboard",
            },
        )

    @application.get("/partials/jobs", response_class=HTMLResponse)
    async def partial_jobs(request: Request):
        return templates.TemplateResponse(
            request,
            "_jobs_table.html",
            {"jobs": _list_jobs()},
        )

    @application.get("/accounts", response_class=HTMLResponse)
    async def accounts_page(request: Request):
        connector = TikTokConnector(settings)
        with db_session(settings.database_path) as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT id, display_name, open_id, publish_mode, status, scopes, expires_at_utc, updated_at_utc
                    FROM tiktok_accounts ORDER BY id DESC
                    """
                ).fetchall()
            ]
        for row in rows:
            row["capability"] = connector.capabilities(row["publish_mode"])
        return templates.TemplateResponse(
            request,
            "accounts.html",
            {
                "settings": settings,
                "accounts": rows,
                "active_tab": "accounts",
            },
        )

    @application.get("/auth/tiktok/start")
    async def tiktok_auth_start(
        display_name: str = "Akun TikTok",
        publish_mode: str = "draft_only",
    ):
        mode = publish_mode if publish_mode in ("draft_only", "direct_post") else "draft_only"
        state = secrets.token_urlsafe(24)
        verifier, _ = generate_pkce_pair()
        expires = (utc_now() + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")

        with db_session(settings.database_path) as conn:
            conn.execute(
                """
                INSERT INTO oauth_states (state, code_verifier, account_label, publish_mode, expires_at_utc)
                VALUES (?, ?, ?, ?, ?)
                """,
                (state, verifier, display_name.strip() or "Akun TikTok", mode, expires),
            )

        if settings.dry_run or not settings.tiktok_client_key:
            return RedirectResponse(
                url=f"/auth/tiktok/callback?code=dryrun_auth_code&state={state}",
                status_code=302,
            )

        connector = TikTokConnector(settings)
        auth_url = connector.build_authorize_url(state=state, code_verifier=verifier, publish_mode=mode)
        return RedirectResponse(url=auth_url, status_code=302)

    @application.get("/auth/tiktok/callback")
    async def tiktok_auth_callback(code: str = "", state: str = "", error: str = ""):
        if error:
            raise HTTPException(status_code=400, detail=f"TikTok OAuth declined: {error}")
        if not code or not state:
            raise HTTPException(status_code=400, detail="Missing OAuth code or state")

        with db_session(settings.database_path) as conn:
            row = conn.execute("SELECT * FROM oauth_states WHERE state = ?", (state,)).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
            conn.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
            if parse_utc_iso(row["expires_at_utc"]) < utc_now():
                raise HTTPException(status_code=400, detail="OAuth state expired")

        connector = TikTokConnector(settings)
        token_bundle = connector.exchange_code(code=code, code_verifier=row["code_verifier"])
        now_iso = utc_now_iso()
        expires_at = (
            utc_now() + timedelta(seconds=int(token_bundle.get("expires_in") or 86400))
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        label = row["account_label"] or token_bundle["display_name"]

        with db_session(settings.database_path) as conn:
            conn.execute(
                """
                INSERT INTO tiktok_accounts (
                    display_name, open_id, publish_mode, status,
                    access_token_enc, refresh_token_enc, scopes,
                    expires_at_utc, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, 'connected', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(open_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    publish_mode = excluded.publish_mode,
                    status = 'connected',
                    access_token_enc = excluded.access_token_enc,
                    refresh_token_enc = excluded.refresh_token_enc,
                    scopes = excluded.scopes,
                    expires_at_utc = excluded.expires_at_utc,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    label,
                    token_bundle["open_id"],
                    row["publish_mode"],
                    token_bundle["access_token_enc"],
                    token_bundle["refresh_token_enc"],
                    token_bundle["scopes"],
                    expires_at,
                    now_iso,
                    now_iso,
                ),
            )
        return RedirectResponse(url="/accounts", status_code=303)

    @application.post("/accounts/{account_id}/mode")
    async def toggle_account_mode(account_id: int, publish_mode: str = Form(...)):
        if publish_mode not in ("draft_only", "direct_post"):
            raise HTTPException(status_code=400, detail="Invalid publish_mode")
        with db_session(settings.database_path) as conn:
            conn.execute(
                "UPDATE tiktok_accounts SET publish_mode = ?, updated_at_utc = ? WHERE id = ?",
                (publish_mode, utc_now_iso(), account_id),
            )
        return RedirectResponse(url="/accounts", status_code=303)

    @application.post("/accounts/{account_id}/delete")
    async def delete_account(account_id: int):
        with db_session(settings.database_path) as conn:
            conn.execute("DELETE FROM tiktok_accounts WHERE id = ?", (account_id,))
        return RedirectResponse(url="/accounts", status_code=303)

    @application.get("/campaigns", response_class=HTMLResponse)
    async def campaigns_page(request: Request):
        with db_session(settings.database_path) as conn:
            accounts = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, display_name, publish_mode, status FROM tiktok_accounts ORDER BY id DESC"
                ).fetchall()
            ]
            campaigns = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT c.*, a.display_name AS account_name
                    FROM campaigns c
                    LEFT JOIN tiktok_accounts a ON a.id = c.account_id
                    ORDER BY c.id DESC
                    """
                ).fetchall()
            ]
        return templates.TemplateResponse(
            request,
            "campaigns.html",
            {
                "settings": settings,
                "accounts": accounts,
                "campaigns": campaigns,
                "active_tab": "campaigns",
            },
        )

    @application.post("/campaigns")
    async def create_campaign(
        account_id: int = Form(...),
        name: str = Form(...),
        mode: str = Form(...),
        niche: str = Form(...),
        keywords: str = Form(""),
        language: str = Form("id"),
        translate_to_language: str = Form(""),
        tts_voice: str = Form("id-ID-ArdiNeural"),
        schedule_slots: str = Form("12:00,19:00"),
        auto_pilot: str = Form("1"),
    ):
        if mode not in ("youtube_clipper", "faceless_ai"):
            raise HTTPException(status_code=400, detail="Invalid campaign mode")
        if language not in ("id", "en"):
            raise HTTPException(status_code=400, detail="Invalid language")
        slots = [s.strip() for s in schedule_slots.split(",") if ":" in s]
        if not slots:
            slots = ["12:00", "19:00"]
        translate_val = translate_to_language.strip() if translate_to_language.strip() in ("id", "en") else None
        is_autopilot = 1 if auto_pilot in ("1", "true", "on") else 0

        with db_session(settings.database_path) as conn:
            acc = conn.execute("SELECT id FROM tiktok_accounts WHERE id = ?", (account_id,)).fetchone()
            if not acc:
                raise HTTPException(status_code=404, detail="TikTok account not found")
            conn.execute(
                """
                INSERT INTO campaigns (
                    account_id, name, mode, niche, keywords, language,
                    translate_to_language, tts_voice, schedule_times_jakarta,
                    is_active, auto_pilot, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    account_id,
                    name.strip(),
                    mode,
                    niche.strip(),
                    keywords.strip(),
                    language,
                    translate_val,
                    tts_voice.strip() or "id-ID-ArdiNeural",
                    json.dumps(slots),
                    is_autopilot,
                    utc_now_iso(),
                ),
            )
        return RedirectResponse(url="/campaigns", status_code=303)

    @application.post("/campaigns/{campaign_id}/toggle")
    async def toggle_campaign(campaign_id: int):
        with db_session(settings.database_path) as conn:
            conn.execute(
                "UPDATE campaigns SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?",
                (campaign_id,),
            )
        return RedirectResponse(url="/campaigns", status_code=303)

    @application.post("/campaigns/{campaign_id}/run-now")
    async def run_campaign_now(campaign_id: int, source_url: str = Form("")):
        with db_session(settings.database_path) as conn:
            camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if not camp:
                raise HTTPException(status_code=404, detail="Campaign not found")
            now_iso = utc_now_iso()
            sched_iso = (utc_now() + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            idem = f"manual_{campaign_id}_{secrets.token_hex(6)}"
            cur = conn.execute(
                """
                INSERT INTO content_jobs (
                    campaign_id, account_id, idempotency_key, mode, source_url,
                    status, scheduled_at_utc, timezone, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, 'pending_research', ?, ?, ?, ?)
                """,
                (
                    camp["id"],
                    camp["account_id"],
                    idem,
                    camp["mode"],
                    source_url.strip(),
                    sched_iso,
                    settings.timezone,
                    now_iso,
                    now_iso,
                ),
            )
            job_id = cur.lastrowid
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @application.get("/jobs/{job_id}", response_class=HTMLResponse)
    async def job_preview(request: Request, job_id: int):
        with db_session(settings.database_path) as conn:
            job = conn.execute(
                """
                SELECT j.*, a.display_name AS account_name, a.publish_mode, c.name AS campaign_name
                FROM content_jobs j
                LEFT JOIN tiktok_accounts a ON a.id = j.account_id
                LEFT JOIN campaigns c ON c.id = j.campaign_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            logs = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM job_logs WHERE job_id = ? ORDER BY id DESC LIMIT 30",
                    (job_id,),
                ).fetchall()
            ]
        return templates.TemplateResponse(
            request,
            "job_preview.html",
            {
                "settings": settings,
                "job": dict(job),
                "logs": logs,
                "sched_jakarta": utc_to_jakarta_str(job["scheduled_at_utc"], settings.timezone).replace(" ", "T"),
                "active_tab": "dashboard",
            },
        )

    @application.post("/jobs/{job_id}/update")
    async def update_job(
        job_id: int,
        title_overlay: str = Form(""),
        caption: str = Form(""),
        hashtags: str = Form(""),
        scheduled_at_jakarta: str = Form(""),
    ):
        with db_session(settings.database_path) as conn:
            job = conn.execute("SELECT * FROM content_jobs WHERE id = ?", (job_id,)).fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            new_sched_utc = (
                jakarta_to_utc_iso(scheduled_at_jakarta, settings.timezone)
                if scheduled_at_jakarta.strip()
                else job["scheduled_at_utc"]
            )
            conn.execute(
                """
                UPDATE content_jobs
                SET title_overlay = ?, caption = ?, hashtags = ?, scheduled_at_utc = ?, updated_at_utc = ?
                WHERE id = ?
                """,
                (
                    title_overlay.strip(),
                    caption.strip(),
                    hashtags.strip(),
                    new_sched_utc,
                    utc_now_iso(),
                    job_id,
                ),
            )
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @application.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: int):
        with db_session(settings.database_path) as conn:
            job = conn.execute("SELECT * FROM content_jobs WHERE id = ?", (job_id,)).fetchone()
            if not job:
                raise HTTPException(status_code=404, detail="Job not found")
            conn.execute(
                """
                UPDATE content_jobs
                SET status = 'cancelled', updated_at_utc = ?
                WHERE id = ? AND status IN ('pending_research', 'scheduled', 'retry_wait', 'queued')
                """,
                (utc_now_iso(), job_id),
            )
            if job["r2_object_key"]:
                CloudflareR2Service(settings).delete_object(job["r2_object_key"])
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    @application.get("/terms", response_class=HTMLResponse)
    async def terms_page():
        return HTMLResponse(
            "<html><head><title>Terms of Service - TikTok Auto-Pilot Bot</title></head>"
            "<body style='font-family:sans-serif;max-width:720px;margin:40px auto;padding:0 16px'>"
            "<h1>Terms of Service</h1>"
            "<p>This application automates content creation and scheduled publishing for authorized TikTok accounts via the official TikTok Content Posting API.</p>"
            "</body></html>"
        )

    @application.get("/privacy", response_class=HTMLResponse)
    async def privacy_page():
        return HTMLResponse(
            "<html><head><title>Privacy Policy - TikTok Auto-Pilot Bot</title></head>"
            "<body style='font-family:sans-serif;max-width:720px;margin:40px auto;padding:0 16px'>"
            "<h1>Privacy Policy</h1>"
            "<p>OAuth tokens are encrypted at rest and used solely to publish user-scheduled videos to their connected TikTok account. Temporary media files are deleted after processing.</p>"
            "</body></html>"
        )

    @application.get("/health")
    async def health():
        with db_session(settings.database_path) as conn:
            conn.execute("SELECT 1").fetchone()
            hb = conn.execute(
                "SELECT * FROM system_heartbeats WHERE service_name = 'worker'"
            ).fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) AS c FROM content_jobs WHERE status = 'pending_research'"
            ).fetchone()["c"]
            scheduled = conn.execute(
                "SELECT COUNT(*) AS c FROM content_jobs WHERE status = 'scheduled'"
            ).fetchone()["c"]

        worker_stale = True
        worker_last = None
        if hb:
            worker_last = hb["last_seen_utc"]
            age = (utc_now() - parse_utc_iso(worker_last)).total_seconds()
            worker_stale = age > max(120, settings.worker_poll_seconds * 4)

        return JSONResponse(
            {
                "status": "ok",
                "db": "connected",
                "dry_run": settings.dry_run,
                "worker_last_seen_utc": worker_last,
                "worker_stale": worker_stale,
                "active_temp_dirs": count_active_temp_dirs(settings.temp_root),
                "pending_jobs": pending,
                "scheduled_jobs": scheduled,
            }
        )

    return application


app = create_app()

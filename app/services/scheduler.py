"""Reliable scheduler, atomic job claiming, dual-mode video pipeline, and reconciliation."""

from __future__ import annotations

import hashlib
import json
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import db_session, parse_utc_iso, slot_jakarta_to_utc_iso, utc_now, utc_now_iso
from app.security import redact_secrets
from app.services.ai_service import AIService
from app.services.research_service import ResearchService
from app.services.storage import CloudflareImagesService, CloudflareR2Service
from app.services.temp_manager import isolated_job_dir
from app.services.tiktok_connector import TikTokConnector
from app.services.tts_service import DEFAULT_VOICES, TTSService
from app.services.video_engine import VideoEngine, generate_tiktok_ass_subtitles

logger = logging.getLogger(__name__)


def append_job_log(db_path: str, job_id: int, level: str, message: str, secrets_list: list[str] = ()) -> None:
    """Write a redacted audit log line for a content job."""
    safe_msg = redact_secrets(message, secrets_list)
    with db_session(db_path) as conn:
        conn.execute(
            "INSERT INTO job_logs (job_id, level, message, created_at_utc) VALUES (?, ?, ?, ?)",
            (job_id, level.upper(), safe_msg, utc_now_iso()),
        )


def compute_idempotency_key(campaign_id: int | None, slot_utc_iso: str) -> str:
    """Build deterministic idempotency key for a campaign schedule slot."""
    raw = f"campaign:{campaign_id}:{slot_utc_iso}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def plan_autopilot_jobs(
    db_path: str,
    settings: Settings,
    now_utc: datetime | None = None,
) -> int:
    """Create upcoming pending_research jobs for active auto_pilot campaigns idempotently."""
    ref_utc = now_utc or utc_now()
    tz = ZoneInfo(settings.timezone)
    today_local = ref_utc.astimezone(tz)
    created_count = 0

    with db_session(db_path) as conn:
        campaigns = conn.execute(
            """
            SELECT c.*, a.status AS account_status
            FROM campaigns c
            JOIN tiktok_accounts a ON a.id = c.account_id
            WHERE c.is_active = 1 AND c.auto_pilot = 1 AND a.status = 'connected'
            """
        ).fetchall()

        now_iso = ref_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        for camp in campaigns:
            try:
                slots = json.loads(camp["schedule_times_jakarta"] or "[]")
            except json.JSONDecodeError:
                slots = []
            for day_offset in (0, 1):
                target_day = today_local + timedelta(days=day_offset)
                for hhmm in slots:
                    try:
                        slot_utc = slot_jakarta_to_utc_iso(target_day, str(hhmm), settings.timezone)
                    except Exception:
                        continue
                    if parse_utc_iso(slot_utc) <= ref_utc:
                        continue
                    idem = compute_idempotency_key(camp["id"], slot_utc)
                    cur = conn.execute(
                        """
                        INSERT OR IGNORE INTO content_jobs (
                            campaign_id, account_id, idempotency_key, mode,
                            status, scheduled_at_utc, timezone,
                            created_at_utc, updated_at_utc
                        ) VALUES (?, ?, ?, ?, 'pending_research', ?, ?, ?, ?)
                        """,
                        (
                            camp["id"],
                            camp["account_id"],
                            idem,
                            camp["mode"],
                            slot_utc,
                            settings.timezone,
                            now_iso,
                            now_iso,
                        ),
                    )
                    if cur.rowcount > 0:
                        created_count += 1
    return created_count


def claim_next_pending_job(db_path: str) -> dict[str, Any] | None:
    """Atomically claim ONE pending_research job (concurrency=1 for 2 GB RAM safety)."""
    with db_session(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT j.*, c.niche, c.keywords, c.language, c.translate_to_language, c.tts_voice
                FROM content_jobs j
                LEFT JOIN campaigns c ON c.id = j.campaign_id
                WHERE j.status = 'pending_research'
                ORDER BY j.scheduled_at_utc ASC, j.id ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                conn.execute("COMMIT")
                return None

            updated = conn.execute(
                """
                UPDATE content_jobs
                SET status = 'rendering', updated_at_utc = ?
                WHERE id = ? AND status = 'pending_research'
                """,
                (utc_now_iso(), row["id"]),
            )
            conn.execute("COMMIT")
            if updated.rowcount == 0:
                return None
            return dict(row)
        except Exception:
            conn.execute("ROLLBACK")
            raise


def claim_due_publish_jobs(db_path: str, now_utc: datetime | None = None) -> list[dict[str, Any]]:
    """Atomically claim scheduled/retry_wait jobs due for publishing, isolated per connected account."""
    ref_iso = (now_utc or utc_now()).strftime("%Y-%m-%dT%H:%M:%SZ")
    claimed: list[dict[str, Any]] = []

    with db_session(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                """
                SELECT j.*
                FROM content_jobs j
                JOIN tiktok_accounts a ON a.id = j.account_id
                WHERE a.status = 'connected'
                  AND (
                    (j.status = 'scheduled' AND j.scheduled_at_utc <= ?)
                    OR (j.status = 'retry_wait' AND j.next_retry_at_utc IS NOT NULL AND j.next_retry_at_utc <= ?)
                  )
                ORDER BY j.scheduled_at_utc ASC, j.id ASC
                """,
                (ref_iso, ref_iso),
            ).fetchall()

            for row in rows:
                res = conn.execute(
                    """
                    UPDATE content_jobs
                    SET status = 'queued', updated_at_utc = ?
                    WHERE id = ? AND status IN ('scheduled', 'retry_wait')
                    """,
                    (ref_iso, row["id"]),
                )
                if res.rowcount == 1:
                    item = dict(row)
                    item["status"] = "queued"
                    claimed.append(item)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return claimed


def list_reconciling_jobs(db_path: str) -> list[dict[str, Any]]:
    """Fetch jobs currently awaiting TikTok status reconciliation."""
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT j.*
            FROM content_jobs j
            JOIN tiktok_accounts a ON a.id = j.account_id
            WHERE j.status = 'reconciling' AND a.status = 'connected'
            ORDER BY j.updated_at_utc ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def process_render_job(
    job: dict[str, Any],
    db_path: str,
    settings: Settings,
    notify_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Execute research -> TTS/transcribe -> FFmpeg render -> R2/CF upload -> delete temp dir."""
    job_id = int(job["id"])
    mode = str(job.get("mode") or "faceless_ai")
    niche = str(job.get("niche") or "edukasi teknologi")
    keywords = str(job.get("keywords") or niche)
    language = str(job.get("language") or "id")
    translate_to = job.get("translate_to_language") or None
    tts_voice = str(job.get("tts_voice") or DEFAULT_VOICES.get(translate_to or language, "id-ID-ArdiNeural"))

    ai_service = AIService(settings)
    research = ResearchService(settings)
    tts = TTSService(settings)
    video_engine = VideoEngine(settings)
    r2 = CloudflareR2Service(settings)
    cf_images = CloudflareImagesService(settings)

    append_job_log(db_path, job_id, "INFO", f"Starting {mode} pipeline in isolated temp workspace")

    try:
        with isolated_job_dir(job_id, settings.temp_root) as work_dir:
            out_mp4 = work_dir / f"final_job_{job_id}.mp4"
            thumb_jpg = work_dir / f"thumb_job_{job_id}.jpg"
            ass_path = work_dir / "subtitles.ass"

            if mode == "youtube_clipper":
                source_url = str(job.get("source_url") or "").strip()
                video_title = ""
                if not source_url:
                    candidates = research.search_viral_youtube(niche, keywords, max_results=5)
                    if not candidates:
                        raise RuntimeError("No YouTube viral candidate videos found")
                    source_url = candidates[0]["url"]
                    video_title = candidates[0].get("title", "")

                transcript = research.fetch_youtube_transcript(source_url, language, work_dir)
                clip_plan = ai_service.select_viral_clip(
                    transcript_segments=transcript,
                    niche=niche,
                    language=language,
                    translate_to=translate_to,
                    video_title=video_title,
                )
                raw_clip = work_dir / "raw_yt_section.mp4"
                video_engine.download_youtube_section(
                    source_url=source_url,
                    start_sec=float(clip_plan["start_sec"]),
                    end_sec=float(clip_plan["end_sec"]),
                    out_raw_path=raw_clip,
                    work_dir=work_dir,
                )

                dub_audio_path: Path | None = None
                target_lang = translate_to or language
                if translate_to and translate_to != language and clip_plan.get("translated_script"):
                    dub_audio_path = work_dir / "dub_audio.mp3"
                    word_timings = tts.synthesize_speech(
                        clip_plan["translated_script"],
                        language=target_lang,
                        voice=tts_voice,
                        out_audio_path=dub_audio_path,
                    )
                    script_text = clip_plan["translated_script"]
                else:
                    extracted_mp3 = work_dir / "clip_audio.mp3"
                    import subprocess

                    subprocess.run(
                        [
                            settings.resolve_ffmpeg_bin(),
                            "-y",
                            "-i",
                            str(raw_clip),
                            "-vn",
                            "-acodec",
                            "libmp3lame",
                            "-b:a",
                            "96k",
                            str(extracted_mp3),
                        ],
                        capture_output=True,
                        check=True,
                        timeout=60,
                    )
                    word_timings = ai_service.transcribe_audio(extracted_mp3, language=target_lang)
                    script_text = " ".join(str(w.get("word", "")) for w in word_timings)

                clip_dur = video_engine.probe_duration(dub_audio_path or raw_clip)
                generate_tiktok_ass_subtitles(
                    word_timings=word_timings,
                    out_ass_path=ass_path,
                    title_overlay=clip_plan["title_overlay"],
                    total_duration=clip_dur,
                )
                video_engine.render_youtube_clip(
                    raw_clip_path=raw_clip,
                    ass_path=ass_path,
                    out_mp4_path=out_mp4,
                    work_dir=work_dir,
                    dub_audio_path=dub_audio_path,
                )
                title_overlay = clip_plan["title_overlay"]
                caption = clip_plan["caption"]
                hashtags = clip_plan["hashtags"]
            else:
                source_url = ""
                plan = ai_service.generate_faceless_script(niche=niche, keywords=keywords, language=language)
                dub_audio_path = work_dir / "faceless_dub.mp3"
                word_timings = tts.synthesize_speech(
                    plan["script"],
                    language=language,
                    voice=tts_voice,
                    out_audio_path=dub_audio_path,
                )
                media_files = research.fetch_stock_media(plan["visual_queries"], work_dir=work_dir)
                audio_dur = video_engine.probe_duration(dub_audio_path)
                generate_tiktok_ass_subtitles(
                    word_timings=word_timings,
                    out_ass_path=ass_path,
                    title_overlay=plan["title_overlay"],
                    total_duration=audio_dur,
                )
                video_engine.render_faceless_video(
                    media_files=media_files,
                    dub_audio_path=dub_audio_path,
                    ass_path=ass_path,
                    out_mp4_path=out_mp4,
                    work_dir=work_dir,
                )
                title_overlay = plan["title_overlay"]
                caption = plan["caption"]
                hashtags = plan["hashtags"]
                script_text = plan["script"]

            video_engine.extract_thumbnail(out_mp4, thumb_jpg)
            r2_key = f"jobs/job_{job_id}_{int(utc_now().timestamp())}.mp4"
            r2_url = r2.upload_video(out_mp4, r2_key)
            cf_img = cf_images.upload_image(thumb_jpg, metadata={"job_id": str(job_id)})

        # Temp directory is guaranteed deleted right here before DB status update completes.
        with db_session(db_path) as conn:
            conn.execute(
                """
                UPDATE content_jobs
                SET source_url = ?, title_overlay = ?, caption = ?, hashtags = ?, script_text = ?,
                    r2_object_key = ?, r2_video_url = ?, cf_image_id = ?, thumbnail_url = ?,
                    status = 'scheduled', last_error = '', updated_at_utc = ?
                WHERE id = ? AND status = 'rendering'
                """,
                (
                    source_url,
                    title_overlay,
                    caption,
                    hashtags,
                    script_text,
                    r2_key,
                    r2_url,
                    cf_img["image_id"],
                    cf_img["url"],
                    utc_now_iso(),
                    job_id,
                ),
            )
            updated_row = dict(conn.execute("SELECT * FROM content_jobs WHERE id = ?", (job_id,)).fetchone())

        append_job_log(db_path, job_id, "INFO", f"Rendered & uploaded to R2 ({r2_key}); local temp cleaned")
        if notify_callback:
            notify_callback(updated_row)
        return updated_row

    except Exception as exc:
        safe_err = redact_secrets(str(exc), settings.configured_secrets())
        with db_session(db_path) as conn:
            conn.execute(
                """
                UPDATE content_jobs
                SET status = 'failed_permanent', last_error = ?, updated_at_utc = ?
                WHERE id = ?
                """,
                (safe_err, utc_now_iso(), job_id),
            )
        append_job_log(db_path, job_id, "ERROR", f"Render failed: {safe_err}", settings.configured_secrets())
        raise


def dispatch_publish_job(
    job_id: int,
    db_path: str,
    settings: Settings,
    connector: TikTokConnector | None = None,
    r2_service: CloudflareR2Service | None = None,
    http_client: Any = None,
) -> str:
    """Publish a queued job to TikTok with atomic lock, backoff retry, and reconciliation guard."""
    connector = connector or TikTokConnector(settings)
    r2_service = r2_service or CloudflareR2Service(settings)

    with db_session(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT * FROM content_jobs WHERE id = ?", (job_id,)).fetchone()
            if not row or row["status"] != "queued":
                conn.execute("COMMIT")
                return row["status"] if row else "missing"

            account = conn.execute(
                "SELECT * FROM tiktok_accounts WHERE id = ?", (row["account_id"],)
            ).fetchone()
            if not account or account["status"] != "connected":
                conn.execute(
                    "UPDATE content_jobs SET status = 'needs_reconnect', updated_at_utc = ? WHERE id = ?",
                    (utc_now_iso(), job_id),
                )
                conn.execute("COMMIT")
                return "needs_reconnect"

            conn.execute(
                "UPDATE content_jobs SET status = 'publishing', updated_at_utc = ? WHERE id = ?",
                (utc_now_iso(), job_id),
            )
            conn.execute("COMMIT")
            job_dict = dict(row)
            account_dict = dict(account)
        except Exception:
            conn.execute("ROLLBACK")
            raise

    outcome = connector.publish_video(account_dict, job_dict, http_client=http_client)
    now_iso = utc_now_iso()

    with db_session(db_path) as conn:
        if outcome.outcome == "published":
            conn.execute(
                """
                UPDATE content_jobs
                SET status = 'published', tiktok_publish_id = ?, last_error = '', updated_at_utc = ?
                WHERE id = ?
                """,
                (outcome.publish_id, now_iso, job_id),
            )
            append_job_log(db_path, job_id, "INFO", f"Published to TikTok (publish_id={outcome.publish_id})")
            if settings.r2_auto_delete_after_publish and job_dict.get("r2_object_key"):
                r2_service.delete_object(str(job_dict["r2_object_key"]))
            return "published"

        if outcome.outcome == "reconciling":
            conn.execute(
                """
                UPDATE content_jobs
                SET status = 'reconciling', tiktok_publish_id = COALESCE(NULLIF(?, ''), tiktok_publish_id),
                    last_error = ?, updated_at_utc = ?
                WHERE id = ?
                """,
                (outcome.publish_id, outcome.error, now_iso, job_id),
            )
            append_job_log(db_path, job_id, "WARN", f"Marked reconciling (no blind retry): {outcome.error}")
            return "reconciling"

        if outcome.outcome == "needs_reconnect":
            conn.execute(
                "UPDATE tiktok_accounts SET status = 'needs_reconnect', updated_at_utc = ? WHERE id = ?",
                (now_iso, account_dict["id"]),
            )
            conn.execute(
                "UPDATE content_jobs SET status = 'needs_reconnect', last_error = ?, updated_at_utc = ? WHERE id = ?",
                (outcome.error, now_iso, job_id),
            )
            append_job_log(db_path, job_id, "ERROR", f"Account requires reconnect: {outcome.error}")
            return "needs_reconnect"

        if outcome.outcome == "retryable":
            attempts = int(job_dict.get("attempts") or 0) + 1
            max_attempts = int(job_dict.get("max_attempts") or 3)
            if attempts < max_attempts:
                backoff_sec = min(1800, (60 * (2 ** (attempts - 1))) + random.randint(5, 25))
                next_retry = (utc_now() + timedelta(seconds=backoff_sec)).strftime("%Y-%m-%dT%H:%M:%SZ")
                conn.execute(
                    """
                    UPDATE content_jobs
                    SET status = 'retry_wait', attempts = ?, next_retry_at_utc = ?,
                        last_error = ?, updated_at_utc = ?
                    WHERE id = ?
                    """,
                    (attempts, next_retry, outcome.error, now_iso, job_id),
                )
                append_job_log(db_path, job_id, "WARN", f"Retry scheduled ({attempts}/{max_attempts}): {outcome.error}")
                return "retry_wait"

            conn.execute(
                """
                UPDATE content_jobs
                SET status = 'failed_permanent', attempts = ?, last_error = ?, updated_at_utc = ?
                WHERE id = ?
                """,
                (attempts, outcome.error, now_iso, job_id),
            )
            append_job_log(db_path, job_id, "ERROR", f"Max retries exhausted: {outcome.error}")
            return "failed_permanent"

        conn.execute(
            "UPDATE content_jobs SET status = 'failed_permanent', last_error = ?, updated_at_utc = ? WHERE id = ?",
            (outcome.error, now_iso, job_id),
        )
        append_job_log(db_path, job_id, "ERROR", f"Permanent publish error: {outcome.error}")
        return "failed_permanent"


def reconcile_job(
    job: dict[str, Any],
    db_path: str,
    settings: Settings,
    connector: TikTokConnector | None = None,
    r2_service: CloudflareR2Service | None = None,
    http_client: Any = None,
) -> str:
    """Check external TikTok publish status for a reconciling job without blind retrying."""
    connector = connector or TikTokConnector(settings)
    r2_service = r2_service or CloudflareR2Service(settings)
    job_id = int(job["id"])

    with db_session(db_path) as conn:
        account = conn.execute("SELECT * FROM tiktok_accounts WHERE id = ?", (job["account_id"],)).fetchone()
        if not account:
            return "failed_permanent"
        account_dict = dict(account)

    outcome = connector.fetch_publish_status(
        account_dict,
        str(job.get("tiktok_publish_id") or ""),
        http_client=http_client,
    )
    now_iso = utc_now_iso()

    with db_session(db_path) as conn:
        if outcome.outcome == "published":
            conn.execute(
                "UPDATE content_jobs SET status = 'published', last_error = '', updated_at_utc = ? WHERE id = ?",
                (now_iso, job_id),
            )
            append_job_log(db_path, job_id, "INFO", "Reconciliation confirmed video is published on TikTok")
            if settings.r2_auto_delete_after_publish and job.get("r2_object_key"):
                r2_service.delete_object(str(job["r2_object_key"]))
            return "published"

        if outcome.outcome == "retryable":
            attempts = int(job.get("attempts") or 0) + 1
            max_attempts = int(job.get("max_attempts") or 3)
            new_status = "retry_wait" if attempts < max_attempts else "failed_permanent"
            next_retry = (utc_now() + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            conn.execute(
                """
                UPDATE content_jobs
                SET status = ?, attempts = ?, next_retry_at_utc = ?, last_error = ?, updated_at_utc = ?
                WHERE id = ?
                """,
                (new_status, attempts, next_retry if new_status == "retry_wait" else None, outcome.error, now_iso, job_id),
            )
            return new_status

        if outcome.outcome == "needs_reconnect":
            conn.execute("UPDATE tiktok_accounts SET status = 'needs_reconnect', updated_at_utc = ? WHERE id = ?", (now_iso, account_dict["id"]))
            conn.execute("UPDATE content_jobs SET status = 'needs_reconnect', last_error = ?, updated_at_utc = ? WHERE id = ?", (outcome.error, now_iso, job_id))
            return "needs_reconnect"

        if outcome.outcome == "permanent":
            conn.execute(
                "UPDATE content_jobs SET status = 'failed_permanent', last_error = ?, updated_at_utc = ? WHERE id = ?",
                (outcome.error, now_iso, job_id),
            )
            return "failed_permanent"

    return "reconciling"

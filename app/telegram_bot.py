"""Telegram Bot (Long Polling) for remote monitoring, manual triggers, and R2 preview."""

from __future__ import annotations

import logging
import secrets
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app.config import Settings
from app.db import db_session, utc_now_iso, utc_to_jakarta_str
from app.services.temp_manager import count_active_temp_dirs

logger = logging.getLogger(__name__)


def _is_authorized(update: Update, settings: Settings) -> bool:
    allowed_id = str(settings.telegram_admin_chat_id or "").strip()
    if not allowed_id:
        return True
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    return chat_id == allowed_id


def _cancel_job_in_db(db_path: str, job_id: int) -> bool:
    with db_session(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE content_jobs
            SET status = 'cancelled', updated_at_utc = ?
            WHERE id = ? AND status IN ('pending_research', 'scheduled', 'retry_wait', 'queued')
            """,
            (utc_now_iso(), job_id),
        )
        return cur.rowcount > 0


async def send_job_ready_notification(
    telegram_app: Application, settings: Settings, job: dict[str, Any]
) -> None:
    """Send R2 preview link & inline Cancel button to admin chat when render finishes."""
    chat_id = str(settings.telegram_admin_chat_id or "").strip()
    if not chat_id:
        return

    job_id = job["id"]
    sched_wib = utc_to_jakarta_str(job.get("scheduled_at_utc"), settings.timezone)
    preview_url = f"{settings.app_base_url.rstrip('/')}/jobs/{job_id}"
    video_url = job.get("r2_video_url") or "-"
    text = (
        f"Job #{job_id} siap tayang ({job.get('mode')})\n"
        f"Judul Overlay: {job.get('title_overlay') or '-'}\n"
        f"Jadwal (WIB): {sched_wib}\n"
        f"Caption: {(job.get('caption') or '')[:140]}\n"
        f"Preview R2: {video_url}\n"
        f"Dashboard: {preview_url}"
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Batalkan Jadwal", callback_data=f"cancel:{job_id}")]]
    )
    try:
        await telegram_app.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
    except Exception as exc:
        logger.warning("Failed sending Telegram notification for job %s: %s", job_id, exc)


def build_telegram_application(settings: Settings) -> Application:
    """Construct python-telegram-bot Application with long-polling handlers."""
    app = Application.builder().token(settings.telegram_bot_token).build()

    async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update, settings):
            return
        await update.message.reply_text(
            "TikTok Auto-Pilot Bot aktif.\n"
            "Perintah:\n"
            "/status - Status antrean & kebersihan temp disk\n"
            "/accounts - Daftar akun TikTok terhubung\n"
            "/campaigns - Daftar campaign aktif\n"
            "/run <campaign_id> - Jalankan campaign sekarang\n"
            "/cancel <job_id> - Batalkan job terjadwal"
        )

    async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update, settings):
            return
        with db_session(settings.database_path) as conn:
            counts = dict(
                conn.execute(
                    """
                    SELECT
                        SUM(CASE WHEN status = 'pending_research' THEN 1 ELSE 0 END) AS pending,
                        SUM(CASE WHEN status = 'scheduled' THEN 1 ELSE 0 END) AS scheduled,
                        SUM(CASE WHEN status = 'published' THEN 1 ELSE 0 END) AS published,
                        SUM(CASE WHEN status IN ('reconciling', 'needs_reconnect') THEN 1 ELSE 0 END) AS attention
                    FROM content_jobs
                    """
                ).fetchone()
            )
        active_tmp = count_active_temp_dirs(settings.temp_root)
        await update.message.reply_text(
            f"Status Bot:\n"
            f"- Pending Riset/Render: {counts.get('pending') or 0}\n"
            f"- Terjadwal: {counts.get('scheduled') or 0}\n"
            f"- Published: {counts.get('published') or 0}\n"
            f"- Butuh Perhatian: {counts.get('attention') or 0}\n"
            f"- Folder Temp Lokal Aktif: {active_tmp}"
        )

    async def cmd_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update, settings):
            return
        with db_session(settings.database_path) as conn:
            rows = conn.execute(
                "SELECT id, display_name, publish_mode, status FROM tiktok_accounts ORDER BY id ASC"
            ).fetchall()
        if not rows:
            await update.message.reply_text("Belum ada akun TikTok terhubung.")
            return
        lines = [
            f"#{r['id']} {r['display_name']} | mode={r['publish_mode']} | status={r['status']}"
            for r in rows
        ]
        await update.message.reply_text("Akun TikTok:\n" + "\n".join(lines))

    async def cmd_campaigns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update, settings):
            return
        with db_session(settings.database_path) as conn:
            rows = conn.execute(
                "SELECT id, name, mode, language, is_active, auto_pilot FROM campaigns ORDER BY id ASC"
            ).fetchall()
        if not rows:
            await update.message.reply_text("Belum ada campaign.")
            return
        lines = [
            f"#{r['id']} {r['name']} ({r['mode']}, {r['language']}) active={bool(r['is_active'])}"
            for r in rows
        ]
        await update.message.reply_text("Campaigns:\n" + "\n".join(lines))

    async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update, settings):
            return
        if not context.args:
            await update.message.reply_text("Gunakan: /run <campaign_id>")
            return
        try:
            campaign_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("campaign_id harus berupa angka.")
            return

        now_iso = utc_now_iso()
        with db_session(settings.database_path) as conn:
            camp = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
            if not camp:
                await update.message.reply_text(f"Campaign #{campaign_id} tidak ditemukan.")
                return
            idem = f"tg_manual_{campaign_id}_{secrets.token_hex(6)}"
            cur = conn.execute(
                """
                INSERT INTO content_jobs (
                    campaign_id, account_id, idempotency_key, mode, status,
                    scheduled_at_utc, timezone, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, 'pending_research', ?, ?, ?, ?)
                """,
                (
                    camp["id"],
                    camp["account_id"],
                    idem,
                    camp["mode"],
                    now_iso,
                    settings.timezone,
                    now_iso,
                    now_iso,
                ),
            )
            job_id = cur.lastrowid
        await update.message.reply_text(f"Job #{job_id} masuk antrean riset & render.")

    async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update, settings):
            return
        if not context.args:
            await update.message.reply_text("Gunakan: /cancel <job_id>")
            return
        try:
            job_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("job_id harus berupa angka.")
            return
        ok = _cancel_job_in_db(settings.database_path, job_id)
        msg = f"Job #{job_id} dibatalkan." if ok else f"Job #{job_id} tidak dapat dibatalkan."
        await update.message.reply_text(msg)

    async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _is_authorized(update, settings) or not update.callback_query:
            return
        query = update.callback_query
        await query.answer()
        data = query.data or ""
        if data.startswith("cancel:"):
            try:
                job_id = int(data.split(":", 1)[1])
            except ValueError:
                return
            ok = _cancel_job_in_db(settings.database_path, job_id)
            reply = f"Job #{job_id} berhasil dibatalkan." if ok else f"Job #{job_id} sudah diproses/tidak aktif."
            await query.edit_message_text(f"{query.message.text}\n\n[Status: {reply}]")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("campaigns", cmd_campaigns))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    return app

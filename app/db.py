"""SQLite database initialization, WAL configuration, and timezone helpers."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


JAKARTA_TZ = ZoneInfo("Asia/Jakarta")
UTC_TZ = timezone.utc


def utc_now() -> datetime:
    """Return current timezone-aware UTC datetime."""
    return datetime.now(UTC_TZ)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc_iso(value: str) -> datetime:
    """Parse stored ISO-8601 UTC timestamp string into aware UTC datetime."""
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC_TZ)
    return dt.astimezone(UTC_TZ)


def jakarta_to_utc_iso(local_dt_str: str, tz_name: str = "Asia/Jakarta") -> str:
    """Convert a local datetime string (YYYY-MM-DDTHH:MM or YYYY-MM-DD HH:MM) to UTC ISO."""
    normalized = local_dt_str.strip().replace(" ", "T")
    if len(normalized) == 16:
        normalized += ":00"
    naive = datetime.fromisoformat(normalized)
    if naive.tzinfo is None:
        local_dt = naive.replace(tzinfo=ZoneInfo(tz_name))
    else:
        local_dt = naive.astimezone(ZoneInfo(tz_name))
    return local_dt.astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_to_jakarta_str(utc_iso: str | None, tz_name: str = "Asia/Jakarta") -> str:
    """Format a UTC ISO string into local display format (YYYY-MM-DD HH:MM WIB)."""
    if not utc_iso:
        return "-"
    dt = parse_utc_iso(utc_iso).astimezone(ZoneInfo(tz_name))
    return dt.strftime("%Y-%m-%d %H:%M")


def slot_jakarta_to_utc_iso(date_jakarta: datetime, hhmm: str, tz_name: str = "Asia/Jakarta") -> str:
    """Combine a Jakarta date and HH:MM string into a UTC ISO string."""
    hour_str, minute_str = hhmm.strip().split(":")
    tz = ZoneInfo(tz_name)
    local_dt = datetime(
        year=date_jakarta.year,
        month=date_jakarta.month,
        day=date_jakarta.day,
        hour=int(hour_str),
        minute=int(minute_str),
        second=0,
        tzinfo=tz,
    )
    return local_dt.astimezone(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")


def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


@contextmanager
def db_session(db_path: str) -> Iterator[sqlite3.Connection]:
    """Yield a configured SQLite connection with automatic commit/rollback."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    _configure_connection(conn)
    try:
        yield conn
    finally:
        conn.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tiktok_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    open_id TEXT NOT NULL UNIQUE,
    publish_mode TEXT NOT NULL DEFAULT 'draft_only' CHECK (publish_mode IN ('draft_only', 'direct_post')),
    status TEXT NOT NULL DEFAULT 'connected' CHECK (status IN ('connected', 'needs_reconnect')),
    access_token_enc TEXT NOT NULL DEFAULT '',
    refresh_token_enc TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '',
    expires_at_utc TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state TEXT PRIMARY KEY,
    code_verifier TEXT NOT NULL,
    account_label TEXT NOT NULL DEFAULT '',
    publish_mode TEXT NOT NULL DEFAULT 'draft_only',
    expires_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES tiktok_accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('youtube_clipper', 'faceless_ai')),
    niche TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'id' CHECK (language IN ('id', 'en')),
    translate_to_language TEXT,
    tts_voice TEXT NOT NULL DEFAULT 'id-ID-ArdiNeural',
    schedule_times_jakarta TEXT NOT NULL DEFAULT '["12:00", "19:00"]',
    is_active INTEGER NOT NULL DEFAULT 1,
    auto_pilot INTEGER NOT NULL DEFAULT 1,
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS content_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER REFERENCES campaigns(id) ON DELETE SET NULL,
    account_id INTEGER NOT NULL REFERENCES tiktok_accounts(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK (mode IN ('youtube_clipper', 'faceless_ai')),
    source_url TEXT NOT NULL DEFAULT '',
    title_overlay TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    hashtags TEXT NOT NULL DEFAULT '',
    script_text TEXT NOT NULL DEFAULT '',
    r2_object_key TEXT NOT NULL DEFAULT '',
    r2_video_url TEXT NOT NULL DEFAULT '',
    cf_image_id TEXT NOT NULL DEFAULT '',
    thumbnail_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending_research' CHECK (
        status IN (
            'pending_research',
            'rendering',
            'scheduled',
            'queued',
            'publishing',
            'published',
            'retry_wait',
            'reconciling',
            'failed_permanent',
            'needs_reconnect',
            'cancelled'
        )
    ),
    scheduled_at_utc TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Jakarta',
    tiktok_publish_id TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_retry_at_utc TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_content_jobs_status_sched
    ON content_jobs(status, scheduled_at_utc);
CREATE INDEX IF NOT EXISTS idx_content_jobs_account
    ON content_jobs(account_id, status);

CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES content_jobs(id) ON DELETE CASCADE,
    level TEXT NOT NULL DEFAULT 'INFO',
    message TEXT NOT NULL,
    created_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs(job_id, id DESC);

CREATE TABLE IF NOT EXISTS system_heartbeats (
    service_name TEXT PRIMARY KEY,
    last_seen_utc TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);
"""


def init_db(db_path: str) -> None:
    """Create database tables and indexes if they do not exist."""
    with db_session(db_path) as conn:
        conn.executescript(SCHEMA_SQL)

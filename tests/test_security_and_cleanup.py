"""Tests for Fernet token encryption, secret redaction, SSRF guards, and temp cleanup."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.db import db_session, init_db, utc_now_iso
from app.security import TokenCipher, redact_secrets, validate_public_https_url
from app.services.storage import CloudflareR2Service
from app.services.temp_manager import (
    count_active_temp_dirs,
    isolated_job_dir,
    sweep_orphan_temp_dirs,
)


def test_token_cipher_encrypts_and_decrypts_in_sqlite(tmp_path: Path) -> None:
    db_file = str(tmp_path / "test.db")
    init_db(db_file)
    cipher = TokenCipher("super-secret-app-key-for-testing")
    plain_access = "act.secret_tiktok_access_token_12345"
    plain_refresh = "rft.secret_tiktok_refresh_token_67890"

    enc_access = cipher.encrypt(plain_access)
    enc_refresh = cipher.encrypt(plain_refresh)
    assert enc_access != plain_access
    assert enc_refresh != plain_refresh

    now = utc_now_iso()
    with db_session(db_file) as conn:
        conn.execute(
            """
            INSERT INTO tiktok_accounts (
                display_name, open_id, publish_mode, status,
                access_token_enc, refresh_token_enc, scopes,
                created_at_utc, updated_at_utc
            ) VALUES (?, ?, 'draft_only', 'connected', ?, ?, 'video.upload', ?, ?)
            """,
            ("Test Account", "open_123", enc_access, enc_refresh, now, now),
        )
        raw_row = conn.execute("SELECT access_token_enc, refresh_token_enc FROM tiktok_accounts").fetchone()

    assert raw_row["access_token_enc"] != plain_access
    assert cipher.decrypt(raw_row["access_token_enc"]) == plain_access
    assert cipher.decrypt(raw_row["refresh_token_enc"]) == plain_refresh


def test_secret_redaction_masks_tokens_and_headers() -> None:
    sample = (
        "Failed calling https://api.telegram.org/bot123456:AAFakeSecret/sendMessage "
        "with Authorization: Bearer tok_super_secret_999 and access_token=abc123 "
        "and X-Amz-Signature=deadbeef99 and custom=my_secret_value_here"
    )
    redacted = redact_secrets(sample, extra_secrets=["my_secret_value_here"])
    assert "tok_super_secret_999" not in redacted
    assert "abc123" not in redacted
    assert "deadbeef99" not in redacted
    assert "AAFakeSecret" not in redacted
    assert "my_secret_value_here" not in redacted
    assert "[REDACTED]" in redacted


def test_ssrf_url_validation_and_r2_production_check() -> None:
    assert validate_public_https_url("https://media.example.com/video.mp4", resolve_dns=False) == "https://media.example.com/video.mp4"

    bad_urls = [
        "http://media.example.com/video.mp4",
        "https://localhost/admin",
        "https://127.0.0.1/secret",
        "https://10.0.0.5/internal",
        "https://192.168.1.1/router",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/loopback",
        "https://user:pass@media.example.com/file.mp4",
    ]
    for bad in bad_urls:
        with pytest.raises(ValueError):
            validate_public_https_url(bad, resolve_dns=False)

    prod_settings = Settings(
        app_env="production",
        r2_public_base_url="https://pub-12345.r2.dev",
    )
    with pytest.raises(ValueError, match="r2.dev"):
        CloudflareR2Service(prod_settings).validate_public_base_url()


def test_isolated_job_dir_deletes_on_success_and_error(tmp_path: Path) -> None:
    temp_root = str(tmp_path / "bot_tmp")

    with isolated_job_dir(101, temp_root) as job_dir:
        (job_dir / "render.mp4").write_bytes(b"fake video bytes")
        assert count_active_temp_dirs(temp_root) == 1

    assert count_active_temp_dirs(temp_root) == 0

    with pytest.raises(RuntimeError, match="boom"):
        with isolated_job_dir(102, temp_root) as job_dir:
            (job_dir / "partial.mp4").write_bytes(b"partial")
            assert count_active_temp_dirs(temp_root) == 1
            raise RuntimeError("boom")

    assert count_active_temp_dirs(temp_root) == 0


def test_sweep_orphan_temp_dirs_removes_stale_directories(tmp_path: Path) -> None:
    temp_root = str(tmp_path / "bot_tmp")
    stale_dir = Path(temp_root) / "jobs" / "job_orphan_1"
    fresh_dir = Path(temp_root) / "jobs" / "job_fresh_2"
    stale_dir.mkdir(parents=True)
    fresh_dir.mkdir(parents=True)
    (stale_dir / "leftover.mp4").write_bytes(b"123")

    old_mtime = time.time() - 7200
    os.utime(stale_dir, (old_mtime, old_mtime))

    removed = sweep_orphan_temp_dirs(temp_root, max_age_seconds=3600)
    assert removed == 1
    assert not stale_dir.exists()
    assert fresh_dir.exists()

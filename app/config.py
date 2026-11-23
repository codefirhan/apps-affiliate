"""Application configuration loaded from environment variables."""

from __future__ import annotations

import os
import shutil
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings for the TikTok Auto-Pilot dashboard and worker."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "TikTok Auto-Pilot Bot"
    app_env: str = "local"
    app_secret_key: str = "dev-insecure-change-me-before-production"
    app_base_url: str = "http://localhost:8000"
    timezone: str = "Asia/Jakarta"
    dry_run: bool = True

    database_path: str = "./data/tiktok_bot.db"
    temp_root: str = "/tmp/tiktok-bot"
    ffmpeg_bin: str = ""

    # AI providers
    llm_provider: str = "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    transcribe_provider: str = "groq"
    groq_api_key: str = ""

    # TTS & stock media
    tts_provider: str = "edge"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    pexels_api_key: str = ""
    youtube_api_key: str = ""

    # Cloudflare R2 & Images
    r2_account_id: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = ""
    r2_public_base_url: str = ""
    r2_auto_delete_after_publish: bool = True

    cf_images_account_id: str = ""
    cf_images_api_token: str = ""
    cf_images_account_hash: str = ""

    # TikTok Official Content Posting API
    tiktok_client_key: str = ""
    tiktok_client_secret: str = ""
    tiktok_redirect_uri: str = "http://localhost:8000/auth/tiktok/callback"

    # Telegram Bot
    telegram_bot_token: str = ""
    telegram_admin_chat_id: str = ""

    # Worker loop settings
    worker_poll_seconds: int = 15
    temp_orphan_max_age_seconds: int = 3600

    def resolve_ffmpeg_bin(self) -> str:
        """Return an FFmpeg binary path with libass/drawtext support when available."""
        if self.ffmpeg_bin and os.path.exists(self.ffmpeg_bin):
            return self.ffmpeg_bin
        mac_full = Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg")
        if mac_full.exists():
            return str(mac_full)
        return shutil.which("ffmpeg") or "ffmpeg"

    def resolve_ffprobe_bin(self) -> str:
        """Return matching ffprobe binary path."""
        ffmpeg_path = Path(self.resolve_ffmpeg_bin())
        sibling = ffmpeg_path.parent / "ffprobe"
        if sibling.exists():
            return str(sibling)
        return shutil.which("ffprobe") or "ffprobe"

    def configured_secrets(self) -> list[str]:
        """Collect non-empty configured secret values for log redaction."""
        candidates = [
            self.app_secret_key,
            self.gemini_api_key,
            self.openai_api_key,
            self.groq_api_key,
            self.elevenlabs_api_key,
            self.pexels_api_key,
            self.youtube_api_key,
            self.r2_access_key_id,
            self.r2_secret_access_key,
            self.cf_images_api_token,
            self.tiktok_client_secret,
            self.telegram_bot_token,
        ]
        return [s for s in candidates if s and len(s) >= 6]


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()

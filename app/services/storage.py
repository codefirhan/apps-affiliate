"""Cloudflare R2 (video object storage) and Cloudflare Images (thumbnail/asset storage)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
import httpx

from app.config import Settings
from app.security import redact_secrets, validate_public_https_url

logger = logging.getLogger(__name__)


class CloudflareR2Service:
    """Upload rendered MP4 videos to Cloudflare R2 and clean them up after publish."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.r2_account_id
            and self.settings.r2_access_key_id
            and self.settings.r2_secret_access_key
            and self.settings.r2_bucket
            and self.settings.r2_public_base_url
        )

    def validate_public_base_url(self) -> str:
        """Validate R2 public base URL; reject *.r2.dev in production."""
        base_url = self.settings.r2_public_base_url.strip().rstrip("/")
        if not base_url:
            raise ValueError("R2_PUBLIC_BASE_URL is required")
        validate_public_https_url(base_url, resolve_dns=False)
        parsed = urlparse(base_url)
        host = (parsed.hostname or "").lower()
        if self.settings.app_env.lower() == "production" and host.endswith(".r2.dev"):
            raise ValueError("Production mode forbids *.r2.dev public URLs; configure a custom domain")
        return base_url

    def _client(self) -> Any:
        endpoint = f"https://{self.settings.r2_account_id}.r2.cloudflarestorage.com"
        return boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=self.settings.r2_access_key_id,
            aws_secret_access_key=self.settings.r2_secret_access_key,
            region_name="auto",
        )

    def upload_video(self, local_path: Path, object_key: str) -> str:
        """Upload a local MP4 file to R2 and return its public HTTPS URL."""
        clean_key = object_key.lstrip("/")
        if not local_path.exists():
            raise FileNotFoundError(f"Video file not found for R2 upload: {local_path}")

        if self.settings.dry_run or not self.is_configured:
            base = self.settings.r2_public_base_url.strip().rstrip("/") or "https://mock-r2.example.com"
            return f"{base}/{clean_key}"

        base_url = self.validate_public_base_url()
        client = self._client()
        try:
            client.upload_file(
                str(local_path),
                self.settings.r2_bucket,
                clean_key,
                ExtraArgs={"ContentType": "video/mp4"},
            )
        except Exception as exc:
            raise RuntimeError(
                redact_secrets(f"R2 upload failed: {exc}", self.settings.configured_secrets())
            ) from exc

        return f"{base_url}/{clean_key}"

    def delete_object(self, object_key: str) -> bool:
        """Delete a video object from R2 after successful TikTok upload or cancellation."""
        clean_key = object_key.lstrip("/")
        if not clean_key:
            return False
        if self.settings.dry_run or not self.is_configured:
            return True

        client = self._client()
        try:
            client.delete_object(Bucket=self.settings.r2_bucket, Key=clean_key)
            return True
        except Exception as exc:
            logger.warning(
                "Failed deleting R2 object %s: %s",
                clean_key,
                redact_secrets(str(exc), self.settings.configured_secrets()),
            )
            return False


class CloudflareImagesService:
    """Upload thumbnails and faceless image assets to Cloudflare Images."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(
            self.settings.cf_images_account_id
            and self.settings.cf_images_api_token
        )

    def upload_image(self, local_path: Path, metadata: dict[str, Any] | None = None) -> dict[str, str]:
        """Upload an image file to Cloudflare Images and return image_id + public URL."""
        if not local_path.exists():
            raise FileNotFoundError(f"Image file not found: {local_path}")

        if self.settings.dry_run or not self.is_configured:
            fake_id = f"mock-img-{local_path.stem}"
            account_hash = self.settings.cf_images_account_hash or "mockhash"
            return {
                "image_id": fake_id,
                "url": f"https://imagedelivery.net/{account_hash}/{fake_id}/public",
            }

        endpoint = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self.settings.cf_images_account_id}/images/v1"
        )
        headers = {"Authorization": f"Bearer {self.settings.cf_images_api_token}"}

        try:
            with local_path.open("rb") as fh, httpx.Client(timeout=20.0) as client:
                files = {"file": (local_path.name, fh, "image/jpeg")}
                data = {}
                if metadata:
                    import json

                    data["metadata"] = json.dumps(metadata)
                resp = client.post(endpoint, headers=headers, files=files, data=data)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            raise RuntimeError(
                redact_secrets(f"Cloudflare Images upload failed: {exc}", self.settings.configured_secrets())
            ) from exc

        result = payload.get("result") or {}
        image_id = result.get("id", "")
        variants = result.get("variants") or []
        public_url = variants[0] if variants else (
            f"https://imagedelivery.net/{self.settings.cf_images_account_hash}/{image_id}/public"
        )
        return {"image_id": image_id, "url": public_url}

    def delete_image(self, image_id: str) -> bool:
        """Delete an uploaded image from Cloudflare Images."""
        if not image_id or self.settings.dry_run or not self.is_configured:
            return True

        endpoint = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self.settings.cf_images_account_id}/images/v1/{image_id}"
        )
        headers = {"Authorization": f"Bearer {self.settings.cf_images_api_token}"}
        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.delete(endpoint, headers=headers)
                return resp.status_code in (200, 404)
        except Exception as exc:
            logger.warning(
                "Failed deleting Cloudflare Image %s: %s",
                image_id,
                redact_secrets(str(exc), self.settings.configured_secrets()),
            )
            return False

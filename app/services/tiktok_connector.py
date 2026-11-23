"""TikTok Official Content Posting API v2 connector with OAuth PKCE and reconciliation."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.security import TokenCipher, redact_secrets

logger = logging.getLogger(__name__)

TIKTOK_AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_USER_INFO_URL = "https://open.tiktokapis.com/v2/user/info/"
TIKTOK_INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
TIKTOK_DIRECT_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
TIKTOK_STATUS_FETCH_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"


@dataclass
class PublishOutcome:
    """Normalized outcome from TikTok upload or status reconciliation."""

    outcome: str  # published | retryable | reconciling | needs_reconnect | permanent
    publish_id: str = ""
    error: str = ""


def generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, S256 code_challenge) for OAuth PKCE."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class TikTokConnector:
    """Adapter for TikTok Content Posting API v2."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.cipher = TokenCipher(settings.app_secret_key)

    def capabilities(self, publish_mode: str = "draft_only") -> dict[str, Any]:
        """Return honest capability manifest based on TikTok App Audit status."""
        if publish_mode == "direct_post":
            return {
                "mode": "direct_post",
                "label": "Direct Post (Public Feed)",
                "scopes": ["user.info.basic", "video.upload", "video.publish"],
                "requires_audit": True,
                "supports_status_reconciliation": True,
            }
        return {
            "mode": "draft_only",
            "label": "Draft / Inbox Only (Pre-Audit Safe)",
            "scopes": ["user.info.basic", "video.upload"],
            "requires_audit": False,
            "supports_status_reconciliation": True,
        }

    def build_authorize_url(
        self,
        state: str,
        code_verifier: str,
        publish_mode: str = "draft_only",
    ) -> str:
        """Build TikTok OAuth 2.0 authorization URL with PKCE S256."""
        if not self.settings.tiktok_client_key:
            raise ValueError("TIKTOK_CLIENT_KEY is not configured")

        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        scopes = self.capabilities(publish_mode)["scopes"]

        params = {
            "client_key": self.settings.tiktok_client_key,
            "scope": ",".join(scopes),
            "response_type": "code",
            "redirect_uri": self.settings.tiktok_redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{TIKTOK_AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        """Exchange OAuth authorization code for encrypted tokens and user profile."""
        if self.settings.dry_run or not self.settings.tiktok_client_key:
            fake_open_id = f"dry_open_{secrets.token_hex(4)}"
            return {
                "open_id": fake_open_id,
                "display_name": "TikTok Demo Account",
                "access_token_enc": self.cipher.encrypt("dryrun_access_token"),
                "refresh_token_enc": self.cipher.encrypt("dryrun_refresh_token"),
                "scopes": "user.info.basic,video.upload",
                "expires_in": 86400,
            }

        try:
            with httpx.Client(timeout=20.0) as client:
                token_resp = client.post(
                    TIKTOK_TOKEN_URL,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data={
                        "client_key": self.settings.tiktok_client_key,
                        "client_secret": self.settings.tiktok_client_secret,
                        "code": code,
                        "grant_type": "authorization_code",
                        "redirect_uri": self.settings.tiktok_redirect_uri,
                        "code_verifier": code_verifier,
                    },
                )
                token_resp.raise_for_status()
                token_data = token_resp.json()
                access_token = token_data.get("access_token", "")
                refresh_token = token_data.get("refresh_token", "")
                open_id = token_data.get("open_id", "")

                display_name = open_id or "TikTok User"
                if access_token:
                    user_resp = client.get(
                        TIKTOK_USER_INFO_URL,
                        headers={"Authorization": f"Bearer {access_token}"},
                        params={"fields": "open_id,display_name"},
                    )
                    if user_resp.status_code == 200:
                        user_obj = (user_resp.json().get("data") or {}).get("user") or {}
                        display_name = user_obj.get("display_name") or display_name
                        open_id = user_obj.get("open_id") or open_id
        except Exception as exc:
            raise RuntimeError(
                redact_secrets(f"TikTok OAuth exchange failed: {exc}", self.settings.configured_secrets())
            ) from exc

        return {
            "open_id": open_id,
            "display_name": display_name,
            "access_token_enc": self.cipher.encrypt(access_token),
            "refresh_token_enc": self.cipher.encrypt(refresh_token),
            "scopes": str(token_data.get("scope", "")),
            "expires_in": int(token_data.get("expires_in") or 86400),
        }

    def refresh_access_token(self, refresh_token_enc: str) -> dict[str, Any]:
        """Refresh an expired TikTok OAuth token."""
        plain_refresh = self.cipher.decrypt(refresh_token_enc)
        if not plain_refresh:
            raise ValueError("Missing refresh token")
        if self.settings.dry_run or not self.settings.tiktok_client_key:
            return {
                "access_token_enc": self.cipher.encrypt("dryrun_refreshed_access"),
                "refresh_token_enc": self.cipher.encrypt("dryrun_refreshed_refresh"),
                "expires_in": 86400,
            }

        with httpx.Client(timeout=20.0) as client:
            resp = client.post(
                TIKTOK_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_key": self.settings.tiktok_client_key,
                    "client_secret": self.settings.tiktok_client_secret,
                    "grant_type": "refresh_token",
                    "refresh_token": plain_refresh,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        new_access = data.get("access_token", "")
        new_refresh = data.get("refresh_token", plain_refresh)
        if not new_access:
            raise RuntimeError("TikTok refresh response missing access_token")

        return {
            "access_token_enc": self.cipher.encrypt(new_access),
            "refresh_token_enc": self.cipher.encrypt(new_refresh),
            "expires_in": int(data.get("expires_in") or 86400),
        }

    def _redact(self, message: str, *tokens: str) -> str:
        secrets_list = self.settings.configured_secrets() + [t for t in tokens if t]
        return redact_secrets(message, secrets_list)

    def publish_video(
        self,
        account: dict[str, Any],
        job: dict[str, Any],
        http_client: httpx.Client | None = None,
    ) -> PublishOutcome:
        """Push video via PULL_FROM_URL (R2 HTTPS URL) to TikTok Inbox or Direct Post."""
        video_url = str(job.get("r2_video_url") or "").strip()
        if not video_url.startswith("https://"):
            return PublishOutcome(outcome="permanent", error="Invalid or missing HTTPS R2 video URL")

        if (self.settings.dry_run or not self.settings.tiktok_client_key) and http_client is None:
            return PublishOutcome(
                outcome="published",
                publish_id=f"dryrun_pub_{job.get('id', '0')}",
            )

        access_token = self.cipher.decrypt(str(account.get("access_token_enc") or ""))
        if not access_token:
            return PublishOutcome(outcome="needs_reconnect", error="Missing encrypted OAuth access token")

        publish_mode = str(account.get("publish_mode") or "draft_only")
        caption = str(job.get("caption") or "").strip()
        hashtags = str(job.get("hashtags") or "").strip()
        full_title = f"{caption} {hashtags}".strip()[:2200]

        if publish_mode == "direct_post":
            endpoint = TIKTOK_DIRECT_INIT_URL
            body = {
                "post_info": {
                    "title": full_title,
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                },
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": video_url,
                },
            }
        else:
            endpoint = TIKTOK_INBOX_INIT_URL
            body = {
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": video_url,
                }
            }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        }

        own_client = http_client is None
        client = http_client or httpx.Client(timeout=35.0)
        try:
            resp = client.post(endpoint, headers=headers, json=body)
            outcome = self._interpret_publish_response(resp, access_token)
            if (
                outcome.outcome == "permanent"
                and publish_mode == "direct_post"
                and any(
                    k in outcome.error
                    for k in (
                        "unaudited_client_can_only_post_to_private_accounts",
                        "privacy_level_option_mismatch",
                        "private",
                    )
                )
            ):
                body["post_info"]["privacy_level"] = "SELF_ONLY"
                resp = client.post(endpoint, headers=headers, json=body)
                outcome = self._interpret_publish_response(resp, access_token)

            if (
                outcome.outcome == "permanent"
                and "url_ownership_unverified" in outcome.error
                and http_client is None
            ):
                return self._publish_via_file_upload(
                    client=client,
                    endpoint=endpoint,
                    headers=headers,
                    post_info=body.get("post_info"),
                    video_url=video_url,
                    access_token=access_token,
                )
            return outcome
        except httpx.TimeoutException as exc:
            # Ambiguous failure after sending request: MUST reconcile instead of blind retry.
            return PublishOutcome(
                outcome="reconciling",
                error=self._redact(f"Ambiguous timeout during TikTok publish: {exc}", access_token),
            )
        except httpx.RequestError as exc:
            return PublishOutcome(
                outcome="retryable",
                error=self._redact(f"Network error before TikTok response: {exc}", access_token),
            )
        finally:
            if own_client:
                client.close()

    def _publish_via_file_upload(
        self,
        client: httpx.Client,
        endpoint: str,
        headers: dict[str, str],
        post_info: dict[str, Any] | None,
        video_url: str,
        access_token: str,
    ) -> PublishOutcome:
        """Fallback to TikTok FILE_UPLOAD by streaming MP4 bytes from R2 in memory."""
        media_resp = client.get(video_url, timeout=45.0)
        media_resp.raise_for_status()
        video_bytes = media_resp.content
        size = len(video_bytes)
        if size == 0:
            return PublishOutcome(outcome="permanent", error="R2 video is empty (0 bytes)")

        init_body: dict[str, Any] = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": size,
                "chunk_size": size,
                "total_chunk_count": 1,
            }
        }
        if post_info:
            init_body["post_info"] = post_info

        init_resp = client.post(endpoint, headers=headers, json=init_body)
        outcome = self._interpret_publish_response(init_resp, access_token)
        if (
            outcome.outcome == "permanent"
            and post_info
            and any(
                k in outcome.error
                for k in (
                    "unaudited_client_can_only_post_to_private_accounts",
                    "privacy_level_option_mismatch",
                    "private",
                )
            )
        ):
            init_body["post_info"]["privacy_level"] = "SELF_ONLY"
            init_resp = client.post(endpoint, headers=headers, json=init_body)

        if init_resp.status_code != 200:
            return self._interpret_publish_response(init_resp, access_token)

        payload = init_resp.json()
        err_code = str((payload.get("error") or {}).get("code") or "ok")
        if err_code not in ("ok", ""):
            return self._interpret_publish_response(init_resp, access_token)

        data = payload.get("data") or {}
        publish_id = str(data.get("publish_id") or "")
        upload_url = str(data.get("upload_url") or "")
        if not publish_id or not upload_url:
            return PublishOutcome(outcome="permanent", error="TikTok FILE_UPLOAD missing upload_url")

        put_resp = client.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(size),
                "Content-Range": f"bytes 0-{size - 1}/{size}",
            },
            content=video_bytes,
            timeout=90.0,
        )
        if put_resp.status_code not in (200, 201, 206):
            return PublishOutcome(
                outcome="retryable" if put_resp.status_code >= 500 else "permanent",
                error=self._redact(f"TikTok chunk PUT HTTP {put_resp.status_code}", access_token),
            )
        return PublishOutcome(outcome="published", publish_id=publish_id)

    def _interpret_publish_response(self, resp: httpx.Response, access_token: str) -> PublishOutcome:
        status = resp.status_code
        if status in (401, 403):
            return PublishOutcome(
                outcome="needs_reconnect",
                error=self._redact(f"TikTok auth rejected ({status})", access_token),
            )
        if status == 429 or status >= 500:
            return PublishOutcome(
                outcome="retryable",
                error=self._redact(f"TikTok transient HTTP {status}", access_token),
            )

        try:
            payload = resp.json()
        except Exception:
            return PublishOutcome(
                outcome="permanent" if 400 <= status < 500 else "reconciling",
                error=self._redact(f"Non-JSON TikTok response HTTP {status}", access_token),
            )

        err_obj = payload.get("error") or {}
        err_code = str(err_obj.get("code") or "ok")
        if err_code in ("access_token_invalid", "scope_not_authorized"):
            return PublishOutcome(
                outcome="needs_reconnect",
                error=self._redact(f"TikTok OAuth error: {err_code}", access_token),
            )
        if err_code not in ("ok", ""):
            outcome = "retryable" if err_code == "rate_limit_exceeded" else "permanent"
            msg = str(err_obj.get("message") or err_code)
            return PublishOutcome(outcome=outcome, error=self._redact(msg, access_token))

        publish_id = str((payload.get("data") or {}).get("publish_id") or "")
        if not publish_id:
            return PublishOutcome(
                outcome="reconciling",
                error="TikTok accepted request without publish_id; reconciliation required",
            )
        return PublishOutcome(outcome="published", publish_id=publish_id)

    def fetch_publish_status(
        self,
        account: dict[str, Any],
        publish_id: str,
        http_client: httpx.Client | None = None,
    ) -> PublishOutcome:
        """Verify external TikTok publish status before allowing any retry."""
        if not publish_id:
            return PublishOutcome(
                outcome="permanent",
                error="Cannot reconcile automatically without tiktok_publish_id; manual check required",
            )
        if self.settings.dry_run and http_client is None:
            return PublishOutcome(outcome="published", publish_id=publish_id)

        access_token = self.cipher.decrypt(str(account.get("access_token_enc") or ""))
        if not access_token:
            return PublishOutcome(outcome="needs_reconnect", error="Missing access token")

        own_client = http_client is None
        client = http_client or httpx.Client(timeout=20.0)
        try:
            resp = client.post(
                TIKTOK_STATUS_FETCH_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                json={"publish_id": publish_id},
            )
            if resp.status_code in (401, 403):
                return PublishOutcome(outcome="needs_reconnect", error=f"Auth rejected ({resp.status_code})")
            if resp.status_code == 429 or resp.status_code >= 500:
                return PublishOutcome(outcome="reconciling", publish_id=publish_id, error=f"HTTP {resp.status_code}")

            data = resp.json().get("data") or {}
            ext_status = str(data.get("status") or "").upper()
            if ext_status in ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX"):
                return PublishOutcome(outcome="published", publish_id=publish_id)
            if ext_status == "FAILED":
                reason = str(data.get("fail_reason") or "TikTok reported publish FAILED")
                return PublishOutcome(outcome="retryable", publish_id=publish_id, error=self._redact(reason, access_token))
            return PublishOutcome(outcome="reconciling", publish_id=publish_id, error=f"External status: {ext_status or 'PROCESSING'}")
        except httpx.RequestError as exc:
            return PublishOutcome(
                outcome="reconciling",
                publish_id=publish_id,
                error=self._redact(f"Status fetch network error: {exc}", access_token),
            )
        finally:
            if own_client:
                client.close()

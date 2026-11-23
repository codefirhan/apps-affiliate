"""Security helpers: Fernet OAuth token encryption, secret redaction, and SSRF protection."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import re
import socket
from typing import Iterable
from urllib.parse import urlparse

from cryptography.fernet import Fernet


class TokenCipher:
    """Encrypt and decrypt OAuth tokens at rest using Fernet."""

    def __init__(self, secret_key: str) -> None:
        self._fernet = Fernet(self._derive_fernet_key(secret_key))

    @staticmethod
    def _derive_fernet_key(secret_key: str) -> bytes:
        raw = secret_key.strip().encode("utf-8")
        try:
            decoded = base64.urlsafe_b64decode(raw)
            if len(decoded) == 32 and len(raw) == 44:
                return raw
        except Exception:
            pass
        digest = hashlib.sha256(raw).digest()
        return base64.urlsafe_b64encode(digest)

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a plaintext token into a UTF-8 Fernet token string."""
        if not plaintext:
            return ""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a stored Fernet token string back to plaintext."""
        if not ciphertext:
            return ""
        return self._fernet.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


_BEARER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+|bearer\s+)([a-z0-9\-._~+/]+=*)")
_KV_SECRET_RE = re.compile(
    r"(?i)\b(access_token|refresh_token|client_secret|code|api_key|apikey|x-api-key|"
    r"x-amz-signature|x-amz-credential|secret|token|password)\b\s*([=:])\s*([^\s&\"',;]+)"
)
_TELEGRAM_BOT_URL_RE = re.compile(r"(https?://api\.telegram\.org/bot)([^/\s]+)")


def redact_secrets(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Redact credentials, OAuth tokens, signed query params, and configured secrets."""
    if not text:
        return text

    sanitized = text
    for secret in extra_secrets:
        if secret and len(secret) >= 4:
            sanitized = sanitized.replace(secret, "[REDACTED]")

    sanitized = _TELEGRAM_BOT_URL_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _BEARER_RE.sub(r"\1[REDACTED]", sanitized)
    sanitized = _KV_SECRET_RE.sub(r"\1\2[REDACTED]", sanitized)
    return sanitized


_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")


def _is_forbidden_ip(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    if isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT_NET:
        return True
    return False


def validate_public_https_url(url: str, resolve_dns: bool = True) -> str:
    """Validate that a URL is HTTPS on a standard port and does not resolve to internal IPs."""
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() != "https":
        raise ValueError("URL must use https:// scheme")
    if parsed.username or parsed.password:
        raise ValueError("URL must not contain embedded credentials")
    if parsed.port not in (None, 443):
        raise ValueError("URL must use standard HTTPS port 443")

    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise ValueError("URL is missing a valid hostname")
    if (
        hostname in _BLOCKED_HOSTNAMES
        or hostname.endswith(".local")
        or hostname.endswith(".internal")
    ):
        raise ValueError(f"Blocked internal hostname: {hostname}")

    try:
        ipaddress.ip_address(hostname)
        if _is_forbidden_ip(hostname):
            raise ValueError(f"Blocked internal IP address: {hostname}")
        return url.strip()
    except ValueError as exc:
        if "Blocked internal" in str(exc):
            raise

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError(f"Unable to resolve hostname {hostname}: {exc}") from exc
        if not infos:
            raise ValueError(f"No DNS records resolved for {hostname}")
        for info in infos:
            resolved_ip = info[4][0]
            if _is_forbidden_ip(resolved_ip):
                raise ValueError(f"Hostname {hostname} resolved to blocked IP {resolved_ip}")

    return url.strip()

"""Content research service: viral YouTube discovery and portrait stock media fetching."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.security import redact_secrets, validate_public_https_url
from app.services.video_engine import VideoEngine

logger = logging.getLogger(__name__)


def _parse_vtt_to_segments(vtt_text: str) -> list[dict[str, Any]]:
    """Parse WebVTT subtitle content into [{start, end, text}] segments."""
    timestamp_re = re.compile(
        r"(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})\s*-->\s*(?:(\d+):)?(\d{2}):(\d{2})[.,](\d{3})"
    )

    def to_sec(h: str | None, m: str, s: str, ms: str) -> float:
        return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    segments: list[dict[str, Any]] = []
    lines = vtt_text.splitlines()
    i = 0
    while i < len(lines):
        match = timestamp_re.search(lines[i])
        if match:
            start = to_sec(*match.groups()[:4])
            end = to_sec(*match.groups()[4:])
            i += 1
            text_lines: list[str] = []
            while i < len(lines) and lines[i].strip():
                cleaned = re.sub(r"<[^>]+>", "", lines[i]).strip()
                if cleaned:
                    text_lines.append(cleaned)
                i += 1
            joined = " ".join(text_lines).strip()
            if joined:
                segments.append({"start": start, "end": end, "text": joined})
        i += 1
    return segments


class ResearchService:
    """Discover viral YouTube candidates and portrait stock media."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def search_viral_youtube(
        self, niche: str, keywords: str = "", max_results: int = 5
    ) -> list[dict[str, Any]]:
        """Find trending/high-view YouTube videos for clipping without downloading full files."""
        query = f"{niche} {keywords}".strip()
        if self.settings.dry_run:
            return [
                {
                    "video_id": "dryrun_yt_001",
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "title": f"Viral {niche.title()} Breakdown",
                    "view_count": 1250000,
                    "duration": 240,
                }
            ]

        if self.settings.youtube_api_key:
            try:
                with httpx.Client(timeout=20.0) as client:
                    search_resp = client.get(
                        "https://www.googleapis.com/youtube/v3/search",
                        params={
                            "part": "snippet",
                            "q": query,
                            "type": "video",
                            "order": "viewCount",
                            "videoDuration": "medium",
                            "maxResults": max_results,
                            "key": self.settings.youtube_api_key,
                        },
                    )
                    search_resp.raise_for_status()
                    items = search_resp.json().get("items") or []
                    results = []
                    for item in items:
                        vid = item.get("id", {}).get("videoId")
                        if not vid:
                            continue
                        snippet = item.get("snippet") or {}
                        results.append(
                            {
                                "video_id": vid,
                                "url": f"https://www.youtube.com/watch?v={vid}",
                                "title": snippet.get("title", ""),
                                "view_count": 0,
                                "duration": 0,
                            }
                        )
                    if results:
                        return results
            except Exception as exc:
                logger.warning(
                    "YouTube Data API fallback to yt-dlp search: %s",
                    redact_secrets(str(exc), self.settings.configured_secrets()),
                )

        proc = subprocess.run(
            [
                "yt-dlp",
                "--dump-json",
                "--flat-playlist",
                f"ytsearch{max_results}:{query}",
            ],
            capture_output=True,
            text=True,
            timeout=45,
            check=True,
        )
        candidates: list[dict[str, Any]] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            vid = entry.get("id", "")
            duration = int(entry.get("duration") or 0)
            if vid and (duration == 0 or duration <= 3600):
                candidates.append(
                    {
                        "video_id": vid,
                        "url": entry.get("webpage_url") or f"https://www.youtube.com/watch?v={vid}",
                        "title": entry.get("title", ""),
                        "view_count": int(entry.get("view_count") or 0),
                        "duration": duration,
                    }
                )
        candidates.sort(key=lambda item: item.get("view_count", 0), reverse=True)
        return candidates

    def fetch_youtube_transcript(
        self, video_url: str, language: str, work_dir: Path
    ) -> list[dict[str, Any]]:
        """Attempt to fetch YouTube subtitles/auto-captions without downloading video."""
        if self.settings.dry_run:
            return [
                {"start": 0.0, "end": 4.0, "text": "Hook pembuka yang menarik perhatian penonton."},
                {"start": 4.0, "end": 9.0, "text": "Penjelasan inti yang langsung ke pokok masalah."},
            ]

        try:
            subprocess.run(
                [
                    "yt-dlp",
                    "--skip-download",
                    "--write-sub",
                    "--write-auto-sub",
                    "--sub-lang",
                    f"{language},en,id",
                    "--sub-format",
                    "vtt",
                    "-o",
                    str(work_dir / "subs.%(ext)s"),
                    video_url,
                ],
                cwd=str(work_dir),
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            vtt_files = sorted(work_dir.glob("subs*.vtt"))
            if vtt_files:
                return _parse_vtt_to_segments(vtt_files[0].read_text(encoding="utf-8", errors="ignore"))
        except Exception as exc:
            logger.warning("Failed fetching YouTube VTT transcript: %s", exc)
        return []

    def fetch_stock_media(
        self, queries: list[str], work_dir: Path, max_clips: int = 3
    ) -> list[Path]:
        """Download portrait stock clips from Pexels into work_dir (deleted on job exit)."""
        if self.settings.dry_run or not self.settings.pexels_api_key:
            engine = VideoEngine(self.settings)
            placeholder = work_dir / "stock_dryrun.mp4"
            engine._generate_placeholder_clip(placeholder, 4.0)
            return [placeholder]

        downloaded: list[Path] = []
        headers = {"Authorization": self.settings.pexels_api_key}
        try:
            with httpx.Client(timeout=30.0) as client:
                for idx, query in enumerate(queries[:max_clips]):
                    resp = client.get(
                        "https://api.pexels.com/videos/search",
                        headers=headers,
                        params={
                            "query": query,
                            "orientation": "portrait",
                            "size": "medium",
                            "per_page": 2,
                        },
                    )
                    resp.raise_for_status()
                    videos = resp.json().get("videos") or []
                    if not videos:
                        continue
                    files = videos[0].get("video_files") or []
                    hd = [f for f in files if (f.get("height") or 0) >= 720 and f.get("file_type") == "video/mp4"]
                    chosen = hd[0] if hd else (files[0] if files else None)
                    if not chosen or not chosen.get("link"):
                        continue
                    dl_url = validate_public_https_url(chosen["link"])
                    out_file = work_dir / f"stock_{idx}.mp4"
                    with client.stream("GET", dl_url, follow_redirects=False) as stream_resp:
                        stream_resp.raise_for_status()
                        with out_file.open("wb") as fh:
                            for chunk in stream_resp.iter_bytes(65536):
                                fh.write(chunk)
                    downloaded.append(out_file)
        except Exception as exc:
            logger.warning(
                "Pexels stock fetch failed (%s); falling back to generated background",
                redact_secrets(str(exc), self.settings.configured_secrets()),
            )

        if not downloaded:
            engine = VideoEngine(self.settings)
            placeholder = work_dir / "stock_fallback.mp4"
            engine._generate_placeholder_clip(placeholder, 4.0)
            return [placeholder]

        return downloaded

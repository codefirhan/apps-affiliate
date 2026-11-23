"""Text-to-Speech dubbing service (Edge-TTS default + optional ElevenLabs)."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Any

import edge_tts
import httpx

from app.config import Settings
from app.security import redact_secrets

logger = logging.getLogger(__name__)

DEFAULT_VOICES = {
    "id": "id-ID-ArdiNeural",
    "en": "en-US-AndrewNeural",
}


def estimate_word_timings(text: str, total_duration: float) -> list[dict[str, Any]]:
    """Distribute word timings proportionally across total audio duration."""
    words = [w.strip() for w in text.split() if w.strip()]
    if not words:
        return []
    duration = max(1.0, float(total_duration))
    weights = [max(1, len(w)) for w in words]
    total_weight = sum(weights)
    cursor = 0.0
    timings: list[dict[str, Any]] = []
    for word, weight in zip(words, weights):
        span = duration * (weight / total_weight)
        timings.append(
            {
                "start": round(cursor, 3),
                "end": round(cursor + span, 3),
                "word": word,
                "text": word,
            }
        )
        cursor += span
    return timings


class TTSService:
    """Synthesize spoken voiceover and return synchronized word timings."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def probe_duration(self, media_path: Path) -> float:
        """Read duration in seconds using ffprobe."""
        ffprobe = self.settings.resolve_ffprobe_bin()
        try:
            proc = subprocess.run(
                [
                    ffprobe,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(media_path),
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            return max(0.5, float(proc.stdout.strip()))
        except Exception:
            return 5.0

    def _synthesize_silent_fallback(self, text: str, out_audio_path: Path) -> list[dict[str, Any]]:
        words = [w for w in text.split() if w.strip()]
        duration = max(3.0, min(55.0, len(words) * 0.42))
        ffmpeg = self.settings.resolve_ffmpeg_bin()
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=44100:cl=mono",
                "-t",
                f"{duration:.2f}",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "128k",
                str(out_audio_path),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return estimate_word_timings(text, duration)

    async def _run_edge_tts(
        self, text: str, voice: str, out_audio_path: Path
    ) -> list[dict[str, Any]]:
        communicate = edge_tts.Communicate(text=text, voice=voice, boundary="WordBoundary")
        word_timings: list[dict[str, Any]] = []
        audio_chunks = bytearray()

        async for chunk in communicate.stream():
            ctype = chunk.get("type")
            if ctype == "audio":
                audio_chunks.extend(chunk["data"])
            elif ctype == "WordBoundary":
                offset_sec = float(chunk.get("offset", 0)) / 10_000_000.0
                dur_sec = float(chunk.get("duration", 0)) / 10_000_000.0
                word_text = str(chunk.get("text", "")).strip()
                if word_text:
                    word_timings.append(
                        {
                            "start": round(offset_sec, 3),
                            "end": round(offset_sec + max(0.08, dur_sec), 3),
                            "word": word_text,
                            "text": word_text,
                        }
                    )

        if not audio_chunks:
            raise RuntimeError("Edge-TTS returned empty audio stream")

        out_audio_path.write_bytes(bytes(audio_chunks))
        if not word_timings:
            duration = self.probe_duration(out_audio_path)
            return estimate_word_timings(text, duration)
        return word_timings

    def _run_elevenlabs(
        self, text: str, voice_id: str, out_audio_path: Path
    ) -> list[dict[str, Any]]:
        vid = voice_id or self.settings.elevenlabs_voice_id or "21m00Tcm4TlvDq8ikWAM"
        endpoint = f"https://api.elevenlabs.io/v1/text-to-speech/{vid}"
        try:
            with httpx.Client(timeout=45.0) as client:
                resp = client.post(
                    endpoint,
                    headers={
                        "xi-api-key": self.settings.elevenlabs_api_key,
                        "Content-Type": "application/json",
                    },
                    json={"text": text, "model_id": "eleven_multilingual_v2"},
                )
                resp.raise_for_status()
                out_audio_path.write_bytes(resp.content)
        except Exception as exc:
            raise RuntimeError(
                redact_secrets(f"ElevenLabs TTS failed: {exc}", self.settings.configured_secrets())
            ) from exc

        duration = self.probe_duration(out_audio_path)
        return estimate_word_timings(text, duration)

    def synthesize_speech(
        self,
        text: str,
        language: str = "id",
        voice: str = "",
        out_audio_path: Path | None = None,
    ) -> list[dict[str, Any]]:
        """Generate TTS MP3 audio at out_audio_path and return word timings."""
        if out_audio_path is None:
            raise ValueError("out_audio_path is required")
        out_audio_path.parent.mkdir(parents=True, exist_ok=True)
        clean_text = " ".join(text.split())
        if not clean_text:
            return self._synthesize_silent_fallback("...", out_audio_path)

        if self.settings.dry_run:
            return self._synthesize_silent_fallback(clean_text, out_audio_path)

        provider = self.settings.tts_provider.lower()
        if provider == "elevenlabs" and self.settings.elevenlabs_api_key:
            return self._run_elevenlabs(clean_text, voice, out_audio_path)

        selected_voice = voice or DEFAULT_VOICES.get(language.lower(), DEFAULT_VOICES["id"])
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop and loop.is_running():
                # Run inside a dedicated thread when called from an active event loop.
                import concurrent.futures

                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(
                        lambda: asyncio.run(self._run_edge_tts(clean_text, selected_voice, out_audio_path))
                    ).result(timeout=60)
            return asyncio.run(self._run_edge_tts(clean_text, selected_voice, out_audio_path))
        except Exception as exc:
            logger.warning("Edge-TTS failed (%s), using silent fallback", exc)
            return self._synthesize_silent_fallback(clean_text, out_audio_path)

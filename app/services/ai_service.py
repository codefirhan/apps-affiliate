"""AI research, viral clip selection, script generation, and cloud Whisper transcription."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.security import redact_secrets

logger = logging.getLogger(__name__)


class AIService:
    """Bilingual (ID/EN) LLM & cloud Whisper transcription adapter."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _has_llm_key(self) -> bool:
        if self.settings.llm_provider.lower() == "openai":
            return bool(self.settings.openai_api_key)
        return bool(self.settings.gemini_api_key)

    def _call_llm_json(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        provider = self.settings.llm_provider.lower()
        try:
            if provider == "openai" and self.settings.openai_api_key:
                with httpx.Client(timeout=45.0) as client:
                    resp = client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.settings.openai_api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.settings.openai_model,
                            "response_format": {"type": "json_object"},
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": user_prompt},
                            ],
                        },
                    )
                    resp.raise_for_status()
                    content = resp.json()["choices"][0]["message"]["content"]
                    return json.loads(content)

            if self.settings.gemini_api_key:
                url = (
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{self.settings.gemini_model}:generateContent"
                )
                with httpx.Client(timeout=45.0) as client:
                    resp = client.post(
                        url,
                        params={"key": self.settings.gemini_api_key},
                        json={
                            "systemInstruction": {"parts": [{"text": system_prompt}]},
                            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                            "generationConfig": {
                                "responseMimeType": "application/json",
                                "temperature": 0.7,
                            },
                        },
                    )
                    resp.raise_for_status()
                    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(text)
        except Exception as exc:
            raise RuntimeError(
                redact_secrets(f"LLM request failed: {exc}", self.settings.configured_secrets())
            ) from exc

        raise RuntimeError("No LLM API key configured")

    def select_viral_clip(
        self,
        transcript_segments: list[dict[str, Any]],
        niche: str,
        language: str = "id",
        translate_to: str | None = None,
        video_title: str = "",
    ) -> dict[str, Any]:
        """Choose the highest-FYP 25-60s segment from a YouTube transcript."""
        target_lang = (translate_to or language or "id").lower()
        if self.settings.dry_run or not self._has_llm_key():
            return self._fallback_viral_clip(transcript_segments, niche, target_lang, video_title)

        compact_transcript = [
            {
                "start": round(float(seg.get("start", 0.0)), 2),
                "end": round(float(seg.get("end", 0.0)), 2),
                "text": str(seg.get("text", ""))[:180],
            }
            for seg in transcript_segments[:120]
        ]

        system_prompt = (
            "You are a TikTok FYP viral editor. Given transcript segments with timestamps, "
            "pick ONE self-contained high-retention 25 to 55 second segment with a strong hook. "
            "Return strict JSON with keys: start_sec (number), end_sec (number), "
            "title_overlay (short 4-7 word hook banner), caption (engaging TikTok description), "
            "hashtags (space-separated #tags), translated_script (natural spoken script in target_lang "
            "if dubbing/translation is requested, else empty string)."
        )
        user_prompt = json.dumps(
            {
                "niche": niche,
                "source_title": video_title,
                "source_language": language,
                "target_language": target_lang,
                "needs_translation_dub": bool(translate_to and translate_to != language),
                "segments": compact_transcript,
            },
            ensure_ascii=False,
        )
        result = self._call_llm_json(system_prompt, user_prompt)
        start_sec = max(0.0, float(result.get("start_sec", 0.0)))
        end_sec = max(start_sec + 15.0, float(result.get("end_sec", start_sec + 35.0)))
        if end_sec - start_sec > 60.0:
            end_sec = start_sec + 55.0

        return {
            "start_sec": round(start_sec, 2),
            "end_sec": round(end_sec, 2),
            "title_overlay": str(result.get("title_overlay") or video_title[:42] or "Fakta Menarik!"),
            "caption": str(result.get("caption") or f"Highlight seputar {niche}"),
            "hashtags": str(result.get("hashtags") or "#fyp #tiktok #viral"),
            "translated_script": str(result.get("translated_script") or ""),
        }

    def generate_faceless_script(
        self,
        niche: str,
        keywords: str = "",
        language: str = "id",
    ) -> dict[str, Any]:
        """Research an FYP angle and generate a complete faceless TikTok script & metadata."""
        lang = (language or "id").lower()
        if self.settings.dry_run or not self._has_llm_key():
            return self._fallback_faceless_script(niche, keywords, lang)

        lang_label = "Indonesian (natural conversational TikTok style)" if lang == "id" else "English (punchy TikTok style)"
        system_prompt = (
            "You are a TikTok FYP content strategist. Create a viral 30-45 second faceless short script. "
            "Return strict JSON with keys: topic, hook, title_overlay (max 6 words), "
            "script (spoken narration, 75-115 words), visual_queries (list of 3 short English stock-video "
            "search phrases), caption, hashtags."
        )
        user_prompt = json.dumps(
            {"niche": niche, "keywords": keywords, "language": lang_label},
            ensure_ascii=False,
        )
        result = self._call_llm_json(system_prompt, user_prompt)
        queries = result.get("visual_queries") or [niche, "technology abstract", "lifestyle"]
        if isinstance(queries, str):
            queries = [q.strip() for q in queries.split(",") if q.strip()]

        return {
            "topic": str(result.get("topic") or niche),
            "hook": str(result.get("hook") or ""),
            "title_overlay": str(result.get("title_overlay") or niche.title())[:60],
            "script": str(result.get("script") or ""),
            "visual_queries": [str(q) for q in queries[:4]],
            "caption": str(result.get("caption") or f"Tips {niche} wajib tahu!"),
            "hashtags": str(result.get("hashtags") or "#fyp #edukasi #viral"),
        }

    def transcribe_audio(self, audio_path: Path, language: str = "id") -> list[dict[str, Any]]:
        """Transcribe an audio file via Groq or OpenAI Whisper API (zero local model RAM)."""
        if self.settings.dry_run or (not self.settings.groq_api_key and not self.settings.openai_api_key):
            return [
                {"start": 0.0, "end": 2.0, "word": "Ini", "text": "Ini"},
                {"start": 2.0, "end": 4.0, "word": "adalah", "text": "adalah"},
                {"start": 4.0, "end": 6.0, "word": "contoh", "text": "contoh"},
                {"start": 6.0, "end": 8.0, "word": "klip", "text": "klip"},
                {"start": 8.0, "end": 10.0, "word": "viral.", "text": "viral."},
            ]

        provider = self.settings.transcribe_provider.lower()
        if provider == "groq" and self.settings.groq_api_key:
            endpoint = "https://api.groq.com/openai/v1/audio/transcriptions"
            api_key = self.settings.groq_api_key
            model = "whisper-large-v3-turbo"
        elif self.settings.openai_api_key:
            endpoint = "https://api.openai.com/v1/audio/transcriptions"
            api_key = self.settings.openai_api_key
            model = "whisper-1"
        else:
            raise RuntimeError("No Groq or OpenAI API key configured for cloud transcription")

        try:
            with audio_path.open("rb") as fh, httpx.Client(timeout=90.0) as client:
                files = {"file": (audio_path.name, fh, "audio/mpeg")}
                data: list[tuple[str, str]] = [
                    ("model", model),
                    ("response_format", "verbose_json"),
                    ("timestamp_granularities[]", "word"),
                    ("timestamp_granularities[]", "segment"),
                ]
                if language:
                    data.append(("language", language))
                resp = client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files=files,
                    data=data,
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            raise RuntimeError(
                redact_secrets(f"Whisper API transcription failed: {exc}", self.settings.configured_secrets())
            ) from exc

        words = payload.get("words") or []
        if words:
            return [
                {
                    "start": float(w.get("start", 0.0)),
                    "end": float(w.get("end", 0.0)),
                    "word": str(w.get("word", "")).strip(),
                    "text": str(w.get("word", "")).strip(),
                }
                for w in words
                if str(w.get("word", "")).strip()
            ]

        segments = payload.get("segments") or []
        return [
            {
                "start": float(s.get("start", 0.0)),
                "end": float(s.get("end", 0.0)),
                "word": str(s.get("text", "")).strip(),
                "text": str(s.get("text", "")).strip(),
            }
            for s in segments
            if str(s.get("text", "")).strip()
        ]

    @staticmethod
    def _fallback_viral_clip(
        transcript_segments: list[dict[str, Any]],
        niche: str,
        target_lang: str,
        video_title: str,
    ) -> dict[str, Any]:
        start_sec = 0.0
        end_sec = 10.0
        if transcript_segments:
            start_sec = max(0.0, float(transcript_segments[0].get("start", 0.0)))
            last_end = float(transcript_segments[-1].get("end", start_sec + 10.0))
            end_sec = min(start_sec + 35.0, max(start_sec + 8.0, last_end))

        if target_lang == "en":
            title_overlay = (video_title[:40] if video_title else f"{niche.title()} Secret")
            caption = f"Wait until the end! Key takeaway about {niche}."
            hashtags = "#fyp #viral #tips #shorts"
            script = f"Wait until you hear this hidden insight about {niche}. Most people miss this detail."
        else:
            title_overlay = (video_title[:40] if video_title else f"Rahasia {niche.title()}")
            caption = f"Jangan skip! Poin penting soal {niche} yang jarang dibahas."
            hashtags = "#fyp #viral #edukasi #tiktokindonesia"
            script = f"Jangan lewatkan insight penting tentang {niche} ini. Banyak orang belum sadar detail kecil yang bikin hasilnya beda jauh."

        return {
            "start_sec": round(start_sec, 2),
            "end_sec": round(end_sec, 2),
            "title_overlay": title_overlay,
            "caption": caption,
            "hashtags": hashtags,
            "translated_script": script,
        }

    @staticmethod
    def _fallback_faceless_script(niche: str, keywords: str, lang: str) -> dict[str, Any]:
        topic = keywords.strip() or niche
        if lang == "en":
            return {
                "topic": topic,
                "hook": f"Stop scrolling if you care about {topic}.",
                "title_overlay": f"3 Facts About {niche.title()}"[:48],
                "script": (
                    f"Stop scrolling if you want to level up in {topic}. "
                    "First, focus on consistency over perfection every single day. "
                    "Second, track what actually gets attention in the first three seconds. "
                    "Save this video so you do not forget."
                ),
                "visual_queries": [niche, "workspace", "city night"],
                "caption": f"Quick reminder for anyone exploring {topic}!",
                "hashtags": "#fyp #productivity #learnontiktok #viral",
            }
        return {
            "topic": topic,
            "hook": f"Banyak yang masih salah soal {topic}!",
            "title_overlay": f"Fakta Penting {niche.title()}"[:48],
            "script": (
                f"Banyak yang masih salah paham soal {topic}. "
                "Pertama, tiga detik pertama menentukan apakah penonton bertahan atau langsung scroll. "
                "Kedua, gunakan subtitle besar yang jelas agar tetap nyaman ditonton tanpa suara. "
                "Simpan video ini supaya tidak lupa."
            ),
            "visual_queries": [niche, "minimal workspace", "abstract motion"],
            "caption": f"Tips singkat seputar {topic} biar makin cepat FYP!",
            "hashtags": "#fyp #belajar #tips #tiktokindonesia",
        }

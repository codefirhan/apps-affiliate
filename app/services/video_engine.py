"""FFmpeg 9:16 portrait video rendering, YouTube clipping, and TikTok .ass subtitles."""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

from app.config import Settings

logger = logging.getLogger(__name__)


def _format_ass_time(seconds: float) -> str:
    sec = max(0.0, float(seconds))
    hours = int(sec // 3600)
    minutes = int((sec % 3600) // 60)
    secs = sec % 60.0
    centis = int(round((secs - int(secs)) * 100))
    whole_sec = int(secs)
    if centis >= 100:
        centis -= 100
        whole_sec += 1
    return f"{hours}:{minutes:02d}:{whole_sec:02d}.{centis:02d}"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
        .replace("\n", r"\N")
    )


def generate_tiktok_ass_subtitles(
    word_timings: list[dict[str, Any]],
    out_ass_path: Path,
    title_overlay: str = "",
    words_per_chunk: int = 4,
    total_duration: float = 60.0,
) -> Path:
    """Create a 1080x1920 .ass subtitle file with TikTok-safe captions and optional top title."""
    header = """[Script Info]
Title: TikTok Auto-Pilot Subtitles
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,DejaVu Sans,68,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,1,0,0,0,100,100,0,0,1,5,2,2,60,60,420,1
Style: TitleBanner,DejaVu Sans,58,&H0000FFFF,&H00FFFFFF,&H00000000,&HCC101010,1,0,0,0,100,100,0,0,3,16,0,8,70,70,190,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]

    clean_title = " ".join(title_overlay.split()).strip()
    max_end = max([float(w.get("end", 0.0)) for w in word_timings] + [total_duration, 5.0])
    if clean_title:
        lines.append(
            f"Dialogue: 1,0:00:00.00,{_format_ass_time(max_end)},TitleBanner,,0,0,0,,{_escape_ass_text(clean_title)}"
        )

    cleaned_words = [
        w for w in word_timings if str(w.get("word") or w.get("text") or "").strip()
    ]
    for idx in range(0, len(cleaned_words), max(1, words_per_chunk)):
        chunk = cleaned_words[idx : idx + words_per_chunk]
        start = float(chunk[0].get("start", 0.0))
        end = max(start + 0.3, float(chunk[-1].get("end", start + 0.8)))
        text = " ".join(str(item.get("word") or item.get("text")).strip() for item in chunk)
        lines.append(
            f"Dialogue: 0,{_format_ass_time(start)},{_format_ass_time(end)},Caption,,0,0,0,,{_escape_ass_text(text.upper())}"
        )

    out_ass_path.parent.mkdir(parents=True, exist_ok=True)
    out_ass_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_ass_path


class VideoEngine:
    """FFmpeg & yt-dlp composition engine tuned for 2 vCPU / 2 GB RAM."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def probe_duration(self, media_path: Path) -> float:
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
            return 8.0

    def extract_thumbnail(self, mp4_path: Path, out_jpg_path: Path) -> Path:
        """Grab a vivid mid-action JPEG frame (with burned title & subtitle) for cover thumbnail."""
        ffmpeg = self.settings.resolve_ffmpeg_bin()
        dur = self.probe_duration(mp4_path)
        seek_sec = min(2.5, max(0.5, dur * 0.25))
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-ss",
                f"{seek_sec:.2f}",
                "-i",
                str(mp4_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
                "-q:v",
                "2",
                str(out_jpg_path),
            ],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return out_jpg_path

    def download_youtube_section(
        self,
        source_url: str,
        start_sec: float,
        end_sec: float,
        out_raw_path: Path,
        work_dir: Path,
    ) -> Path:
        """Download clip from YouTube (section) or TikTok trending URL via yt-dlp."""
        if self.settings.dry_run:
            return self._generate_placeholder_clip(out_raw_path, max(3.0, end_sec - start_sec))

        ffmpeg_location = str(Path(self.settings.resolve_ffmpeg_bin()).parent)
        is_tiktok = "tiktok.com" in source_url.lower()

        if is_tiktok:
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-playlist",
                "--ffmpeg-location",
                ffmpeg_location,
                "-f",
                "best[ext=mp4]/best",
                "--merge-output-format",
                "mp4",
                "-o",
                str(out_raw_path),
                source_url,
            ]
        else:
            section = f"*{max(0.0, start_sec):.2f}-{max(start_sec + 5.0, end_sec):.2f}"
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-playlist",
                "--ffmpeg-location",
                ffmpeg_location,
                "--download-sections",
                section,
                "-f",
                "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]/best",
                "--merge-output-format",
                "mp4",
                "-o",
                str(out_raw_path),
                source_url,
            ]
        proc = subprocess.run(cmd, cwd=str(work_dir), capture_output=True, text=True, check=False, timeout=210)
        if not out_raw_path.exists():
            matches = sorted(work_dir.glob(f"{out_raw_path.stem}*"))
            if matches:
                matches[0].rename(out_raw_path)
        if not out_raw_path.exists():
            stderr_tail = (proc.stderr or proc.stdout or "")[-400:]
            raise FileNotFoundError(f"yt-dlp failed producing clip: {stderr_tail}")
        return out_raw_path

    def render_youtube_clip(
        self,
        raw_clip_path: Path,
        ass_path: Path,
        out_mp4_path: Path,
        work_dir: Path,
        dub_audio_path: Path | None = None,
    ) -> Path:
        """Reframe clip to 9:16 1080x1920 with blurred backdrop + full centered frame and .ass subtitles."""
        ffmpeg = self.settings.resolve_ffmpeg_bin()
        ass_rel = ass_path.name
        vf = (
            "[0:v]split=2[bg][fg];"
            "[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=16:4[bgblur];"
            "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fgscale];"
            f"[bgblur][fgscale]overlay=(W-w)/2:(H-h)/2,ass={ass_rel}[vout]"
        )

        if dub_audio_path and dub_audio_path.exists():
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(raw_clip_path),
                "-i",
                str(dub_audio_path),
                "-filter_complex",
                vf,
                "-map",
                "[vout]",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-threads",
                "2",
                "-r",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_mp4_path),
            ]
        else:
            cmd = [
                ffmpeg,
                "-y",
                "-i",
                str(raw_clip_path),
                "-filter_complex",
                vf,
                "-map",
                "[vout]",
                "-map",
                "0:a:0?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-threads",
                "2",
                "-r",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-movflags",
                "+faststart",
                str(out_mp4_path),
            ]

        subprocess.run(cmd, cwd=str(work_dir), capture_output=True, text=True, check=True, timeout=240)
        return out_mp4_path

    def render_faceless_video(
        self,
        media_files: list[Path],
        dub_audio_path: Path,
        ass_path: Path,
        out_mp4_path: Path,
        work_dir: Path,
    ) -> Path:
        """Compose stock clips/images into 1080x1920 video synced to dub audio + burned subtitles."""
        ffmpeg = self.settings.resolve_ffmpeg_bin()
        audio_dur = self.probe_duration(dub_audio_path)

        if not media_files:
            placeholder = work_dir / "bg_placeholder.mp4"
            self._generate_placeholder_clip(placeholder, audio_dur)
            media_files = [placeholder]

        normalized_segments: list[Path] = []
        per_clip_dur = max(2.0, audio_dur / max(1, len(media_files)))
        for idx, media in enumerate(media_files):
            seg_out = work_dir / f"seg_{idx}.mp4"
            is_image = media.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
            if is_image:
                cmd = [
                    ffmpeg,
                    "-y",
                    "-loop",
                    "1",
                    "-t",
                    f"{per_clip_dur:.2f}",
                    "-i",
                    str(media),
                    "-vf",
                    "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-threads",
                    "2",
                    "-r",
                    "30",
                    "-pix_fmt",
                    "yuv420p",
                    str(seg_out),
                ]
            else:
                cmd = [
                    ffmpeg,
                    "-y",
                    "-stream_loop",
                    "-1",
                    "-t",
                    f"{per_clip_dur:.2f}",
                    "-i",
                    str(media),
                    "-vf",
                    "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,setsar=1",
                    "-an",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "veryfast",
                    "-threads",
                    "2",
                    "-r",
                    "30",
                    "-pix_fmt",
                    "yuv420p",
                    str(seg_out),
                ]
            subprocess.run(cmd, cwd=str(work_dir), capture_output=True, text=True, check=True, timeout=120)
            normalized_segments.append(seg_out)

        concat_file = work_dir / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{seg.name}'" for seg in normalized_segments) + "\n",
            encoding="utf-8",
        )
        merged_video = work_dir / "merged_silent.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file.name,
                "-c",
                "copy",
                str(merged_video),
            ],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            check=True,
            timeout=90,
        )

        ass_rel = ass_path.name
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                merged_video.name,
                "-i",
                str(dub_audio_path),
                "-vf",
                f"ass={ass_rel}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-threads",
                "2",
                "-r",
                "30",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_mp4_path),
            ],
            cwd=str(work_dir),
            capture_output=True,
            text=True,
            check=True,
            timeout=240,
        )
        return out_mp4_path

    def _generate_placeholder_clip(self, out_path: Path, duration: float) -> Path:
        ffmpeg = self.settings.resolve_ffmpeg_bin()
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c=#0f172a:s=1080x1920:r=30:d={duration:.2f}",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=44100:cl=mono:d={duration:.2f}",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-threads",
                "2",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-shortest",
                str(out_path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return out_path

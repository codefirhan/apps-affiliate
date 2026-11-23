"""Ephemeral per-job temp directory manager guaranteeing zero persistent local media."""

from __future__ import annotations

import logging
import re
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _jobs_base(temp_root: str) -> Path:
    return Path(temp_root) / "jobs"


@contextmanager
def isolated_job_dir(job_id: str | int, temp_root: str) -> Iterator[Path]:
    """Create an isolated temp directory for a job and always delete it on exit."""
    safe_id = _SAFE_ID_RE.sub("_", str(job_id))
    job_dir = _jobs_base(temp_root) / f"job_{safe_id}_{int(time.time() * 1000)}"
    job_dir.mkdir(parents=True, exist_ok=True)
    try:
        yield job_dir
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.debug("Cleaned up isolated job temp dir: %s", job_dir)


def sweep_orphan_temp_dirs(temp_root: str, max_age_seconds: int = 3600) -> int:
    """Delete leftover temp job directories older than max_age_seconds."""
    base = _jobs_base(temp_root)
    if not base.exists():
        return 0

    now = time.time()
    removed = 0
    for entry in base.iterdir():
        try:
            age = now - entry.stat().st_mtime
            if age >= max_age_seconds:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:
            continue
        except Exception as exc:
            logger.warning("Failed to sweep orphan temp entry %s: %s", entry, exc)
    return removed


def count_active_temp_dirs(temp_root: str) -> int:
    """Count any job directories currently residing in temp_root/jobs."""
    base = _jobs_base(temp_root)
    if not base.exists():
        return 0
    return sum(1 for entry in base.iterdir() if entry.is_dir())

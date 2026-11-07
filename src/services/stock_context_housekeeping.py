"""Bounded logging and cleanup of explicitly retired engine artifacts."""

from __future__ import annotations

import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def remove_retired_artifacts(directory: Path, *, older_than_seconds: float = 86_400) -> int:
    """Remove only closed, explicitly retired context artifacts from this directory."""
    if older_than_seconds <= 0:
        raise ValueError("Artifact retention must be positive.")
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Artifact directory must be an existing non-symlink directory.")
    now = time.time()
    removed = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.startswith("retired-stock-context-") or not entry.name.endswith(
                (".frame", ".stream", ".cache")
            ):
                continue
            if not entry.is_file(follow_symlinks=False):
                continue
            if now - entry.stat(follow_symlinks=False).st_mtime < older_than_seconds:
                continue
            os.unlink(entry.path)
            removed += 1
    return removed


class ContextRotatingFileHandler(RotatingFileHandler):
    """Rotate context logs and clean explicitly retired artifacts on rollover."""

    def __init__(self, directory: Path, *, max_bytes: int = 2_000_000,
                 backup_count: int = 3, retention_seconds: float = 86_400):
        if max_bytes <= 0 or backup_count < 1 or retention_seconds <= 0:
            raise ValueError("Rotation size, backup count and retention must be positive.")
        directory = Path(directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Artifact directory must be an existing non-symlink directory.")
        self.artifact_directory = directory.resolve()
        self.retention_seconds = retention_seconds
        super().__init__(self.artifact_directory / "stock-context.log", maxBytes=max_bytes,
                         backupCount=backup_count, encoding="utf-8", delay=True)

    def doRollover(self) -> None:
        super().doRollover()
        remove_retired_artifacts(self.artifact_directory, older_than_seconds=self.retention_seconds)

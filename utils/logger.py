"""
utils/logger.py

Structured logging setup + a lightweight SyncStats tracker used to report
success/failure counts and durations per service, per run.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def setup_logger(name: str = "bc_sync", level: int = logging.INFO) -> logging.Logger:
    """
    Configures and returns a logger that writes to both the console and a
    daily-rotating file under logs/. Safe to call multiple times (won't
    duplicate handlers).
    """
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(level)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    file_handler = TimedRotatingFileHandler(
        filename=str(LOG_DIR / "sync.log"),
        when="midnight",
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    logger.propagate = False
    return logger


@dataclass
class SyncStats:
    """Per-service counters for a single sync run."""

    service_name: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    failed: int = 0
    skipped: int = 0   # dropped, not errors (e.g. keyless rows)
    pages_fetched: int = 0
    error_message: str | None = None
    status: str = "in_progress"  # in_progress | success | partial_failure | failed

    def mark_finished(self, status: str = "success", error_message: str | None = None) -> None:
        self.finished_at = time.time()
        self.status = status
        self.error_message = error_message

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.started_at, 2)

    def to_dict(self) -> dict:
        return {
            "service_name": self.service_name,
            "status": self.status,
            "fetched": self.fetched,
            "inserted": self.inserted,
            "updated": self.updated,
            "failed": self.failed,
            "skipped": self.skipped,
            "pages_fetched": self.pages_fetched,
            "duration_seconds": self.duration_seconds,
            "error_message": self.error_message,
        }

    def summary_line(self) -> str:
        return (
            f"[{self.service_name}] status={self.status} "
            f"fetched={self.fetched} inserted={self.inserted} updated={self.updated} "
            f"failed={self.failed} skipped={self.skipped} pages={self.pages_fetched} "
            f"duration={self.duration_seconds}s"
        )


def log_run_summary(logger: logging.Logger, stats_list: list[SyncStats]) -> None:
    """Logs a clean summary block at the end of a run and writes a JSON
    summary file to logs/ for machine-readable monitoring (e.g. CI artifacts)."""
    logger.info("=" * 70)
    logger.info("SYNC RUN SUMMARY")
    total_fetched = total_failed = 0
    for stats in stats_list:
        logger.info(stats.summary_line())
        total_fetched += stats.fetched
        total_failed += stats.failed
    logger.info(f"TOTAL: fetched={total_fetched} failed={total_failed}")
    logger.info("=" * 70)

    summary_path = LOG_DIR / f"run_summary_{int(time.time())}.json"
    summary_path.write_text(
        json.dumps([s.to_dict() for s in stats_list], indent=2), encoding="utf-8"
    )

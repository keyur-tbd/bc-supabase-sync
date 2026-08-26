"""
models/sync_state.py

Typed representation of a row in the etl_sync_state table in Supabase, used by
sync_service.py to decide whether to run a chunked historical backfill or
an incremental pull, and where to resume from after a crash.

Two distinct watermarks are tracked because a single one cannot serve both
phases when the backfill axis differs from the incremental axis (e.g. GL is
back-filled by Posting_Date but kept current by SystemCreatedAt):

- history_through : how far the chunked backfill has progressed along the
                    history_field date axis (a completed-window upper bound).
                    Lets an interrupted backfill resume at the next window
                    instead of re-pulling everything.
- last_sync_at    : the incremental watermark — the value the NEXT
                    incremental run filters incremental_field against. Set to
                    "now" when the backfill completes, then advanced each
                    incremental run.

resume_url is the intra-window pagination link (BC @odata.nextLink) for
page-level resume within whichever window/filter was in flight.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any


@dataclass
class SyncState:
    service_name: str
    status: str = "never_run"
    last_sync_at: datetime | None = None
    history_through: date | None = None
    resume_url: str | None = None
    records_processed: int = 0
    records_failed: int = 0
    last_error: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any] | None, service_name: str) -> "SyncState":
        if row is None:
            return cls(service_name=service_name)
        return cls(
            service_name=service_name,
            status=row.get("status", "never_run"),
            last_sync_at=row.get("last_sync_at"),
            history_through=row.get("history_through"),
            resume_url=row.get("resume_url"),
            records_processed=row.get("records_processed", 0) or 0,
            records_failed=row.get("records_failed", 0) or 0,
            last_error=row.get("last_error"),
        )

    @property
    def has_incomplete_run(self) -> bool:
        return bool(self.resume_url)

    @property
    def backfill_done(self) -> bool:
        """Backfill is considered complete once an incremental watermark has
        been established (set when the last historical window finishes)."""
        return self.last_sync_at is not None

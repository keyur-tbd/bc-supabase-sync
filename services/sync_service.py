"""
services/sync_service.py

Orchestrates the end-to-end sync for one or more Business Central web
services.

Two phases, chosen per service from its sync state:

1. BACKFILL (historical): used on --mode full, or automatically on the
   first ever run. The [sync_start_date, today] range is sliced into
   windows (monthly by default) along the service's `history_field`
   (a populated business date such as Posting_Date / Document_Date), and
   each window is paged to exhaustion. After each window completes,
   `history_through` advances, so an interrupted backfill resumes at the
   next window instead of re-pulling everything. When the final window
   finishes, an incremental watermark is established (set to "now"),
   handing the service over to incremental mode.

2. INCREMENTAL: once a watermark exists, pulls only records at/after
   (watermark - lookback) on the service's `incremental_field`. For tables
   that expose a row-creation timestamp (e.g. GL's SystemCreatedAt) this
   reliably catches newly-created back-dated entries; for date-only tables
   the lookback buffer re-scans a trailing window to catch late postings.

The watermark is always set from wall-clock "now" (clamped, never a value
read out of the data) so a corrupt future-dated row — e.g. a Posting_Date
of 2031 — can never poison it and freeze the sync.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

from config import BCConfig, SupabaseConfig, load_web_services
from models.sync_state import SyncState
from services.auth_service import BCAuthService
from services.bc_api_service import BCApiService
from services.supabase_service import SupabaseService
from utils.date_chunker import build_range_filter, iter_windows
from utils.logger import SyncStats
from utils.retry_helper import NonRetryableError
from utils.schema_helper import normalize_column_name, normalize_pk, prepare_row

logger = logging.getLogger("bc_sync")

# Defaults applied when a service entry doesn't specify them.
DEFAULT_HISTORY_FIELD = "Posting_Date"
DEFAULT_INCREMENTAL_FIELD = "Posting_Date"
DEFAULT_CHUNK = "monthly"
DEFAULT_SYNC_START = "2024-01-01"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


class SyncService:
    def __init__(self, bc_config: BCConfig, db_config: SupabaseConfig):
        self._bc_config = bc_config
        self._auth = BCAuthService(bc_config)
        self._api = BCApiService(bc_config, self._auth)
        self._db = SupabaseService(db_config)

    def run(self, mode: str = "incremental", service_filter: list[str] | None = None) -> list[SyncStats]:
        services = load_web_services()
        if service_filter:
            services = [s for s in services if s["name"] in service_filter]
            if not services:
                raise ValueError(f"No matching services for filter: {service_filter}")

        if not self._db.test_connection():
            raise RuntimeError("Could not connect to Supabase/Postgres — aborting run.")

        all_stats = []
        for service in services:
            all_stats.append(self._sync_one_service(service, mode))
        return all_stats

    # ------------------------------------------------------------------ #

    def _sync_one_service(self, service: dict, mode: str) -> SyncStats:
        name = service["name"]
        table_name = service["table_name"]
        primary_key = service.get("primary_key", "SystemId")
        history_field = service.get("history_field", DEFAULT_HISTORY_FIELD)
        incremental_field = service.get("incremental_field", DEFAULT_INCREMENTAL_FIELD)
        incremental_is_datetime = bool(service.get("incremental_is_datetime", False))
        granularity = service.get("chunk", DEFAULT_CHUNK)
        lookback_days = int(service.get("lookback_days", self._bc_config.default_lookback_days))
        sync_start = _parse_date(service.get("sync_start_date", DEFAULT_SYNC_START))

        stats = SyncStats(service_name=name)
        state = SyncState.from_row(self._db.get_sync_state(name), name)

        self._guard_primary_key(table_name, primary_key, name)
        self._db.mark_run_started(name)

        # Choose phase: explicit --mode full forces backfill; otherwise
        # backfill until a watermark exists, then incremental.
        strategy = service.get("sync_strategy", "date")
        do_backfill = (mode == "full") or (not state.backfill_done)

        try:
            if strategy == "full_refresh":
                # No usable timestamp/key on this table: re-pull everything
                # every run. mode is irrelevant here.
                processed, failed = self._run_full_refresh(
                    state, stats, name, table_name, primary_key,
                )
            elif do_backfill:
                processed, failed = self._run_backfill(
                    service, state, stats, name, table_name, primary_key,
                    history_field, granularity, sync_start,
                )
            else:
                processed, failed = self._run_incremental(
                    service, state, stats, name, table_name, primary_key,
                    incremental_field, incremental_is_datetime, lookback_days,
                )

            status = "success" if failed == 0 else "partial_failure"
            self._db.mark_run_completed(name, status=status, records_processed=processed, records_failed=failed)
            # Establish/advance the incremental watermark only on a clean,
            # fully-fetched run. Always "now" (clamped wall clock), never a
            # value pulled from the data.
            if status == "success":
                self._db.set_incremental_watermark(name, _utc_now())
            stats.mark_finished(status=status)

        except NonRetryableError as exc:
            logger.error(f"[{name}] Non-retryable failure: {exc}")
            self._db.mark_run_completed(name, status="failed", records_processed=stats.inserted,
                                        records_failed=stats.failed, error=str(exc))
            stats.mark_finished(status="failed", error_message=str(exc))
        except Exception as exc:  # noqa: BLE001 - top-level run boundary
            logger.exception(f"[{name}] Unexpected failure during sync.")
            self._db.mark_run_completed(name, status="failed", records_processed=stats.inserted,
                                        records_failed=stats.failed, error=str(exc))
            stats.mark_finished(status="failed", error_message=str(exc))

        return stats

    # ------------------------------------------------------------------ #

    def _run_backfill(
        self, service, state, stats, name, table_name, primary_key,
        history_field, granularity, sync_start,
    ) -> tuple[int, int]:
        # Resume point: start at the furthest completed window, never before
        # the configured floor. Clearing the etl_sync_state row forces a
        # clean restart from sync_start_date.
        start = max(sync_start, state.history_through) if state.history_through else sync_start
        today = date.today()
        end_exclusive = today + timedelta(days=1)  # include today's postings

        if start >= end_exclusive:
            logger.info(f"[{name}] Backfill already current (through {state.history_through}). Nothing to do.")
            return 0, 0

        windows = list(iter_windows(start, end_exclusive, granularity))
        logger.info(
            f"[{name}] Backfill on {history_field} from {start} to {today} "
            f"({len(windows)} {granularity} window(s))."
            + (" Clear etl_sync_state to force a full restart." if state.history_through else "")
        )

        total_processed = 0
        total_failed = 0
        first_window = True

        for w_start, w_end in windows:
            odata_filter = build_range_filter(history_field, w_start, w_end, is_datetime=False)
            # The stored resume_url (if any) belongs to the first, in-flight
            # window — feed it only there.
            resume_url = state.resume_url if (first_window and state.has_incomplete_run) else None
            first_window = False
            logger.info(f"[{name}] Window {w_start} .. {w_end}")

            for records, next_url in self._api.fetch_pages(name, odata_filter=odata_filter, resume_url=resume_url):
                p, f = self._process_page(
                    name, table_name, primary_key, history_field, records, stats
                )
                total_processed += p
                total_failed += f
                self._db.save_resume_point(name, next_url, total_processed)

            # Window finished — advance the durable backfill marker.
            self._db.save_history_progress(name, w_end)

        return total_processed, total_failed

    def _run_incremental(
        self, service, state, stats, name, table_name, primary_key,
        incremental_field, incremental_is_datetime, lookback_days,
    ) -> tuple[int, int]:
        watermark = state.last_sync_at or _utc_now()
        floor = watermark - timedelta(days=lookback_days)
        if not incremental_is_datetime:
            floor = floor.date() if isinstance(floor, datetime) else floor
        odata_filter = build_range_filter(
            incremental_field, floor, None, is_datetime=incremental_is_datetime
        )
        resume_url = state.resume_url if state.has_incomplete_run else None
        logger.info(
            f"[{name}] Incremental on {incremental_field} ge {floor} "
            f"(watermark {watermark}, lookback {lookback_days}d)."
        )

        total_processed = 0
        total_failed = 0
        for records, next_url in self._api.fetch_pages(name, odata_filter=odata_filter, resume_url=resume_url):
            p, f = self._process_page(name, table_name, primary_key, incremental_field, records, stats)
            total_processed += p
            total_failed += f
            self._db.save_resume_point(name, next_url, total_processed)

        return total_processed, total_failed

    def _run_full_refresh(self, state, stats, name, table_name, primary_key) -> tuple[int, int]:
        """For tables with no usable timestamp and no monotonic key: re-pull
        the ENTIRE table every run (no $filter) and upsert. Correct for both
        append-only and mutable reference tables; cost scales with table
        size, so it suits small/medium tables. Still resumable mid-pull via
        the stored nextLink."""
        resume_url = state.resume_url if state.has_incomplete_run else None
        logger.info(f"[{name}] Full-refresh (no incremental key) — pulling entire table and upserting.")
        total_processed = 0
        total_failed = 0
        for records, next_url in self._api.fetch_pages(name, odata_filter=None, resume_url=resume_url):
            p, f = self._process_page(name, table_name, primary_key, None, records, stats)
            total_processed += p
            total_failed += f
            self._db.save_resume_point(name, next_url, total_processed)
        return total_processed, total_failed

    # ------------------------------------------------------------------ #

    def _process_page(self, name, table_name, primary_key, date_field, records, stats) -> tuple[int, int]:
        stats.pages_fetched += 1
        stats.fetched += len(records)
        if not records:
            return 0, 0

        rows: list[dict] = []
        json_cols: set[str] = set()
        for r in records:
            row, cols = prepare_row(r)
            rows.append(row)
            json_cols |= cols

        self._db.ensure_table(table_name, rows, primary_key, json_columns=json_cols)

        # Flag (but do not drop) rows whose date_field is implausibly in the
        # future — almost always data-entry errors in BC. Skipped when the
        # strategy has no date field (full_refresh).
        if date_field:
            self._flag_future_dated(table_name, date_field, rows)

        succeeded, failed = self._db.upsert_rows(
            table_name=table_name, rows=rows, primary_key=primary_key, raw_records=records,
        )
        stats.inserted += succeeded
        stats.failed += failed
        return succeeded, failed

    def _flag_future_dated(self, table_name: str, date_field: str, rows: list[dict]) -> None:
        col = normalize_column_name(date_field)
        today = date.today()
        future = []
        for row in rows:
            v = row.get(col)
            d = v.date() if isinstance(v, datetime) else v
            if isinstance(d, date) and d > today:
                future.append(row)
        if future:
            self._db._save_quarantine(  # noqa: SLF001 - intentional internal reuse
                table_name, future, f"{col} is in the future (kept in table, flagged for review)"
            )

    def _guard_primary_key(self, table_name: str, configured_pk: str, name: str) -> None:
        actual = self._db.actual_primary_key(table_name)
        if actual is None:
            return  # table doesn't exist yet — will be created with the right PK
        expected_cols = set(normalize_pk(configured_pk))
        actual_cols = {c.strip() for c in actual.split(",")}
        if expected_cols != actual_cols:
            logger.warning(
                f"[{name}] PRIMARY KEY mismatch on `{table_name}`: table key is "
                f"`{actual}` but config expects `{','.join(normalize_pk(configured_pk))}`. "
                f"Upserts dedupe on the TABLE's key, so this can cause duplicates or "
                f"insert failures. Migrate the table (drop/re-add PRIMARY KEY) — editing "
                f"web_services.json alone won't fix it."
            )

    # ------------------------------------------------------------------ #

    def ensure_tables_for_first_run(self, sample_size: int = 50) -> None:
        """Pre-creates tables for all configured services by pulling a small
        sample page each, useful before the first real run."""
        for service in load_web_services():
            name = service["name"]
            try:
                for records, _ in self._api.fetch_pages(name):
                    if records:
                        rows, json_cols = [], set()
                        for r in records[:sample_size]:
                            row, cols = prepare_row(r)
                            rows.append(row)
                            json_cols |= cols
                        self._db.ensure_table(
                            service["table_name"], rows, service.get("primary_key", "SystemId"),
                            json_columns=json_cols,
                        )
                    break  # only need the first page for schema discovery
            except Exception:
                logger.exception(f"Failed to pre-create table for {name}")

    def close(self) -> None:
        self._db.dispose()

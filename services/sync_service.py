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
from utils.date_chunker import build_incremental_filter, build_range_filter, iter_windows
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


def _odata_str(value: str) -> str:
    """Single-quoted OData string literal; an embedded quote is doubled."""
    return "'" + str(value).replace("'", "''") + "'"


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


class SyncService:
    def __init__(self, bc_config: BCConfig, db_config: SupabaseConfig):
        self._bc_config = bc_config
        self._auth = BCAuthService(bc_config)
        # Which endpoint family each service lives on ("odata" | "v2.0").
        _services = load_web_services()
        self._api = BCApiService(
            bc_config, self._auth,
            service_apis={s["name"]: s.get("api", "odata") for s in _services},
            service_page_sizes={s["name"]: s["max_page_size"]
                                for s in _services if s.get("max_page_size")},
        )
        self._db = SupabaseService(db_config)

    def run(self, mode: str = "incremental", service_filter: list[str] | None = None) -> list[SyncStats]:
        services = load_web_services()
        if service_filter:
            services = [s for s in services if s["name"] in service_filter]
            if not services:
                raise ValueError(f"No matching services for filter: {service_filter}")

        if not self._db.test_connection():
            raise RuntimeError("Could not connect to Supabase/Postgres — aborting run.")

        # Disk guard: checked before the run and again before every service,
        # so a long backfill stops itself long before the volume fills.
        status = self._db.check_disk_headroom()
        if status:
            logger.info(status)

        all_stats = []
        for service in services:
            if all_stats:
                status = self._db.check_disk_headroom()
                if status:
                    logger.info(status)
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
        # Some pages legitimately emit rows with no primary key - e.g. credit
        # memo lines with no product on them. Those cannot be upserted and are
        # not wanted, so dropping them quietly is correct; quarantining them
        # would refill quarantine/ every run and pin the service at
        # partial_failure forever.
        quarantine_invalid = bool(service.get("quarantine_missing_pk", True))
        # Key columns where '' is a real value (blank Ship_To_Code = whole
        # customer) rather than a missing key.
        self._db.pk_allow_empty[table_name] = set(service.get("pk_allow_empty", []))

        try:
            if strategy == "series_key":
                # No date field on the page at all, but the business key is a
                # BC no.-series (26CNMUM-00001 ...) that only ever grows.
                processed, failed = self._run_series_key(
                    service, state, stats, name, table_name, primary_key, mode,
                    quarantine_invalid=quarantine_invalid,
                )
            elif strategy == "full_refresh":
                # No usable timestamp/key on this table: re-pull everything
                # every run. mode is irrelevant here.
                processed, failed = self._run_full_refresh(
                    state, stats, name, table_name, primary_key,
                    quarantine_invalid=quarantine_invalid,
                )
            elif do_backfill:
                processed, failed = self._run_backfill(
                    service, state, stats, name, table_name, primary_key,
                    history_field, granularity, sync_start,
                    quarantine_invalid=quarantine_invalid,
                )
            else:
                processed, failed = self._run_incremental(
                    service, state, stats, name, table_name, primary_key,
                    incremental_field, incremental_is_datetime, lookback_days,
                    quarantine_invalid=quarantine_invalid,
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
        history_field, granularity, sync_start, quarantine_invalid: bool = True,
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
                    name, table_name, primary_key, history_field, records, stats,
                    quarantine_invalid=quarantine_invalid,
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
        quarantine_invalid: bool = True,
    ) -> tuple[int, int]:
        watermark = state.last_sync_at or _utc_now()
        floor = watermark - timedelta(days=lookback_days)
        if not incremental_is_datetime:
            floor = floor.date() if isinstance(floor, datetime) else floor
        include_undated = bool(service.get("incremental_include_undated", False))
        odata_filter = build_incremental_filter(
            incremental_field, floor, is_datetime=incremental_is_datetime,
            include_undated=include_undated,
        )
        resume_url = state.resume_url if state.has_incomplete_run else None
        logger.info(
            f"[{name}] Incremental on {incremental_field} ge {floor} "
            f"(watermark {watermark}, lookback {lookback_days}d"
            + (", incl. undated" if include_undated else "") + ")."
        )

        total_processed = 0
        total_failed = 0
        for records, next_url in self._api.fetch_pages(name, odata_filter=odata_filter, resume_url=resume_url):
            p, f = self._process_page(name, table_name, primary_key, incremental_field, records, stats,
                                      quarantine_invalid=quarantine_invalid)
            total_processed += p
            total_failed += f
            self._db.save_resume_point(name, next_url, total_processed)

        # Mutable ledgers (e.g. customer ledger entries) change long after
        # they were posted - an invoice from last year gets Closed_at_Date /
        # Open / Remaining_Amount updated when a payment is applied. BC
        # refuses "A ge x or B ge x" across distinct fields (501), so each
        # extra field is a separate pass over the same floor. Upserts are
        # idempotent, so overlap between passes is harmless.
        for extra_field in service.get("incremental_extra_fields", []) or []:
            extra_filter = build_incremental_filter(extra_field, floor, is_datetime=incremental_is_datetime)
            logger.info(f"[{name}] Incremental extra pass on {extra_field} ge {floor}.")
            for records, next_url in self._api.fetch_pages(name, odata_filter=extra_filter):
                p, f = self._process_page(name, table_name, primary_key, None, records, stats,
                                          quarantine_invalid=quarantine_invalid)
                total_processed += p
                total_failed += f
                self._db.save_resume_point(name, next_url, total_processed)

        return total_processed, total_failed

    def _run_series_key(
        self, service, state, stats, name, table_name, primary_key, mode,
        quarantine_invalid: bool = True,
    ) -> tuple[int, int]:
        """For pages with no date field whose key is a BC no.-series.

        Posted documents are immutable and their numbers only ever increase
        within a series, so "everything above the highest number already
        stored" is a complete and correct incremental window.

        One request per series, because BC rejects an OR of AND-groups with
        501 BadRequest_MethodNotImplemented - the whole thing cannot be
        expressed as a single filter. It also rejects "not (...)", so a
        brand-new series cannot be discovered from the API; series_source
        names a parent table (synced by date) to learn them from instead.

        The watermark is re-derived from the stored data on every run, so an
        interrupted run resumes correctly with no bookkeeping - provided
        pages arrive in ascending key order, which is why $orderby is sent.
        """
        field = service.get("series_key_field", "Document_No")
        sep = service.get("series_separator", "-")
        src = service.get("series_source") or {}

        highs = self._db.max_by_series(table_name, field, sep)

        # First ever run, or an explicit --mode full: take the lot, unfiltered
        # - unless a series_source is configured, in which case the parent
        # table (synced by date, so already limited to sync_start_date)
        # defines which series are in scope and older years are skipped.
        if (mode == "full" or not highs) and not (src.get("table") and src.get("column")):
            why = "--mode full" if mode == "full" else "no rows stored yet"
            logger.info(f"[{name}] Series-key: full pull ({why}).")
            total_p = total_f = 0
            for records, next_url in self._api.fetch_pages(name, order_by=field):
                p, f = self._process_page(name, table_name, primary_key, None, records, stats,
                                          quarantine_invalid=quarantine_invalid)
                total_p += p
                total_f += f
                self._db.save_resume_point(name, next_url, total_p)
            return total_p, total_f

        series = set(highs)
        if mode == "full":
            highs = {}   # re-pull every in-scope series from its start
        if src.get("table") and src.get("column"):
            discovered = self._db.distinct_series(src["table"], src["column"], sep)
            new = discovered - series
            if new:
                logger.info(f"[{name}] Series-key: {len(new)} new series from "
                            f"{src['table']}: {sorted(new)}")
            series |= discovered

        logger.info(f"[{name}] Series-key on {field}: {len(series)} series, "
                    f"one request each, above each series' stored maximum.")
        total_p = total_f = 0
        for s in sorted(series):
            high = highs.get(s)
            prefix = _odata_str(s)
            odata_filter = f"startswith({field},{prefix})"
            if high:
                odata_filter += f" and {field} gt {_odata_str(high)}"
            for records, next_url in self._api.fetch_pages(
                name, odata_filter=odata_filter, order_by=field
            ):
                p, f = self._process_page(name, table_name, primary_key, None, records, stats,
                                          quarantine_invalid=quarantine_invalid)
                total_p += p
                total_f += f
                self._db.save_resume_point(name, next_url, total_p)
        return total_p, total_f

    def _run_full_refresh(self, state, stats, name, table_name, primary_key,
                          quarantine_invalid: bool = True) -> tuple[int, int]:
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
            p, f = self._process_page(name, table_name, primary_key, None, records, stats,
                                      quarantine_invalid=quarantine_invalid)
            total_processed += p
            total_failed += f
            self._db.save_resume_point(name, next_url, total_processed)
        return total_processed, total_failed

    # ------------------------------------------------------------------ #

    def _process_page(self, name, table_name, primary_key, date_field, records, stats,
                      quarantine_invalid: bool = True) -> tuple[int, int]:
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
            quarantine_invalid=quarantine_invalid,
        )
        stats.inserted += succeeded
        stats.failed += failed
        # Rows neither written nor failed were dropped for a missing key on a
        # service that expects that. Tracked separately so the run still ends
        # "success" but the count stays visible in the log/run summary.
        stats.skipped += max(0, len(rows) - succeeded - failed)
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

"""
services/supabase_service.py

Supabase (Postgres) integration layer, built on SQLAlchemy Core + psycopg 3.
Drop-in replacement for the previous services/mysql_service.py: the public
method surface is identical, so sync_service.py is unchanged apart from
imports.

Why a direct Postgres connection rather than supabase-py / PostgREST:
this pipeline creates and alters tables at runtime and bulk-upserts whole
pages at a time. PostgREST cannot issue DDL at all, and its per-request
overhead makes large backfills slow. Supabase is plain Postgres underneath,
so we simply connect to it as such. The service-role/postgres database user
also bypasses RLS, which is what a backend ETL wants.

Responsibilities (unchanged in intent from the MySQL version):
- create tables on the fly from inferred schema
- detect and additively apply schema drift: new columns are added, and
  legacy VARCHAR columns are PROMOTED TO TEXT when needed (never dropped
  or narrowed)
- upsert batches via INSERT ... ON CONFLICT (pk) DO UPDATE
- validate rows before insert: rows with a null/empty primary key are
  quarantined (they cannot be safely upserted) rather than failing the run
- track per-service sync state in etl_sync_state: the incremental watermark
  (last_sync_at), the backfill progress marker (history_through), and the
  in-flight pagination resume link (resume_url)
- persist failed batches to failed_records/ and quarantined rows to
  quarantine/ for inspection / replay

Postgres-specific behaviour worth knowing:
- Session time zone is pinned to UTC, so naive datetimes (the watermark)
  and tz-aware ones (parsed from BC) both land as the correct instant.
- Prepared statements are disabled by default because Supabase's
  transaction pooler (port 6543 / *.pooler.supabase.com) is PgBouncer in
  transaction mode and does not support them.
- Tables created here get RLS enabled by default so they are not readable
  through the project's public anon key. See SUPABASE_ENABLE_RLS.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from config import SupabaseConfig
from utils.schema_helper import (
    RAW_JSON_COL,
    SYNCED_AT_COL,
    build_create_table_sql,
    infer_schema,
    normalize_column_name,
    normalize_pk,
    qualified_table,
    quote_ident,
)

logger = logging.getLogger("bc_sync")

_BASE_DIR = Path(__file__).resolve().parent.parent
FAILED_RECORDS_DIR = _BASE_DIR / "failed_records"
QUARANTINE_DIR = _BASE_DIR / "quarantine"
FAILED_RECORDS_DIR.mkdir(parents=True, exist_ok=True)
QUARANTINE_DIR.mkdir(parents=True, exist_ok=True)

SYNC_STATE_TABLE = "etl_sync_state"

# Postgres types that must receive a JSON cast on insert: a plain text
# parameter is not implicitly coerced into jsonb.
_JSON_TYPES = {"json", "jsonb"}


class SupabaseService:
    def __init__(self, sb_config: SupabaseConfig):
        self._config = sb_config
        self._schema = sb_config.schema

        connect_args: dict[str, Any] = {}
        if sb_config.use_pooler or sb_config.is_transaction_pooler:
            # PgBouncer in transaction mode cannot hold server-side prepared
            # statements across transactions. psycopg 3 prepares a statement
            # automatically after a few executions, which would then fail.
            connect_args["prepare_threshold"] = None

        self._engine: Engine = create_engine(
            sb_config.sqlalchemy_url,
            pool_size=sb_config.pool_size,
            max_overflow=sb_config.max_overflow,
            pool_timeout=sb_config.pool_timeout,
            pool_recycle=sb_config.pool_recycle,
            pool_pre_ping=True,  # detects dropped connections before use
            connect_args=connect_args,
        )

        self._install_session_settings()
        self._known_columns: dict[str, set[str]] = {}
        self._json_columns: dict[str, set[str]] = {}
        self._ensure_schema_exists()
        self._ensure_sync_state_table()

    def _stores_raw_json(self, table_name: str) -> bool:
        """Whether `table_name` carries the _raw_json copy of the source record.

        Excluded tables skip it entirely: the column is not created, not
        referenced in the INSERT column list, and not populated. It duplicates
        data the typed columns already hold and dominates on-disk size once a
        table reaches millions of rows.
        """
        return table_name not in self._config.raw_json_exclude_tables

    # ---------- connectivity / setup ----------

    def _install_session_settings(self) -> None:
        """Pin every pooled connection to UTC (and optionally a statement
        timeout). Done per-connection rather than once, since the pool opens
        connections lazily and PgBouncer may hand back a fresh backend."""
        timeout_ms = self._config.statement_timeout_ms

        @event.listens_for(self._engine, "connect")
        def _set_session(dbapi_conn, _record):  # noqa: ANN001
            with dbapi_conn.cursor() as cur:
                cur.execute("SET TIME ZONE 'UTC'")
                if timeout_ms and timeout_ms > 0:
                    cur.execute(f"SET statement_timeout = {int(timeout_ms)}")

    def _ensure_schema_exists(self) -> None:
        if self._schema == "public":
            return
        with self._engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(self._schema)}"))
        logger.info(f"Ensured schema {self._schema} exists.")

    def _sync_state_table(self) -> str:
        return qualified_table(self._schema, SYNC_STATE_TABLE)

    def _ensure_sync_state_table(self) -> None:
        ddl = f"""
        CREATE TABLE IF NOT EXISTS {self._sync_state_table()} (
          service_name TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'never_run',
          last_sync_at TIMESTAMPTZ NULL,
          history_through DATE NULL,
          resume_url TEXT NULL,
          last_run_started_at TIMESTAMPTZ NULL,
          last_run_completed_at TIMESTAMPTZ NULL,
          records_processed BIGINT NOT NULL DEFAULT 0,
          records_failed BIGINT NOT NULL DEFAULT 0,
          last_error TEXT NULL,
          PRIMARY KEY (service_name)
        );
        """
        with self._engine.begin() as conn:
            conn.execute(text(ddl))

        # Migrate older state tables that predate the history_through column.
        existing = self._existing_columns(SYNC_STATE_TABLE)
        if "history_through" not in existing:
            logger.info("Adding history_through column to etl_sync_state (migration).")
            with self._engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE {self._sync_state_table()} ADD COLUMN IF NOT EXISTS history_through DATE NULL")
                )
            self._known_columns.pop(SYNC_STATE_TABLE, None)

        self._enable_rls(SYNC_STATE_TABLE)

    def _enable_rls(self, table_name: str) -> None:
        """Enable row level security on a table we own.

        On Supabase, anything in the `public` schema is reachable through
        PostgREST with the project's anon key. Enabling RLS with no policies
        attached denies all anon/authenticated access while leaving this
        sync (which connects as the database owner and therefore bypasses
        RLS) completely unaffected. Turn off with SUPABASE_ENABLE_RLS=false
        if you intend to expose these tables and manage policies yourself.
        """
        if not self._config.enable_rls:
            return
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    text(f"ALTER TABLE {qualified_table(self._schema, table_name)} ENABLE ROW LEVEL SECURITY")
                )
        except SQLAlchemyError as exc:
            # Not fatal: a non-owner role may lack permission to alter RLS.
            logger.warning(f"Could not enable RLS on {table_name}: {exc}")

    def test_connection(self) -> bool:
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as exc:
            logger.error(f"Supabase/Postgres connection test failed: {exc}")
            return False

    # ---------- introspection ----------

    def _existing_columns(self, table_name: str) -> set[str]:
        if table_name in self._known_columns:
            return self._known_columns[table_name]
        query = text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :tbl"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(query, {"schema": self._schema, "tbl": table_name}).fetchall()
        cols = {r[0] for r in rows}
        self._known_columns[table_name] = cols
        return cols

    def _existing_column_types(self, table_name: str) -> dict[str, tuple[str, int | None]]:
        """Returns {column: (data_type, character_maximum_length)} for an
        existing table. Used to decide whether a legacy VARCHAR needs
        promoting to TEXT, and to find JSON/JSONB columns."""
        query = text(
            "SELECT column_name, data_type, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = :schema AND table_name = :tbl"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(query, {"schema": self._schema, "tbl": table_name}).fetchall()
        return {r[0]: (str(r[1]).lower(), r[2]) for r in rows}

    def _jsonb_columns(self, table_name: str, refresh: bool = False) -> set[str]:
        """Columns that need an explicit ::jsonb cast when bound."""
        if refresh or table_name not in self._json_columns:
            types = self._existing_column_types(table_name)
            self._json_columns[table_name] = {
                col for col, (dtype, _) in types.items() if dtype in _JSON_TYPES
            }
        return self._json_columns[table_name]

    def actual_primary_key(self, table_name: str) -> str | None:
        """Returns the primary-key column(s) of an existing table as a
        comma-joined string, or None if the table doesn't exist / has no PK.
        Used to warn when the configured primary_key doesn't match the
        table's real key."""
        query = text(
            "SELECT kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            " AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            "  AND tc.table_schema = :schema AND tc.table_name = :tbl "
            "ORDER BY kcu.ordinal_position"
        )
        with self._engine.connect() as conn:
            rows = conn.execute(query, {"schema": self._schema, "tbl": table_name}).fetchall()
        cols = [r[0] for r in rows]
        if not cols:
            return None
        return cols[0] if len(cols) == 1 else ",".join(cols)

    # ---------- dynamic schema management ----------

    def ensure_table(
        self, table_name: str, sample_rows: list[dict], primary_key: str, json_columns: set[str] | None = None
    ) -> dict[str, str]:
        """
        Ensures `table_name` exists with at least the columns present in
        sample_rows. Existing tables are only ever widened (new columns
        added, legacy VARCHARs promoted to TEXT) - never altered
        destructively.
        """
        inferred = infer_schema(sample_rows, json_columns=json_columns)
        existing_cols = self._existing_columns(table_name)
        pk_cols = normalize_pk(primary_key)

        if not existing_cols:
            store_raw = self._stores_raw_json(table_name)
            ddl = build_create_table_sql(
                self._schema, table_name, inferred, primary_key, store_raw_json=store_raw
            )
            logger.info(f"Creating table {self._schema}.{table_name}...")
            with self._engine.begin() as conn:
                conn.execute(text(ddl))
            bookkeeping = {SYNCED_AT_COL} | ({RAW_JSON_COL} if store_raw else set())
            self._known_columns[table_name] = set(inferred.keys()) | set(pk_cols) | bookkeeping
            self._json_columns.pop(table_name, None)
            self._enable_rls(table_name)
            return inferred

        # 1) Additive: add columns we haven't seen before.
        new_cols = {c: t for c, t in inferred.items() if c not in existing_cols}
        if new_cols:
            logger.info(f"Schema drift on {table_name}: adding {list(new_cols.keys())}")
            with self._engine.begin() as conn:
                for col, col_type in new_cols.items():
                    conn.execute(
                        text(
                            f"ALTER TABLE {qualified_table(self._schema, table_name)} "
                            f"ADD COLUMN IF NOT EXISTS {quote_ident(col)} {col_type}"
                        )
                    )
            self._known_columns[table_name] = existing_cols | set(new_cols.keys())
            self._json_columns.pop(table_name, None)

        # 2) Promote: legacy VARCHAR(n) columns (typically inherited from a
        #    table migrated over from MySQL) become TEXT so no value can be
        #    rejected for length. Never narrows. PK and bookkeeping columns
        #    are left untouched.
        self._promote_varchars_to_text(table_name, inferred, pk_cols)
        return inferred

    def _promote_varchars_to_text(self, table_name: str, inferred: dict[str, str], pk_cols: list[str]) -> None:
        protected = set(pk_cols) | {SYNCED_AT_COL, RAW_JSON_COL}
        current_types = self._existing_column_types(table_name)
        promotions: list[str] = []

        for col, inferred_type in inferred.items():
            if col in protected or col not in current_types:
                continue
            cur_data_type, _cur_len = current_types[col]
            if cur_data_type in ("character varying", "character") and inferred_type.strip().upper() == "TEXT":
                promotions.append(col)

        if promotions:
            logger.info(f"Promoting column(s) on {table_name} to TEXT: {', '.join(promotions)}")
            with self._engine.begin() as conn:
                for col in promotions:
                    conn.execute(
                        text(
                            f"ALTER TABLE {qualified_table(self._schema, table_name)} "
                            f"ALTER COLUMN {quote_ident(col)} TYPE TEXT"
                        )
                    )
            self._json_columns.pop(table_name, None)

    def max_by_series(self, table_name: str, column: str, separator: str = "-") -> dict[str, str]:
        """{series_prefix: highest value seen} for `column`, where the prefix
        is everything before the first `separator` (e.g. 26CNMUM-21208 ->
        26CNMUM). Drives the series_key strategy's per-series watermark.

        Relies on the numeric part being zero-padded to a constant width
        within a series, which is how BC no.-series work - otherwise a
        lexicographic max would not be the numeric max.
        Returns {} when the table does not exist yet.
        """
        col = normalize_column_name(column)
        if not self._existing_columns(table_name):
            return {}
        sql = text(
            f"SELECT split_part({quote_ident(col)}, :sep, 1) AS series, "
            f"MAX({quote_ident(col)}) AS high "
            f"FROM {qualified_table(self._schema, table_name)} "
            f"WHERE {quote_ident(col)} IS NOT NULL AND {quote_ident(col)} <> '' "
            f"GROUP BY 1"
        )
        with self._engine.connect() as conn:
            return {r[0]: r[1] for r in conn.execute(sql, {"sep": separator}).fetchall() if r[0]}

    def distinct_series(self, table_name: str, column: str, separator: str = "-") -> set[str]:
        """Series prefixes present in `table_name`.`column`. Used to spot a
        series that exists on the parent document table but has no lines
        stored yet - BC rejects a "not (...)" filter, so a brand-new series
        cannot be discovered from the API side."""
        return set(self.max_by_series(table_name, column, separator).keys())

    # ---------- upserts ----------

    def upsert_rows(
        self,
        table_name: str,
        rows: list[dict],
        primary_key: str,
        raw_records: list[dict] | None = None,
        batch_size: int = 500,
        quarantine_invalid: bool = True,
    ) -> tuple[int, int]:
        """
        Validates and upserts rows. Rows with a null/empty primary key cannot
        be upserted reliably and are dropped. Remaining rows go through
        INSERT ... ON CONFLICT (pk) DO UPDATE in batches. Returns
        (succeeded_count, failed_count).

        quarantine_invalid=True (default) writes those dropped rows to
        quarantine/ and counts them as failures, which is right when a
        missing key is a surprise. Set it False for a page where keyless
        rows are simply not wanted - e.g. credit-memo lines carrying no
        product - so they are dropped quietly instead of filling
        quarantine/ and forcing the run to partial_failure every time.
        Dropped rows are still derivable: len(rows) - succeeded - failed.
        """
        if not rows:
            return 0, 0

        pk_cols = normalize_pk(primary_key)
        raw_records = raw_records if raw_records is not None else [None] * len(rows)

        def _missing_key(row: dict) -> bool:
            for c in pk_cols:
                v = row.get(c)
                if v is None or (isinstance(v, str) and not v.strip()):
                    return True
            return False

        valid_rows: list[dict] = []
        valid_raw: list[Any] = []
        invalid_rows: list[dict] = []
        for row, raw in zip(rows, raw_records):
            if _missing_key(row):
                invalid_rows.append(row)
            else:
                valid_rows.append(row)
                valid_raw.append(raw)

        failed = 0
        if invalid_rows and quarantine_invalid:
            self._save_quarantine(table_name, invalid_rows, f"null/empty primary key {pk_cols}")
            failed += len(invalid_rows)
            logger.warning(
                f"{len(invalid_rows)} row(s) for {table_name} had a null/empty "
                f"primary key and were quarantined."
            )
        elif invalid_rows:
            logger.info(
                f"{len(invalid_rows)} row(s) for {table_name} had no {pk_cols} value "
                f"and were dropped (expected for this service; not quarantined)."
            )

        if not valid_rows:
            return 0, failed

        store_raw = self._stores_raw_json(table_name)
        all_cols = sorted(
            {c for row in valid_rows for c in row.keys()}
            | set(pk_cols)
            | ({RAW_JSON_COL} if store_raw else set())
        )
        json_cols = self._jsonb_columns(table_name)
        succeeded = 0
        for start in range(0, len(valid_rows), batch_size):
            batch = valid_rows[start:start + batch_size]
            raw_batch = valid_raw[start:start + batch_size]
            try:
                self._upsert_batch(table_name, batch, raw_batch, all_cols, pk_cols, json_cols)
                succeeded += len(batch)
            except SQLAlchemyError as exc:
                logger.error(f"Batch upsert failed for {table_name} rows {start}-{start+len(batch)}: {exc}")
                self._save_failed_batch(table_name, batch, str(exc))
                failed += len(batch)

        return succeeded, failed

    def _upsert_batch(
        self,
        table_name: str,
        batch: list[dict],
        raw_batch: list[Any],
        all_cols: list[str],
        pk_cols: list[str],
        json_cols: set[str],
    ) -> None:
        pk_set = set(pk_cols)
        prepared = []
        for i, row in enumerate(batch):
            full_row = {col: row.get(col) for col in all_cols}
            if RAW_JSON_COL in all_cols and i < len(raw_batch) and raw_batch[i] is not None:
                full_row[RAW_JSON_COL] = json.dumps(raw_batch[i], default=str)
            # PK columns are TEXT; Postgres will not implicitly cast an int
            # parameter into a text column the way MySQL did, so coerce here.
            for c in pk_cols:
                v = full_row.get(c)
                if v is not None and not isinstance(v, str):
                    full_row[c] = str(v)
            # An empty string is not valid JSON - normalize to NULL so the
            # ::jsonb cast cannot blow up the batch.
            for c in json_cols:
                if c in full_row and isinstance(full_row[c], str) and not full_row[c].strip():
                    full_row[c] = None
            prepared.append(full_row)

        col_list = ", ".join(quote_ident(c) for c in all_cols)
        # CAST(:x AS JSONB) rather than :x::jsonb - SQLAlchemy's text()
        # treats a bare colon as the start of a bind parameter, so the ::
        # shorthand is ambiguous inside text() constructs.
        placeholders = ", ".join(
            (f"CAST(:{c} AS JSONB)" if c in json_cols else f":{c}") for c in all_cols
        )
        update_cols = [c for c in all_cols if c not in pk_set]
        update_clause = ", ".join(f"{quote_ident(c)} = EXCLUDED.{quote_ident(c)}" for c in update_cols)
        # Postgres has no ON UPDATE CURRENT_TIMESTAMP, so refresh the
        # bookkeeping timestamp explicitly on every update.
        update_clause = (
            f"{update_clause}, {quote_ident(SYNCED_AT_COL)} = now()"
            if update_clause
            else f"{quote_ident(SYNCED_AT_COL)} = now()"
        )
        conflict_target = ", ".join(quote_ident(c) for c in pk_cols)

        sql = (
            f"INSERT INTO {qualified_table(self._schema, table_name)} ({col_list}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_target}) DO UPDATE SET {update_clause}"
        )
        with self._engine.begin() as conn:
            conn.execute(text(sql), prepared)

    def _save_failed_batch(self, table_name: str, batch: list[dict], error: str) -> None:
        path = FAILED_RECORDS_DIR / f"{table_name}_{int(time.time()*1000)}.json"
        path.write_text(
            json.dumps({"error": error, "records": batch}, indent=2, default=str),
            encoding="utf-8",
        )
        logger.warning(f"Wrote {len(batch)} failed record(s) to {path}")

    def _save_quarantine(self, table_name: str, rows: list[dict], reason: str) -> None:
        path = QUARANTINE_DIR / f"{table_name}_{int(time.time()*1000)}.json"
        path.write_text(
            json.dumps({"reason": reason, "records": rows}, indent=2, default=str),
            encoding="utf-8",
        )
        logger.warning(f"Quarantined {len(rows)} row(s) for {table_name} ({reason}) -> {path}")

    # ---------- sync state ----------

    def get_sync_state(self, service_name: str) -> dict[str, Any] | None:
        query = text(f"SELECT * FROM {self._sync_state_table()} WHERE service_name = :name")
        with self._engine.connect() as conn:
            row = conn.execute(query, {"name": service_name}).mappings().fetchone()
        return dict(row) if row else None

    def mark_run_started(self, service_name: str) -> None:
        sql = text(
            f"INSERT INTO {self._sync_state_table()} (service_name, status, last_run_started_at) "
            f"VALUES (:name, 'in_progress', now()) "
            f"ON CONFLICT (service_name) DO UPDATE SET "
            f"status = 'in_progress', last_run_started_at = now()"
        )
        with self._engine.begin() as conn:
            conn.execute(sql, {"name": service_name})

    def save_resume_point(self, service_name: str, resume_url: str | None, processed_so_far: int) -> None:
        sql = text(
            f"UPDATE {self._sync_state_table()} SET resume_url = :url, records_processed = :count "
            f"WHERE service_name = :name"
        )
        with self._engine.begin() as conn:
            conn.execute(sql, {"url": resume_url, "count": processed_so_far, "name": service_name})

    def save_history_progress(self, service_name: str, through: date) -> None:
        """Record how far the chunked backfill has completed (a window upper
        bound), so an interrupted backfill resumes at the next window."""
        sql = text(
            f"UPDATE {self._sync_state_table()} SET history_through = :through WHERE service_name = :name"
        )
        with self._engine.begin() as conn:
            conn.execute(sql, {"through": through, "name": service_name})

    def set_incremental_watermark(self, service_name: str, watermark: datetime) -> None:
        """Set the incremental watermark (the value the next incremental run
        filters incremental_field against). The session time zone is UTC, so
        a naive UTC datetime is stored as the correct instant."""
        sql = text(
            f"UPDATE {self._sync_state_table()} SET last_sync_at = :ts WHERE service_name = :name"
        )
        with self._engine.begin() as conn:
            conn.execute(sql, {"ts": watermark, "name": service_name})

    def mark_run_completed(
        self,
        service_name: str,
        status: str,
        records_processed: int,
        records_failed: int,
        error: str | None = None,
    ) -> None:
        # Clear the resume pointer only when pagination ran to completion.
        # success / partial_failure both mean every page was fetched
        # (partial_failure just means some upserts failed within those
        # pages). On "failed", a page fetch blew up mid-run, so we KEEP
        # resume_url so the next invocation resumes from that exact page.
        clear_resume_url = status in ("success", "partial_failure")
        sql = text(
            f"UPDATE {self._sync_state_table()} SET "
            f"status = :status, "
            f"last_run_completed_at = now(), "
            f"records_processed = :processed, "
            f"records_failed = :failed, "
            f"last_error = :error"
            + (", resume_url = NULL" if clear_resume_url else "")
            + " WHERE service_name = :name"
        )
        with self._engine.begin() as conn:
            conn.execute(
                sql,
                {
                    "status": status,
                    "processed": records_processed,
                    "failed": records_failed,
                    "error": error,
                    "name": service_name,
                },
            )

    def dispose(self) -> None:
        self._engine.dispose()

#!/usr/bin/env python3
"""
app.py

CLI entry point for the Business Central -> Supabase (Postgres) sync.

Designed to run as a single, idempotent invocation — ideal for a GitHub
Actions scheduled workflow (cron) or any external scheduler. Each run:
  1. authenticates fresh (no token writeback, nothing persisted to disk)
  2. for each configured web service, pulls new/changed records
  3. upserts them into Supabase Postgres
  4. records sync state in Supabase so the NEXT run knows where to resume /
     what watermark to use

Examples:
    python app.py                                  # incremental sync, all services
    python app.py --mode full                      # full re-pull of all services
    python app.py --service Item_Ledger_Entries_Excel
    python app.py --service A --service B --mode incremental
    python app.py --setup                          # pre-create tables only
"""

from __future__ import annotations

import argparse
import logging
import sys

from config import ConfigError, load_bc_config, load_supabase_config
from etl_alerts import DiskGuardStop, guard
from services.sync_service import SyncService
from utils.logger import log_run_summary, setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Business Central web services into Supabase Postgres.")
    parser.add_argument(
        "--mode",
        choices=["incremental", "full"],
        default="incremental",
        help="incremental (default): only records changed since last successful run. "
             "full: re-pull everything (still an upsert, no data is dropped).",
    )
    parser.add_argument(
        "--service",
        action="append",
        dest="services",
        default=None,
        help="Restrict the run to one or more service names (repeatable). "
             "Matches the 'name' field in config/web_services.json.",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Only pre-create Supabase tables for all configured services, then exit.",
    )
    return parser.parse_args()


def main() -> int:
    logger = setup_logger()
    args = parse_args()

    try:
        bc_config = load_bc_config()
        db_config = load_supabase_config()
    except ConfigError as exc:
        logger.error(f"Configuration error: {exc}")
        return 1

    # Shared disk guard, once per run. This is the one that EMAILS - it also
    # lets budgets grow into unallocated space first, so a pipeline that is
    # legitimately growing is not blocked by a number guessed months ago.
    # SyncService keeps its own per-service check for mid-run protection; that
    # one only logs, because a long backfill must not mail on every service.
    try:
        guard("bc_sync")
    except DiskGuardStop as exc:
        logger.error(f"Disk guard stopped this run: {exc}")
        return 4
    except Exception:
        # Never let the guard itself take down a run that could have succeeded.
        logger.exception("Disk guard could not run - continuing without it.")

    sync_service = SyncService(bc_config, db_config)

    try:
        if args.setup:
            logger.info("Running in --setup mode: pre-creating tables only.")
            sync_service.ensure_tables_for_first_run()
            return 0

        stats_list = sync_service.run(mode=args.mode, service_filter=args.services)
        log_run_summary(logger, stats_list)

        any_failed = any(s.status == "failed" for s in stats_list)
        any_partial = any(s.status == "partial_failure" for s in stats_list)
        if any_failed:
            return 2
        if any_partial:
            return 3
        return 0

    except Exception:
        logger.exception("Sync run aborted due to an unexpected error.")
        return 1
    finally:
        sync_service.close()


if __name__ == "__main__":
    sys.exit(main())

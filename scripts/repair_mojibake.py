#!/usr/bin/env python3
"""Re-fetch, from Business Central, the rows whose text was mangled by the
August-2026 MySQL -> Supabase CSV migration.

Those rows carry U+FFFD (the Unicode replacement character) where the source
had a non-ASCII byte - most often NBSP (U+00A0) inside a customer name, e.g.
"CROPBASKET AGRO INNOVATIONS PVT\xa0 LTD.". The live BC API returns the
character correctly and the current sync round-trips it correctly (verified:
client_encoding and server_encoding are both UTF8, and every table written
only by the BC sync is clean), so this is a one-off repair of legacy rows, not
a workaround for an ongoing bug.

The rows are re-pulled through the sync's own fetch + prepare_row + upsert
path, so they end up identical to what a normal sync run would have written.

Usage:
    python scripts/repair_mojibake.py            # report only
    python scripts/repair_mojibake.py --apply    # re-fetch and upsert
"""
from __future__ import annotations

import argparse
import logging
import sys

# Same idiom as scripts/reclaim_vacuum_full.py: run from anywhere.
sys.path.insert(0, __file__.rsplit("scripts", 1)[0])

from sqlalchemy import text

from config import load_bc_config, load_supabase_config, load_web_services
from services.auth_service import BCAuthService
from services.bc_api_service import BCApiService
from services.supabase_service import SupabaseService
from utils.logger import setup_logger
from utils.schema_helper import prepare_row

logger = logging.getLogger("bc_sync")

# table -> (service name, key column, text columns worth scanning)
TARGETS = {
    "bc_posted_sales_invoice_excel": (
        "Posted_Sales_Invoice_Excel", "No",
        ["Sell_to_Customer_Name", "Ship_to_Name", "Bill_to_Name",
         "External_Document_No", "Sell_to_Address", "Ship_to_Address",
         "Bill_to_Address", "Sell_to_Contact", "Ship_to_Contact"],
    ),
    "bc_posted_sales_credit_memo": (
        "Posted_Sales_Credit_Memo_Excel", "No",
        ["Sell_to_Customer_Name", "Ship_to_Name", "Bill_to_Name",
         "External_Document_No", "Sell_to_Address", "Ship_to_Address",
         "Bill_to_Address", "Sell_to_Contact"],
    ),
}

REPLACEMENT_CHAR = "\ufffd"
BATCH = 40          # documents per OData request; keeps the URL well short of limits


def find_bad_keys(db: SupabaseService, table: str, key_col: str, cols: list[str]) -> list[str]:
    existing = db._existing_columns(table)
    cols = [c for c in cols if c in existing]
    if not cols:
        return []
    pred = " OR ".join(f'"{c}" LIKE :pat' for c in cols)
    sql = text(f'SELECT "{key_col}" FROM public.{table} WHERE {pred} ORDER BY 1')
    with db._engine.connect() as conn:
        rows = conn.execute(sql, {"pat": f"%{REPLACEMENT_CHAR}%"}).fetchall()
    return [r[0] for r in rows]


def odata_in(field: str, values: list[str]) -> str:
    return " or ".join(f"{field} eq '" + v.replace("'", "''") + "'" for v in values)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="re-fetch and upsert (default: report only)")
    args = ap.parse_args()
    setup_logger()

    bc_cfg = load_bc_config()
    services = {s["name"]: s for s in load_web_services()}
    api = BCApiService(bc_cfg, BCAuthService(bc_cfg),
                       service_apis={s["name"]: s.get("api", "odata") for s in services.values()})
    db = SupabaseService(load_supabase_config())

    total_found = total_fixed = 0
    for table, (service_name, key_col, cols) in TARGETS.items():
        bad = find_bad_keys(db, table, key_col, cols)
        total_found += len(bad)
        logger.info(f"[{table}] {len(bad)} row(s) carry U+FFFD")
        if not bad or not args.apply:
            continue
        if service_name not in services:
            logger.warning(f"[{table}] service {service_name} is not enabled in config - skipping")
            continue
        pk = services[service_name].get("primary_key", key_col)
        for i in range(0, len(bad), BATCH):
            chunk = bad[i:i + BATCH]
            fetched = []
            for records, _ in api.fetch_pages(service_name, odata_filter=odata_in(key_col, chunk)):
                fetched.extend(records)
            if not fetched:
                logger.warning(f"[{table}] BC returned nothing for {chunk[0]}..{chunk[-1]}")
                continue
            rows, json_cols = [], set()
            for rec in fetched:
                row, jc = prepare_row(rec)
                rows.append(row)
                json_cols |= jc
            db.ensure_table(table, rows, pk, json_columns=json_cols)
            ok, failed = db.upsert_rows(table, rows, pk)
            total_fixed += ok
            logger.info(f"[{table}] refreshed {ok} row(s) ({i + len(chunk)}/{len(bad)}), {failed} failed")

        left = find_bad_keys(db, table, key_col, cols)
        logger.info(f"[{table}] {len(left)} row(s) still carry U+FFFD after repair")

    if not args.apply:
        logger.info(f"{total_found} row(s) affected in total. Re-run with --apply to repair.")
    else:
        logger.info(f"Repair complete: {total_fixed} row(s) re-fetched from BC.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""One-off: pull Item Ledger Entries for a bounded Posting_Date range.

Daily windows: BC pages deep into a month-wide filter at ~800 rows/min, a day at ~7k.
Used 2026-09-21 to add the ERP's go-live year (2024-04-01 .. 2025-03-31) that the
scheduled sync never pulled, without re-reading the 2.1M rows already present.

Deliberately touches NOTHING in etl_sync_state (no resume point, no history marker,
no watermark), so it can run while the 2-hourly GitHub sync keeps going: the only
shared state is the table itself, and both sides upsert on Entry_No.

    python scripts/backfill_ile_window.py 2024-04-01 2025-04-01   # end exclusive

The go-live opening balance is dated 2024-03-31 (2,478 entries); it was pulled the same way.
"""
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_bc_config, load_supabase_config  # noqa: E402
from services.sync_service import SyncService  # noqa: E402
from utils.date_chunker import build_range_filter, iter_windows  # noqa: E402
from utils.logger import SyncStats  # noqa: E402

NAME, TABLE, PK, FIELD = "Item_Ledger_Entries_Excel", "bc_item_ledger_entries", "Entry_No", "Posting_Date"


def main() -> int:
    start, end = date.fromisoformat(sys.argv[1]), date.fromisoformat(sys.argv[2])
    svc = SyncService(load_bc_config(), load_supabase_config())
    stats = SyncStats(service_name=NAME)
    # FY24/25 rows carry decimals in columns the table holds as BIGINT (Shelf_Life_Percent,
    # e.g. -391.89). The sync would widen the column, which it refuses to do under the five
    # materialized views on this table, failing every page. Nothing reads those columns, so
    # round them to the column's type instead -- and say how many were rounded.
    types = svc._db._existing_column_types(TABLE)  # noqa: SLF001
    bigint_cols = {c for c, (t, _n) in types.items() if t in ("bigint", "integer", "smallint")}
    rounded = Counter()
    t0 = time.time()
    for w_start, w_end in iter_windows(start, end, "daily"):
        before = stats.inserted
        flt = build_range_filter(FIELD, w_start, w_end, is_datetime=False)
        for records, _next in svc._api.fetch_pages(NAME, odata_filter=flt):  # noqa: SLF001
            for r in records:
                for k, v in r.items():
                    if k in bigint_cols and isinstance(v, float) and v != int(v):
                        r[k] = int(round(v))
                        rounded[k] += 1
            svc._process_page(NAME, TABLE, PK, FIELD, records, stats)  # noqa: SLF001
        print(f"{w_start}..{w_end}: {stats.inserted - before} rows "
              f"(total {stats.inserted}, failed {stats.failed}, {time.time() - t0:.0f}s)", flush=True)
    print("rounded to integer:", dict(rounded) or "none", flush=True)
    return 0 if stats.failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())

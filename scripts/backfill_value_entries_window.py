"""One-off: pull Value Entries of ONE item-ledger entry type for a bounded Posting_Date range.

Written 2026-11-20 when Value_Entries_Excel was widened from sales to sales + purchases: the scheduled
incremental only looks back lookback_days, so the purchase history from 2026-04-01 has to be loaded
once. A mode=full run would re-read the 1.07M sales rows as well; this pulls only the type asked for.

Daily windows (BC pages deep into a month-wide filter slowly; a day is fast). Deliberately touches
NOTHING in etl_sync_state (no resume point, no history marker, no watermark), so it can run while the
scheduled sync keeps going: both sides upsert on Entry_No.

    python scripts/backfill_value_entries_window.py Purchase 2026-04-01 2026-11-21   # end exclusive
"""
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import load_bc_config, load_supabase_config  # noqa: E402
from services.sync_service import SyncService  # noqa: E402
from utils.date_chunker import build_range_filter, iter_windows  # noqa: E402
from utils.logger import SyncStats  # noqa: E402

NAME, TABLE, PK, FIELD = "Value_Entries_Excel", "bc_value_entries", "Entry_No", "Posting_Date"
TYPES = {"Purchase", "Sale", "Output", "Consumption", "Positive Adjmt.", "Negative Adjmt.", "Transfer"}


def main() -> int:
    kind = sys.argv[1]
    if kind not in TYPES:
        sys.exit(f"type must be one of {sorted(TYPES)}")
    start, end = date.fromisoformat(sys.argv[2]), date.fromisoformat(sys.argv[3])
    svc = SyncService(load_bc_config(), load_supabase_config())
    stats = SyncStats(service_name=NAME)
    t0 = time.time()
    for w_start, w_end in iter_windows(start, end, "daily"):
        before = stats.inserted
        # the service's static filter (sale or purchase) is AND-ed on by the API layer
        flt = f"({build_range_filter(FIELD, w_start, w_end, is_datetime=False)}) and Item_Ledger_Entry_Type eq '{kind}'"
        for records, _next in svc._api.fetch_pages(NAME, odata_filter=flt):  # noqa: SLF001
            svc._process_page(NAME, TABLE, PK, FIELD, records, stats)  # noqa: SLF001
        print(f"{w_start}..{w_end}: {stats.inserted - before} rows "
              f"(total {stats.inserted}, failed {stats.failed}, {time.time() - t0:.0f}s)", flush=True)
    return 0 if stats.failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())

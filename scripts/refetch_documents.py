"""Re-fetch posted documents' lines by document number, for series_key line services.

Written 2026-11-27. series_key line services used to resume strictly ABOVE each series' highest stored
document, so a run that stopped mid-page (disk guard, job timeout, a failed page) left that document
with only its first lines for ever: 27BLR-07638 lost its last line (Rs 3,733.33), 27CNMUM-10469 Rs 2,250,
both visible as GSTR-1 differences. sync_service now re-reads the highest document (ge); this repairs the
documents truncated before that.

--check lists posted invoices / credit memos (from 2026-04-01) whose stored lines plus GST-ledger tax
fall short of the header's AmountToCustomer, i.e. lines are missing; with --apply it re-fetches them. A document
posted today can show up only because its GST ledger tax has not synced yet; re-fetching it is harmless.
Documents with NO lines are left to the sync's own gap sweep. Touches nothing in etl_sync_state; upserts on
Document_No + Line_No, so it can run beside the scheduled sync.

    python scripts/refetch_documents.py --check                    # list truncated documents
    python scripts/refetch_documents.py --check --apply            # and re-fetch them
    python scripts/refetch_documents.py Posted_Sales_Invoice_ExcelSalesInvLines 27BLR-07638 ...
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from config import load_bc_config, load_supabase_config, load_web_services  # noqa: E402
from services.sync_service import SyncService, _odata_str  # noqa: E402
from utils.logger import SyncStats  # noqa: E402

# line service -> (header table, line table, line amount column)
CHECKS = {
    "Posted_Sales_Invoice_ExcelSalesInvLines": ("bc_posted_sales_invoice_excel", "bc_posted_sales_invoice_lines", "Line_Amount"),
    "Posted_Sales_Credit_Memo_ExcelSalesCrMemoLines": ("bc_posted_sales_credit_memo", "bc_posted_sales_cr_memo_lines", "Line_Amount"),
    "Posted_Sales_Credit_Memo_Lines_Excel": ("bc_posted_sales_credit_memo", "bc_posted_sales_credit_memo_lines", "Amount"),
}

TRUNCATED_SQL = """
with tax as (
  select d."Document_No" doc_no, abs(sum(d."GST_Amount")) tax
  from public.bc_detailed_gst_ledger_entries d
  where d."Transaction_Type" = 'Sales' and not coalesce(d."Reversed", false) and d."Posting_Date" >= date '2026-04-01'
  group by 1),
doc as (
  select h."No" doc_no, h."AmountToCustomer" atc, sum(l."{amount}") lines
  from public.{header} h join public.{lines} l on l."Document_No" = h."No"
  where h."Posting_Date" >= date '2026-04-01'
  group by 1, 2)
select d.doc_no from doc d left join tax t on t.doc_no = d.doc_no
where d.atc - (d.lines + coalesce(t.tax, 0)) > 2
order by 1
"""


def refetch(svc, service, docs) -> SyncStats:
    name, table, pk = service["name"], service["table_name"], service["primary_key"]
    field = service.get("series_key_field", "Document_No")
    stats = SyncStats(service_name=name)
    for doc in docs:
        before = stats.inserted + stats.updated
        for records, _next in svc._api.fetch_pages(name, odata_filter=f"{field} eq {_odata_str(doc)}", order_by=field):  # noqa: SLF001
            svc._process_page(name, table, pk, None, records, stats)  # noqa: SLF001
        print(f"  {doc}: {stats.inserted + stats.updated - before} line(s) written", flush=True)
    return stats


def main() -> int:
    args = sys.argv[1:]
    services = {s["name"]: s for s in load_web_services()}
    svc = SyncService(load_bc_config(), load_supabase_config())
    failed = 0
    try:
        if args and args[0] == "--check":
            apply = "--apply" in args
            for name, (header, lines, amount) in CHECKS.items():
                with svc._db._engine.connect() as cx:  # noqa: SLF001
                    docs = [r[0] for r in cx.execute(text(TRUNCATED_SQL.format(header=header, lines=lines, amount=amount)))]
                print(f"{name}: {len(docs)} truncated document(s){': ' + ', '.join(docs) if docs else ''}")
                if docs and apply:
                    failed += refetch(svc, services[name], docs).failed
        else:
            name, docs = args[0], args[1:]
            failed += refetch(svc, services[name], docs).failed
    finally:
        svc.close()
    return 0 if failed == 0 else 3


if __name__ == "__main__":
    sys.exit(main())

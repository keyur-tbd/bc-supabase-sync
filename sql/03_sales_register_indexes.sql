-- Indexes the Sales Register view needs.
--
-- The sync creates each bc_* table with its primary key and nothing else, so
-- the two biggest inputs - the GST ledger and the value entries - are keyed on
-- Entry_No while every join in v_sales_register_gst_detail is on
-- (Document_No, Document_Line_No). Without these, each query sequentially
-- scans both tables.
--
-- The line and header tables need nothing added: their primary keys are already
-- (Document_No, Line_No) and ("No") respectively.
--
-- CONCURRENTLY: the GST ledger and value entries are written by the sync, and
-- a plain CREATE INDEX would block those inserts for the duration.
--
-- Cost is roughly 25-35 MB per index at FY 2026-27 volumes. Run this once;
-- IF NOT EXISTS makes a re-run a no-op.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gst_ledger_doc_line
    ON public.bc_detailed_gst_ledger_entries ("Document_No", "Document_Line_No");

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gst_ledger_posting_date
    ON public.bc_detailed_gst_ledger_entries ("Posting_Date");

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_value_entries_doc_line
    ON public.bc_value_entries ("Document_No", "Document_Line_No");

-- Date-range queries against the view ("give me July") filter on the header's
-- Posting_Date, which is otherwise unindexed on both document tables.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_posted_sales_invoice_posting_date
    ON public.bc_posted_sales_invoice_excel ("Posting_Date");

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_posted_sales_cr_memo_posting_date
    ON public.bc_posted_sales_credit_memo ("Posting_Date");

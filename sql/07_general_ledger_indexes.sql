-- Indexes the general ledger needs to be queryable at all.
--
-- The sync creates bc_general_ledger_entries with its primary key ("Entry_No")
-- and nothing else. At 10.1M rows / 4.4 GB (2026-10-02) every question that
-- filters on a date, an account, a document or a user is a full sequential
-- scan, and Birbal's 25 second statement cap cancels it: on 2026-10-02 "one
-- account for one month", "entries posted in the last two days" and "the
-- entries of one document" all timed out, so the ledger was in the warehouse
-- but could not be read. Only the account-card balances answered.
--
--   ("Posting_Date")                   any period question, and "who posted what recently"
--   ("G_L_Account_No", "Posting_Date") one account (or a range of accounts) over a period
--   ("Document_No")                    the postings behind one invoice / memo / journal
--
-- CONCURRENTLY: the sync upserts into this table every run, and a plain CREATE
-- INDEX would block those writes for the minutes the build takes.
--
-- Cost is roughly 70-320 MB per index at this volume. IF NOT EXISTS makes a
-- re-run a no-op.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gl_entries_posting_date
    ON public.bc_general_ledger_entries ("Posting_Date");

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gl_entries_account_posting_date
    ON public.bc_general_ledger_entries ("G_L_Account_No", "Posting_Date");

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_gl_entries_document_no
    ON public.bc_general_ledger_entries ("Document_No");

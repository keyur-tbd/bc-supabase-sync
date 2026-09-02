-- Per-table autovacuum thresholds for the large bc_* tables.
--
-- WHY: Postgres triggers autovacuum at autovacuum_vacuum_scale_factor (0.2)
-- of the table. On bc_general_ledger_entries that is 1,260,804 dead tuples,
-- and the sync produces tens of thousands over weeks - so autovacuum had
-- NEVER run on it, nor on bc_item_ledger_entries or bc_value_entries.
-- Statistics drifted with it: on 2026-09-02 the planner believed the GL table
-- held 10,261 rows against an actual 6,303,769, a 500x error that misleads
-- every query anyone writes against these tables.
--
-- The fix is a SMALLER percentage plus a floor, not a flat count. A flat
-- threshold (tried first: 50,000) helps the huge tables but is WORSE than the
-- default on the smaller ones here - bc_vendor_ledger_entries triggers at
-- 7,181 by default, so a flat 50,000 would have vacuumed it less often, not
-- more. 0.02 + 5,000 is strictly better than the default at every size:
--
--   table                          rows        default     now
--   bc_general_ledger_entries      6,303,769   1,260,804   131,075
--   bc_item_ledger_entries         2,029,398     405,930    45,588
--   bc_detailed_gst_ledger_entries   651,286     130,307    18,026
--   bc_posted_sales_invoice_excel    107,908      21,632     7,158
--   bc_vendor_ledger_entries          35,654       7,181     5,713
--
-- Cheap to act on: a full VACUUM ANALYZE of the 2.8 GB GL table took 12.7s.
--
-- Metadata-only change: SHARE UPDATE EXCLUSIVE, no rewrite, does not block
-- reads or writes. Safe to reapply on every sync, which is what happens.
--
-- Applies to the bc_* tables listed below AND, dynamically, to every table in
-- the schema above 250,000 rows - including the ad/marketplace tables owned by
-- other pipelines sharing this database (instamart_ads_performance at 10.5M
-- rows needs 2,104,376 dead tuples to trigger by default). Added on the user's
-- instruction 2026-09-02. Those tables ARE being autovacuumed, unlike the bc_*
-- ones, because their backfills insert enough to cross the insert threshold -
-- so for them this is about keeping statistics fresh during a long backfill
-- rather than rescuing a table that has never been vacuumed.
--
-- LOCK SAFETY: ALTER TABLE ... SET (autovacuum_*) needs SHARE UPDATE EXCLUSIVE.
-- That does not conflict with INSERT/UPDATE/SELECT, so it cannot block a
-- backfill directly - but a lock WAIT would queue every later writer behind it.
-- lock_timeout is therefore 3s and each table is attempted independently: a
-- table that is busy is skipped with a notice, and the next run picks it up.

DO $$
DECLARE
    t text;
    r record;
    skipped int := 0;
    applied int := 0;
    tables text[] := ARRAY[
        'bc_general_ledger_entries',
        'bc_item_ledger_entries',
        'bc_value_entries',
        'bc_detailed_gst_ledger_entries',
        'bc_customer_ledger_entries',
        'bc_sales_return_order_lines',
        'bc_posted_sales_credit_memo_lines',
        'bc_posted_sales_invoice_lines',
        'bc_posted_sales_cr_memo_lines',
        'bc_customer_item_reference',
        'bc_posted_sales_invoice_excel',
        'bc_vendor_ledger_entries'
    ];
BEGIN
    SET LOCAL lock_timeout = '3s';

    -- Named bc_* tables, so the smaller ones are covered too.
    FOREACH t IN ARRAY tables LOOP
        -- Skip anything not yet created: on a fresh database the sync builds
        -- these, and this file must not fail before it has.
        CONTINUE WHEN to_regclass('public.' || t) IS NULL;
        BEGIN
            EXECUTE format(
                'ALTER TABLE public.%I SET ('
                '  autovacuum_vacuum_scale_factor = 0.02,'
                '  autovacuum_vacuum_threshold = 5000,'
                '  autovacuum_analyze_scale_factor = 0.01,'
                '  autovacuum_analyze_threshold = 2500)', t);
            applied := applied + 1;
        EXCEPTION WHEN lock_not_available THEN
            skipped := skipped + 1;
            RAISE NOTICE 'busy, skipped (next run will retry): %', t;
        END;
    END LOOP;

    -- Everything else above 250k rows, whoever owns it. Catches tables that
    -- grow past the line later without anyone editing this file.
    FOR r IN
        SELECT s.relname
        FROM pg_stat_user_tables s
        JOIN pg_class c ON c.oid = s.relid
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND s.n_live_tup > 250000
          AND NOT (s.relname = ANY(tables))
    LOOP
        BEGIN
            EXECUTE format(
                'ALTER TABLE public.%I SET ('
                '  autovacuum_vacuum_scale_factor = 0.02,'
                '  autovacuum_vacuum_threshold = 5000,'
                '  autovacuum_analyze_scale_factor = 0.01,'
                '  autovacuum_analyze_threshold = 2500)', r.relname);
            applied := applied + 1;
        EXCEPTION WHEN lock_not_available THEN
            skipped := skipped + 1;
            RAISE NOTICE 'busy, skipped (next run will retry): %', r.relname;
        END;
    END LOOP;

    RAISE NOTICE 'autovacuum tuning: % table(s) applied, % skipped as busy', applied, skipped;
END $$;

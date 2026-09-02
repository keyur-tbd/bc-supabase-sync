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
-- NOT touched: instamart_ads_performance (10.5M rows), zepto_campaign_
-- performance (6.7M), bb_shopper_level_search, blinkit_*, amz_*, fk_pla_*.
-- They are larger and have the same problem, but they belong to other
-- pipelines sharing this database - their owners should decide.

DO $$
DECLARE
    t text;
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
    FOREACH t IN ARRAY tables LOOP
        -- Skip anything not yet created: on a fresh database the sync builds
        -- these, and this file must not fail before it has.
        IF to_regclass('public.' || t) IS NOT NULL THEN
            EXECUTE format(
                'ALTER TABLE public.%I SET ('
                '  autovacuum_vacuum_scale_factor = 0.02,'
                '  autovacuum_vacuum_threshold = 5000,'
                '  autovacuum_analyze_scale_factor = 0.01,'
                '  autovacuum_analyze_threshold = 2500)', t);
        END IF;
    END LOOP;
END $$;

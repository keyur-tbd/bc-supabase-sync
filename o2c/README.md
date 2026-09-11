# Order-to-cash bridge (PO -> invoice -> GRN -> credit memo)

Materialized views in `public` that join the FY27 posted sales invoices to the customers' GRN feeds and
to the short-GRN credit memos, one row per invoice x ERP item. Birbal reads them as
`warehouse.o2c_bridge`, `warehouse.o2c_invoices`, `warehouse.o2c_credit_memos`,
`warehouse.o2c_grn_lines` and `warehouse.ref_customer_invoicing`; the `warehouse_meta` docs are in
`o2c_docs.sql`.

| file | what | when to run |
|---|---|---|
| `o2c_bridge.sql` | drops and rebuilds all six matviews, `o2c_refresh()`, the warehouse views and grants | only when the definitions change (6-10 min, by hand, as `postgres`) |
| `o2c_tags.sql`, `o2c_tags2.sql` | `ref_customer_invoicing` corrections (which GRN feed / matching rule per customer) | after `o2c_bridge.sql`, or alone when a customer's feed changes |
| `o2c_docs.sql` | `warehouse.warehouse_meta` rows for the exposed views | after either of the above |

**These are NOT applied by `scripts/apply_sql.py`** (it only reads `sql/*.sql`): a rebuild drops the
matviews Birbal is querying and takes minutes, so it stays a deliberate, hand-run step. Apply with
`psql "$SUPABASE_DB_URL" -v ON_ERROR_STOP=1 -c "set statement_timeout='1800000'" -f o2c/o2c_bridge.sql`
(then tags, then docs), or the equivalent from psycopg2 with the same timeout.

Refreshing the data is a different thing and is scheduled **in the database**: `pg_cron_schedule.sql`
has pg_cron call `select public.o2c_refresh()` at **09:00 and 14:00 IST** (since 2026-09-11; it was
09:00 and 18:00 on GitHub Actions, which started those runs 4-5 hours late every day). Check runs with
`select * from cron.job_run_details order by start_time desc limit 10`. `.github/workflows/o2c_refresh.yml`
no longer has a schedule; run it by hand from the Actions tab after a GRN backfill or between beats.
Birbal's fill-rate and sales-facts crons (its `vercel.json`) are timed off these two times.

Two rules that bit already:

* Supabase's default privileges grant every new object in `public` to `anon`/`authenticated`. Tables
  are covered by an empty RLS policy set; materialized views cannot have RLS, so `o2c_bridge.sql` ends
  with an explicit `revoke ... from anon, authenticated`. Keep it there.
* The `warehouse.*` views over these are hand-made and not in `app.sync_warehouse_views()`'s list, so
  that function leaves them alone; `app.sync_role_grants()` re-grants them because the Birbal role is a
  wildcard role. If a non-wildcard role is ever added, its `role_tables` rows must list them.

The matching rules, known feed anomalies and the Zepto sheet gap are documented in the
`warehouse_meta` docs and in `o2c_bridge.sql` comments.

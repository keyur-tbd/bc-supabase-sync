-- o2c/o2c_refresh.sql
-- The o2c refresh, split in two and timed step by step (2026-11-20).
--
-- WHY. o2c_refresh() did everything in ONE transaction: the six o2c matviews, then feed_inventory (~4.5 min), the
-- debit-note reversals, the capping ledger and cogs_refresh() (the costed register, the MIS view, the P&L lines and
-- the Spends facts). By Nov 2026 a run took 35-42 minutes and ran ~10 times a day (the 09:00 / 14:00 IST beats plus
-- Birbal's birbal_scm_follow_up() after every BC sync). On 2026-11-20 the 11:53 IST run hit its 50-minute
-- statement_timeout inside cogs_refresh -> app.refresh_spend_facts, and the timeout rolled back the BRIDGE too: the
-- SCM board waited another ~50 minutes for a rerun. o2c_grn_lines / o2c_grn_matched are refreshed without
-- CONCURRENTLY, so their ACCESS EXCLUSIVE lock also lasted until the end of the costing work.
--
-- NOW:
--   o2c_refresh_bridge()      what the SCM board and the Order to Cash report read: o2c_sync_customer_ref, the six
--                             matviews, register_reversals_refresh, rtv_capping_refresh (the last two are ~10 s each
--                             and feed the GST-register readers). The pg_cron beats (pg_cron_schedule.sql) and
--                             Birbal's follow-up call this.
--   o2c_refresh_downstream()  feed_inventory + cogs_refresh. Birbal's birbal_scm_follow_up() runs it as its own step
--                             after the board and the report, in its own transaction, so a failure or timeout there
--                             can no longer undo the bridge.
--   o2c_refresh()             both, in one transaction, as before -- for a hand run / the GitHub workflow only.
--
-- public.o2c_refresh_steps keeps one row per step with its duration (60 days). A run that FAILS rolls its rows back
-- with it; cron.job_run_details.return_message names the failing step in its CONTEXT lines.
--   select fn, step, count(*), round(avg(secs)) avg_s, max(secs) max_s
--     from public.o2c_refresh_steps where started_at > now() - interval '3 days' group by 1, 2 order by 4 desc;
--
-- Apply as postgres, whole file, idempotent; takes no lock on the matviews (functions + one small table).

create table if not exists public.o2c_refresh_steps (
    id             bigserial primary key,
    run_started_at timestamptz not null,
    fn             text not null,
    step           text not null,
    started_at     timestamptz not null,
    secs           numeric(10, 1) not null,
    ok             boolean not null,
    note           text
);
create index if not exists o2c_refresh_steps_started on public.o2c_refresh_steps (started_at desc);
alter table public.o2c_refresh_steps enable row level security;
revoke all on public.o2c_refresh_steps from anon, authenticated;
revoke all on sequence public.o2c_refresh_steps_id_seq from anon, authenticated;
comment on table public.o2c_refresh_steps is
  'o2c/o2c_refresh.sql (2026-11-20): one row per step of o2c_refresh_bridge() / o2c_refresh_downstream(), with its '
  'duration. Failed runs roll their rows back; see cron.job_run_details for those. Kept 60 days.';

create or replace function public.o2c_log_step(p_run timestamptz, p_fn text, p_step text, p_t0 timestamptz,
                                               p_ok boolean default true, p_note text default null)
returns void language sql security definer as $$
    insert into public.o2c_refresh_steps (run_started_at, fn, step, started_at, secs, ok, note)
    values (p_run, p_fn, p_step, p_t0, round(extract(epoch from clock_timestamp() - p_t0)::numeric, 1), p_ok,
            left(p_note, 500));
$$;

-- ------------------------------------------------------------------ the bridge
create or replace function public.o2c_refresh_bridge() returns text language plpgsql security definer as $$
declare
    t0 timestamptz := clock_timestamp();
    t  timestamptz;
begin
    delete from public.o2c_refresh_steps where started_at < now() - interval '60 days';

    -- 2026-10-19 (o2c_customer_ref.sql): ref_customer_invoicing follows the ERP card and channel_map -- a customer that
    -- invoices without a row gets one, erp_channel is kept current, non-reviewed rows follow a platform change. First,
    -- so o2c_invoice_hdr below refreshes with the rows in place. Its absence or failure must not fail the refresh.
    t := clock_timestamp();
    begin
        perform public.o2c_sync_customer_ref();
        perform public.o2c_log_step(t0, 'bridge', 'o2c_sync_customer_ref', t);
    exception
        when undefined_function then
            raise warning 'o2c_sync_customer_ref() not present (o2c/o2c_customer_ref.sql not applied); skipped';
            perform public.o2c_log_step(t0, 'bridge', 'o2c_sync_customer_ref', t, false, 'not present');
        when others then
            raise warning 'o2c_sync_customer_ref failed: %', sqlerrm;
            perform public.o2c_log_step(t0, 'bridge', 'o2c_sync_customer_ref', t, false, sqlerrm);
    end;

    -- CONCURRENTLY where a unique index exists so Birbal's reads are never blocked; the two intermediates have
    -- no natural unique key and are refreshed in place (they are read only through o2c_grn_lines).
    t := clock_timestamp();
    refresh materialized view concurrently public.o2c_invoice_hdr;
    perform public.o2c_log_step(t0, 'bridge', 'o2c_invoice_hdr', t);
    t := clock_timestamp();
    refresh materialized view concurrently public.o2c_invoice_lines;
    perform public.o2c_log_step(t0, 'bridge', 'o2c_invoice_lines', t);
    t := clock_timestamp();
    refresh materialized view public.o2c_grn_lines;
    perform public.o2c_log_step(t0, 'bridge', 'o2c_grn_lines', t);
    t := clock_timestamp();
    refresh materialized view public.o2c_grn_matched;
    perform public.o2c_log_step(t0, 'bridge', 'o2c_grn_matched', t);
    t := clock_timestamp();
    refresh materialized view concurrently public.o2c_credit_memos;
    perform public.o2c_log_step(t0, 'bridge', 'o2c_credit_memos', t);
    t := clock_timestamp();
    refresh materialized view concurrently public.o2c_bridge;
    perform public.o2c_log_step(t0, 'bridge', 'o2c_bridge', t);

    -- Birbal migration 076 (2026-09-12): the credit notes BC withdrew with a debit note, and those debit notes, are
    -- paired line for line into public.register_reversal_lines and left out of warehouse.v_sales_register_gst_detail.
    -- Everything costed downstream reads that view, so the pairs are rebuilt here, before o2c_refresh_downstream();
    -- a stale table only means a withdrawn note counts until the next beat, never a wrong pair.
    t := clock_timestamp();
    begin
        perform public.register_reversals_refresh();
        perform public.o2c_log_step(t0, 'bridge', 'register_reversals_refresh', t);
    exception
        when undefined_function then
            raise warning 'register_reversals_refresh() not present (Birbal migration 076 not applied); skipped';
            perform public.o2c_log_step(t0, 'bridge', 'register_reversals_refresh', t, false, 'not present');
        when others then
            raise warning 'register_reversals_refresh failed: %', sqlerrm;
            perform public.o2c_log_step(t0, 'bridge', 'register_reversals_refresh', t, false, sqlerrm);
    end;
    -- Birbal migration 036 (2026-09-09): the returns-capping ledger (mv_rtv_capping_ledger) is built off the same GST
    -- register; it was on no beat at all before, so the provision Birbal quotes trailed the sales it provides against.
    -- Its own block, so one failing surface does not skip the next.
    t := clock_timestamp();
    begin
        perform public.rtv_capping_refresh();
        perform public.o2c_log_step(t0, 'bridge', 'rtv_capping_refresh', t);
    exception
        when undefined_function then
            raise warning 'rtv_capping_refresh() not present (Birbal migration 036 not applied); skipped';
            perform public.o2c_log_step(t0, 'bridge', 'rtv_capping_refresh', t, false, 'not present');
        when others then
            raise warning 'rtv_capping_refresh failed: %', sqlerrm;
            perform public.o2c_log_step(t0, 'bridge', 'rtv_capping_refresh', t, false, sqlerrm);
    end;

    perform public.o2c_log_step(t0, 'bridge', 'total', t0);
    return 'o2c bridge refreshed in ' || round(extract(epoch from clock_timestamp() - t0)) || 's at ' || now();
end $$;

-- ------------------------------------------------------------------ after the bridge
create or replace function public.o2c_refresh_downstream() returns text language plpgsql security definer as $$
declare
    t0 timestamptz := clock_timestamp();
    t  timestamptz;
    r  text;
begin
    -- Birbal migration 037 (2026-09-09): the "what data exists" inventory (~4.5 min; it counts every warehouse
    -- relation). Birbal answers "do you have X data" from warehouse.feed_inventory, so it must not go stale -- but a
    -- probe failure must never fail the refresh, hence the exception block.
    t := clock_timestamp();
    begin
        perform app.refresh_feed_inventory();
        perform public.o2c_log_step(t0, 'downstream', 'refresh_feed_inventory', t);
    exception when others then
        raise warning 'feed_inventory refresh failed: %', sqlerrm;
        perform public.o2c_log_step(t0, 'downstream', 'refresh_feed_inventory', t, false, sqlerrm);
    end;
    -- Birbal migration 039 (2026-09-09) and later: the costed sales register (mv_sales_cogs), mv_mis_sales,
    -- mv_ads_campaign_month, mv_pnl_lines, the Primary board's net-sales reconcile and the Spends facts, all off the
    -- GST register. cogs_refresh() returns its own per-surface counts; they go in the note.
    t := clock_timestamp();
    begin
        r := public.cogs_refresh();
        perform public.o2c_log_step(t0, 'downstream', 'cogs_refresh', t, true, r);
    exception
        when undefined_function then
            raise warning 'cogs_refresh() not present (Birbal migration 039 not applied); skipped';
            perform public.o2c_log_step(t0, 'downstream', 'cogs_refresh', t, false, 'not present');
        when others then
            raise warning 'cogs_refresh failed: %', sqlerrm;
            perform public.o2c_log_step(t0, 'downstream', 'cogs_refresh', t, false, sqlerrm);
    end;

    perform public.o2c_log_step(t0, 'downstream', 'total', t0);
    return 'o2c downstream refreshed in ' || round(extract(epoch from clock_timestamp() - t0)) || 's at ' || now();
end $$;

-- ------------------------------------------------------------------ both, one transaction (hand runs only)
create or replace function public.o2c_refresh() returns text language plpgsql security definer as $$
declare t0 timestamptz := clock_timestamp();
begin
    -- Not on any schedule since 2026-11-20: the beats call o2c_refresh_bridge() and Birbal's follow-up runs
    -- o2c_refresh_downstream() as its own step. Kept for a hand run (GitHub o2c_refresh.yml calls the two parts in
    -- separate transactions instead).
    perform public.o2c_refresh_bridge();
    perform public.o2c_refresh_downstream();
    return 'o2c refreshed in ' || round(extract(epoch from clock_timestamp() - t0)) || 's at ' || now();
end $$;

-- Supabase grants EXECUTE on every new function in public to anon / authenticated; these are SECURITY DEFINER
-- refreshes that run for minutes. postgres (pg_cron) and service_role only.
revoke execute on function public.o2c_log_step(timestamptz, text, text, timestamptz, boolean, text),
                           public.o2c_refresh_bridge(), public.o2c_refresh_downstream(), public.o2c_refresh()
  from public, anon, authenticated;
grant execute on function public.o2c_refresh_bridge(), public.o2c_refresh_downstream(), public.o2c_refresh()
  to service_role;

-- o2c_customer_ref.sql -- ref_customer_invoicing follows the ERP customer card and Birbal's channel_map (2026-10-19)
--
-- Keyur, 2026-10-19: "all the parties that do not have platforms should be seen against the ERP for channel consideration".
-- ref_customer_invoicing was seeded once (2026-09-06) and corrected by hand since, so every customer opened after that
-- invoiced with platform NULL (16 by 19 Oct 2026, two of them online) and Birbal's boards showed them under no platform.
-- From here public.o2c_sync_customer_ref() runs first thing in o2c_refresh() (and can be run alone):
--   1. erp_channel = bc_customer_card."Customer_Posting_Group", the ERP's own channel for the customer (TR-ONLINE,
--      TR-MOD T, TR-GEN T, TR-DIST, TR-ALTERNATE CHANNEL, TR-OTHER; TR-D2C / TR-MP sit on dead codes), kept current on every row;
--   2. a row a person has NOT marked reviewed follows warehouse.channel_map when its platform changes (first case: the
--      2026-10-19 merge of 'Flipkart' into 'Flipkart Quick', Birbal migration 185); the platform's feed text is copied in
--      when the row had none;
--   3. a customer with FY27 invoices and no row gets one: platform = channel_map (Birbal's search-name rules plus
--      channel_map_overrides), else 'Trade' for every ERP channel except TR-ONLINE -- an online customer no rule names keeps
--      platform NULL and says so in grn_match_rule, so it shows up instead of hiding under Trade; grn_feed / grn_match_rule are
--      copied from the platform's rows ('No GRN is received from this customer' for Trade); po_invoice_pattern comes from the
--      observed statistics with the same >= 5% test the first seed used (a blank PO counts as its own PO).
-- Reviewed rows are never touched. Apply as postgres after o2c_bridge.sql, or alone when the rule changes.

alter table public.ref_customer_invoicing add column if not exists erp_channel text;
comment on column public.ref_customer_invoicing.erp_channel is
  'bc_customer_card.Customer_Posting_Group: the channel the ERP files the customer under (TR-ONLINE, TR-MOD T, TR-GEN T, TR-DIST, TR-ALTERNATE CHANNEL, TR-OTHER); kept current by o2c_sync_customer_ref()';

create or replace function public.o2c_sync_customer_ref() returns text language plpgsql security definer as $$
declare n_chan int; n_plat int; n_new int;
begin
    -- 1. the ERP channel, every row
    update public.ref_customer_invoicing r
       set erp_channel = c."Customer_Posting_Group"
      from public.bc_customer_card c
     where c."No" = r.customer_no and r.erp_channel is distinct from c."Customer_Posting_Group";
    get diagnostics n_chan = row_count;

    -- 2. non-reviewed rows follow channel_map when Birbal's platform for the customer changes
    update public.ref_customer_invoicing r
       set platform = cm.platform,
           grn_feed = coalesce(r.grn_feed, tpl.grn_feed),
           grn_match_rule = case when r.grn_feed is null and tpl.grn_feed is not null then tpl.grn_match_rule else r.grn_match_rule end,
           note = concat_ws(' | ', r.note, 'platform ' || coalesce(r.platform, 'NULL') || ' -> ' || cm.platform || ' from channel_map on ' || current_date),
           updated_at = now()
      from warehouse.channel_map cm
      left join lateral (select t.grn_feed, t.grn_match_rule from public.ref_customer_invoicing t
                          where t.platform = cm.platform and t.grn_feed is not null and t.customer_no <> cm.customer_no
                          order by t.updated_at desc limit 1) tpl on true
     where cm.customer_no = r.customer_no and cm.platform is not null and r.platform is distinct from cm.platform and not r.reviewed;
    get diagnostics n_plat = row_count;

    -- 3. customers invoicing since Apr 2026 with no row at all
    insert into public.ref_customer_invoicing (customer_no, customer_name, search_name, platform, erp_channel, po_invoice_pattern,
                                               grn_feed, grn_match_rule, pos_since_apr26, invoices_since_apr26, inv_per_po,
                                               max_invoices_per_po, pct_pos_multi_invoice, note, reviewed, updated_at)
    select s.customer_no, c."Name", c."Search_Name",
           coalesce(cm.platform, case when c."Customer_Posting_Group" = 'TR-ONLINE' then null else 'Trade' end),
           c."Customer_Posting_Group",
           case when s.pct_multi >= 5 then 'ONE_PO_MANY_INVOICES' else 'ONE_PO_ONE_INVOICE' end,
           tpl.grn_feed,
           coalesce(tpl.grn_match_rule,
                    case when cm.platform is null and c."Customer_Posting_Group" = 'TR-ONLINE'
                         then 'ERP channel TR-ONLINE but no channel_map rule names the platform: add a warehouse.channel_map_overrides row and the next o2c_refresh() picks it up'
                         else 'No GRN is received from this customer' end),
           s.pos::int, s.inv::int, s.inv_per_po, s.max_inv::int, s.pct_multi,
           'added by o2c_sync_customer_ref() on ' || current_date || ' from the ERP card (Customer_Posting_Group ' || coalesce(c."Customer_Posting_Group", '?') || ')'
             || case when s.pct_multi >= 5 then '; observed: >=5% of POs carry more than one live invoice' else '' end,
           false, now()
      from (select h.customer_no,
                   count(distinct h.po_key) as pos,
                   count(*) as inv,
                   round(count(*)::numeric / greatest(count(distinct h.po_key), 1), 2) as inv_per_po,
                   max(h.n_per_po) as max_inv,
                   round(100.0 * count(distinct h.po_key) filter (where h.n_per_po > 1) / greatest(count(distinct h.po_key), 1), 1) as pct_multi
              from (select x.customer_no, x.po_key, count(*) over (partition by x.customer_no, x.po_key) as n_per_po
                      from (select customer_no, coalesce(nullif(po_number, ''), invoice_no) as po_key
                              from public.o2c_invoice_hdr where not cancelled) x) h
             group by h.customer_no) s
      join public.bc_customer_card c on c."No" = s.customer_no
      left join warehouse.channel_map cm on cm.customer_no = s.customer_no
      left join lateral (select t.grn_feed, t.grn_match_rule from public.ref_customer_invoicing t
                          where t.platform = cm.platform and t.grn_feed is not null order by t.updated_at desc limit 1) tpl on true
     where not exists (select 1 from public.ref_customer_invoicing r where r.customer_no = s.customer_no);
    get diagnostics n_new = row_count;

    return format('ref_customer_invoicing: %s new customer(s), %s platform change(s), %s erp_channel update(s)', n_new, n_plat, n_chan);
end $$;

revoke all on function public.o2c_sync_customer_ref() from public;
grant execute on function public.o2c_sync_customer_ref() to service_role;

-- the Birbal view picks up erp_channel (select * is expanded when the view is created); Birbal migration 185 rebuilds the
-- birbal_r twin with app.build_restricted_surface()
create or replace view warehouse.ref_customer_invoicing as select * from public.ref_customer_invoicing;

-- Order-to-Cash bridge: PO -> Invoice -> Credit Memo -> GRN, for Birbal.
-- Materialised views (no pg_cron on this project): call select public.o2c_refresh() after the BC sync and the GRN loaders run.
-- Window: posted sales invoices from FY27 (posting date >= 2026-04-01), which is also where bc_posted_sales_invoice_lines begins.

drop materialized view if exists public.o2c_bridge cascade;
drop materialized view if exists public.o2c_credit_memos cascade;
drop materialized view if exists public.o2c_grn_matched cascade;
drop materialized view if exists public.o2c_invoice_lines cascade;
drop materialized view if exists public.o2c_invoice_hdr cascade;
drop materialized view if exists public.o2c_grn_lines cascade;

-- ------------------------------------------------------------------ 1. invoice headers, FY27, with PO cleaning and cancellation links
create materialized view public.o2c_invoice_hdr as
with h as (
    select h."No" as invoice_no, h."Sell_to_Customer_No" as customer_no, h."Sell_to_Customer_Name" as customer_name, h."Ship_to_Code" as ship_to_code,
           nullif(h."Order_No", '') as so_no, h."External_Document_No" as po_number_raw,
           regexp_replace(replace(coalesce(h."External_Document_No", ''), U&'\FFFD', ''), '[^A-Za-z0-9]+$', '') as po_number,
           h."Posting_Date" as invoice_date, coalesce(h."Cancelled", false) as cancelled, h."Location_Code" as warehouse_code, h."AmountToCustomer" as invoice_amount,
           upper(regexp_replace(h."No", '[^A-Za-z0-9]', '', 'g')) as inv_norm,
           substring(h."No" from '^[0-9]{2}([A-Z]{3})-') as branch, substring(h."No" from '-([0-9]{5})$') as num5
    from public.bc_posted_sales_invoice_excel h
    where h."Posting_Date" >= date '2026-04-01'
),
cm as (
    select "Applies_to_Doc_No" as invoice_no, min("No") as cancellation_cm_no
    from public.bc_posted_sales_credit_memo where (coalesce("Corrective", false) or coalesce("Correction", false)) and "Applies_to_Doc_Type" = 'Invoice' group by 1
)
select h.*,
       upper(h.po_number) as po_number_u,
       h.po_number_raw <> h.po_number as po_suffixed,
       case when h.cancelled then 'CANCELLED' else 'LIVE' end as invoice_status,
       cm.cancellation_cm_no,
       (select min(r.invoice_no) from h r where r.customer_no = h.customer_no and r.po_number = h.po_number and not r.cancelled and r.invoice_no <> h.invoice_no and r.invoice_date >= h.invoice_date) as replacement_invoice_no,
       (select max(c.invoice_no) from h c where c.customer_no = h.customer_no and c.po_number = h.po_number and c.cancelled and c.invoice_no <> h.invoice_no and c.invoice_date <= h.invoice_date) as replaces_invoice_no,
       coalesce(rc.po_invoice_pattern, 'ONE_PO_ONE_INVOICE') as po_invoice_pattern, rc.platform, rc.search_name, rc.grn_feed as expected_grn_feed
from h
left join cm on cm.invoice_no = h.invoice_no
left join public.ref_customer_invoicing rc on rc.customer_no = h.customer_no;
create unique index on public.o2c_invoice_hdr (invoice_no);
create index on public.o2c_invoice_hdr (inv_norm);
create index on public.o2c_invoice_hdr (branch, num5);
create index on public.o2c_invoice_hdr (customer_no, po_number);
create index on public.o2c_invoice_hdr (customer_no, po_number_u);

-- ------------------------------------------------------------------ 2. invoice item lines, one row per invoice x item
-- item codes are normalised through item_rename_map: the ERP still posts ~7% of lines under the old FG code of a renamed item
create materialized view public.o2c_invoice_lines as
select l."Document_No" as invoice_no, coalesce(r.new_no, l."No") as erp_item_no, min(l."Line_No") as line_no,
       sum(l."Quantity") as invoiced_qty, max(l."Purchase_Order_Quantity") as po_qty, max(l."Unit_Price") as unit_price, sum(l."Line_Amount") as invoiced_value,
       max(l."Purchase_Order_No") as po_number_line, string_agg(distinct l."No", ', ') as erp_item_no_raw
from public.bc_posted_sales_invoice_lines l
left join warehouse.item_rename_map r on r.old_no = l."No"
where l."Type" = 'Item' and l."Document_No" in (select invoice_no from public.o2c_invoice_hdr)
group by 1, 2;
create unique index on public.o2c_invoice_lines (invoice_no, erp_item_no);
create index on public.o2c_invoice_lines (erp_item_no);

-- ------------------------------------------------------------------ 3. GRN feeds, one shape
create materialized view public.o2c_grn_lines as
with raw as (
    select 'hot_grn' as feed, g.id, 'Blinkit' as platform, array['C00524'] as customer_nos, g.po_number as po, null::text as inv, null::text as grn_no, null::date as grn_date,
           g.item_code::text as feed_item_code, 'Blinkit' as map_platform, g.quantity_grn as received_qty, g.quantity_po as ordered_qty, null::numeric as rejected_qty, null::text as reject_reason, g.source_file
    from public.hot_grn g
    union all
    select 'hyperpure_grn', g.id, 'Blinkit', array['C00063'], g.po_number, g.vendor_invoice_number, null, g.grn_date, g.product_no::text, 'Blinkit ZHPL', g.grn_qty, g.qty_ordered, g.damaged_qty, case when coalesce(g.damaged_qty, 0) > 0 then 'damaged' end, g.source_file
    from public.hyperpure_grn g
    union all
    select 'instamart_grn', g.id, 'Instamart', array['C00152', 'C00153', 'C00154', 'C00156', 'C00157'], g.po_number, g.invoice_no, g.grn_no, g.grn_date, g.sku_code::text, 'Instamart', g.recv_qty, g.exp_qty, null, null, g.source_file
    from public.instamart_grn g
    union all
    select 'milkbasket_grn', g.id, 'Milkbasket', array['C00035'], g.po_number, g.vendor_invoice_number, g.grn_number, g.grn_date, g.article::text, 'Milkbasket', g.accepted_qty, g.received_qty, null, null, g.source_file
    from public.milkbasket_grn g
    union all
    select 'reliance_grn', g.id, 'Milkbasket', array['C00035'], g.po_number, g.vendor_invoice_number, g.grn_number, g.grn_date, null, null, null, null, null, null, g.source_file
    from public.reliance_grn g
    union all
    select 'nb_grn', g.id, 'Nature''s Basket', array['C00026'], g.po_no, g.invoice_no, g.grn_no, g.grn_date, g.article_code::text, 'Nature''s Basket', g.accepted_quantity, g.received_quantity, g.rejected_quantity, null, g.source_file
    from public.nb_grn g
    union all
    select 'mraws_grn', g.id, 'More Retail', array['C00049'], g.po_number, g.vendor_invoice_number, null, g.grn_date, g.sku::text, 'More Retail', g.rcv_qty, g.ord_qty, null, null, g.source_file
    from public.mraws_grn g
    union all
    select 'mrgrn_grn', g.id, 'More Retail', array['C00049'], g.po_number, g.vendor_invoice_number, null, g.grn_date, g.sku::text, 'More Retail', g.rcv_qty, g.ord_qty, null, null, g.source_file
    from public.mrgrn_grn g
    union all
    select 'bb_alert_grn', g.id, 'BigBasket', array['C00005'], g.po_no, g.invoice_no, g.grn_no, g.grn_timestamp::date, g.sku_code::text, 'BigBasket', g.accepted_quantity, null, g.rejected_quantity, nullif(g.rejected_reason, '0'), g.source_file
    from public.bb_alert_grn g
    union all
    select 'bb_net_grn', g.id, 'BigBasket', array['C00005'], g.pono, g.invoiceno, g.grnno, g.invoicedate::date, g.skucode::text, 'BigBasket', g.quantity, null, null, null, g.source_file
    from public.bb_net_grn g
    union all
    -- Flipkart Minutes: GRN documents arrive under Flipkart's name while the ERP invoices the fulfilment partner (63IDEAS/Ninjacart, PRR, Fatema, Cropbasket, Mulyam, SL Veggies, Shreyash), so every feed is matched on PO number across the whole family
    select 'flipkart_cb_grn', g.id, 'Flipkart', array['C00048', 'C00198', 'C00213', 'C00347', 'C00408', 'C00409', 'C00410', 'C00521', 'C00542', 'C00483'], g.po_number, null, null, g.expected_delivery_date::date, g.item_description, 'Flipkart', g.grn_quantity, g.po_quantity, null, null, g.source_file
    from public.flipkart_cb_grn g
    union all
    select 'flipkart_ppr_grn', g.id, 'Flipkart', array['C00048', 'C00198', 'C00213', 'C00347', 'C00408', 'C00409', 'C00410', 'C00521', 'C00542', 'C00483'], null, coalesce(g.supplier_invoice_number, g.invoice_number), null, g.invoice_date::date, g.description_of_goods, 'Flipkart', g.quantity, null, null, null, g.source_file
    from public.flipkart_ppr_grn g
    union all
    select 'fatema_grn', g.id, 'Flipkart', array['C00048', 'C00198', 'C00213', 'C00347', 'C00408', 'C00409', 'C00410', 'C00521', 'C00542', 'C00483'], g.po_number, null, g.bill_no, g.bill_date::date, coalesce(g.fsn_no, g.item_code), 'Flipkart', g.quantity, null, null, null, g.source_file
    from public.fatema_grn g
    union all
    select 'slveggies_grn', g.id, 'Flipkart', array['C00048', 'C00198', 'C00213', 'C00347', 'C00408', 'C00409', 'C00410', 'C00521', 'C00542', 'C00483'], g.po_number, g.invoice_number, g.bill_number, g.bill_date::date, g.fsn_number, 'Flipkart', coalesce(g.grn_quantity, g.received_quantity), g.indent_quantity, g.return_quantity, null, g.source_file
    from public.slveggies_grn g
    union all
    -- Zepto: portal PO report pasted into a Google Sheet (sheet_grn_loader). One row per PO x SKU; the GRN quantity is only evidence once the PO has closed
    select 'zepto_grn', g.id, 'Zepto', array(select customer_no from warehouse.channel_map where platform = 'Zepto'), g.po_no, null, null, null, lower(g.sku), 'Zepto',
           case when g.status in ('COMPLETED', 'GRN_DONE', 'EXPIRED', 'CANCELLED') then g.grn_quantity end, g.qty, null,
           case when g.status in ('PENDING_ACKNOWLEDGEMENT', 'ASN_CREATED', 'PENDING_GRN') then 'grn pending (' || g.status || ')' when g.status in ('EXPIRED', 'CANCELLED') then lower(g.status) end, g.source_file
    from public.zepto_grn g
    union all
    -- Amazon Vendor Central receipts pasted into a Google Sheet (sheet_grn_loader). One row per PO x ASIN; Unconfirmed POs have not been received yet
    select 'amazon_grn', g.id, 'Amazon', array(select customer_no from warehouse.channel_map where platform = 'Amazon'), g.po_number, nullif(g.invoice_number, ''), null, g.invoice_date, g.asin, 'Amazon',
           case when g.invoice_status <> 'Unconfirmed' then g.received_quantity end, g.invoice_quantity, g.shortage_quantity,
           case when g.invoice_status = 'Unconfirmed' then 'not yet received (Unconfirmed)' when coalesce(g.has_shortage, '') <> '' then 'shortage: ' || g.has_shortage end, g.source_file
    from public.amazon_grn g
),
split as (
    select r.*, upper(regexp_replace(replace(coalesce(r.po, ''), U&'\FFFD', ''), '[^A-Za-z0-9]+$', '')) as po_number,   -- upper: Cropbasket's portal writes 682026.dbe.po.10, the ERP 682026.DBE.PO.10
           trim(c.cand) as invoice_ref
    from raw r
    left join lateral regexp_split_to_table(coalesce(r.inv, ''), '\s*[,;&]\s*|\s+and\s+') as c(cand) on true
)
select s.feed, s.id as grn_row_id, s.platform, s.customer_nos, s.po_number, s.inv as invoice_ref_raw, s.invoice_ref,
       upper(regexp_replace(s.invoice_ref, '[^A-Za-z0-9]', '', 'g')) as cand_norm,
       (regexp_match(upper(regexp_replace(s.invoice_ref, '/.*$', '')), '(?:[0-9]{2}\s*)?([A-Z]{3})\s*-?\s*0*([0-9]{3,5})(?![0-9])'))[1] as cand_branch,
       lpad((coalesce(regexp_match(upper(regexp_replace(s.invoice_ref, '/.*$', '')), '(?:[0-9]{2}\s*)?[A-Z]{3}\s*-?\s*0*([0-9]{3,5})(?![0-9])'), regexp_match(s.invoice_ref, '^0*([0-9]{3,5})$')))[1], 5, '0') as cand_num5,
       s.grn_no, s.grn_date, s.feed_item_code, coalesce(r.new_no, m.erp_item_no) as erp_item_no, s.received_qty, s.ordered_qty, s.rejected_qty, s.reject_reason, s.source_file,
       row_number() over (partition by s.feed, s.po_number, s.inv, s.grn_no, s.feed_item_code, s.grn_date, s.received_qty order by s.id) as dup_seq
from split s
left join public.ref_platform_item_map m on m.platform = s.map_platform and lower(m.platform_sku_code) = lower(s.feed_item_code) and m.erp_item_no is not null
left join warehouse.item_rename_map r on r.old_no = m.erp_item_no;
create index on public.o2c_grn_lines (cand_norm);
create index on public.o2c_grn_lines (cand_branch, cand_num5);
create index on public.o2c_grn_lines (po_number);
create index on public.o2c_grn_lines (feed, grn_row_id);

-- ------------------------------------------------------------------ 4. resolve each GRN line to an invoice (cascade)
create materialized view public.o2c_grn_matched as
select g.*, r.invoice_no, r.match_method
from public.o2c_grn_lines g
left join lateral (
    select c.invoice_no, c.match_method from (
        select h.invoice_no, 1 as pri, 'invoice_exact' as match_method from public.o2c_invoice_hdr h where g.cand_norm <> '' and h.inv_norm = g.cand_norm
        union all
        select h.invoice_no, 2, 'invoice_core+po' from public.o2c_invoice_hdr h
         where g.cand_num5 is not null and h.num5 = g.cand_num5 and (g.cand_branch is null or h.branch = g.cand_branch) and g.po_number <> '' and h.po_number_u = g.po_number and h.customer_no = any(g.customer_nos)
        union all
        select h.invoice_no, 3, 'invoice_core' from public.o2c_invoice_hdr h
         where g.cand_num5 is not null and g.cand_branch is not null and h.num5 = g.cand_num5 and h.branch = g.cand_branch and h.customer_no = any(g.customer_nos)
           and (select count(*) from public.o2c_invoice_hdr x where x.num5 = g.cand_num5 and x.branch = g.cand_branch and x.customer_no = any(g.customer_nos)) = 1
        union all
        select h.invoice_no, 4, 'po_unique' from public.o2c_invoice_hdr h
         where g.po_number <> '' and h.po_number_u = g.po_number and h.customer_no = any(g.customer_nos) and not h.cancelled
           and (select count(*) from public.o2c_invoice_hdr x where x.po_number_u = g.po_number and x.customer_no = any(g.customer_nos) and not x.cancelled) = 1
        union all
        select h.invoice_no, 5, 'po+item+qty' from public.o2c_invoice_hdr h join public.o2c_invoice_lines l on l.invoice_no = h.invoice_no
         where g.po_number <> '' and h.po_number_u = g.po_number and h.customer_no = any(g.customer_nos) and not h.cancelled
           and g.erp_item_no is not null and l.erp_item_no = g.erp_item_no and g.received_qty is not null and l.invoiced_qty = g.received_qty
        union all
        select h.invoice_no, 6, 'po+item+date' from public.o2c_invoice_hdr h join public.o2c_invoice_lines l on l.invoice_no = h.invoice_no
         where g.po_number <> '' and h.po_number_u = g.po_number and h.customer_no = any(g.customer_nos) and not h.cancelled
           and g.erp_item_no is not null and l.erp_item_no = g.erp_item_no and h.invoice_date <= coalesce(g.grn_date, h.invoice_date)
        union all
        select h.invoice_no, 7, 'po_ambiguous' from public.o2c_invoice_hdr h
         where g.po_number <> '' and h.po_number_u = g.po_number and h.customer_no = any(g.customer_nos) and not h.cancelled
    ) c
    join public.o2c_invoice_hdr hh on hh.invoice_no = c.invoice_no
    order by c.pri, hh.invoice_date desc, c.invoice_no
    limit 1
) r on true;
create index on public.o2c_grn_matched (invoice_no, erp_item_no);
create index on public.o2c_grn_matched (feed, grn_row_id);

-- ------------------------------------------------------------------ 5. credit memos, FY27, categorised and linked to an invoice
create materialized view public.o2c_credit_memos as
with c as (
    select cm."No" as credit_memo_no, cm."Sell_to_Customer_No" as customer_no, cm."Sell_to_Customer_Name" as customer_name, cm."Posting_Date" as credit_memo_date,
           coalesce(cm."AmountToCustomer", 0) as credit_memo_amount, nullif(cm."Sales_Return_Reason_Code", '') as reason_code,
           coalesce(cm."Corrective", false) or coalesce(cm."Correction", false) as is_cancellation,
           nullif(cm."Applies_to_Doc_No", '') as applies_to_doc_no, cm."Applies_to_Doc_Type" as applies_to_doc_type,
           nullif(cm."External_Document_No", '') as external_document_no, nullif(cm."PRN_Document_No", '') as prn_document_no, cm."PRN_Document_Date" as prn_document_date,
           cm."Location_Code" as warehouse_code
    from public.bc_posted_sales_credit_memo cm where cm."Posting_Date" >= date '2026-04-01'
)
select c.*,
       case when c.is_cancellation then 'INVOICE_CANCELLATION'
            when c.reason_code = 'SHGRN' then 'SHORT_GRN'
            when c.reason_code = 'EXP' then 'EXPIRY_RETURN'
            when c.reason_code = 'DMG' then 'DAMAGE'
            when c.reason_code = 'INC' then 'INC'
            when c.reason_code = 'FR' then 'FR'
            when c.reason_code = 'RECO-CN' then 'RECONCILIATION'
            when c.reason_code = 'CAPPING RT' then 'CAPPING_RETURN'
            else coalesce(c.reason_code, 'UNSPECIFIED') end as category,
       r.invoice_no, r.link_method,
       coalesce(h.po_number, regexp_replace(replace(coalesce(c.external_document_no, ''), U&'\FFFD', ''), '[^A-Za-z0-9]+$', '')) as po_number,
       h.platform
from c
left join lateral (
    select x.invoice_no, x.link_method from (
        select h1.invoice_no, 1 as pri, 'applies_to' as link_method from public.o2c_invoice_hdr h1 where c.applies_to_doc_type = 'Invoice' and h1.invoice_no = c.applies_to_doc_no
        union all
        select h2.invoice_no, 2, 'document_ref' from public.o2c_invoice_hdr h2
         where h2.inv_norm in (upper(regexp_replace(regexp_replace(coalesce(c.external_document_no, ''), '/.*$', ''), '[^A-Za-z0-9]', '', 'g')),
                               upper(regexp_replace(regexp_replace(coalesce(c.prn_document_no, ''), '/.*$', ''), '[^A-Za-z0-9]', '', 'g')))
        union all
        select h3.invoice_no, 3, 'po_unique' from public.o2c_invoice_hdr h3
         where h3.customer_no = c.customer_no and not h3.cancelled
           and h3.po_number = regexp_replace(replace(coalesce(c.external_document_no, ''), U&'\FFFD', ''), '[^A-Za-z0-9]+$', '') and h3.po_number <> ''
           and (select count(*) from public.o2c_invoice_hdr y where y.customer_no = c.customer_no and not y.cancelled and y.po_number = h3.po_number) = 1
    ) x order by x.pri, x.invoice_no limit 1
) r on true
left join public.o2c_invoice_hdr h on h.invoice_no = r.invoice_no;
create unique index on public.o2c_credit_memos (credit_memo_no);
create index on public.o2c_credit_memos (invoice_no);

-- ------------------------------------------------------------------ 6. the bridge: one row per invoice x item
create materialized view public.o2c_bridge as
with grn as (
    select m.invoice_no, m.erp_item_no,
           string_agg(distinct m.feed, ', ') as grn_feed,
           string_agg(distinct m.grn_no, ', ') filter (where m.grn_no is not null) as grn_nos,
           max(m.grn_date) as grn_date,
           sum(m.received_qty) filter (where m.erp_item_no is not null and m.dup_seq = 1) as grn_qty,
           sum(m.rejected_qty) filter (where m.dup_seq = 1) as grn_rejected_qty,
           count(*) filter (where m.dup_seq > 1) as grn_duplicate_rows,
           string_agg(distinct m.reject_reason, ', ') filter (where m.reject_reason is not null) as grn_reject_reason,
           min(m.match_method) as grn_match_method,
           bool_or(m.match_method = 'po_ambiguous') as grn_ambiguous
    from public.o2c_grn_matched m where m.invoice_no is not null
    group by 1, 2
),
grn_inv as (   -- invoice-level presence, plus whether the invoice's GRN rows are item-complete (every row mapped to an ERP item)
    select m.invoice_no, string_agg(distinct m.feed, ', ') as grn_feed, string_agg(distinct m.grn_no, ', ') filter (where m.grn_no is not null) as grn_nos, max(m.grn_date) as grn_date, min(m.match_method) as grn_match_method,
           count(*) filter (where m.feed_item_code is not null and m.erp_item_no is not null) as item_rows,
           count(*) filter (where m.feed_item_code is not null and m.erp_item_no is null) as unmapped_rows,
           count(*) filter (where m.feed_item_code is null) as header_only_rows
    from public.o2c_grn_matched m where m.invoice_no is not null group by 1
),
shcm as (
    select cm.invoice_no, coalesce(r.new_no, coalesce(nullif(l."Item_No", ''), l."No")) as erp_item_no,
           string_agg(distinct cm.credit_memo_no, ', ') as shgrn_cm_nos, sum(l."Quantity") as shgrn_cm_qty, sum(l."Line_Amount") as shgrn_cm_value
    from public.o2c_credit_memos cm
    join public.bc_posted_sales_cr_memo_lines l on l."Document_No" = cm.credit_memo_no and l."Type" = 'Item'
    left join warehouse.item_rename_map r on r.old_no = coalesce(nullif(l."Item_No", ''), l."No")
    where cm.category = 'SHORT_GRN' and cm.invoice_no is not null
    group by 1, 2
),
shcm_inv as (
    select cm.invoice_no, string_agg(distinct cm.credit_memo_no, ', ') as shgrn_cm_nos, sum(cm.credit_memo_amount) as shgrn_cm_amount
    from public.o2c_credit_memos cm where cm.category = 'SHORT_GRN' and cm.invoice_no is not null group by 1
)
select h.po_number, h.po_number_raw, h.po_suffixed, h.platform, h.customer_no, h.customer_name, h.search_name as customer_search_name, h.po_invoice_pattern,
       h.ship_to_code, cl."Name" as ship_to_name, h.warehouse_code, h.so_no,
       h.invoice_no, h.invoice_date, h.invoice_status, h.cancellation_cm_no, h.replacement_invoice_no, h.replaces_invoice_no,
       l.erp_item_no, ic."Description" as item_name, ic."Item_Category_Code" as item_category,
       l.po_qty, l.invoiced_qty, l.unit_price, l.invoiced_value,
       coalesce(g.grn_feed, gi.grn_feed) as grn_feed, coalesce(g.grn_nos, gi.grn_nos) as grn_nos, coalesce(g.grn_date, gi.grn_date) as grn_date,
       g.grn_qty, g.grn_rejected_qty, g.grn_reject_reason, g.grn_duplicate_rows, coalesce(g.grn_match_method, gi.grn_match_method) as grn_match_method,
       cov.grn_coverage,
       case when cov.grn_coverage = 'ITEM_QTY' then greatest(l.invoiced_qty - g.grn_qty, 0) when cov.grn_coverage = 'ITEM_NOT_IN_GRN' then l.invoiced_qty end as short_qty,
       case when cov.grn_coverage = 'ITEM_QTY' then greatest(g.grn_qty - l.invoiced_qty, 0) end as excess_qty,
       case when cov.grn_coverage = 'ITEM_QTY' then round(greatest(l.invoiced_qty - g.grn_qty, 0) * l.unit_price, 2) when cov.grn_coverage = 'ITEM_NOT_IN_GRN' then round(l.invoiced_qty * l.unit_price, 2) end as short_value,
       coalesce(s.shgrn_cm_nos, si.shgrn_cm_nos) as shgrn_cm_nos, s.shgrn_cm_qty, s.shgrn_cm_value,
       case when h.cancelled then 'INVOICE_CANCELLED'
            when cov.grn_coverage = 'ITEM_QTY' then
                 case when g.grn_qty >= l.invoiced_qty then case when s.shgrn_cm_qty > 0 then 'CREDITED_WITHOUT_SHORTAGE' else 'GRN_FULL' end
                      when coalesce(s.shgrn_cm_qty, 0) >= l.invoiced_qty - g.grn_qty then 'SHORT_CREDITED'
                      when coalesce(s.shgrn_cm_qty, 0) > 0 then 'SHORT_PARTIALLY_CREDITED'
                      when si.invoice_no is not null then 'SHORT_CREDITED_AT_INVOICE_LEVEL'
                      else 'SHORT_CREDIT_PENDING' end
            when cov.grn_coverage = 'ITEM_NOT_IN_GRN' then
                 case when coalesce(s.shgrn_cm_qty, 0) >= l.invoiced_qty then 'SHORT_CREDITED'
                      when coalesce(s.shgrn_cm_qty, 0) > 0 then 'SHORT_PARTIALLY_CREDITED'
                      when si.invoice_no is not null then 'SHORT_CREDITED_AT_INVOICE_LEVEL'
                      else 'NOT_IN_GRN_CREDIT_PENDING' end
            when si.invoice_no is not null then 'CREDITED_NO_GRN_VISIBILITY'
            else 'GRN_NOT_AVAILABLE' end as credit_status
from public.o2c_invoice_hdr h
join public.o2c_invoice_lines l on l.invoice_no = h.invoice_no
left join grn g on g.invoice_no = h.invoice_no and g.erp_item_no = l.erp_item_no
left join grn_inv gi on gi.invoice_no = h.invoice_no
left join shcm s on s.invoice_no = h.invoice_no and s.erp_item_no = l.erp_item_no
left join shcm_inv si on si.invoice_no = h.invoice_no
cross join lateral (select case when h.cancelled then 'CANCELLED_INVOICE'
                                when g.grn_qty is not null then 'ITEM_QTY'
                                when gi.invoice_no is not null and gi.item_rows > 0 and gi.unmapped_rows = 0 and gi.header_only_rows = 0 then 'ITEM_NOT_IN_GRN'
                                when gi.invoice_no is not null then 'INVOICE_ONLY'
                                when h.expected_grn_feed is null then 'NO_FEED'
                                else 'NOT_FOUND' end as grn_coverage) cov
left join public.bc_item_card ic on ic."No" = l.erp_item_no
left join public.bc_ship_to_address cl on cl."Code" = h.ship_to_code;
create unique index on public.o2c_bridge (invoice_no, erp_item_no);
create index on public.o2c_bridge (po_number);
create index on public.o2c_bridge (customer_no, invoice_date);
create index on public.o2c_bridge (credit_status);

-- ------------------------------------------------------------------ refresh
create or replace function public.o2c_refresh() returns text language plpgsql security definer as $$
declare t0 timestamptz := clock_timestamp();
begin
    -- CONCURRENTLY where a unique index exists so Birbal's reads are never blocked; the two intermediates have
    -- no natural unique key and are refreshed in place (they are read only through o2c_grn_lines).
    refresh materialized view concurrently public.o2c_invoice_hdr;
    refresh materialized view concurrently public.o2c_invoice_lines;
    refresh materialized view public.o2c_grn_lines;
    refresh materialized view public.o2c_grn_matched;
    refresh materialized view concurrently public.o2c_credit_memos;
    refresh materialized view concurrently public.o2c_bridge;
    return 'o2c refreshed in ' || round(extract(epoch from clock_timestamp() - t0)) || 's at ' || now();
end $$;

-- ------------------------------------------------------------------ Birbal surface
create or replace view warehouse.o2c_bridge as select * from public.o2c_bridge;
create or replace view warehouse.o2c_invoices as
select po_number, po_number_raw, po_suffixed, platform, customer_no, customer_name, customer_search_name, po_invoice_pattern, ship_to_code, ship_to_name, warehouse_code, so_no,
       invoice_no, invoice_date, invoice_status, cancellation_cm_no, replacement_invoice_no, replaces_invoice_no,
       count(*) as item_lines, sum(po_qty) as po_qty, sum(invoiced_qty) as invoiced_qty, sum(invoiced_value) as invoiced_value,
       max(grn_feed) as grn_feed, max(grn_nos) as grn_nos, max(grn_date) as grn_date, sum(grn_qty) as grn_qty, sum(short_qty) as short_qty, sum(excess_qty) as excess_qty, sum(short_value) as short_value,
       count(*) filter (where grn_coverage = 'ITEM_NOT_IN_GRN') as lines_not_in_grn,
       min(grn_coverage) as grn_coverage, max(shgrn_cm_nos) as shgrn_cm_nos, sum(shgrn_cm_qty) as shgrn_cm_qty, sum(shgrn_cm_value) as shgrn_cm_value,
       case when bool_or(invoice_status = 'CANCELLED') then 'INVOICE_CANCELLED'
            when bool_or(credit_status = 'SHORT_CREDIT_PENDING') then 'SHORT_CREDIT_PENDING'
            when bool_or(credit_status = 'NOT_IN_GRN_CREDIT_PENDING') then 'NOT_IN_GRN_CREDIT_PENDING'
            when bool_or(credit_status = 'SHORT_PARTIALLY_CREDITED') then 'SHORT_PARTIALLY_CREDITED'
            when bool_or(credit_status in ('SHORT_CREDITED', 'SHORT_CREDITED_AT_INVOICE_LEVEL')) then 'SHORT_CREDITED'
            when bool_or(credit_status = 'CREDITED_NO_GRN_VISIBILITY') then 'CREDITED_NO_GRN_VISIBILITY'
            when bool_and(credit_status in ('GRN_FULL', 'CREDITED_WITHOUT_SHORTAGE')) then 'GRN_FULL'
            when bool_or(credit_status = 'GRN_FULL') then 'PARTLY_VISIBLE_GRN_FULL'
            else 'GRN_NOT_AVAILABLE' end as credit_status
from public.o2c_bridge
group by po_number, po_number_raw, po_suffixed, platform, customer_no, customer_name, customer_search_name, po_invoice_pattern, ship_to_code, ship_to_name, warehouse_code, so_no,
         invoice_no, invoice_date, invoice_status, cancellation_cm_no, replacement_invoice_no, replaces_invoice_no;
create or replace view warehouse.o2c_credit_memos as select * from public.o2c_credit_memos;
create or replace view warehouse.o2c_grn_lines as
select feed, platform, po_number, invoice_ref_raw, invoice_no, match_method, grn_no, grn_date, feed_item_code, erp_item_no, received_qty, ordered_qty, rejected_qty, reject_reason, source_file
from public.o2c_grn_matched;
create or replace view warehouse.ref_customer_invoicing as select * from public.ref_customer_invoicing;
create or replace view warehouse.zepto_grn as select * from public.zepto_grn;
create or replace view warehouse.amazon_grn as select * from public.amazon_grn;
grant select on warehouse.zepto_grn, warehouse.amazon_grn to birbal_scope_df78b2a571ce4041b7b1db77c72cb69e;
grant select on warehouse.o2c_bridge, warehouse.o2c_invoices, warehouse.o2c_credit_memos, warehouse.o2c_grn_lines, warehouse.ref_customer_invoicing to birbal_scope_df78b2a571ce4041b7b1db77c72cb69e;
grant select on public.o2c_bridge, public.o2c_credit_memos, public.o2c_grn_matched, public.ref_customer_invoicing to birbal_scope_df78b2a571ce4041b7b1db77c72cb69e;
-- Supabase's default privileges hand every new object in public to anon/authenticated (the keys that ship in
-- browsers). Tables are protected by an empty RLS policy set; materialized views cannot have RLS, so the grant
-- must be revoked explicitly every time these are rebuilt.
revoke all on public.o2c_invoice_hdr, public.o2c_invoice_lines, public.o2c_grn_lines, public.o2c_grn_matched,
           public.o2c_credit_memos, public.o2c_bridge from anon, authenticated;

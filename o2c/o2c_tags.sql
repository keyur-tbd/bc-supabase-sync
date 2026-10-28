-- Corrections to the customer tagging (business input 2026-09-06). Never touches rows a person has marked reviewed.
update public.ref_customer_invoicing set
    grn_feed = 'milkbasket_grn + reliance_grn',
    grn_match_rule = 'Milkbasket: milkbasket_grn by INVOICE, MHB-numbered rows by PO + ITEM + QTY. Reliance Signature / Smart stores: reliance_grn by INVOICE, item-level from the GRN PDF lines (article via the Milkbasket item map). 1 PO : many invoices.',
    note = 'named by the business as 1 PO : many invoices; GRN received for Milkbasket, Reliance Signature and Reliance Smart',
    updated_at = now()
where customer_no = 'C00035' and not reviewed;

update public.ref_customer_invoicing set
    grn_feed = null,
    grn_match_rule = 'GRN is received from Metro Cash & Carry but no loader exists in Supabase yet',
    note = 'named by the business as 1 PO : many invoices; GRN received but not loaded',
    updated_at = now()
where customer_no = 'C00356' and not reviewed;

-- 2026-10-19 (Keyur: "there is not Flipkart, everything is Flipkart Minutes"): the whole family is platform 'Flipkart Quick' --
-- Flipkart India (C00048), Mulyam and every fulfilment partner warehouse.channel_map tags so (Birbal migration 185) -- and
-- o2c_sync_customer_ref() keeps the platform in step with channel_map, so the family is addressed by platform, not by code.
update public.ref_customer_invoicing set
    grn_feed = 'flipkart_quick_grn / flipkart_cb_grn / flipkart_ppr_grn / fatema_grn / slveggies_grn',
    grn_match_rule = 'flipkart_quick_grn (Flipkart Minutes portal PO report, FLIPKART QUICK tab of sheet 1IjwfjZg..., loader sheet_grn_loader, 3-hourly since 2026-10-16) by PO + FSN, CLOSED / CANCELLED PO lines only; its FLS... POs are invoiced on C00048 FLIPKART INDIA. The partners'' own GRN documents (flipkart_cb_grn = Cropbasket, flipkart_ppr_grn = PRR; fatema_grn and slveggies_grn have never received a file) by PO number across every Flipkart Minutes entity (channel_map platform Flipkart Quick)',
    updated_at = now()
where platform = 'Flipkart Quick' and not reviewed
  and (grn_feed is distinct from 'flipkart_quick_grn / flipkart_cb_grn / flipkart_ppr_grn / fatema_grn / slveggies_grn'
       or grn_match_rule is null or grn_match_rule not like '%channel_map platform Flipkart Quick%');

update public.ref_customer_invoicing set
    grn_match_rule = 'PO + ITEM against zepto_grn (Zepto portal PO report pasted into a Google Sheet, loaded 3-hourly by sheet_grn_loader). GRN quantity counts once the PO status is COMPLETED / GRN_DONE / EXPIRED / CANCELLED, or on a PENDING_GRN / ASN_CREATED line once Zepto has booked a GRN quantity. An invoice whose Zepto lines are all still open with nothing booked is GRN pending, not short.',
    note = 'GRN sheet: docs.google.com/spreadsheets/d/1Txws1Qan9QVyR3qJTlup5BadQ9KwI6qk84KQb8bMbEQ',
    updated_at = now()
where customer_no in ('C00155', 'C00043') and not reviewed;

update public.ref_customer_invoicing set
    grn_match_rule = 'amazon_grn (Google Sheet downloaded from Vendor Central; loader sheet_grn_loader, pending sheet access) by PO number',
    note = 'GRN sheet: docs.google.com/spreadsheets/d/1IjwfjZg_l9JTiRlosZaNId-QPpfHi48JMH1_Tz8trPo',
    updated_at = now()
where customer_no = 'C00002' and not reviewed;

update public.ref_customer_invoicing set
    grn_match_rule = 'No GRN is received from this customer',
    updated_at = now()
where grn_feed is null and customer_no not in ('C00356', 'C00155', 'C00043', 'C00002') and platform in ('Trade', 'DMart', 'DMart Ready', 'JioBP', 'Vision', 'Cranberry', 'First Club', 'Nature''s Basket') and not reviewed;

-- Metro Cash & Carry's GRNs arrive in the Reliance feed (2026-09-23)
update public.ref_customer_invoicing
   set grn_feed = 'reliance_grn',
       grn_match_rule = 'reliance_grn (Metro ship-to rows) by INVOICE, else PO; item-level from the GRN PDF lines (article via the Milkbasket item map). Metro GRNs start with invoices of 12 Aug 2026.',
       reviewed = true, updated_at = now()
 where customer_no = 'C00356';

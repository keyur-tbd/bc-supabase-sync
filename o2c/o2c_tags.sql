-- Corrections to the customer tagging (business input 2026-09-06). Never touches rows a person has marked reviewed.
update public.ref_customer_invoicing set
    grn_feed = 'milkbasket_grn + reliance_grn',
    grn_match_rule = 'Milkbasket: milkbasket_grn by INVOICE, MHB-numbered rows by PO + ITEM + QTY. Reliance Signature / Smart stores: reliance_grn by INVOICE (header-level feed, no item quantities). 1 PO : many invoices.',
    note = 'named by the business as 1 PO : many invoices; GRN received for Milkbasket, Reliance Signature and Reliance Smart',
    updated_at = now()
where customer_no = 'C00035' and not reviewed;

update public.ref_customer_invoicing set
    grn_feed = null,
    grn_match_rule = 'GRN is received from Metro Cash & Carry but no loader exists in Supabase yet',
    note = 'named by the business as 1 PO : many invoices; GRN received but not loaded',
    updated_at = now()
where customer_no = 'C00356' and not reviewed;

update public.ref_customer_invoicing set
    grn_feed = 'flipkart_cb_grn / flipkart_ppr_grn / fatema_grn / slveggies_grn',
    grn_match_rule = 'PO number matched across every Flipkart Minutes entity (GRN documents arrive under Flipkart''s name while the ERP invoices the fulfilment partner); feeds hold only a few rows as of Sep 2026',
    updated_at = now()
where customer_no in ('C00048', 'C00198', 'C00213', 'C00347', 'C00408', 'C00409', 'C00410', 'C00521', 'C00542', 'C00483') and not reviewed;

update public.ref_customer_invoicing set
    grn_match_rule = 'zepto_grn (Google Sheet downloaded from the Zepto portal; loader sheet_grn_loader, pending sheet access) by PO number',
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

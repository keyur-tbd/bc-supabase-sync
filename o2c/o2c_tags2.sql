-- Zepto and Amazon GRN now loaded from the portal sheets (sheet_grn_loader), 2026-09-06
update public.ref_customer_invoicing set
    grn_feed = 'zepto_grn',
    grn_match_rule = 'PO + ITEM against zepto_grn (Zepto portal PO report pasted into a Google Sheet, loaded daily by sheet_grn_loader). GRN quantity counts once the PO status is COMPLETED / GRN_DONE / EXPIRED / CANCELLED; pending POs show as GRN pending.',
    note = 'GRN sheet: docs.google.com/spreadsheets/d/1Txws1Qan9QVyR3qJTlup5BadQ9KwI6qk84KQb8bMbEQ (shared with instamart@thebakersdozen.in)',
    updated_at = now()
where customer_no in (select customer_no from warehouse.channel_map where platform = 'Zepto') and not reviewed;

update public.ref_customer_invoicing set
    grn_feed = 'amazon_grn',
    grn_match_rule = 'PO + ASIN against amazon_grn (Vendor Central receipts pasted into a Google Sheet, loaded daily by sheet_grn_loader; the report carries no invoice number). Unconfirmed POs are not yet received.',
    note = 'GRN sheet: docs.google.com/spreadsheets/d/1IjwfjZg_l9JTiRlosZaNId-QPpfHi48JMH1_Tz8trPo, tab Amazon (shared with instamart@thebakersdozen.in)',
    updated_at = now()
where customer_no in (select customer_no from warehouse.channel_map where platform = 'Amazon') and not reviewed;

delete from warehouse.warehouse_meta where table_name in ('zepto_grn', 'amazon_grn');
insert into warehouse.warehouse_meta (table_name, doc_md) values
('zepto_grn', $doc$LIVE GRN. Zepto's own PO report (downloaded from the Zepto vendor portal and pasted into a Google Sheet, loaded daily by sheet_grn_loader): one row per PO x SKU with the PO quantity (qty), ASN quantity and the GRN quantity Zepto booked (grn_quantity), plus status (COMPLETED, GRN_DONE = received; PENDING_ACKNOWLEDGEMENT, ASN_CREATED, PENDING_GRN = not yet received; EXPIRED, CANCELLED). po_no is the Zepto PO number (= po_lines.po_number and uw_sale_order.display_order_code); sku is Zepto's GUID (map to the ERP item through ref_platform_item_map, platform Zepto, lower-cased); del_location is the Zepto warehouse; po_date is month-only text (e.g. Sep-2026). No invoice number, so o2c_bridge ties it to the invoice by PO + item. Re-reads of the sheet are idempotent (row_hash). Columns: po_no, po_date, status, vendor_name, del_location, sku, sku_desc, mrp, qty, unit_base_cost, landing_cost, total_amount, asn_quantity, grn_quantity, source_file, drive_file_id, sheet_row, processed_at$doc$),
('amazon_grn', $doc$LIVE GRN. Amazon Vendor Central receipts (downloaded from Vendor Central and pasted into the Google Sheet tab "Amazon", loaded daily by sheet_grn_loader): one row per PO x ASIN with received_quantity (what Amazon booked in), invoice_status (Closed = done, Confirmed, Unconfirmed = not yet received), invoice_date, cost_price, amount_received, and shortage / price-discrepancy columns where Amazon raised one. po_number is the Amazon PO (= po_lines.po_number and uw_sale_order.display_order_code); asin maps to the ERP item through ref_platform_item_map, platform Amazon. The report does not carry our invoice number, so o2c_bridge ties it to the invoice by PO + ASIN. Columns: po_number, asin, external_id, vendor_code, invoice_number, parent_invoice_number, invoice_date, invoice_status, has_shortage, has_price_discrepancy, cost_price, invoice_quantity, received_quantity, amount_received, shortage_quantity, amount_shortage, price_discrepancy_amount, amazon_paid_cost, freight_term, currency, source_file, drive_file_id, sheet_row, processed_at$doc$);

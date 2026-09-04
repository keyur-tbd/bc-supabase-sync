-- v_sales_register_gst_detail
-- =============================================================================
-- Rebuild of Business Central partner report 74365 "Sales Register GST Detail",
-- which cannot be fetched as a report over OData. Column names are the report's
-- own headers, verbatim, so an export of this view lines up 1:1 with an export
-- of the report.
--
-- SCOPE: FY 2026-27 onwards (the source tables are synced from 2026-04-01).
-- The view itself has no date floor - it shows whatever the source tables hold.
--
-- GRAIN: one row per posted document line with a non-zero quantity. The LINES
-- are the driver and the GST ledger is LEFT JOINed - not the other way round.
-- That ordering matters: 8,358 of April-2026's 69,194 rows (542 invoice, 7,816
-- credit memo) have NO Detailed GST Ledger entry at all, yet the report still
-- prints them. Exempt lines on an INVOICE do get 0% ledger entries, but the
-- equivalent credit-memo lines mostly do not, so a ledger-driven join silently
-- loses two thirds of the credit-memo rows. Zero-quantity lines are excluded:
-- there are 1,913 of them in April and the report prints none.
--
-- What a line with no ledger entry gets instead, all verified against the 8,246
-- such rows in the April-2026 export:
--   GST %              0                    (100% of them)
--   GST Group Type     'Goods'              (100%)
--   GST Group Code     from the Item Card, blank on G/L Account lines
--   GST HSN/SAC        from the Item Card - authoritative, and the only source
--                      that covers the 3 items appearing nowhere in the ledger
--   GST Base Amount    the line's own taxable amount
--   place of supply /  taken from another line of the same document, else
--   jurisdiction /     computed (jurisdiction) or defaulted
--   both GSTINs
--
-- The ledger holds one row per line PER COMPONENT (CGST + SGST, or IGST); this
-- pivots them back to one row with three amount columns.
--
-- SIGN CONVENTION: the report prints -1 x the ledger. A sales invoice's
-- GST_Base_Amount is negative in the ledger and positive in the report; a
-- credit memo's is positive in the ledger and negative in the report. COGS is
-- the exception - the report prints it positive on both, so the sign is taken
-- off the document type rather than off the ledger.
--
-- NOT INCLUDED: Transfer Shipment rows. The report prints them (5,477 of the
-- 74,671 rows in the April-2026 export) but they carry no customer, no GST and
-- no COGS; the decision on 2026-08-28 was that the register is for P&L use and
-- does not need them. Adding them later means a third UNION branch off
-- Transfer_Order_Excel / its shipment page, which is not currently synced.
--
-- ALWAYS-BLANK COLUMNS: Varient, Colour, Size, Barcode, Category, Sub-Category
-- and Season Code are blank in every one of the 69,194 invoice/credit-memo rows
-- of the April-2026 export - BC prints the header but never fills it. They are
-- emitted as empty strings so the column list matches. Likewise TCS Amount and
-- MRP Price are 0 throughout, and Transfer Order No. is blank on non-transfer
-- rows by definition.
--
-- SOURCES
--   bc_detailed_gst_ledger_entries  GST split, HSN, place of supply, both GSTINs
--   bc_posted_sales_invoice_excel   invoice headers
--   bc_posted_sales_invoice_lines   invoice lines
--   bc_posted_sales_credit_memo     credit-memo headers
--   bc_posted_sales_cr_memo_lines   credit-memo lines (keyed on the real Line_No)
--   bc_value_entries                COGS, and the bridge to the item ledger
--   bc_item_ledger_entries          MRP (per entry, so historically correct)
--   bc_customer_card                GST Registration Type, E-Commerce Operator
--   bc_item_card                    HSN/SAC and GST Group Code per item
--   bc_ship_to_address              ship-to GSTIN and name (loaded over SOAP,
--                                   see scripts/load_ship_to_address_soap.py)
--   ref_gst_state                   'MH' -> '27-MAHARASHTRA'
-- =============================================================================

-- DROP + CREATE rather than CREATE OR REPLACE: replacing a view cannot change
-- a column's data type, and these expressions do change type as the derivation
-- is refined (MRP went bigint -> numeric when it stopped being read straight
-- from the item ledger).
--
-- Things outside this repo DO depend on this view - warehouse.v_sales_register_gst_detail
-- wraps it for the returns dashboard - so a bare DROP fails with "cannot drop
-- ... because other objects depend on it" and takes the whole sync run red
-- (it did, 2026-09-04 05:34 UTC). DROP CASCADE on its own would be worse: it
-- deletes somebody else's view without a word. So capture every dependent
-- view first, DROP CASCADE, and rebuild them after the CREATE below.
--
-- This file is applied as ONE transaction, so a dependent that cannot be
-- rebuilt - because this view no longer has a column it selects - rolls the
-- whole thing back. The apply fails loudly instead of dropping a consumer.
-- Capture and rebuild run in the same session, which is what makes it safe to
-- replay pg_get_viewdef's unqualified names: they re-resolve under the same
-- search_path they were printed for.
--
-- Column-level grants are NOT carried over (nothing here uses them); table
-- privileges, owner and comment are.
CREATE TEMP TABLE _srgd_dependents ON COMMIT DROP AS
WITH RECURSIVE dep AS (
    -- to_regclass, not a ::regclass cast: on a fresh database the view does
    -- not exist yet and the cast would raise instead of finding nothing.
    SELECT to_regclass('public.v_sales_register_gst_detail') AS oid, 0 AS depth
    UNION ALL
    SELECT dc.oid, dep.depth + 1
    FROM dep
    JOIN pg_depend   d  ON d.refobjid   = dep.oid
                       AND d.refclassid = 'pg_class'::regclass
                       AND d.classid    = 'pg_rewrite'::regclass
    JOIN pg_rewrite  rw ON rw.oid = d.objid
    JOIN pg_class    dc ON dc.oid = rw.ev_class
                       AND dc.oid <> dep.oid
)
SELECT
    format('%I.%I', n.nspname, c.relname)        AS ident,
    -- A view reachable by two paths is recorded at its deepest, so rebuilding
    -- in ascending depth always finds what it selects from already there.
    max(dep.depth)                               AS depth,
    c.relkind                                    AS relkind,
    pg_get_viewdef(c.oid, true)                  AS definition,
    pg_get_userbyid(c.relowner)                  AS owner,
    array_to_string(c.reloptions, ', ')          AS reloptions,
    obj_description(c.oid, 'pg_class')           AS comment,
    (SELECT array_agg(format('GRANT %s ON %I.%I TO %s%s',
                             a.privilege_type, n.nspname, c.relname,
                             CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                                  ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
                             CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END))
       FROM aclexplode(c.relacl) a
      -- The owner's own privileges come back with the object; replaying them
      -- would only re-grant what CREATE VIEW already implies.
      WHERE a.grantee <> c.relowner)             AS grants
FROM dep
JOIN pg_class     c ON c.oid = dep.oid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE dep.depth > 0
GROUP BY c.oid, n.nspname, c.relname, c.relkind, c.relowner, c.reloptions, c.relacl;

-- A materialized view holds data, and rebuilding it from its definition would
-- leave it empty until somebody refreshed it. Refuse rather than truncate.
DO $srgd_guard$
DECLARE
    matviews text;
BEGIN
    SELECT string_agg(ident, ', ') INTO matviews
      FROM _srgd_dependents WHERE relkind <> 'v';
    IF matviews IS NOT NULL THEN
        RAISE EXCEPTION
            'materialized view(s) depend on public.v_sales_register_gst_detail: %',
            matviews
        USING HINT = 'Drop or detach them first - this script will not rebuild '
                     'a matview, and recreating one empty would silently break '
                     'whatever reads it.';
    END IF;
END
$srgd_guard$;

-- The DROP throws away the grants on THIS view too. Supabase's default
-- privileges re-grant postgres and service_role on the new object, so that
-- much heals itself, but a role granted read by hand - birbal_register_reader,
-- the reporting login - just quietly lost it on every apply until this was
-- added. Capture those and replay them after the CREATE.
CREATE TEMP TABLE _srgd_self ON COMMIT DROP AS
SELECT
    obj_description(c.oid, 'pg_class') AS comment,
    (SELECT array_agg(format('GRANT %s ON public.v_sales_register_gst_detail TO %s%s',
                             a.privilege_type,
                             CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                                  ELSE quote_ident(pg_get_userbyid(a.grantee)) END,
                             CASE WHEN a.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END))
       FROM aclexplode(c.relacl) a
      WHERE a.grantee <> c.relowner)  AS grants
FROM pg_class c
WHERE c.oid = to_regclass('public.v_sales_register_gst_detail');

DROP VIEW IF EXISTS public.v_sales_register_gst_detail CASCADE;

-- security_invoker = on: without it the view runs with the privileges of its
-- owner (postgres, which has BYPASSRLS), so anon - a role every browser holds
-- the key for - could read the whole sales register through the view even
-- though row level security is enabled on every bc_* table it selects from.
-- With it, the view is evaluated as the querying role and RLS applies. The
-- consumers (Power BI and the sync itself) connect as postgres, which
-- bypasses RLS regardless, so nothing they see changes.
CREATE VIEW public.v_sales_register_gst_detail
    WITH (security_invoker = on) AS

-- One row per document line per GST component, then per document line.
-- Reversed entries are excluded: BC flips that flag when an entry is cancelled,
-- and the report does not print them.
WITH gst_component AS (
    SELECT
        g."Document_No"                        AS doc_no,
        g."Document_Line_No"::numeric          AS line_no,
        g."GST_Component_Code"                 AS component,
        SUM(g."GST_Amount")                    AS amount,
        MAX(g."GST_Percent")                   AS pct
    FROM public.bc_detailed_gst_ledger_entries g
    WHERE g."Transaction_Type" = 'Sales'
      AND COALESCE(g."Reversed", false) = false
      AND g."Document_Type" IN ('Invoice', 'Credit Memo')
    GROUP BY 1, 2, 3
),
gst_line AS (
    SELECT
        g."Document_No"                        AS doc_no,
        g."Document_Line_No"::numeric          AS line_no,
        MAX(g."Document_Type")                 AS doc_type,
        MAX(g."HSN_SAC_Code")                  AS hsn_sac,
        MAX(g."GST_Group_Code")                AS gst_group_code,
        MAX(g."GST_Group_Type")                AS gst_group_type,
        MAX(g."GST_Jurisdiction_Type")         AS jurisdiction_type,
        MAX(g."GST_Place_of_Supply")           AS place_of_supply,
        MAX(g."Location_Reg_No")               AS company_gstin,
        MAX(g."Buyer_Seller_Reg_No")           AS customer_gstin,
        -- Every component of a line carries the same base; MAX picks that one
        -- value rather than summing it once per component.
        -1 * MAX(g."GST_Base_Amount")          AS gst_base_amount
    FROM public.bc_detailed_gst_ledger_entries g
    WHERE g."Transaction_Type" = 'Sales'
      AND COALESCE(g."Reversed", false) = false
      AND g."Document_Type" IN ('Invoice', 'Credit Memo')
    GROUP BY 1, 2
),
gst AS (
    SELECT
        l.*,
        COALESCE(-SUM(c.amount) FILTER (WHERE c.component = 'IGST'), 0) AS igst,
        COALESCE(-SUM(c.amount) FILTER (WHERE c.component = 'CGST'), 0) AS cgst,
        COALESCE(-SUM(c.amount) FILTER (WHERE c.component = 'SGST'), 0) AS sgst,
        -- The rate is per component (2.5 + 2.5 intrastate, 5 interstate), so the
        -- printed "GST %" is the sum across components.
        COALESCE(SUM(c.pct), 0)                                         AS gst_percent
    FROM gst_line l
    JOIN gst_component c ON c.doc_no = l.doc_no AND c.line_no = l.line_no
    GROUP BY l.doc_no, l.line_no, l.doc_type, l.hsn_sac, l.gst_group_code,
             l.gst_group_type, l.jurisdiction_type, l.place_of_supply,
             l.company_gstin, l.customer_gstin, l.gst_base_amount
),

-- Document-level fallbacks. A line with no ledger entry of its own usually
-- sits on a document where OTHER lines do have one, and these four values are
-- properties of the document, not the line.
gst_doc AS (
    SELECT
        g."Document_No"                AS doc_no,
        MAX(g."Location_Reg_No")       AS company_gstin,
        MAX(g."Buyer_Seller_Reg_No")   AS customer_gstin,
        MAX(g."GST_Place_of_Supply")   AS place_of_supply,
        MAX(g."GST_Jurisdiction_Type") AS jurisdiction_type
    FROM public.bc_detailed_gst_ledger_entries g
    WHERE g."Transaction_Type" = 'Sales'
      AND COALESCE(g."Reversed", false) = false
    GROUP BY 1
),

-- Customer x state -> GSTIN. The report always prints the customer's
-- registration IN THE SHIP-TO STATE (true for 69,097 of April-2026's 69,194
-- rows; the remaining 97 are unregistered B2C and print blank). The ledger's
-- own per-line Buyer_Seller_Reg_No is NOT that value on 4,860 rows - e.g.
-- 27BLR-00232 ships to Tamil Nadu, and five of its six lines carry the
-- customer's Gujarat GSTIN while only line 30000 carries the Tamil Nadu one.
-- A GSTIN's first two digits are its state code, which is what keys this.
cust_gstin AS (
    SELECT g."Source_No"                                        AS customer_no,
           LEFT(g."Buyer_Seller_Reg_No", 2)                     AS state_no,
           MODE() WITHIN GROUP (ORDER BY g."Buyer_Seller_Reg_No") AS gstin
    FROM public.bc_detailed_gst_ledger_entries g
    WHERE g."Transaction_Type" = 'Sales'
      AND COALESCE(g."Buyer_Seller_Reg_No", '') <> ''
    GROUP BY 1, 2
),

-- Location -> company GSTIN, learned by pairing each invoice header's location
-- with the GSTIN its own GST entries carry. The invoice header page exposes no
-- Location_GST_Reg_No (the credit-memo page does), so for an invoice whose
-- every line is exempt this is the only way back to the company GSTIN.
loc_gstin AS (
    SELECT h."Location_Code" AS location_code,
           MODE() WITHIN GROUP (ORDER BY gd.company_gstin) AS company_gstin
    FROM public.bc_posted_sales_invoice_excel h
    JOIN gst_doc gd ON gd.doc_no = h."No"
    WHERE gd.company_gstin IS NOT NULL AND h."Location_Code" IS NOT NULL
    GROUP BY 1
),

-- COGS and MRP both hang off the value entries, which are the only rows keyed
-- on (posted document, document line). The item ledger cannot be joined
-- directly: its Document_No is the SHIPMENT number (27SSHIP-...), never the
-- invoice number - hence the hop through Item_Ledger_Entry_No.
cost AS (
    SELECT
        v."Document_No"               AS doc_no,
        v."Document_Line_No"::numeric AS line_no,
        SUM(v."Cost_Amount_Actual")   AS cost_amount,
        MAX(i."MRP")                  AS mrp
    FROM public.bc_value_entries v
    LEFT JOIN public.bc_item_ledger_entries i
           -- Entry_No arrives as a JSON integer on the value-entry page and as
           -- a string on the item-ledger page, so the sync typed them bigint
           -- and text. Cast to text, not the reverse: that keeps the item
           -- ledger's primary-key index usable.
           ON i."Entry_No" = v."Item_Ledger_Entry_No"::text
    WHERE v."Item_Ledger_Entry_Type" = 'Sale'
    GROUP BY 1, 2
),

-- Both document flavours normalised to one shape before the report columns are
-- formatted, so the formatting is written once instead of twice.
doc_raw AS (
    SELECT
        'Invoice'::text                       AS document_type,
        h."No"                                AS doc_no,
        NULLIF(l."Line_No", '')::numeric      AS line_no,
        h."Location_Code"                     AS company_location,
        h."Invoice_Type"                      AS invoice_type,
        -- Printed only on credit memos: the report leaves it blank on
        -- invoices even where the line carries one (9 such April rows).
        NULL::text                            AS return_reason_code,
        h."Cancelled"                         AS cancelled,
        h."IRN_Hash"                          AS irn_no,
        h."E_Way_Bill_No"                     AS e_way_bill_no,
        h."Posting_Date"                      AS posting_date,
        h."Document_Date"                     AS invoice_date,
        h."Sell_to_Customer_No"               AS customer_no,
        h."Sell_to_Customer_Name"             AS customer_name,
        h."GST_Customer_Type"                 AS gst_customer_type,
        h."Ship_to_Code"                      AS ship_to_code,
        h."Ship_to_Name"                      AS ship_to_name,
        h."GST_Bill_to_State_Code"            AS bill_to_state,
        h."GST_Ship_to_State_Code"            AS ship_to_state,
        h."Location_State_Code"               AS location_state,
        h."Nature_of_Supply"                  AS nature_of_supply,
        h."Currency_Code"                     AS currency_code,
        h."GST_Without_Payment_of_Duty"       AS gst_wo_payment_of_duty,
        h."Bill_Of_Export_No"                 AS bill_of_export_no,
        h."Bill_Of_Export_Date"               AS bill_of_export_date,
        h."E_Commerce_Customer"               AS e_commerce_customer,
        h."E_Comm_Merchant_Id"                AS e_comm_merchant_id,
        h."Shortcut_Dimension_1_Code"         AS state_dim,
        h."Shortcut_Dimension_2_Code"         AS bu_dim,
        NULL::text                            AS prn_document_no,
        h."External_Document_No"              AS external_document_no,
        l."Type"                              AS line_type,
        l."No"                                AS line_no_field,
        l."Item_No"                           AS item_no,
        l."Description"                       AS line_description,
        l."Location_Code"                     AS line_location_code,
        l."Unit_of_Measure_Code"              AS unit_of_measure,
        l."Quantity"                          AS quantity,
        l."Unit_Price"                        AS unit_price,
        l."Line_Discount_Percent"             AS discount_percent,
        l."Line_Discount_Amount"              AS discount_amount,
        l."Line_Amount"                       AS line_amount,
        -- Page 132 exposes neither GSTIN; page 134 exposes both.
        NULL::text                            AS location_gst_reg_no,
        NULL::text                            AS ship_to_gst_reg_no
    FROM public.bc_posted_sales_invoice_excel h
    JOIN public.bc_posted_sales_invoice_lines l ON l."Document_No" = h."No"
    WHERE l."Quantity" <> 0

    UNION ALL

    SELECT
        'Credit Memo'::text,
        h."No",
        NULLIF(l."Line_No", '')::numeric,
        h."Location_Code",
        h."Invoice_Type",
        COALESCE(NULLIF(l."Return_Reason_Code", ''), h."Sales_Return_Reason_Code"),
        -- On a credit memo the report's "Cancelled" column is the CORRECTIVE
        -- flag, not Cancelled: all 341 April rows where the two disagree have
        -- Cancelled=false, Corrective=true, and the report prints 'Yes'.
        h."Corrective",
        h."IRN_Hash",
        -- GAP: page 134 exposes no e-way bill field at all (verified against
        -- the live $metadata), so credit-memo e-way bill numbers cannot be
        -- sourced from any published web service. 1,578 of April's 69,194 rows
        -- carry one. Fix is to add the field to page 134 in BC.
        NULL::text,
        h."Posting_Date",
        -- The report prints Invoice Date on invoices only; blank on credit
        -- memos (all 11,853 April credit-memo rows).
        NULL::date,
        h."Sell_to_Customer_No",
        h."Sell_to_Customer_Name",
        h."GST_Customer_Type",
        -- Page 134 exposes no Ship_to_Code on the header (page 132 does); the
        -- credit-memo LINE carries it instead.
        l."Ship_to_Code",
        h."Ship_to_Name",
        h."GST_Bill_to_State_Code",
        h."GST_Ship_to_State_Code",
        h."Location_State_Code",
        h."Nature_of_Supply",
        h."Currency_Code",
        h."GST_Without_Payment_of_Duty",
        h."Bill_Of_Export_No",
        h."Bill_Of_Export_Date",
        h."e_Commerce_Customer",
        h."E_Comm_Merchant_Id",
        h."Shortcut_Dimension_1_Code",
        h."Shortcut_Dimension_2_Code",
        h."PRN_Document_No",
        h."External_Document_No",
        l."Type",
        l."No",
        l."Item_No",
        l."Description",
        l."Location_Code",
        l."Unit_of_Measure_Code",
        -- BC stores credit-memo lines POSITIVE (58,413 positive, 0 negative
        -- across FY 2026-27) - a credit memo for 2 pieces is quantity 2. The
        -- report prints them negative. Negating here rather than at each
        -- formula keeps AmountLCY/GMV identical for both branches. The
        -- discount PERCENT is not negated: the report prints it positive.
        -l."Quantity",
        l."Unit_Price",
        l."Line_Discount_Percent",
        -l."Line_Discount_Amount",
        -l."Line_Amount",
        h."Location_GST_Reg_No",
        -- The report's GSTN is the SHIP-TO registration, not Customer_GST_Reg_No:
        -- on 27CNAHD-00002 the customer is registered 29AACCI2053A1Z3 in
        -- Karnataka but the report prints the Gujarat ship-to 24AACCI2053A1ZD.
        h."Ship_to_GST_Reg_No"
    FROM public.bc_posted_sales_credit_memo h
    JOIN public.bc_posted_sales_cr_memo_lines l ON l."Document_No" = h."No"
    WHERE l."Quantity" <> 0
),

-- A G/L account line is not always printed as one. BC posts sales returns to
-- account 35120010 while keeping the item in Item_No, and the report then
-- prints the ITEM, its master description, its HSN and GGST-0% - exactly as if
-- it were an item line. Three rules, all exact on April-2026's 608 G/L rows:
--   invoice        -> print the account number            (494 rows)
--   credit memo, Item_No set   -> print the item          ( 73 rows)
--   credit memo, Item_No empty -> print nothing at all    ( 41 rows)
-- MRP, GMV and COGS are 0 on all three kinds.
doc AS (
    SELECT
        d.*,
        -- What gets PRINTED in Item/Account.
        CASE
            WHEN d.line_type = 'Item'              THEN d.line_no_field
            WHEN d.document_type = 'Invoice'       THEN d.line_no_field
            WHEN COALESCE(d.item_no, '') <> ''     THEN d.item_no
            ELSE ''
        END AS eff_item,
        -- Which item's MASTER DATA to read - a different question. A debit
        -- note (Invoice_Type 'Debit Note', e.g. 27DNMUM-00006) posts to a G/L
        -- account and prints that account number, yet takes its Description,
        -- HSN and GST Group from the item in Item_No. So Item_No decides the
        -- lookup regardless of document type, while the printed code does not.
        CASE WHEN d.line_type = 'Item' THEN d.line_no_field
             ELSE NULLIF(d.item_no, '') END AS lookup_item
    FROM doc_raw d
)

SELECT
    d.company_location                                    AS "Company Location",
    COALESCE(g.company_gstin, gd.company_gstin,
             d.location_gst_reg_no, lg.company_gstin, '')     AS "Company GSTIN",
    d.document_type                                       AS "Document Type",
    d.invoice_type                                        AS "Invoice Type",
    COALESCE(d.return_reason_code, '')                    AS "Return Reason Code",
    d.doc_no                                              AS "Document No",
    CASE WHEN d.cancelled THEN 'Yes' ELSE 'No' END        AS "Cancelled",
    ''::text                                              AS "Transfer Order No.",
    COALESCE(d.irn_no, '')                                AS "IRN No.",
    COALESCE(d.e_way_bill_no, '')                         AS "E-Way Bill No.",
    d.posting_date                                        AS "Posting Date",
    d.invoice_date                                        AS "Invoice Date",
    d.customer_no                                         AS "Customer Code/TransToCode",
    -- The CUSTOMER CARD name, not the posted document's. They differ on 47
    -- of April-2026's rows (a customer renamed since posting), and the card
    -- matches the report on all 69,194 while the document matches 69,147.
    COALESCE(cc."Name", d.customer_name)                  AS "Name",
    -- "Customer State" is the SHIP-TO state, not the customer card's state: it
    -- equalled GST Ship-to State Code in all 69,194 April-2026 rows, including
    -- the ones where the customer is billed in another state.
    -- When the header carries no ship-to state (154 rows Apr-Aug) the report
    -- falls back to the bill-to state.
    COALESCE(sh.label, bl.label, lo.label,
             d.ship_to_state, d.bill_to_state, '')        AS "Customer State",
    -- The SHIP-TO ADDRESS's own registration is authoritative and is what the
    -- report prints. It beats the GST ledger's per-line Buyer_Seller_Reg_No,
    -- which carries the bill-to GSTIN on thousands of rows.
    CASE
        -- The ledger's own value wins when it is already a registration in the
        -- ship-to state - it is the value as at posting, and the ship-to master
        -- is live and may have been re-registered since.
        WHEN LEFT(COALESCE(g.customer_gstin, ''), 2) = sh.gst_state_no
            THEN g.customer_gstin
        -- Otherwise the ship-to address record, the only place the ship-to
        -- state's registration exists (BC puts the bill-to GSTIN on the ledger).
        WHEN COALESCE(sta."GST_Registration_No", '') <> ''
            THEN sta."GST_Registration_No"
        -- A line with a ledger entry carrying no GSTIN, and no ship-to
        -- registration either, is a genuinely unregistered B2C sale.
        WHEN g.doc_no IS NOT NULL AND COALESCE(g.customer_gstin, '') = '' THEN ''
        ELSE COALESCE(cg.gstin, d.ship_to_gst_reg_no, g.customer_gstin,
                      gd.customer_gstin, '')
    END                                                   AS "GSTN",
    COALESCE(d.gst_customer_type, '')                     AS "GST Customer Type",
    COALESCE(cc."GST_Registration_Type", '')              AS "GST Registration Type",
    CASE WHEN cc."E_Commerce_Operator" THEN 'Yes' ELSE 'No' END
                                                          AS "E-Commerce Operator",
    COALESCE(d.ship_to_code, '')                          AS "Ship to Address Code",
    -- The DOCUMENT's stored name is what the report prints - it is frozen at
    -- posting and legitimately differs from the current ship-to master. The
    -- ONE exception is BC's OData layer replacing non-ASCII with '?'
    -- ("D-MART ? AGARWAL..." for an en-dash); the SOAP-sourced master has the
    -- real text, so it is used only to repair those. Preferring the master
    -- wholesale breaks 59,032 rows - every credit memo - because their line
    -- level ship-to code resolves to a differently-named current record.
    -- BC's OData layer transliterates non-ASCII to '?' ("D-MART ? AGARWAL",
    -- "KWPL MH4 ?  VALARPURAM"); the report has the original en-dash. Repair
    -- the CHARACTER rather than swapping in the ship-to master's name: the
    -- master is a live record that has since been edited (single vs double
    -- space above), and substituting it wholesale broke 59,032 rows. 35 of
    -- 107,712 invoice headers are affected.
    -- chr(8211) is EN DASH; written as chr() rather than a U&'' literal,
    -- whose backslash escape does not survive being written to file.
    REPLACE(COALESCE(d.ship_to_name, ''), '?', chr(8211))  AS "Ship to Name",
    COALESCE(bl.label, d.bill_to_state, '')               AS "GST Bill-to State Code",
    -- NO fallback here, unlike Customer State above: the report prints this
    -- blank exactly when the header carries no ship-to state (215 of 351,170
    -- rows Apr-Aug, and never otherwise).
    COALESCE(sh.label, d.ship_to_state, '')               AS "GST Ship-to State Code",
    COALESCE(d.nature_of_supply, '')                      AS "Nature of Supply",
    -- Deliberately NOT falling back to the document (gd.place_of_supply):
    -- measured against April-2026, "the line's ledger value, else Ship-to
    -- Address" is wrong on 4 rows while routing through the document is wrong
    -- on 14. Jurisdiction below is the opposite way round - there the document
    -- fallback helps (11 wrong vs 14) - so the two are not symmetric.
    COALESCE(g.place_of_supply,
             CASE WHEN COALESCE(d.ship_to_state, '') = ''
                  THEN 'Bill-to Address' ELSE 'Ship-to Address' END)
                                                          AS "GST Place Of Supply",
    -- The state the supply is taxed in follows the place of supply.
    CASE COALESCE(g.place_of_supply,
                  CASE WHEN COALESCE(d.ship_to_state, '') = ''
                       THEN 'Bill-to Address' ELSE 'Ship-to Address' END)
        WHEN 'Ship-to Address' THEN COALESCE(sh.label, d.ship_to_state)
        WHEN 'Bill-to Address' THEN COALESCE(bl.label, d.bill_to_state)
        ELSE COALESCE(lo.label, d.location_state)
    END                                                   AS "GST State",
    d.line_type                                           AS "Type",
    d.eff_item                                            AS "Item/Account",
    ''::text                                              AS "Varient",
    ''::text                                              AS "Colour",
    ''::text                                              AS "Size",
    ''::text                                              AS "Barcode",
    -- The report prints no description on G/L account lines, even though
    -- the line carries one ("BANK CHARGES" etc).
    -- Item master description, not the line's. The line keeps whatever text
    -- was current at posting ("DONUT CAKE"); the report prints the item's
    -- present name ("DONUT CAKE - 42g"). The master matches all 68,586 Item
    -- rows, the line 68,565. G/L account lines print nothing at all.
    CASE WHEN d.lookup_item IS NOT NULL
         THEN COALESCE(im."displayName", d.line_description, '')
         ELSE '' END                                      AS "Description",
    ''::text                                              AS "Category",
    ''::text                                              AS "Sub-Category",
    ''::text                                              AS "Season Code",
    COALESCE(d.line_location_code, '')                    AS "Location Code",
    COALESCE(d.unit_of_measure, '')                       AS "Unit of Measure",
    COALESCE(d.currency_code, '')                         AS "Currency Code",
    d.quantity                                            AS "Quantity",
    0::numeric                                            AS "RateFCY",
    ROUND(d.unit_price, 2)                                AS "RateLCY",
    0::numeric                                            AS "AmountFCY",
    -- MRP is DERIVED FROM THE LINE, not read from the item ledger. The
    -- report's GMV is what the discount percentage is taken on, so
    --     GMV = Line_Discount_Amount / (Line_Discount_Percent / 100)
    -- and MRP = GMV / Quantity. That matters because the item ledger's MRP is
    -- stamped when the goods move and goes stale the moment an item reprices:
    -- FG/0129 went 40 -> 45 on 2026-05-13, and invoice 27MUM-02603 (posted
    -- 12 May, ledger entry MRP 40) is printed by the report at 45 - which the
    -- line's own arithmetic confirms (discount 45 = 25% of 4 x 45).
    --
    -- Measured over all 347,269 item rows, April-August 2026:
    --     discount -> item ledger -> unit price   110 wrong   <- this
    --     discount -> item ledger                 144 wrong
    --     discount -> unit price                  239 wrong
    --     item ledger alone                     7,378 wrong
    -- The item ledger alone looked fine on April (49 wrong) and fell apart in
    -- May (3,953) - a reminder that one month is not a validation.
    --
    -- Zero-discount lines have no discount to invert, hence the fallbacks.
    -- G/L account lines print 0, as do SALVAGE and the PM/ (packaging) and
    -- RM/ (raw material) items, which carry no MRP in any source - decision
    -- 2026-09-01: keep those rows, leave MRP/GMV 0, do not chase it.
    CASE
        WHEN d.line_type <> 'Item' THEN 0
        WHEN COALESCE(d.discount_percent, 0) <> 0 AND COALESCE(d.quantity, 0) <> 0
            THEN ROUND((d.discount_amount / (d.discount_percent / 100)) / d.quantity)
        WHEN COALESCE(c.mrp, 0) <> 0 THEN c.mrp
        ELSE ROUND(COALESCE(d.unit_price, 0))
    END                                                   AS "MRP",
    -- ABS, not the signed quantity: the report prints GMV as a magnitude on
    -- credit memos too (qty -2 x MRP 163 prints 326, not -326), unlike every
    -- other amount column on those rows. Verified on the April-2026 export,
    -- where the credit-memo GMV total is +15,292,577.10 against a quantity
    -- total of -273,955.
    ROUND(ABS(d.quantity) * CASE
        WHEN d.line_type <> 'Item' THEN 0
        WHEN COALESCE(d.discount_percent, 0) <> 0 AND COALESCE(d.quantity, 0) <> 0
            THEN ROUND((d.discount_amount / (d.discount_percent / 100)) / d.quantity)
        WHEN COALESCE(c.mrp, 0) <> 0 THEN c.mrp
        ELSE ROUND(COALESCE(d.unit_price, 0))
    END, 2)                                               AS "GMV",
    -- NOT ROUND(quantity * unit_price, 2): that rounds once at the end, while
    -- BC rounds the line first and then adds the discount back. The two differ
    -- by a paisa on 2,657 of April-2026's 57,227 invoice lines (7.73 in total).
    -- This form reproduces the export exactly.
    d.line_amount + d.discount_amount                     AS "AmountLCY",
    d.discount_percent                                    AS "Discount %",
    d.discount_amount                                     AS "Discount",
    -- No ledger entry means the line was not taxed: GGST-0% on an item line,
    -- blank on a G/L account line. True for all 8,246 such April-2026 rows.
    -- Item Card (page 30) is authoritative for both of these and matches the
    -- report on all 8,317 April rows that have no GST ledger entry. It also
    -- covers the 3 items that appear nowhere in the ledger, which an
    -- inferred-from-the-ledger HSN never could.
    COALESCE(g.gst_group_code,
             CASE WHEN d.lookup_item IS NOT NULL THEN ic."GST_Group_Code" END, '')
                                                          AS "GST Group Code",
    COALESCE(g.hsn_sac,
             CASE WHEN d.lookup_item IS NOT NULL THEN ic."HSN_SAC_Code" END, '')
                                                          AS "GST HSN/SAC",
    COALESCE(g.gst_percent, 0)                            AS "GST %",
    COALESCE(g.gst_group_type, 'Goods')                   AS "GST Group Type",
    COALESCE(g.jurisdiction_type, gd.jurisdiction_type,
             CASE WHEN COALESCE(d.ship_to_state, d.location_state) = d.location_state
                  THEN 'Intrastate' ELSE 'Interstate' END) AS "GST Jurisdiction Type",
    -- ALWAYS the line's own amount, never the ledger's GST_Base_Amount. The
    -- two agree on 69,070 of April-2026's 69,079 rows and diverge by a few
    -- paise on the rest (27AHD-00186 line 10000: ledger -76,114.29, line
    -- 76,114.26, report 76,114.26) - the line wins every time, on all 69,009
    -- rows where both exist. Amount To Customer follows from it.
    d.line_amount                                         AS "GST Base Amount",
    COALESCE(g.igst, 0)                                   AS "IGST",
    COALESCE(g.cgst, 0)                                   AS "CGST",
    COALESCE(g.sgst, 0)                                   AS "SGST",
    0::numeric                                            AS "TCS Amount",
    ROUND(d.line_amount + COALESCE(g.igst, 0) + COALESCE(g.cgst, 0)
          + COALESCE(g.sgst, 0), 2)                       AS "Amount To Customer",
    -- Printed positive on invoices AND on credit memos, so the sign comes from
    -- the document type, not from the ledger.
    COALESCE(CASE WHEN d.document_type = 'Invoice'
                  THEN -c.cost_amount ELSE c.cost_amount END, 0)
                                                          AS "COGS",
    CASE WHEN d.gst_wo_payment_of_duty THEN 'Yes' ELSE 'No' END
                                                          AS "GST Without Payment of Duty",
    COALESCE(d.bill_of_export_no, '')                     AS "Bill Of Export No",
    -- BC writes 0001-01-01 for "no date"; the report prints blank.
    NULLIF(d.bill_of_export_date, DATE '0001-01-01')      AS "Bill Of Export Date",
    COALESCE(d.e_commerce_customer, '')                   AS "E-Commerce Customer",
    COALESCE(d.e_comm_merchant_id, '')                    AS "E-Commerce Merchant Id",
    0::numeric                                            AS "MRP Price",
    COALESCE(d.state_dim, '')                             AS "STATE CODE",
    COALESCE(d.bu_dim, '')                                AS "BU CODE",
    COALESCE(d.prn_document_no, '')                       AS "PRN Document No.",
    COALESCE(d.external_document_no, '')                  AS "External Document No."
FROM doc d
LEFT JOIN gst g
       ON g.doc_no = d.doc_no
      AND g.line_no = d.line_no
      AND g.doc_type = d.document_type
LEFT JOIN gst_doc  gd ON gd.doc_no    = d.doc_no
LEFT JOIN public.bc_item_card     ic ON ic."No"       = d.lookup_item
LEFT JOIN loc_gstin lg ON lg.location_code = d.company_location
LEFT JOIN cost c
       ON c.doc_no = d.doc_no
      AND c.line_no = d.line_no
LEFT JOIN public.bc_ship_to_address sta
       ON sta."Customer_No" = d.customer_no
      AND sta."Code"        = d.ship_to_code
LEFT JOIN public.bc_customer_card cc ON cc."No"       = d.customer_no
LEFT JOIN public.bc_api_items     im ON im."number"   = d.lookup_item
LEFT JOIN public.ref_gst_state    sh ON sh.state_code = d.ship_to_state
LEFT JOIN cust_gstin cg ON cg.customer_no = d.customer_no
                       AND cg.state_no    = sh.gst_state_no
LEFT JOIN public.ref_gst_state    bl ON bl.state_code = d.bill_to_state
LEFT JOIN public.ref_gst_state    lo ON lo.state_code = d.location_state;

-- Hand back what the DROP took from this view: its comment and any grant made
-- outside this file. Runs BEFORE the REVOKE below so that revoke still wins,
-- even if anon or authenticated had been granted something explicitly.
DO $srgd_self$
DECLARE
    rec  record;
    stmt text;
BEGIN
    FOR rec IN SELECT * FROM _srgd_self LOOP
        IF rec.comment IS NOT NULL THEN
            EXECUTE format('COMMENT ON VIEW public.v_sales_register_gst_detail IS %L',
                           rec.comment);
        END IF;
        FOREACH stmt IN ARRAY COALESCE(rec.grants, ARRAY[]::text[]) LOOP
            EXECUTE stmt;
        END LOOP;
    END LOOP;
END
$srgd_self$;

-- DROP + CREATE above discards the view's grants, and Supabase's default
-- privileges hand anon and authenticated everything on a fresh object. Take
-- that back on every apply: nothing reaches this view over PostgREST.
REVOKE ALL ON public.v_sales_register_gst_detail FROM anon, authenticated;

-- Put back everything CASCADE took above: definition, storage options, owner,
-- comment and grants, shallowest first so each one finds its source. Anything
-- that fails here aborts the file's transaction, leaving both this view and
-- its dependents exactly as they were.
DO $srgd_restore$
DECLARE
    r       record;
    fresh   record;
    stmt    text;
    n       int := 0;
BEGIN
    FOR r IN SELECT * FROM _srgd_dependents ORDER BY depth, ident LOOP
        EXECUTE format('CREATE VIEW %s %s AS %s', r.ident,
                       CASE WHEN COALESCE(r.reloptions, '') = '' THEN ''
                            ELSE 'WITH (' || r.reloptions || ')' END,
                       r.definition);
        EXECUTE format('ALTER VIEW %s OWNER TO %I', r.ident, r.owner);
        IF r.comment IS NOT NULL THEN
            EXECUTE format('COMMENT ON VIEW %s IS %L', r.ident, r.comment);
        END IF;
        -- Restore the ACL exactly, which means revoking first: Supabase's
        -- default privileges hand anon and authenticated everything on a view
        -- created in public, and a dependent that was deliberately kept off
        -- PostgREST must not come back readable by them.
        FOR fresh IN
            SELECT DISTINCT CASE WHEN a.grantee = 0 THEN 'PUBLIC'
                                 ELSE quote_ident(pg_get_userbyid(a.grantee)) END AS grantee
            FROM pg_class c, aclexplode(c.relacl) a
            WHERE c.oid = r.ident::regclass AND a.grantee <> c.relowner
        LOOP
            EXECUTE format('REVOKE ALL ON %s FROM %s', r.ident, fresh.grantee);
        END LOOP;
        FOREACH stmt IN ARRAY COALESCE(r.grants, ARRAY[]::text[]) LOOP
            EXECUTE stmt;
        END LOOP;
        n := n + 1;
    END LOOP;
    IF n > 0 THEN
        RAISE NOTICE 'rebuilt % dependent view(s) of v_sales_register_gst_detail', n;
    END IF;
END
$srgd_restore$;

-- ---------------------------------------------------------------------------
-- Birbal's read path. public.v_sales_register_gst_detail is SECURITY INVOKER,
-- so the querying role - not the owner - is privilege-checked against every
-- table the view touches. Birbal reaches it through birbal_register_reader, a
-- NOLOGIN role that app.sync_register_reader_grants() (in the Birbal repo,
-- migration 010) grants SELECT on exactly this view's current dependencies,
-- with a matching RLS policy on each. Add a source table to the view here and
-- Birbal starts answering "permission denied for table ..." until that
-- function is re-run.
--
-- So re-run it - but only when the dependency set has actually drifted. The
-- function revokes and re-grants across all of public, which takes an
-- exclusive lock on every table in it; doing that on each 2-hourly apply would
-- stall the other pipelines and Birbal itself for no reason.
DO $srgd_reader$
DECLARE
    stale text;
BEGIN
    IF to_regprocedure('app.sync_register_reader_grants()') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'birbal_register_reader')
    THEN
        RETURN;  -- Birbal is not installed on this database.
    END IF;

    SELECT string_agg(DISTINCT dep::regclass::text, ', ') INTO stale
    FROM (
        SELECT d.refobjid AS dep
        FROM pg_rewrite r
        JOIN pg_depend d ON d.objid = r.oid
                        AND d.refclassid = 'pg_class'::regclass
        WHERE r.ev_class = 'public.v_sales_register_gst_detail'::regclass
          AND d.refobjid <> 'public.v_sales_register_gst_detail'::regclass
    ) deps
    WHERE NOT has_table_privilege('birbal_register_reader', dep, 'SELECT')
       -- A grant is not enough: these tables run RLS, so each one also needs
       -- the reader's own SELECT policy or the view returns zero rows.
       OR EXISTS (SELECT 1 FROM pg_class c
                   WHERE c.oid = dep AND c.relkind IN ('r','p') AND c.relrowsecurity
                     AND NOT EXISTS (SELECT 1 FROM pg_policy p
                                      WHERE p.polrelid = dep
                                        AND p.polname = 'birbal_register_read'));

    IF stale IS NOT NULL THEN
        RAISE NOTICE 'register view dependencies not readable by birbal_register_reader (%), re-running app.sync_register_reader_grants()', stale;
        PERFORM app.sync_register_reader_grants();
    END IF;
END
$srgd_reader$;

-- Birbal does not read this view directly: warehouse.v_sales_register_gst_detail
-- wraps it as "select r.*, <credit-memo date>, <normalized reason>", and a view
-- pins its column list at CREATE time. Adding a column here therefore does NOT
-- reach Birbal - the wrapper keeps serving the old list, with no error anywhere
-- - until somebody re-runs migrations 009 + 011 in birbal-mission-control and
-- updates the warehouse_meta dictionary row. Say so in the apply log rather
-- than let it pass silently; this is a warning, not a failure, because a
-- consumer being behind must not stop the ETL.
DO $srgd_drift$
DECLARE
    missing text;
BEGIN
    IF to_regclass('warehouse.v_sales_register_gst_detail') IS NULL THEN
        RETURN;
    END IF;
    SELECT string_agg(quote_ident(a.attname), ', ' ORDER BY a.attnum) INTO missing
    FROM pg_attribute a
    WHERE a.attrelid = 'public.v_sales_register_gst_detail'::regclass
      AND a.attnum > 0 AND NOT a.attisdropped
      AND NOT EXISTS (
          SELECT 1 FROM pg_attribute w
          WHERE w.attrelid = 'warehouse.v_sales_register_gst_detail'::regclass
            AND w.attnum > 0 AND NOT w.attisdropped
            AND w.attname = a.attname);
    IF missing IS NOT NULL THEN
        RAISE WARNING 'warehouse.v_sales_register_gst_detail is missing column(s) % - Birbal cannot see them until birbal-mission-control migrations 009 + 011 are re-run', missing;
    END IF;
END
$srgd_drift$;

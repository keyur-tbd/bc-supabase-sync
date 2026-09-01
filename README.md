# Business Central → Supabase Sync

Production-ready Python automation that pulls data from Microsoft
Dynamics 365 Business Central's OData v4 web services and syncs it into
Supabase (Postgres), built from the web services list exported from your BC tenant
(`Web_Services_To_Be_Imported.xlsx`, later extended from `Reports to add to supabase_sync.xlsx`).

Currently configured for these services (regenerate
`config/web_services.json` any time you publish more web services in
BC):

| BC service (`name`) | Supabase table | Strategy | history_field | incremental_field |
|---|---|---|---|---|
| `General_Ledger_Entries_Excel` | `bc_general_ledger_entries` | date | `Posting_Date` | `SystemCreatedAt` (datetime) |
| `Item_Ledger_Entries_Excel` | `bc_item_ledger_entries` | date | `Posting_Date` | `Posting_Date` |
| `Posted_Sales_Credit_Memo_Excel` | `bc_posted_sales_credit_memo` | date | `Posting_Date` | `Posting_Date` |
| `Sales_Return_Order_Excel` | `bc_sales_return_order` | date | `Document_Date` | `Document_Date` |
| `Sales_Return_Order_ExcelSalesLines` | `bc_sales_return_order_lines` | date | `Shipment_Date` | `Shipment_Date` (+ undated rows) |
| `Posted_Sales_Invoice_Excel` | `bc_posted_sales_invoice_excel` | date | `Posting_Date` | `Posting_Date` |
| `Posted_Sales_Credit_Memo_Lines_Excel` | `bc_posted_sales_credit_memo_lines` | series_key | — | `Document_No` above stored max per series |
| `Customer_Item_Reference_Excel` (custom page 50003) | `bc_customer_item_reference` | full_refresh | — | — |
| `Ship_to_Address_Excel` (**disabled** – 404, web service not published) | `bc_ship_to_address` | full_refresh | — | — |
| `Requests_to_Approve_Excel` | `bc_request_to_approve` | full_refresh | — | — |
| `Chart_of_Account` (page 16; the BC service name is singular) | `bc_chart_of_accounts` | full_refresh | — | — |
| `Customer_Ledger_Entries_Excel` | `bc_customer_ledger_entries` | date | `Posting_Date` | `Posting_Date` + extra pass on `Closed_at_Date` |
| `G_L_Account_Card_Excel` | `bc_gl_account_card` | full_refresh | — | — |
| `customers` (**API v2.0**) | `bc_api_customers` | full_refresh | — | — |
| `items` (**API v2.0**) | `bc_api_items` | full_refresh | — | — |
| `vendors` (**API v2.0**) | `bc_api_vendors` | full_refresh | — | — |
| `Vendor_Ledger_Entries_Excel` | `bc_vendor_ledger_entries` | date | `Posting_Date` | `Posting_Date` + extra pass on `Closed_at_Date` |
| `Sales_Price_Lists_Excels` (Sales Price List card, page 7000) | `bc_sales_price_lists` | full_refresh | — | — |
| `Sales_Price_Lists_ExcelsLines` (its lines subpage — the customer price list) | `bc_sales_price_list_lines` | full_refresh | — | — |
| `Posted_Sales_Invoice_ExcelSalesInvLines` (FY 2026-27 series only) | `bc_posted_sales_invoice_lines` | series_key | — | `Document_No` |
| `Posted_Sales_Credit_Memo_ExcelSalesCrMemoLines` (FY 2026-27 series only) | `bc_posted_sales_cr_memo_lines` | series_key | — | `Document_No` |
| `Detailed_GST_Ledger_Entry_Excel` | `bc_detailed_gst_ledger_entries` | date | `Posting_Date` | `Posting_Date` |
| `Customer_Card_Excel` | `bc_customer_card` | full_refresh | — | — |
| `Value_Entries_Excel` (sales entries only, `static_filter`) | `bc_value_entries` | date | `Posting_Date` | `Posting_Date` |
| `Item_Card_Excel` | `bc_item_card` | full_refresh | — | — |

> Sales Return Order is an open (un-posted) document, so it has no posting
> date — it is chunked/incremented on `Document_Date` instead. GL uses
> `SystemCreatedAt` for incremental so back-dated new entries aren't missed;
> the other tables have no row timestamp and use `Posting_Date` + a lookback.

## How it works

-   **Auth**: OAuth2 client-credentials flow against Azure AD
    (`login.microsoftonline.com/{tenant}/oauth2/v2.0/token`), scope
    `https://api.businesscentral.dynamics.com/.default`. Tokens live in
    memory for the process only — nothing is written back to `.env` or
    disk, the same pattern used in your other automation scripts.
-   **Pagination**: follows `@odata.nextLink` until exhausted. Page size
    is bounded with the `Prefer: odata.maxpagesize` header rather than
    `$top` — `$top` makes BC cap the result and stop sending a nextLink,
    which silently truncated large pulls to one page. The size is
    `BC_MAXPAGESIZE` (5000), overridable per service — see *Page size*.
-   **Two-phase sync**:
    -   *Backfill* (on `--mode full`, or automatically on first run):
        the `[sync_start_date, today]` range is sliced into windows
        (monthly by default) along a populated business date
        (`history_field`, e.g. `Posting_Date`) and each window is paged to
        exhaustion. `history_through` advances per completed window, so an
        interrupted backfill resumes at the next window.
    -   *Incremental* (once a watermark exists): pulls records at/after
        `(watermark - lookback_days)` on `incremental_field`. Tables that
        expose a creation timestamp (e.g. GL's `SystemCreatedAt`) catch
        back-dated new entries; date-only tables use the lookback buffer.
    -   These flattened `_Excel` pages do **not** expose
        `SystemModifiedAt` / `SystemId`, so the watermark keys off the
        fields configured per service in `web_services.json`, not those.
    -   The watermark is always set from clamped wall-clock "now", never a
        value read out of the data, so a corrupt future-dated row can't
        freeze the sync.
-   **Resumability**: after every page is successfully upserted, the
    next page's URL is saved to the `etl_sync_state` table in Supabase. If the
    process crashes or a GitHub Actions job times out mid-pull, the next
    run resumes from that exact page instead of starting over or
    skipping records.
-   **No `_raw_json` copy**: earlier versions stored the complete source
    record as JSONB next to the typed columns. It duplicated every field,
    ran ~1 KB per row and was ~90% of a wide table's size; on 2026-08-28 it
    was dropped from every `bc_*` table and disabled everywhere with
    `SUPABASE_RAW_JSON_EXCLUDE_TABLES=*` (the workflow default). Leave it
    that way — see *Storage and the disk guard* below.
-   **Disk guard**: before a run and again before every service the sync
    compares `pg_database_size()` with `SUPABASE_DISK_LIMIT_GB` and refuses
    to write once usage passes `SUPABASE_DISK_STOP_PCT` (default 85%),
    exiting non-zero with a clear message. A long backfill therefore stops
    itself long before the volume fills. `0` disables the guard.
-   **Schema**: tables are created automatically from the shape of the
    data. New fields are added as new columns; existing `VARCHAR` columns
    are widened to `TEXT`, and `BIGINT` columns are widened to `NUMERIC`
    the first time a fractional value shows up (BC decimals such as
    `Quantity` arrive as JSON integers whenever the sampled page happens to
    hold whole numbers — without the widening Postgres would silently round
    every later `12.5` to `13`). The check runs per page *before* the
    upsert, so it is lossless. Columns are never dropped or narrowed. The
    primary key is the configured business key (e.g. `No` / `Entry_No`),
    and every table gets a `_synced_at` timestamp.
-   **Validation**: rows with a null/empty primary key are written to
    `quarantine/` and skipped (they can't be upserted); rows whose date
    field is implausibly in the future are copied to `quarantine/` for
    review but still stored, so nothing is lost.
    Where keyless rows are *expected* rather than surprising — e.g. credit
    memo lines carrying no product — set `"quarantine_missing_pk": false`
    on that service. They are then dropped quietly: not written to
    `quarantine/`, not counted as failures (so the run ends `success`
    rather than `partial_failure`), but still reported as `skipped=N` in
    the log line and the run summary JSON.
    Where an *empty string* is a legitimate key value — e.g.
    `Customer_Item_Reference_Excel`, whose blank `Ship_To_Code` means "the
    whole customer" — list those columns in `"pk_allow_empty"` so the rows
    are kept instead of quarantined.
-   **PK guard**: at the start of each service the table's actual primary
    key is compared to the configured one; a mismatch is logged loudly
    (editing config alone never migrates an existing table's key).
-   **Upserts**: one multi-row `INSERT ... VALUES (...), (...) ON CONFLICT
    (pk) DO UPDATE` per batch of 500 (sliced further to stay under
    Postgres's 65 535-parameter limit), so reruns and overlapping
    incremental windows never create duplicates. It replaced a per-row
    `executemany`, which crawled at ~50 rows/s through Supabase's
    transaction pooler; wide line tables now load at ~125 rows/s.
-   **Failures**: a failing batch doesn't kill the whole run — it's
    written to `failed_records/<table>_<timestamp>.json` for
    inspection/replay, and the run is marked `partial_failure` rather
    than `failed`.

## Project structure

```
bc_supabase_sync/
├── app.py                          # CLI entry point
├── config.py                       # env var loading + validation
├── diagnose_connection.py          # works out which SSL settings connect
├── test_supabase_port.py           # integration suite (needs TEST_PG_DSN)
├── requirements.txt
├── .env.example
├── config/
│   └── web_services.json           # generated from the BC web services Excel export
├── scripts/
│   ├── generate_config_from_excel.py
│   └── reclaim_vacuum_full.py      # guarded VACUUM FULL, smallest table first (see Storage)
├── services/
│   ├── auth_service.py             # OAuth2 client-credentials auth
│   ├── bc_api_service.py           # paginated OData fetch + retries
│   ├── supabase_service.py         # pooled Postgres connection, dynamic DDL, upserts, sync state
│   └── sync_service.py             # orchestration
├── models/
│   └── sync_state.py
├── utils/
│   ├── logger.py
│   ├── retry_helper.py
│   ├── schema_helper.py
│   └── date_chunker.py
├── logs/                           # rotating daily log files + JSON run summaries (gitignored)
├── failed_records/                 # JSON dumps of any batch that failed to upsert (gitignored)
├── quarantine/                     # rows rejected or flagged by validation (gitignored)
└── .github/workflows/
    ├── bc_sync.yml                 # scheduled sync (needs repo secrets)
    └── ci.yml                      # secrets-free compile/import/config checks
```

## Setup

### 1. Azure AD App Registration

In Entra ID, create an App Registration, grant it **Application**
permissions for `Dynamics 365 Business Central` (`API.ReadWrite.All` or
a narrower permission if you've scoped it), get admin consent, and
create a client secret. In BC itself (**Microsoft Entra Applications**
page) the app needs permission sets covering the pages you're syncing.
`D365 BUS FULL ACCESS` covers Microsoft's base tables only; a page that
touches a table from a partner extension fails with `403 ... TableData
<id> ... Read: <Extension Name>` until that extension's own permission set
is added (here `ISPL ADVANCE APPROVA` for the price list pages and
`BAKERS DEVELOPMENT` for Customer Item Reference). BC refuses to assign
`SUPER` to an application, and a user whose own SUPER is scoped to one
company can only add company-scoped rows — set the Company column to the
same company rather than leaving it blank.

### 2. Install dependencies

``` bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure environment

``` bash
cp .env.example .env
# fill in BC_CLIENT_ID, BC_CLIENT_SECRET, BC_TENANT_ID, BC_ENVIRONMENT,
# BC_COMPANY_ID, and SUPABASE_DB_URL
```

Storage-related settings (all optional, shown with the values in use):

| Variable | Value | Meaning |
|---|---|---|
| `SUPABASE_RAW_JSON_EXCLUDE_TABLES` | `*` | never create/populate `_raw_json` (`*` = every table; or a comma list) |
| `SUPABASE_DISK_LIMIT_GB` | `18` | the project's disk size, as shown under *Settings → Compute and Disk* |
| `SUPABASE_DISK_STOP_PCT` | `85` | refuse to write above this percentage of the limit |
| `BC_MAXPAGESIZE` | `5000` | default rows per BC page; per-service `max_page_size` overrides it (see *Page size*) |

Update `SUPABASE_DISK_LIMIT_GB` (here, in the workflow default and in the
repo variable) whenever the Supabase disk is resized.

### 4. (Re)generate the service config from your BC Excel export

The included `config/web_services.json` was generated from
`Web_Services_To_Be_Imported.xlsx`. To regenerate it (e.g. after adding
new web services in BC):

``` bash
python scripts/generate_config_from_excel.py path/to/Web_Services_Export.xlsx
```

This only reads `Object Type`, `Object ID`, `Object Name`,
`Service Name`, and `Published` from the sheet — it ignores the
`OData V4 URL` / `SOAP URL` columns on purpose, since those bake in your
tenant ID and company name. URLs are instead built at runtime from your
`.env` values, so the same config file works across sandbox/production
environments without edits.

If a particular page's primary key isn't reliably `SystemId` (it almost
always is) or you want a tighter incremental filter, edit the relevant
entry in `config/web_services.json` directly — your edits aren't
overwritten unless you rerun the generator.

### 5. First run — create tables only

``` bash
python app.py --setup
```

### 6. Run a sync

``` bash
python app.py                    # incremental (auto-backfills first time)
python app.py --mode full        # (re)run the historical backfill
python app.py --service Item_Ledger_Entries_Excel --mode full   # one service
```

The backfill is resumable: if it stops partway, the next run continues from
the last completed window. To force a clean re-pull from `sync_start_date`,
clear that service's state first:

``` sql
DELETE FROM etl_sync_state WHERE service_name = 'Item_Ledger_Entries_Excel';
```

Exit codes: `0` success, `2` at least one service failed outright, `3`
at least one service had a partial batch failure (check
`failed_records/`).

### 7. Schedule it

`.github/workflows/bc_sync.yml` runs the sync every 2 hours via GitHub
Actions cron, using repo secrets for every credential (no secret ever
touches the repo). Add these as **Settings → Secrets and variables →
Actions**: `BC_CLIENT_ID`, `BC_CLIENT_SECRET`, `BC_TENANT_ID`,
`BC_ENVIRONMENT`, `BC_COMPANY_ID`, `SUPABASE_DB_URL`. The repo
**variables** `SUPABASE_RAW_JSON_EXCLUDE_TABLES`, `SUPABASE_DISK_LIMIT_GB`
and `SUPABASE_DISK_STOP_PCT` override the workflow defaults (`*`, `18`,
`85`). Trigger an ad hoc full sync from the Actions tab via "Run workflow"
→ mode `full`.

The cron reads `config/web_services.json` **from the repo**, so a service
added or changed locally only reaches the schedule once it is pushed.

If you'd rather run it as a long-lived process instead of CI cron, wrap
`SyncService.run()` in your own loop (e.g. with `schedule` or
`APScheduler`) — `app.py` deliberately stays single-shot so it behaves
identically whether invoked by cron, GitHub Actions, or by hand.

## Running the tests

`test_supabase_port.py` is an integration suite covering every DB code path
(type inference, schema drift, composite keys, quarantine, batching, sync
state). It needs a real Postgres, supplied via `TEST_PG_DSN`. Everything it
creates lives in two throwaway schemas (`bc_port_test`, `bc_port_test_alt`)
which are dropped at the end, so pointing it at a live Supabase project does
not touch `public` or any `bc_*` table.

``` powershell
$env:TEST_PG_DSN = "postgresql://postgres.<ref>:<password>@<host>:5432/postgres?sslmode=verify-full&sslrootcert=<path-to>/prod-ca-2021.crt"
python test_supabase_port.py
```

On Linux/macOS you can instead `pip install pgserver` and run it with no
`TEST_PG_DSN` — it spins up a disposable local Postgres. There is no
Windows build of `pgserver`, so Windows needs `TEST_PG_DSN`.

CI (`.github/workflows/ci.yml`) deliberately does **not** run this suite —
it has no database and no secrets. It compiles and imports every module on
Python 3.12/3.13, validates `config/web_services.json`, and fails if a
credential ever lands in a tracked file.

## Monitoring

-   `logs/sync.log` — daily-rotating structured log (30 days retained).
-   `logs/run_summary_<timestamp>.json` — machine-readable per-service
    stats for every run (fetched/inserted/updated/failed/duration),
    handy as a CI artifact or for feeding a dashboard.
-   `etl_sync_state` table in Supabase — current status, last watermark,
    and any error message per service; check this first if a service
    looks stale.
-   `failed_records/` — raw JSON of any batch that didn't make it into
    Supabase.

## Notes on the data model

Each BC entity gets one Supabase (Postgres) table. Simple nested objects are flattened
(`Address.City` → `address_city`); arrays and deeply nested structures
are kept as JSON in their own column rather than guessing a relational
shape for them, since BC sub-collections vary a lot in structure across
pages. There is no raw copy of the source record any more (see *Storage
and the disk guard*); the typed columns are the data, which is why the
schema code widens types rather than ever narrowing them.

Two BC-side facts worth knowing when reading the tables: rows with
identical content but different `Entry_No` / `Line_No` are valid BC
output, not duplicates; and BC writes an unset date as `0001-01-01`, not
`NULL`.


## Tables keyed by a BC no.-series (`series_key`)

Some pages expose no date field at all, but their key is a BC number series
(`26CNMUM-00001`, `26CNMUM-00002`, ...). For posted — therefore immutable —
documents, "everything above the highest number already stored" is a complete
and correct incremental window, so those tables do not need `full_refresh`:

``` json
"sync_strategy": "series_key",
"series_key_field": "Document_No",
"series_separator": "-",
"series_source": { "table": "bc_posted_sales_credit_memo", "column": "No" }
```

The watermark is re-derived from the stored data on every run — nothing is
persisted — so an interrupted run resumes with no bookkeeping. That relies on
pages arriving in ascending key order, which is why `$orderby` is sent.

Two BC limits shape the design, both verified against the live API:

-   **One request per series.** BC answers `501
    BadRequest_MethodNotImplemented` to an OR of AND-groups, so the whole
    sweep cannot be a single filter. Each series gets
    `startswith(Field,'PREFIX') and Field gt '<max>'`, which BC accepts.
-   **New series cannot be discovered from the API.** BC also rejects
    `not (...)` — *"Client requests that contain 'Not' filter options are not
    supported"* — so there is no way to ask for "anything outside the series
    I know". `series_source` names the parent document table (which syncs by
    date) and any series present there but absent locally is pulled in full.

When `series_source` is configured, the very first pull (and `--mode
full`) takes its series list from that parent table — which is itself
limited by its `sync_start_date` — instead of pulling the page unfiltered,
so all-time history older than the parent's window is skipped. Only a
service with no `series_source` falls back to an unfiltered pull.

## Page size (`max_page_size`)

`BC_MAXPAGESIZE` sets the default `Prefer: odata.maxpagesize`; any service
can override it with `"max_page_size"` in `config/web_services.json`.

**BC's ceiling is 20000.** Asking for 30000, 50000 or 100000 all returned
exactly 20000 rows with a valid `nextLink` (measured against the live
tenant, 2026-08-29), so anything higher is silently clamped.

What actually matters is **bytes per row, not row count** — the same page
size costs wildly different amounts per entity:

| Entity | B/row | 10000-row page |
|---|---|---|
| `Customer_Item_Reference_Excel` | 275 | 2.8 MB |
| `Posted_Sales_Credit_Memo_Lines_Excel` | 693 | 6.9 MB |
| `customers` (v2.0) | 884 | 8.8 MB |
| `Item_Ledger_Entries_Excel` | 1 879 | 18.8 MB |
| `Posted_Sales_Invoice_Excel` | 3 863 | 38.6 MB |

Throughput on a wide entity went 178 rows/s at 2000 → 276 at 5000 → ~290
at 10000–20000: **nearly all of the gain lands by 5000**, because what the
bigger page removes is per-request overhead, not per-row cost. Past that
the page keeps getting more expensive to lose:

-   a page that hits `BC_REQUEST_TIMEOUT_SECONDS` (300) is **re-fetched
    whole**, up to 5 times with backoff — cheap for a 5000-row page, very
    expensive for a 20000-row one;
-   the resume point is saved per page, so a crash loses up to one page of
    progress;
-   the whole page is held in memory as parsed Python objects (roughly
    5–10× the JSON size) before the batched upsert.

Hence: **5000 by default, 10000 for narrow tables** (currently Customer
Item Reference, credit-memo lines, the two price-list services, and the
API v2.0 `customers` / `items` / `vendors`). Wide document tables stay at
5000. Raising a *wide* table to 20000 is the one combination to avoid.

Note also that the fetch is only about half the wall clock — the Supabase
upsert is the other half — so end-to-end gains are smaller than the
fetch-rate numbers above.

## Standard API v2.0 entities (`"api": "v2.0"`)

Besides the legacy ODataV4 web services (pages you publish yourself in
BC > Web Services), a service can point at the **standard Business Central
API v2.0** — the same `api/v2.0/companies(<guid>)/<entitySet>` endpoints
the Power BI Business Central connector reads. Set `"api": "v2.0"` on the
service and use the camelCase entity-set name (`customers`, `items`,
`salesInvoices`, `generalLedgerEntries`, ...). Conventions that differ from
the legacy pages:

- fields are camelCase and the key is the `id` GUID → `"primary_key": "id"`
- every entity exposes `lastModifiedDateTime` → use it for both
  `history_field` and `incremental_field` with `incremental_is_datetime: true`
  (a 1–2 day lookback is enough; no need to guess at business dates)
- the company is addressed by GUID; `BC_COMPANY_ID` may stay the display
  name — it is resolved through `/companies` once per run

See `standard_api_v2_entity` in `_new_table_templates` for a copy-paste
entry.

## Mutable ledgers (`incremental_extra_fields`)

Some ledgers change after posting — a customer ledger entry gets
`Closed_at_Date` / `Open` / `Remaining_Amount` updated when a payment is
applied months later, well outside any `Posting_Date` lookback. BC rejects
`A ge x or B ge x` across two different fields (501), so list the extra
date fields under `incremental_extra_fields`; each gets its own pass with
the same floor and the rows are upserted on top. `Customer_Ledger_Entries_Excel`
and `Vendor_Ledger_Entries_Excel` use this with `Closed_at_Date`.

## Tables without a usable timestamp

Some pages (often lines/detail tables) expose no date or modified field to
filter on. Set `"sync_strategy": "full_refresh"` for those: the whole table
is re-pulled every run and upserted (correct for both append-only and
mutable tables; cost scales with size, so best for small/medium tables).
Tables that *do* have a date keep the default `"sync_strategy": "date"`.

`full_refresh` only ever **upserts** — it never deletes. A row for a document
that has since been posted or deleted in BC stays in Supabase either way, so
switching such a table to incremental costs nothing in that respect. What it
does cost is edits: full_refresh re-reads every row every run, so any change
anywhere is caught, whereas incremental only revisits rows inside
`lookback_days`. `Sales_Return_Order_ExcelSalesLines` was moved to
date-incremental on `Shipment_Date` for exactly this trade — re-upserting all
305k lines every 2 hours churned ~3.7M dead tuples a day and grew the table
from 206 MB to 456 MB.

BC writes an unset date as `0001-01-01`, not null, so those rows sit below
every `ge <floor>` window and an incremental sync would never fetch them. Set
`"incremental_include_undated": true` to widen the filter to
`(field ge <floor> or field eq 0001-01-01)`.

Lines/detail tables also need a **composite primary key**, e.g.
`"primary_key": ["Document_No", "Line_No"]` — the document number alone
repeats across line rows, so a single-column key would overwrite lines.
See `_new_table_templates` in `config/web_services.json` for copy-paste
examples of both cases.

## Storage and the disk guard

**What happened on 2026-08-28.** A bulk `ALTER TABLE ... ALTER COLUMN ...
TYPE numeric` across every `bc_*` table (including the 6.2M-row GL and
2M-row item-ledger tables) rewrote those tables — a rewrite needs a full
temporary copy — and filled the Supabase disk mid-way. Postgres flipped
read-only, then crash-looped in recovery with *"could not extend file ...
No space left on device"* until Supabase auto-grew the volume (12 → 18 GB).
Nothing was lost: the ALTER rolled back and all committed data survived.

Rules that came out of it:

-   **Check headroom before any bulk load or rewrite.** `pg_database_size`
    vs the disk size in *Settings → Compute and Disk*; keep well under 85%.
    Note that Supabase's "100 GB"-style allowances are Storage/egress
    quotas — the Postgres disk is separate (8 GB on Pro, auto-grown ~50% at
    90% usage, but only once per 6 hours, and manual resizes are blocked
    during that cooldown).
-   **Never run DDL that rewrites a large table** (`ALTER TYPE`, `VACUUM
    FULL`, `CLUSTER`) without room for a full copy of that table, and do it
    one table at a time, smallest first. Prefer forward fixes in the sync
    (the BIGINT → NUMERIC widening above) over rewriting history.
-   The disk guard (`SUPABASE_DISK_LIMIT_GB` / `SUPABASE_DISK_STOP_PCT`) is
    the backstop; keep it configured and update the limit when the disk
    changes.

**Reclaiming space.** Dropping a column (as `_raw_json` was) frees nothing
by itself — the bytes stay inside every existing row until the table is
rewritten. Plain `VACUUM` only marks dead rows reusable and never shrinks
the file. `scripts/reclaim_vacuum_full.py <disk_gb>` rewrites the big
tables with `VACUUM FULL` one at a time, smallest first, and skips any
table for which `db_size + 1.2 × table_size` would exceed 85% of the disk;
the original table is untouched if a rewrite fails. On 2026-08-29 it took
the database from 9.0 GB to 7.5 GB (credit-memo lines 419 → 98 MB; GL and
item ledger were already lean because they never stored raw JSON).

## Sales Register GST Detail

BC partner report 74365 cannot be fetched as a report, so it is rebuilt as a
Supabase view, `v_sales_register_gst_detail`, whose columns carry the report's
own headers verbatim. Built 2026-08-31, **scoped to FY 2026-27** (from
2026-04-01); the source services are limited to that year, the view itself has
no date floor.

    sql/01_ref_gst_state.sql                BC state code -> '27-MAHARASHTRA'
    sql/02_v_sales_register_gst_detail.sql  the view
    sql/03_sales_register_indexes.sql       run once, after the first backfill

### Validation against the BC register exports, April-August 2026

Checked row by row against the report's own monthly output (transfer shipments
excluded). **Row counts are identical in all five months, with no unmatched
keys**, and every money column except GMV ties to the paisa:

| month | rows (export = view) | columns exact |
|---|---:|---:|
| April | 69,194 | 63 of 70 |
| May | 67,839 | 62 of 70 |
| June | 65,890 | 64 of 70 |
| July | 73,485 | 62 of 70 |
| August | 74,762 | 62 of 70 |

Differing cells, all five months together: **12,417 of ~24.6 million**. Of those,
9,514 are E-Way Bill No. (excluded on the user's instruction) and 2,456 are July
COGS (the export is stale, not the view - see below), leaving a **residual of
447 cells, 0.0018%**:

| column | total | why |
|---|---:|---|
| MRP / GMV | 94 / 93 | best of every derivation tested; see below |
| GSTN | 79 | ship-to registration changed after posting |
| GST HSN/SAC + Group Code | 93 | item card is current; these rows repriced |
| GST Jurisdiction Type | 36 | lines with no GST ledger entry |
| Ship to Address Code | 36 | header and report disagree |
| GST Place of Supply / State | 16 | lines with no GST ledger entry |

Money totals, export vs view - **0.00 difference on all of these, every month**:
Quantity, AmountLCY, Discount, GST Base Amount, IGST, CGST, SGST, Amount To
Customer, and COGS (four of five months - see below).

    April    Quantity 3,173,522.32   AmountLCY 214,094,152.83   ATC 151,375,670.74
    May      Quantity 3,049,486.86   AmountLCY 223,983,058.97   ATC 156,509,152.33
    June     Quantity 2,522,459.32   AmountLCY 198,549,002.81   ATC 143,593,500.49
    July     Quantity 3,388,275.90   AmountLCY 259,564,500.17   ATC 186,469,577.89
    August   Quantity 3,358,948.82   AmountLCY 268,756,010.14   ATC 194,447,496.86

**April alone was not a validation.** Three defects survived the April check and
only surfaced on the later months: MRP (see below), the debit-note G/L rule, and
the Customer State fallback. Re-run every month after any change to this view -
`compare_month.py` in the session scratchpad, or rebuild it from this README.

### What the report actually does - things worth knowing before changing the view

Every one of these was found by the row-by-row comparison, and several are
counter-intuitive enough that a plausible-looking "simplification" would break
the register:

-   **The lines drive the view, not the GST ledger.** 8,358 April rows (542
    invoice, 7,816 credit memo) have no Detailed GST Ledger entry at all and
    the report still prints them. Exempt lines on an *invoice* get 0% ledger
    entries; the same lines on a *credit memo* mostly do not. A ledger-driven
    join silently loses two thirds of the credit-memo rows.
-   **Credit-memo lines are stored POSITIVE in BC** (58,413 positive, 0
    negative across FY 2026-27) and printed negative. Passing them through
    unchanged adds 18.4M to April's sales instead of netting it off.
-   **GMV is a magnitude**, even on credit memos: April's credit-memo GMV is
    +15,292,577 against a quantity of -273,955. Every other amount on those
    rows is signed.
-   **AmountLCY is `Line_Amount + Line_Discount_Amount`**, not
    `ROUND(Quantity x Unit_Price, 2)`. BC rounds the line then adds the
    discount back; the two differ by a paisa on 2,657 of April's 57,227
    invoice lines (7.73 in total).
-   **GST Base Amount is the LINE's amount, never the ledger's
    `GST_Base_Amount`.** They diverge by a few paise on 9 rows and the line
    wins every time. Amount To Customer follows from it.
-   **GSTN is the customer's registration in the SHIP-TO state** (69,097 of
    69,194 rows; the rest are unregistered B2C and print blank). The ledger's
    own per-line `Buyer_Seller_Reg_No` is not that value on 4,860 rows.
-   **"Cancelled" on a credit memo is the `Corrective` flag**, not `Cancelled`.
-   **Name comes from the customer card**, not the posted document (card
    matches 69,194 of 69,194, document 69,147 - a customer renamed since
    posting).
-   **Description comes from the item master**, not the line (68,586 of 68,586
    vs 68,565): the line keeps the text that was current at posting.
-   **G/L account lines are not always printed as G/L lines.** BC posts sales
    returns to account 35120010 while keeping the item in `Item_No`, and the
    report then prints the ITEM, its master description, its HSN and GGST-0%.
    Three rules, exact on April's 608 G/L rows: invoice -> the account number
    (494); credit memo with `Item_No` -> the item (73); credit memo without ->
    nothing at all (41). MRP, GMV and COGS are 0 on all three.
-   **HSN/SAC and GST Group Code come from the Item Card** (page 30), which
    matches the report on all 8,317 April rows that have no ledger entry, and
    is the only source covering the 3 items that appear nowhere in the ledger.
-   **Place of supply falls back to 'Ship-to Address', NOT to the document.**
    Measured: line ledger value else 'Ship-to Address' is wrong on 4 rows,
    routing through the document is wrong on 14. Jurisdiction is the opposite -
    there the document fallback helps (11 wrong vs 14) - so the two are
    deliberately asymmetric.
-   **MRP is DERIVED FROM THE LINE, never read from the item ledger.**
    `GMV = Line_Discount_Amount / (Line_Discount_Percent / 100)` and
    `MRP = GMV / Quantity`. The item ledger's MRP is stamped when goods move and
    goes stale the moment an item reprices - FG/0129 went 40 -> 45 on
    2026-05-13 and the ledger keeps saying 40 for invoices posted after it.
    Measured over 347,269 item rows: derived->ledger->unit price 110 wrong;
    derived->ledger 144; derived->unit price 239; **ledger alone 7,378**. The
    ledger alone looked fine on April (49 wrong) and collapsed in May (3,953).
-   **GSTN is the ship-to address's registration**, from `bc_ship_to_address`
    (loaded over SOAP - see below). Precedence matters: the GST ledger's own
    value wins when it is ALREADY a registration in the ship-to state, because
    that is the value as at posting and the ship-to master is live. Putting the
    master first instead cost 278 rows in April alone.
-   **Ship to Name is the DOCUMENT's**, not the ship-to master's - it is frozen
    at posting and legitimately differs from the current record. Preferring the
    master broke 59,032 rows (every credit memo). The only repair needed is
    BC's OData layer transliterating non-ASCII to '?': `REPLACE(name, '?',
    chr(8211))` restores the en-dash on the 35 affected headers.
-   **COGS comes from Value Entries** and nothing else. `bc_item_ledger_entries`
    cannot be joined to a posted document line - its `Document_No` is the
    SHIPMENT number (`27SSHIP-...`). COGS is printed positive on credit memos
    too, so its sign comes from the document type, not the ledger.
-   **Invoice Date is blank on credit memos**; Return Reason Code is blank on
    invoices; G/L account lines print no Description and 0 for MRP/GMV/COGS.
-   BC's `0001-01-01` "no date" must be rendered blank.

### Known gaps (0.045% of cells; both need a change in BC)

| column | rows | why |
|---|---:|---|
| E-Way Bill No. | 1,585 | Page 134 exposes **no e-way bill field at all** (verified against the live OData catalogue), so credit-memo e-way bill numbers cannot be sourced. **Excluded on the user's instruction 2026-09-01** - not needed. Fix if ever wanted: add the field to page 134 in BC. |
| GSTN | 445 | Customers whose ship-to-state GSTIN never appears in the GST ledger - BC recorded the bill-to GSTIN there instead. The authoritative source is the Ship-to Address table. `Ship_to_Address_Excel` returns **404 and is absent from the 119 entity sets the OData catalogue lists**, so it is not reachable on this endpoint. **Fix: confirm that its Web Services row is Published and exposed for OData V4 (a SOAP-only row does not appear), then enable the existing disabled config entry.** |
| MRP / GMV (no MRP at all) | 39 | `SALVAGE`, `PM/0196`, `PM/0272` - a salvage item and two packaging materials, which carry no MRP in any source. **Accepted on the user's instruction 2026-09-01: the rows stay in the register with MRP/GMV 0, and this is not to be chased.** The same applies to any future `PM/` or `RM/` item. Note these are the only `Type = 'Item'` rows without an MRP; the ~3,900 `G/L Account` rows that also show 0 are correct - the report prints 0 there by design. |
| MRP / GMV (wrong MRP) | 48 each | The document was billed at an older MRP than `bc_item_ledger_entries` recorded (27AHD-00438: ledger 55/80/70/55, report 49/75/65/49). Measured against alternatives, the linked item-ledger entry is right on 69,145 rows; item-card Unit_Price manages 3,463 and the item's modal ledger MRP 33,330. No available source carries the report's value. |
| GST Jurisdiction Type / Place of Supply / GST State | 11 / 4 / 1 | Lines with no ledger entry, where neither fallback recovers the report's value. |
| Ship to Name | 3 | BC itself stores `D-MART ? AGARWAL...`; the April export has an en-dash. Source-data difference, not a sync fault - the register follows BC. |

**Transfer Shipment rows are not produced.** The report prints them (5,477 of
April's 74,671) but they carry no customer, no GST and no COGS, and the
2026-08-28 decision was that the register is for P&L use. Adding them means a
third UNION branch off a transfer-shipment page, which is not synced.

### Scoping the pull to one financial year

BC no.-series are FY-scoped (`26AHD-` = FY25/26, `27AHD-` = FY26/27) and the
posted-lines pages carry **no date field**, so the only way to bound them is to
bound which series are requested. `series_source.date_column` + `min_date` do
that: the parent header table, filtered to `min_date`, defines the scope
(12 invoice and 7 credit-memo series, all `27*`). To extend the register
backwards, lower `min_date` and `sync_start_date` together and re-run those
services with `--mode full`; budget roughly 0.9 GB per additional year.

`Value_Entries_Excel` uses the other new option, `static_filter` - an OData
predicate AND-ed into every request for a service. `Item_Ledger_Entry_Type eq
'Sale'` keeps the purchase/output/consumption/adjustment entries out of the
database entirely: 714k rows instead of 1.17M for FY 2026-27.

### Ship-to Address is SOAP-only in this tenant

`Ship_to_Address_Excel` (page 300) is published, but for **SOAP only**: it is in
the SOAP catalogue (78 services) and absent from the OData V4 catalogue (119
entity sets), and its OData URL 404s. Every other service here is in both.
`scripts/load_ship_to_address_soap.py` reads it over SOAP into
`bc_ship_to_address` (2,335 rows, 2,159 with a GSTIN).

It **is** run on schedule: `bc_sync.yml` has a "Load ship-to addresses (SOAP)"
step after the main sync, so ship-to registrations and names stay current
without anyone remembering to run anything. It sits after the sync, with
`if: always()`, so a SOAP outage cannot stop the core data from loading while
its own failure is still reported.

That step exists only because of the SOAP-only publication. Once OData V4 is
ticked on that Web Services row: delete the workflow step, delete the script,
and set `Ship_to_Address_Excel` to enabled in `web_services.json` - it then
syncs like everything else.

### One-off repair

`scripts/repair_mojibake.py` re-fetched 2,398 header rows whose text was
mangled by the August-2026 MySQL -> Supabase CSV migration (U+FFFD where a
non-breaking space belonged). Not an ongoing bug: the BC API returns the
character correctly, `client_encoding` and `server_encoding` are both UTF8, and
every table written only by the BC sync is clean. Re-run it if any other
migrated table shows the same damage.

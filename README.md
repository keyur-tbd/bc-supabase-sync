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
| `Posted_Sales_Invoice_Excel` | `bc_posted_sales_invoice_excel` | date | `Posting_Date` | `Posting_Date` |
| `Sales_Return_Order_ExcelSalesLines` | `bc_sales_return_order_lines` | full_refresh | — | — |
| `Posted_Sales_Credit_Memo_Lines_Excel` | `bc_posted_sales_credit_memo_lines` | full_refresh | — | — |
| `Customer_Item_Reference_Excel` (**disabled** – 403, needs BC permission on table 50003) | `bc_customer_item_reference` | full_refresh | — | — |
| `Ship_to_Address_Excel` (**disabled** – 404, web service not published) | `bc_ship_to_address` | full_refresh | — | — |
| `Requests_to_Approve_Excel` | `bc_request_to_approve` | full_refresh | — | — |
| `Chart_of_Accounts` | `bc_chart_of_accounts` | full_refresh | — | — |
| `Customer_Ledger_Entries_Excel` | `bc_customer_ledger_entries` | date | `Posting_Date` | `Posting_Date` + extra pass on `Closed_at_Date` |
| `G_L_Account_Card_Excel` | `bc_gl_account_card` | full_refresh | — | — |
| `customers` (**API v2.0**) | `bc_api_customers` | full_refresh | — | — |
| `Vendor_Ledger_Entries_Excel` | `bc_vendor_ledger_entries` | date | `Posting_Date` | `Posting_Date` + extra pass on `Closed_at_Date` |

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
    which silently truncated large pulls to one page.
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
-   **`_raw_json` sizing**: by default each table also stores the complete
    source record as JSONB. That is a genuine safety net at small scale, but
    it duplicates every field the typed columns already hold and runs about
    1 KB per row — roughly **90% of a wide row's on-disk size**. On the GL
    table it alone took the database past 6 GB. List high-volume tables in
    `SUPABASE_RAW_JSON_EXCLUDE_TABLES` to skip it; small tables keep it.
    **Order matters** when turning it off for a table that already exists:
    deploy the setting first, *then* drop the column. Dropping first makes
    every upsert fail, because the INSERT still names the column.
-   **Schema**: tables are created automatically from the shape of the
    data. New fields are added as new columns, and existing `VARCHAR`
    columns are **widened** when a longer value appears — columns are
    never dropped or narrowed. The primary key is the configured business
    key (e.g. `No` / `Entry_No`). Each table also gets a `_raw_json`
    column with the full original record and a `_synced_at` timestamp.
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
-   **PK guard**: at the start of each service the table's actual primary
    key is compared to the configured one; a mismatch is logged loudly
    (editing config alone never migrates an existing table's key).
-   **Upserts**: `INSERT ... ON CONFLICT (pk) DO UPDATE` in batches of 500,
    so reruns and overlapping incremental windows never create
    duplicates.
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
│   └── generate_config_from_excel.py
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
create a client secret. In BC itself, the app's service principal needs
a permission set (e.g. via Tenant Admin Center → API access) covering
the pages you're syncing.

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
`BC_ENVIRONMENT`, `BC_COMPANY_ID`, `SUPABASE_DB_URL`. Trigger an ad hoc full
sync from the Actions tab via "Run workflow" → mode `full`.

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
pages. The full original record is always preserved in `_raw_json`
regardless of how the flattened columns turned out, so nothing is ever
lossy even if a column's inferred type ends up too narrow for an edge
case.


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

On `--mode full`, or when the table is still empty, it falls back to an
unfiltered pull.

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

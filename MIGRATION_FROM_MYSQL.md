# Migrating this sync from MySQL to Supabase

Supabase is Postgres, so this is a MySQL → Postgres port. The Business
Central side (OAuth, OData paging, `@odata.nextLink` resume, date
chunking, retries) is database-agnostic and is **byte-for-byte
unchanged**. Everything that changed is in the DB layer, config, and type
mapping.

---

## 1. What changed, file by file

| File | Change |
|---|---|
| `services/mysql_service.py` | **Deleted**, replaced by `services/supabase_service.py`. Same public method surface. |
| `services/supabase_service.py` | **New.** Postgres DDL, `ON CONFLICT` upserts, JSONB casting, RLS, UTC session. |
| `config.py` | `MySQLConfig` / `load_mysql_config()` → `SupabaseConfig` / `load_supabase_config()`. Added `lowercase_columns()`. |
| `utils/schema_helper.py` | Postgres type inference, double-quoted identifiers, schema-qualified DDL. |
| `services/sync_service.py` | Imports and the constructor parameter only. No logic changes. |
| `app.py` | Imports and help text only. |
| `requirements.txt` | `PyMySQL` → `psycopg[binary]`. SQLAlchemy retained. |
| `.env.example` | `MYSQL_*` → `SUPABASE_*`. |
| `.github/workflows/bc_sync.yml` | Five `MYSQL_*` secrets → one `SUPABASE_DB_URL` secret. |
| `auth_service.py`, `bc_api_service.py`, `retry_helper.py`, `date_chunker.py`, `logger.py`, `models/`, `scripts/` | **Unchanged.** |

---

## 2. Why a direct Postgres connection, not `supabase-py`

This pipeline **creates and alters tables at runtime** and bulk-upserts
whole pages at a time. PostgREST (what `supabase-py` talks to) cannot
issue DDL at all, and its per-row/per-request overhead makes backfills
slow. Supabase is plain Postgres underneath, so connecting to it as
Postgres is both simpler and faster. The database user also bypasses RLS,
which is what a backend ETL job wants.

If you later want a *read* path for a frontend, use `supabase-py`/PostgREST
for that — but keep this sync on the direct connection.

---

## 3. Type mapping applied

| MySQL (old) | Postgres (new) | Note |
|---|---|---|
| `VARCHAR(n)` | `TEXT` | See §4 |
| `TEXT` | `TEXT` | |
| `DATETIME` | `TIMESTAMPTZ` | See §5 |
| `DATE` | `DATE` | |
| `DECIMAL(18,4)` | `NUMERIC(18,4)` | |
| `BIGINT` | `BIGINT` | |
| `TINYINT(1)` | `BOOLEAN` | Real booleans now, not 0/1 |
| `JSON` | `JSONB` | Indexable, key-order normalised |
| `INSERT … ON DUPLICATE KEY UPDATE` | `INSERT … ON CONFLICT (pk) DO UPDATE` | |
| `` `backticks` `` | `"double quotes"` | |
| `NOW()` | `now()` | |

---

## 4. VARCHAR is gone — strings are all TEXT

The MySQL version inferred a `VARCHAR(n)` width from sampled values and
carried ~40 lines of *widening* logic to `ALTER` the column whenever a
longer value showed up later.

In Postgres, `TEXT` and `VARCHAR(n)` have **identical** storage and
performance — the length limit buys nothing. So strings are now always
`TEXT`, and the whole "value outgrew its column, batch failed" class of
error disappears.

The widening code was not simply deleted: it became
`_promote_varchars_to_text()`, which promotes any `VARCHAR`/`CHAR` column
to `TEXT`. That exists so that **tables you migrate over from MySQL**
(which will arrive carrying `varchar(n)`) get fixed automatically on the
first sync. Verified in testing.

---

## 5. Timestamps: read this one

BC returns UTC datetimes with a trailing `Z`, and `dateutil.isoparse`
produces a **timezone-aware** datetime. MySQL's driver silently discarded
that tzinfo. Postgres does not — and a `TIMESTAMP WITHOUT TIME ZONE`
column would reinterpret the value in whatever the session time zone
happened to be, quietly shifting every timestamp by your server's offset.

Two things prevent that:

1. Datetime columns are `TIMESTAMPTZ`, so aware values store an
   unambiguous instant.
2. `SET TIME ZONE 'UTC'` runs on **every pooled connection**, so the
   naive UTC watermark (`_utc_now()` in `sync_service.py`) is also
   interpreted as UTC.

Both paths are covered by the test suite. If you ever remove the UTC
session setting, watermarks will drift by your server's offset and the
incremental sync will silently skip or re-pull records.

---

## 6. Primary keys are TEXT, and values are coerced

PK columns are `TEXT NOT NULL` (as they were `VARCHAR(64)` before). MySQL
implicitly cast an integer parameter into a varchar column; **Postgres
does not** and would raise `column "Entry_No" is of type text but
expression is of type bigint`.

So `_upsert_batch()` now calls `str()` on PK values before binding. This
means `Entry_No` 1001 is stored as the string `'1001'` — same as the old
MySQL behaviour. Sort accordingly (`ORDER BY "Entry_No"::bigint`) or
change the PK column type if you want numeric ordering.

---

## 7. Column naming — a decision you must make once

Postgres folds unquoted identifiers to lower case; MySQL did not. This
code quotes every identifier, so BC's casing survives by default:
`"Posting_Date"`, `"Entry_No"`.

`PG_LOWERCASE_COLUMNS` in `.env` controls this:

- **`false` (default)** — column names match the old MySQL database
  exactly, so migrated data and existing downstream queries line up. Cost:
  every hand-written query must quote them.
  `select "Posting_Date" from bc_general_ledger_entries;`
- **`true`** — idiomatic lower-case Postgres, no quoting needed. Cost:
  column names differ from the old MySQL DB.

**Pick one before the first run.** Flipping it later makes the sync add a
*second* set of columns alongside the first rather than renaming them.

The reserved-word list and its `_field` suffixing were deliberately kept
identical to the MySQL version (redundant now, since quoted identifiers
may be reserved words) purely so column names stay character-for-character
compatible.

---

## 8. Supabase-specific: RLS and schema choice

Anything in the `public` schema is reachable through PostgREST with your
project's **anon key**. Raw ERP data almost certainly should not be.

`SUPABASE_ENABLE_RLS=true` (the default) runs
`ALTER TABLE … ENABLE ROW LEVEL SECURITY` on every table this sync
creates. With RLS on and no policies attached, anon and authenticated
roles are denied everything, while this sync (connecting as the database
owner, which bypasses RLS) is unaffected.

Stronger option: set `SUPABASE_DB_SCHEMA=bc`. A non-`public` schema is not
exposed through the API at all unless you explicitly add it in
Dashboard → Settings → API. The sync creates the schema if it is missing.

---

## 9. Connection pooling

Supabase offers a direct connection (port **5432**) and a transaction
pooler (port **6543**, `*.pooler.supabase.com`, PgBouncer).

PgBouncer in transaction mode cannot hold server-side prepared statements
across transactions, and psycopg 3 prepares a statement automatically
after a few executions — which would start failing mid-run. So
`SUPABASE_USE_POOLER=true` (the default) sets `prepare_threshold=None`.

Use the pooler for GitHub Actions: runners have dynamic egress IPs and
open short-lived connections, which is exactly what the pooler is for.
Leaving this `true` on a direct 5432 connection is harmless.

---

## 10. One behavioural difference to be aware of

MySQL was permissive about type mismatches; Postgres is strict. If a
column is inferred `BIGINT` from the first page and a later page sends a
decimal for it, MySQL would silently truncate. Postgres raises, and the
batch lands in `failed_records/` with the error.

This is arguably an improvement — silent truncation is data loss — but it
means you may see `failed_records/` entries on the first Supabase run that
the MySQL version never surfaced. Inspect the JSON, then either fix
`web_services.json` or widen the column:
`ALTER TABLE … ALTER COLUMN "X" TYPE NUMERIC(18,4);`

---

## 11. Migration steps

1. `pip install -r requirements.txt`
2. Copy `.env.example` → `.env`, paste `SUPABASE_DB_URL` from
   Dashboard → Project Settings → Database → Connection string (URI), and
   substitute your database password.
3. Decide `PG_LOWERCASE_COLUMNS` and `SUPABASE_DB_SCHEMA` **now** (§7, §8).
4. `python app.py --setup` — creates tables from one sample page per
   service, no data loaded. Inspect them in the Supabase table editor.
5. Choose one:
   - **Fresh backfill (recommended, simplest):** just run
     `python app.py --mode full`. Nothing to migrate; BC is the source of
     truth. Cost is API time.
   - **Carry the existing MySQL data over:** export each table to CSV and
     `\copy` it in, then seed `etl_sync_state` with the `last_sync_at` and
     `history_through` values from the old MySQL `etl_sync_state` so the
     sync resumes incrementally instead of re-pulling history. Any
     `varchar` columns arriving from MySQL are promoted to `TEXT`
     automatically on the first run (§4).
6. Replace the five `MYSQL_*` GitHub secrets with a single
   `SUPABASE_DB_URL` secret, then run the workflow manually once before
   trusting the cron.

---

## 12. Test coverage

`test_supabase_port.py` (included) runs the DB layer against a real
PostgreSQL instance — 63 assertions: DSN normalisation, UTC session
pinning, table creation, every type mapping, JSONB round-tripping,
insert/update upsert semantics, `_synced_at` refresh, null-PK quarantine,
schema drift, VARCHAR→TEXT promotion, composite keys, the full sync-state
lifecycle (including that a *failed* run keeps its `resume_url` while a
successful one clears it), custom schemas, and multi-batch inserts.

It runs one of two ways:

**Windows, or against any existing Postgres** — set a DSN:

```powershell
$env:TEST_PG_DSN = "postgresql://postgres:PASSWORD@db.xxxx.supabase.co:5432/postgres"
python test_supabase_port.py
```

**Linux / macOS** — no database needed, spins up a throwaway local one:

```bash
pip install pgserver
python test_supabase_port.py
```

Everything is created inside two disposable schemas (`bc_port_test`,
`bc_port_test_alt`) which are dropped at the end, and the sync-state rows
use a fake service name. It never touches `public`, your real `bc_*`
tables, or your real `etl_sync_state`, so it is safe to point at a live
Supabase project — verified by running it twice in a row against the same
database and confirming nothing is left behind.

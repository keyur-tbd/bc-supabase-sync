"""
Integration test for the Supabase (Postgres) port.

Exercises every code path that changed from the MySQL version, against a
real PostgreSQL instance.

WHERE IT RUNS
-------------
  TEST_PG_DSN set    -> uses that Postgres. Works on Windows. Can point at
                        your real Supabase project (see SAFETY below).
  TEST_PG_DSN unset  -> spins up a disposable local Postgres via `pgserver`
                        (Linux/macOS only - pgserver ships no Windows build).

  Windows / PowerShell:
      $env:TEST_PG_DSN = "postgresql://postgres:PASSWORD@db.xxxx.supabase.co:5432/postgres"
      python test_supabase_port.py

  Linux / macOS:
      pip install pgserver
      python test_supabase_port.py

SAFETY
------
Everything is created inside two disposable schemas which are dropped at
the end, and the sync-state rows use a deliberately fake service name.
Nothing touches `public` or your real bc_* tables or etl_sync_state.
Pointing this at a live Supabase project is safe; a scratch project is
still tidier.
"""

import sys
import os
import pathlib
import datetime as dt
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

TEST_SCHEMA = "bc_port_test"
ALT_SCHEMA = "bc_port_test_alt"
FAKE_SERVICE = "__port_test_service__"

_dsn = os.getenv("TEST_PG_DSN", "").strip()
if _dsn:
    DSN = _dsn
    print(f"Testing against TEST_PG_DSN, schemas {TEST_SCHEMA} / {ALT_SCHEMA}\n")
else:
    try:
        import pgserver
    except ImportError:
        sys.exit(
            "No TEST_PG_DSN set and `pgserver` is not installed.\n\n"
            "  Linux/macOS:  pip install pgserver\n"
            "  Windows:      set TEST_PG_DSN to any Postgres, e.g.\n"
            '                $env:TEST_PG_DSN = '
            '"postgresql://postgres:PW@db.xxxx.supabase.co:5432/postgres"\n'
        )
    PGDATA = pathlib.Path(__file__).resolve().parent / "pgdata_test"
    _srv = pgserver.get_server(str(PGDATA))
    DSN = f"postgresql://postgres:@/postgres?host={PGDATA}&sslmode=disable"
    print(f"Testing against local pgserver at {PGDATA}\n")

from config import SupabaseConfig, _normalize_dsn          # noqa: E402
from models.sync_state import SyncState                     # noqa: E402
from services.supabase_service import SupabaseService       # noqa: E402
from utils.schema_helper import prepare_row                 # noqa: E402

T = f'"{TEST_SCHEMA}"'          # pre-quoted schema for raw SQL in this test
PASS: list[str] = []
FAIL: list[str] = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail and not cond else ""))


def scalar(sql, **params):
    with svc._engine.connect() as c:
        return c.execute(text(sql), params).scalar()


# ------------------------------------------------------------------ DSN
norm = _normalize_dsn("postgresql://u:p@db.abc.supabase.co:5432/postgres")
check("DSN: scheme rewritten to psycopg", norm.startswith("postgresql+psycopg://"), norm)
check("DSN: sslmode forced", "sslmode=require" in norm, norm)
norm2 = _normalize_dsn("postgres://u:p@h:6543/postgres?sslmode=disable")
check("DSN: existing sslmode preserved", "sslmode=disable" in norm2 and "require" not in norm2, norm2)

cfg = SupabaseConfig(host="", port=5432, database="postgres", user="postgres",
                     password="", dsn=DSN, schema=TEST_SCHEMA,
                     enable_rls=True, use_pooler=True)
svc = SupabaseService(cfg)
check("connection test", svc.test_connection())
check("session time zone pinned to UTC", scalar("SHOW TIME ZONE") == "UTC")

# --------------------------------------------------------- sync state table
with svc._engine.connect() as c:
    cols = {r[0] for r in c.execute(text(
        "select column_name from information_schema.columns "
        "where table_schema=:s and table_name='etl_sync_state'"), {"s": TEST_SCHEMA}).fetchall()}
check("etl_sync_state created with all columns",
      {"service_name", "status", "last_sync_at", "history_through", "resume_url",
       "last_run_started_at", "last_run_completed_at", "records_processed",
       "records_failed", "last_error"} <= cols, sorted(cols))

RLS_SQL = ("select relrowsecurity from pg_class c join pg_namespace n "
           "on n.oid = c.relnamespace where c.relname = :t and n.nspname = :s")
check("RLS enabled on etl_sync_state", scalar(RLS_SQL, t="etl_sync_state", s=TEST_SCHEMA) is True)

# ------------------------------------------------------ realistic BC records
BC_RECORDS = [
    {"@odata.etag": 'W/"x"', "Entry_No": 1001, "Posting_Date": "2026-01-15",
     "SystemCreatedAt": "2026-01-15T08:30:00.1234567Z", "Description": "Opening bal",
     "Amount": 1523.4567, "Quantity": 12, "Open": True, "Remaining": None,
     "Dimensions": [{"code": "DEPT", "value": "SALES"}],
     "Address": {"City": "Ahmedabad", "Pin": "380015"}},
    {"Entry_No": 1002, "Posting_Date": "2026-02-20",
     "SystemCreatedAt": "2026-02-20T11:00:00Z", "Description": "x" * 3000,
     "Amount": 10.0, "Quantity": 3, "Open": False, "Remaining": 5.5,
     "Dimensions": [], "Address": {"City": "Mumbai", "Pin": "400001"}},
]
rows, jcols = [], set()
for rec in BC_RECORDS:
    row, cs = prepare_row(rec)
    rows.append(row)
    jcols |= cs

check("odata metadata stripped", not any(k.startswith("@odata") for k in rows[0]))
check("nested dict flattened", "Address_City" in rows[0], list(rows[0]))
check("list marked as json column", "Dimensions" in jcols, jcols)
check("ISO datetime coerced to datetime obj", isinstance(rows[0]["SystemCreatedAt"], dt.datetime))
check("7-digit fractional seconds parsed", rows[0]["SystemCreatedAt"].year == 2026)
check("date coerced to date obj", isinstance(rows[0]["Posting_Date"], dt.date))

inferred = svc.ensure_table("bc_test_entries", rows, "Entry_No", json_columns=jcols)
check("float -> NUMERIC(18,4)", inferred.get("Amount") == "NUMERIC(18,4)", inferred.get("Amount"))
check("datetime -> TIMESTAMPTZ", inferred.get("SystemCreatedAt") == "TIMESTAMPTZ", inferred.get("SystemCreatedAt"))
check("date -> DATE", inferred.get("Posting_Date") == "DATE", inferred.get("Posting_Date"))
check("int -> BIGINT", inferred.get("Quantity") == "BIGINT", inferred.get("Quantity"))
check("bool -> BOOLEAN", inferred.get("Open") == "BOOLEAN", inferred.get("Open"))
check("list -> JSONB", inferred.get("Dimensions") == "JSONB", inferred.get("Dimensions"))
check("3000-char string -> TEXT (no width to outgrow)",
      inferred.get("Description") == "TEXT", inferred.get("Description"))

with svc._engine.connect() as c:
    types = dict(c.execute(text(
        "select column_name, data_type from information_schema.columns "
        "where table_schema=:s and table_name='bc_test_entries'"), {"s": TEST_SCHEMA}).fetchall())
check("mixed-case identifiers survived", "Posting_Date" in types, sorted(types))
check("PK column is text", types.get("Entry_No") == "text", types.get("Entry_No"))
check("_synced_at is timestamptz", types.get("_synced_at") == "timestamp with time zone")
check("_raw_json is jsonb", types.get("_raw_json") == "jsonb")
check("actual_primary_key reads back", svc.actual_primary_key("bc_test_entries") == "Entry_No",
      svc.actual_primary_key("bc_test_entries"))
check("RLS enabled on data table", scalar(RLS_SQL, t="bc_test_entries", s=TEST_SCHEMA) is True)

# ----------------------------------------------------------------- insert
ok, bad = svc.upsert_rows("bc_test_entries", rows, "Entry_No", raw_records=BC_RECORDS)
check("insert: 2 ok / 0 failed", (ok, bad) == (2, 0), f"{ok},{bad}")

with svc._engine.connect() as c:
    got = c.execute(text(
        f'select "Entry_No","Amount","Open","Dimensions","Address_City",'
        f'"SystemCreatedAt","_raw_json","Description" '
        f'from {T}.bc_test_entries order by "Entry_No"')).mappings().all()
check("int PK coerced to text", got[0]["Entry_No"] == "1001", got[0]["Entry_No"])
check("numeric stored with scale", float(got[0]["Amount"]) == 1523.4567, got[0]["Amount"])
check("boolean stored", got[0]["Open"] is True)
check("jsonb parsed back as list", got[0]["Dimensions"] == [{"code": "DEPT", "value": "SALES"}], got[0]["Dimensions"])
check("empty list -> jsonb []", got[1]["Dimensions"] == [])
check("nested value stored", got[0]["Address_City"] == "Ahmedabad")
check("3000-char value stored intact", len(got[1]["Description"]) == 3000, len(got[1]["Description"]))
check("_raw_json populated as jsonb",
      isinstance(got[0]["_raw_json"], dict) and got[0]["_raw_json"]["Entry_No"] == 1001)
check("tz-aware datetime stored as correct UTC instant",
      got[0]["SystemCreatedAt"] == dt.datetime(2026, 1, 15, 8, 30, 0, 123456, tzinfo=dt.timezone.utc),
      got[0]["SystemCreatedAt"])

# ------------------------------------------------------------ upsert/update
before = scalar(f'select "_synced_at" from {T}.bc_test_entries where "Entry_No" = \'1001\'')
time.sleep(0.05)
rows[0]["Description"] = "UPDATED"
rows[0]["Amount"] = 99.0
svc.upsert_rows("bc_test_entries", rows, "Entry_No", raw_records=BC_RECORDS)
with svc._engine.connect() as c:
    n = c.execute(text(f"select count(*) from {T}.bc_test_entries")).scalar()
    r = c.execute(text(f'select "Description","Amount","_synced_at" from {T}.bc_test_entries '
                       f'where "Entry_No" = \'1001\'')).mappings().one()
check("upsert did not duplicate (still 2 rows)", n == 2, n)
check("upsert updated the row", r["Description"] == "UPDATED" and float(r["Amount"]) == 99.0)
check("_synced_at refreshed on update (no ON UPDATE in PG)", r["_synced_at"] > before,
      f"{before} -> {r['_synced_at']}")

# ------------------------------------------------------- null PK quarantine
qdir = pathlib.Path(__file__).resolve().parent / "quarantine"
q_before = len(list(qdir.glob("*.json"))) if qdir.exists() else 0
bad_rows = [{"Entry_No": None, "Description": "no key"}, {"Entry_No": "  ", "Description": "blank"}]
ok3, bad3 = svc.upsert_rows("bc_test_entries", bad_rows, "Entry_No")
q_after = len(list(qdir.glob("*.json")))
check("null/blank PK rows quarantined not inserted",
      (ok3, bad3) == (0, 2) and q_after == q_before + 1, f"{ok3},{bad3}")

# ------------------------------------------------------------ schema drift
drift = [{"Entry_No": 1003, "Posting_Date": dt.date(2026, 3, 1), "Brand_New_Field": "hello", "Amount": 1.0}]
svc.ensure_table("bc_test_entries", drift, "Entry_No")
okd, badd = svc.upsert_rows("bc_test_entries", drift, "Entry_No")
v = scalar(f'select "Brand_New_Field" from {T}.bc_test_entries where "Entry_No" = \'1003\'')
check("schema drift: new column added + row inserted", (okd, badd) == (1, 0) and v == "hello", f"{okd},{badd},{v}")

# ------------------------------- varchar -> text promotion (migrated table)
with svc._engine.begin() as c:
    c.execute(text(f'CREATE TABLE {T}.legacy_migrated ('
                   f'"Id" TEXT NOT NULL PRIMARY KEY, "Note" VARCHAR(20), '
                   f'"_synced_at" TIMESTAMPTZ NOT NULL DEFAULT now(), "_raw_json" JSONB)'))
svc._known_columns.pop("legacy_migrated", None)
svc.ensure_table("legacy_migrated", [{"Id": "a", "Note": "y" * 500}], "Id")
promoted = scalar("select data_type from information_schema.columns "
                  "where table_schema=:s and table_name='legacy_migrated' and column_name='Note'",
                  s=TEST_SCHEMA)
oke, bade = svc.upsert_rows("legacy_migrated", [{"Id": "a", "Note": "y" * 500}], "Id")
check("legacy VARCHAR promoted to TEXT", promoted == "text", promoted)
check("over-long value now inserts fine", (oke, bade) == (1, 0), f"{oke},{bade}")

# ------------------------------------------------------------- composite PK
comp = [{"Document_No": "SO-1", "Line_No": 10000, "Qty": 5},
        {"Document_No": "SO-1", "Line_No": 20000, "Qty": 7}]
svc.ensure_table("bc_test_lines", comp, ["Document_No", "Line_No"])
okc, badc = svc.upsert_rows("bc_test_lines", comp, ["Document_No", "Line_No"])
comp[1]["Qty"] = 99
svc.upsert_rows("bc_test_lines", comp, ["Document_No", "Line_No"])
cnt = scalar(f"select count(*) from {T}.bc_test_lines")
qty = scalar(f'select "Qty" from {T}.bc_test_lines where "Line_No" = \'20000\'')
check("composite PK: both lines kept", (okc, badc) == (2, 0) and cnt == 2, f"{okc},{badc},{cnt}")
check("composite PK: upsert updates correct line", qty == 99, qty)
check("composite PK read back", svc.actual_primary_key("bc_test_lines") == "Document_No,Line_No",
      svc.actual_primary_key("bc_test_lines"))

# ------------------------------------------------------ sync state lifecycle
svc.mark_run_started(FAKE_SERVICE)
st = SyncState.from_row(svc.get_sync_state(FAKE_SERVICE), FAKE_SERVICE)
check("mark_run_started -> in_progress", st.status == "in_progress", st.status)
check("fresh service: backfill not done", st.backfill_done is False)

svc.save_resume_point(FAKE_SERVICE, "https://bc/next?page=2", 500)
svc.save_history_progress(FAKE_SERVICE, dt.date(2026, 3, 1))
st = SyncState.from_row(svc.get_sync_state(FAKE_SERVICE), FAKE_SERVICE)
check("resume_url + records_processed persisted",
      st.resume_url.endswith("page=2") and st.records_processed == 500)
check("history_through persisted as date", st.history_through == dt.date(2026, 3, 1), st.history_through)
check("has_incomplete_run true", st.has_incomplete_run is True)

wm = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
svc.set_incremental_watermark(FAKE_SERVICE, wm)
svc.mark_run_completed(FAKE_SERVICE, "success", 500, 0)
st = SyncState.from_row(svc.get_sync_state(FAKE_SERVICE), FAKE_SERVICE)
check("watermark stored as correct UTC instant",
      st.last_sync_at.astimezone(dt.timezone.utc).replace(tzinfo=None) == wm,
      f"{wm} -> {st.last_sync_at}")
check("backfill_done flips once watermark set", st.backfill_done is True)
check("success clears resume_url", st.resume_url is None, st.resume_url)
check("status=success persisted", st.status == "success")

svc.save_resume_point(FAKE_SERVICE, "https://bc/next?page=9", 900)
svc.mark_run_completed(FAKE_SERVICE, "failed", 900, 3, error="boom")
st = SyncState.from_row(svc.get_sync_state(FAKE_SERVICE), FAKE_SERVICE)
check("failed KEEPS resume_url for page-level resume", st.resume_url.endswith("page=9"), st.resume_url)
check("last_error persisted", st.last_error == "boom")
check("records_failed persisted", st.records_failed == 3)

svc.mark_run_started(FAKE_SERVICE)
st = SyncState.from_row(svc.get_sync_state(FAKE_SERVICE), FAKE_SERVICE)
check("re-running existing service upserts state (no PK violation)", st.status == "in_progress")

# ------------------------------------------------------- second/alt schema
cfg2 = SupabaseConfig(host="", port=5432, database="postgres", user="postgres",
                      password="", dsn=DSN, schema=ALT_SCHEMA, enable_rls=True)
svc2 = SupabaseService(cfg2)
svc2.ensure_table("bc_scoped", [{"Id": "z", "V": 1}], "Id")
oks, bads = svc2.upsert_rows("bc_scoped", [{"Id": "z", "V": 1}], "Id")
with svc2._engine.connect() as c:
    ns = c.execute(text(f'select count(*) from "{ALT_SCHEMA}".bc_scoped')).scalar()
check("custom schema auto-created and used", (oks, bads) == (1, 0) and ns == 1, f"{oks},{bads},{ns}")
svc2.dispose()

# ----------------------------------------------------------------- batching
many = [{"Entry_No": 5000 + i, "Description": f"row {i}", "Amount": float(i)} for i in range(1200)]
svc.ensure_table("bc_test_entries", many, "Entry_No")
okb, badb = svc.upsert_rows("bc_test_entries", many, "Entry_No", batch_size=500)
cnt = scalar(f'select count(*) from {T}.bc_test_entries '
             f'where "Entry_No"::bigint between 5000 and 6199')
spot = scalar(f'select "Description" from {T}.bc_test_entries where "Entry_No" = \'6199\'')
check("1200 rows across 3 batches", (okb, badb) == (1200, 0) and cnt == 1200, f"{okb},{badb},{cnt}")
check("last row of last batch present", spot == "row 1199", spot)

# ----------------------------------------------------------------- teardown
with svc._engine.begin() as c:
    c.execute(text(f'DROP SCHEMA IF EXISTS "{TEST_SCHEMA}" CASCADE'))
    c.execute(text(f'DROP SCHEMA IF EXISTS "{ALT_SCHEMA}" CASCADE'))
left = scalar("select count(*) from information_schema.schemata where schema_name in (:a, :b)",
              a=TEST_SCHEMA, b=ALT_SCHEMA)
check("test schemas dropped cleanly", left == 0, left)

svc.dispose()
print("\n" + "=" * 60)
print(f"PASSED: {len(PASS)}    FAILED: {len(FAIL)}")
if FAIL:
    print("FAILING:", FAIL)
print("=" * 60)
sys.exit(1 if FAIL else 0)

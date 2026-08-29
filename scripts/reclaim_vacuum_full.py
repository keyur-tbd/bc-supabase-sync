"""Approved by the user 2026-08-29: VACUUM FULL the bloated bc_* tables so the
bytes of the dropped _raw_json column are released to the OS. One table at a
time, smallest first; each rewrite is skipped unless
db_size + 1.2 x table_size stays under LIMIT_GB x 0.85."""
import sys, time
sys.path.insert(0, __file__.rsplit("scripts", 1)[0])
from sqlalchemy import text
from config import load_supabase_config
from services.supabase_service import SupabaseService

LIMIT_GB = float(sys.argv[1]) if len(sys.argv) > 1 else 18.0
CEILING = LIMIT_GB * 0.85 * 1024**3
TABLES = ["bc_posted_sales_credit_memo_lines", "bc_customer_ledger_entries",
          "bc_sales_return_order_lines", "bc_item_ledger_entries", "bc_general_ledger_entries"]
eng = SupabaseService(load_supabase_config())._engine

def sizes(t):
    with eng.connect() as c:
        return c.execute(text("select pg_database_size(current_database()), pg_total_relation_size(:t)"),
                         {"t": f"public.{t}"}).one()

for t in TABLES:
    db, tbl = sizes(t)
    if db + 1.2 * tbl > CEILING:
        print(f"SKIP {t}: db {db/1e9:.2f} GB + copy {tbl/1e9:.2f} GB would exceed {CEILING/1e9:.2f} GB ceiling", flush=True)
        continue
    print(f"START {t}: {tbl/1e6:.0f} MB, db {db/1e9:.2f} GB", flush=True)
    t0 = time.time()
    raw = eng.raw_connection()
    try:
        # VACUUM cannot run inside a transaction block: set autocommit on the
        # underlying psycopg connection (the pool wrapper ignores the attr).
        raw.driver_connection.autocommit = True
        cur = raw.driver_connection.cursor()
        cur.execute("set statement_timeout = 0")
        cur.execute("set lock_timeout = '5min'")
        cur.execute(f'vacuum (full, analyze) public."{t}"')
        db2, tbl2 = sizes(t)
        print(f"OK   {t}: {tbl/1e6:.0f} MB -> {tbl2/1e6:.0f} MB in {time.time()-t0:.0f}s; db now {db2/1e9:.2f} GB", flush=True)
    except Exception as e:
        print(f"FAIL {t}: {str(e)[:300]}", flush=True)
    finally:
        try:
            raw.driver_connection.autocommit = False
        except Exception:
            pass
        raw.close()
print("done", flush=True)

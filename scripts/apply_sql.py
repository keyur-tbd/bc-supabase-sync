#!/usr/bin/env python3
"""Apply everything in sql/ to Supabase, in filename order.

Keeps the deployed schema equal to what is committed. Without this the view,
the state lookup and the indexes exist only because somebody ran them by hand
once: edit sql/02_*.sql and the database keeps serving the old definition,
silently, while still producing plausible numbers.

Every file is written to be idempotent (CREATE TABLE IF NOT EXISTS + ON
CONFLICT, DROP VIEW IF EXISTS + CREATE VIEW, CREATE INDEX CONCURRENTLY IF NOT
EXISTS), so running this on every sync is a no-op when nothing has changed.

RUN IT AFTER THE SYNC, not before: the view selects from the bc_* tables, so on
a fresh database they have to exist first.

    python scripts/apply_sql.py            # sql/  - after the sync
    python scripts/apply_sql.py sql/pre    # sql/pre/ - before it

sql/pre/ is the exception: schema changes the sync itself depends on, such as a
primary key the upsert's ON CONFLICT has to match. Those cannot wait until
after the run that needs them. Keep it empty of anything that reads bc_* data.
"""
from __future__ import annotations

import logging
import pathlib
import re
import sys

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])

from sqlalchemy import text

from config import load_supabase_config
from services.supabase_service import SupabaseService
from utils.logger import setup_logger

logger = logging.getLogger("bc_sync")

SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"


DOLLAR_QUOTE = re.compile(r"\$[A-Za-z_]*\$")


def statements(sql: str) -> list[str]:
    """Split on ';' after stripping whole-line comments.

    Good enough because these files keep no semicolons inside string literals;
    if that ever changes, this needs a real parser rather than a cleverer regex.

    A DO block or a function body would be shredded by it - every ';' inside
    the dollar-quoted body would become its own "statement" - so refuse rather
    than send nonsense to the server. Only files carrying CREATE INDEX
    CONCURRENTLY reach here; keep those free of dollar quoting, or split the
    file in two.
    """
    if DOLLAR_QUOTE.search(sql):
        raise ValueError(
            "cannot split a file that both needs CONCURRENTLY (so, no "
            "transaction) and contains a dollar-quoted body - put one of them "
            "in its own file"
        )
    body = "\n".join(l for l in sql.splitlines() if not l.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def main() -> int:
    setup_logger()
    root = pathlib.Path(__file__).resolve().parent.parent
    sql_dir = root / sys.argv[1] if len(sys.argv) > 1 else SQL_DIR
    # Only the top level: sql/ must not pick up sql/pre/, which has already run.
    files = sorted(p for p in sql_dir.glob("*.sql") if p.is_file())
    if not files:
        logger.error(f"No .sql files found in {sql_dir} - nothing to apply.")
        return 1

    engine = SupabaseService(load_supabase_config())._engine
    for path in files:
        sql = path.read_text(encoding="utf-8")
        try:
            if "CONCURRENTLY" in sql.upper():
                # CREATE INDEX CONCURRENTLY cannot run inside a transaction, so
                # these go one at a time with autocommit and are not atomic.
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    for stmt in statements(sql):
                        conn.execute(text(stmt))
            else:
                # One transaction per file: a half-applied view is worse than
                # an unapplied one.
                with engine.begin() as conn:
                    conn.execute(text(sql))
            logger.info(f"Applied {path.name}")
        except Exception:
            logger.exception(f"FAILED applying {path.name}")
            return 2

    logger.info(f"All {len(files)} SQL file(s) applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

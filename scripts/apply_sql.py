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

FILES THAT ARE TOO EXPENSIVE TO RE-APPLY
"Idempotent" is not the same as "free". Applying sql/02 drops and rebuilds
every view below the sales register - 29 of them by 2026-09-09, two holding
data - and that cost, paid every two hours for nothing, is what eventually
took five runs red. Such a file declares a fingerprint query in its header:

    -- fingerprint: SELECT md5(...)     (continuation lines are '--' too)

It is skipped when the fingerprint still equals what was recorded the last
time it applied AND the file's own text has not changed. The fingerprint must
cover exactly what the file asserts, so a hand-edit of the deployed object
brings the re-apply back and drift is still caught - the guarantee above is
kept, it is just checked instead of assumed. Files declaring no fingerprint
apply on every run, as before.

    python scripts/apply_sql.py --seed     # record fingerprints, apply nothing

--seed adopts the ledger on a database whose schema already matches the
committed files: it writes what is deployed now, without running anything. Use
it once, and only when you know the two agree - it is a promise, not a check.
"""
from __future__ import annotations

import hashlib
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

FINGERPRINT_MARKER = re.compile(r"^\s*--\s*fingerprint:\s*(.*)$", re.IGNORECASE)

# The ledger lives in the database, not in a state file, because the thing it
# describes is the database. 'etl_' to match the other cross-pipeline tables.
LEDGER_DDL = [
    """
    CREATE TABLE IF NOT EXISTS public.etl_applied_sql (
        filename    text PRIMARY KEY,
        sha256      text        NOT NULL,   -- of the file's text, newline-normalised
        fingerprint text,                   -- what the file's own query answered
        applied_at  timestamptz NOT NULL DEFAULT now()
    )
    """,
    # Supabase's default privileges hand anon and authenticated full DML on a
    # new public table, and only RLS with no policy takes it back. A row here
    # is a promise not to re-apply a file, so anyone who can forge one can
    # freeze the schema; postgres owns the table and bypasses RLS.
    "ALTER TABLE public.etl_applied_sql ENABLE ROW LEVEL SECURITY",
]

LEDGER_UPSERT = """
INSERT INTO public.etl_applied_sql (filename, sha256, fingerprint, applied_at)
VALUES (:filename, :sha256, :fingerprint, now())
ON CONFLICT (filename) DO UPDATE
   SET sha256      = EXCLUDED.sha256,
       fingerprint = EXCLUDED.fingerprint,
       applied_at  = EXCLUDED.applied_at
"""


def file_digest(sql: str) -> str:
    """Hash the file's text, newline-normalised - not its bytes.

    A checkout with different line endings - Windows here, Linux on the runner
    - must hash the same, or each would see the other's line endings as a
    changed file and re-apply it. read_text() already folds CRLF into LF, but
    the invariant belongs here rather than in how the caller happened to read.
    """
    return hashlib.sha256(
        sql.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    ).hexdigest()


def fingerprint_query(sql: str) -> str | None:
    """Pull the '-- fingerprint: ...' query out of a file's header.

    The marker line plus every comment line immediately after it, up to the
    first line that is not a '--' comment. A blank line therefore ends it,
    which is why the header keeps one after the query.
    """
    lines = sql.splitlines()
    for i, line in enumerate(lines):
        m = FINGERPRINT_MARKER.match(line)
        if not m:
            continue
        parts = [m.group(1)]
        for cont in lines[i + 1:]:
            if not cont.strip().startswith("--"):
                break
            parts.append(cont.strip()[2:])
        return "\n".join(parts).strip() or None
    return None


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


def read_fingerprint(engine, query: str) -> str | None:
    """Answer a file's fingerprint query. NULL/no row means 'not deployed'."""
    with engine.connect() as conn:
        row = conn.execute(text(query)).first()
    return None if row is None or row[0] is None else str(row[0])


def ledger_row(engine, filename: str) -> tuple[str, str | None] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT sha256, fingerprint FROM public.etl_applied_sql "
                 "WHERE filename = :f"),
            {"f": filename},
        ).first()
    return (row[0], row[1]) if row else None


def main() -> int:
    setup_logger()
    argv = [a for a in sys.argv[1:] if a != "--seed"]
    seed = "--seed" in sys.argv[1:]
    root = pathlib.Path(__file__).resolve().parent.parent
    sql_dir = root / argv[0] if argv else SQL_DIR
    # Only the top level: sql/ must not pick up sql/pre/, which has already run.
    files = sorted(p for p in sql_dir.glob("*.sql") if p.is_file())
    if not files:
        logger.error(f"No .sql files found in {sql_dir} - nothing to apply.")
        return 1

    engine = SupabaseService(load_supabase_config())._engine
    with engine.begin() as conn:
        for ddl in LEDGER_DDL:
            conn.execute(text(ddl))

    applied = skipped = 0
    for path in files:
        sql = path.read_text(encoding="utf-8")
        digest = file_digest(sql)
        fp_query = fingerprint_query(sql)
        try:
            if seed:
                if fp_query is None:
                    logger.info(f"{path.name}: no fingerprint declared, nothing to seed")
                    continue
                fp = read_fingerprint(engine, fp_query)
                if fp is None:
                    logger.error(
                        f"{path.name}: fingerprint is NULL - the object it "
                        f"describes is not deployed, so there is nothing to "
                        f"promise. Apply the file instead of seeding it."
                    )
                    return 2
                with engine.begin() as conn:
                    conn.execute(text(LEDGER_UPSERT),
                                 {"filename": path.name, "sha256": digest,
                                  "fingerprint": fp})
                logger.warning(f"SEEDED {path.name} as already applied "
                               f"(fingerprint {fp}) - nothing was executed")
                continue

            if fp_query is not None:
                fp_before = read_fingerprint(engine, fp_query)
                prev = ledger_row(engine, path.name)
                if (prev is not None and fp_before is not None
                        and prev[0] == digest and prev[1] == fp_before):
                    logger.info(f"Unchanged, skipped {path.name} "
                                f"(fingerprint {fp_before})")
                    skipped += 1
                    continue
                if prev is not None:
                    why = ("the file changed" if prev[0] != digest
                           else "the deployed object no longer matches"
                                if fp_before is not None else "it is not deployed")
                    logger.info(f"Re-applying {path.name}: {why}")

            if "CONCURRENTLY" in sql.upper():
                # CREATE INDEX CONCURRENTLY cannot run inside a transaction, so
                # these go one at a time with autocommit and are not atomic.
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    for stmt in statements(sql):
                        conn.execute(text(stmt))
                # Not atomic with the apply above, by necessity. A crash in
                # between leaves the ledger behind, not ahead: the file applies
                # again next run, which is the safe direction.
                if fp_query is not None:
                    with engine.begin() as conn:
                        conn.execute(text(LEDGER_UPSERT),
                                     {"filename": path.name, "sha256": digest,
                                      "fingerprint": read_fingerprint(engine, fp_query)})
            else:
                # One transaction per file: a half-applied view is worse than
                # an unapplied one. The ledger write rides in the SAME
                # transaction, so a rolled-back apply records nothing.
                with engine.begin() as conn:
                    conn.execute(text(sql))
                    if fp_query is not None:
                        fp_after = conn.execute(text(fp_query)).first()
                        conn.execute(text(LEDGER_UPSERT),
                                     {"filename": path.name, "sha256": digest,
                                      "fingerprint": None if fp_after is None
                                      else fp_after[0]})
            applied += 1
            logger.info(f"Applied {path.name}")
        except Exception:
            logger.exception(f"FAILED applying {path.name}")
            return 2

    if seed:
        logger.info("Seeding done.")
    else:
        logger.info(f"{applied} SQL file(s) applied, {skipped} unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

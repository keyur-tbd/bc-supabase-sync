"""
utils/schema_helper.py

Turns raw Business Central OData JSON records into Postgres-safe rows and
infers a CREATE TABLE schema from a sample of those rows.

Design choices (unchanged from the MySQL version):
- Simple nested objects (e.g. {"Address": {"City": "..."}}) are flattened
  into Address_City style columns.
- Lists / nested arrays are preserved as-is and stored as JSON columns,
  since BC sub-collections can be arbitrarily shaped and lossy flattening
  would hide data.
- Column types are inferred conservatively from sampled values: if any
  sampled value forces a wider type (e.g. one row has a decimal where
  others have ints), the wider type wins.

Postgres-specific changes:
- All identifiers are double-quoted, so mixed-case column names survive
  (Postgres folds unquoted identifiers to lower case; MySQL did not).
- Strings map to TEXT rather than VARCHAR(n). In Postgres TEXT and VARCHAR
  have identical storage and speed, so there is no reason to impose a
  width, and dropping it removes an entire class of failure: a value that
  outgrows its inferred width can no longer break a batch. The MySQL
  version needed VARCHAR-widening logic for exactly that; here widening is
  only needed to promote columns on tables migrated over from MySQL.
- DATETIME -> TIMESTAMPTZ, DECIMAL -> NUMERIC, TINYINT(1) -> BOOLEAN,
  JSON -> JSONB.
- The identifier length cap is 63 bytes (Postgres NAMEDATALEN-1) rather
  than MySQL's 64.
- The reserved-word list and its "_field" suffixing are kept EXACTLY as the
  MySQL version had them. They are redundant now (a quoted identifier may
  be a reserved word) but keeping them means column names match the old
  MySQL database character for character, so migrated data lines up.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from typing import Any, Iterable

from dateutil import parser as _dateutil_parser

# Kept under the original name and content deliberately - see module docstring.
MYSQL_RESERVED = {
    "key", "order", "group", "table", "index", "select", "where", "from",
    "join", "primary", "foreign", "default", "check", "values", "delete",
    "update", "insert", "create", "drop", "alter", "rank", "row",
}

_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

PG_MAX_IDENTIFIER_LEN = 63

# Bookkeeping columns appended to every synced table.
SYNCED_AT_COL = "_synced_at"
RAW_JSON_COL = "_raw_json"


def _lowercase_enabled() -> bool:
    """Read at call time (not import time) so callers and tests can flip it."""
    return os.getenv("PG_LOWERCASE_COLUMNS", "false").lower() == "true"


def quote_ident(name: str) -> str:
    """Double-quotes a Postgres identifier, escaping any embedded quotes."""
    return '"' + str(name).replace('"', '""') + '"'


def qualified_table(schema: str, table_name: str) -> str:
    return f"{quote_ident(schema)}.{quote_ident(table_name)}"


def normalize_column_name(name: str) -> str:
    """Sanitizes a field name into a safe Postgres column identifier."""
    name = re.sub(r"[^0-9a-zA-Z_]+", "_", str(name)).strip("_")
    if not name:
        name = "field"
    if name[0].isdigit():
        name = f"f_{name}"
    if name.lower() in MYSQL_RESERVED:
        name = f"{name}_field"
    name = name[:PG_MAX_IDENTIFIER_LEN]
    return name.lower() if _lowercase_enabled() else name


def normalize_pk(primary_key) -> list[str]:
    """Accepts a single key name OR a list of names (composite key) and
    returns the list of normalized column names. Lines / detail tables need
    composite keys (e.g. Document_No + Line_No) because the document number
    alone repeats across line rows."""
    cols = primary_key if isinstance(primary_key, (list, tuple)) else [primary_key]
    return [normalize_column_name(c) for c in cols]


def flatten_record(record: dict, parent_key: str = "", sep: str = "_") -> dict:
    """
    Flattens one level of nested dict fields. Lists (including lists of
    dicts) are left intact for the caller to JSON-encode, since flattening
    arbitrary-length arrays into columns doesn't make sense relationally.
    """
    flat: dict[str, Any] = {}
    for key, value in record.items():
        # Skip OData metadata noise
        if key.startswith("@odata"):
            continue
        new_key = f"{parent_key}{sep}{key}" if parent_key else key
        if isinstance(value, dict):
            flat.update(flatten_record(value, new_key, sep=sep))
        else:
            flat[new_key] = value
    return flat


def prepare_row(record: dict) -> tuple[dict, set[str]]:
    """Flattens a record and JSON-encodes any remaining list/dict values,
    returning (row, json_columns) where row is column_name -> python scalar
    ready for insertion, and json_columns is the set of normalized column
    names that were JSON-encoded (so schema inference can type them
    correctly even though, by this point, their value is just a string)."""
    flat = flatten_record(record)
    row: dict[str, Any] = {}
    json_columns: set[str] = set()
    for key, value in flat.items():
        col = normalize_column_name(key)
        if isinstance(value, (list, dict)):
            row[col] = json.dumps(value, default=str)
            json_columns.add(col)
        else:
            row[col] = _coerce_temporal_strings(value)
    return row, json_columns


def _looks_like_datetime(value: str) -> bool:
    return bool(_DATETIME_RE.match(value))


def _looks_like_date(value: str) -> bool:
    return bool(_DATE_RE.match(value))


def _parse_iso_datetime(value: str) -> datetime | None:
    """Parses BC's OData datetime strings (which use a trailing 'Z' and
    sometimes more than 6 fractional-second digits - both of which trip up
    datetime.fromisoformat) into a real datetime object the driver knows how
    to bind correctly. Returns None if parsing fails for any reason.

    The parsed value keeps its UTC tzinfo, which is why datetime columns are
    TIMESTAMPTZ here: Postgres then stores the instant unambiguously instead
    of silently reinterpreting it in the session time zone."""
    try:
        return _dateutil_parser.isoparse(value)
    except (ValueError, OverflowError):
        return None


def _parse_iso_date(value: str) -> date | None:
    try:
        return _dateutil_parser.isoparse(value).date()
    except (ValueError, OverflowError):
        return None


def _coerce_temporal_strings(value: Any) -> Any:
    """If `value` is a string that looks like an ISO date/datetime, convert
    it to a real date/datetime object; otherwise return it unchanged. This
    must happen before the value reaches the driver, since Postgres will not
    implicitly parse a text parameter into a date/timestamp column."""
    if not isinstance(value, str):
        return value
    s = value.strip()
    if _looks_like_datetime(s):
        parsed = _parse_iso_datetime(s)
        return parsed if parsed is not None else value
    if _looks_like_date(s):
        parsed = _parse_iso_date(s)
        return parsed if parsed is not None else value
    return value


def infer_column_type(values: Iterable[Any]) -> str:
    """
    Infers a Postgres column type from a sample of values for one column.
    Falls back progressively to a wider type as conflicting values are seen.
    """
    has_bool = has_int = has_float = has_datetime = has_date = has_json = False
    has_str = False
    saw_any = False

    for v in values:
        if v is None:
            continue
        saw_any = True
        if isinstance(v, bool):
            has_bool = True
        elif isinstance(v, int):
            has_int = True
        elif isinstance(v, float):
            has_float = True
        elif isinstance(v, (dict, list)):
            has_json = True
        elif isinstance(v, (datetime,)):
            has_datetime = True
        elif isinstance(v, (date,)):
            has_date = True
        elif isinstance(v, str):
            s = v.strip()
            if _looks_like_datetime(s):
                has_datetime = True
            elif _looks_like_date(s):
                has_date = True
            else:
                has_str = True
        else:
            has_str = True

    if not saw_any:
        return "TEXT"

    # Widening priority: json > str > datetime > date > float > int > bool
    if has_json:
        return "JSONB"
    if has_str:
        # TEXT unconditionally: no width to outgrow, and identical
        # performance to VARCHAR(n) in Postgres.
        return "TEXT"
    if has_datetime:
        return "TIMESTAMPTZ"
    if has_date:
        return "DATE"
    if has_float:
        return "NUMERIC(18,4)"
    if has_int:
        return "BIGINT"
    if has_bool:
        return "BOOLEAN"
    return "TEXT"


def infer_schema(rows: list[dict], json_columns: set[str] | None = None) -> dict[str, str]:
    """
    Given a list of flattened, normalized rows (as produced by prepare_row),
    returns {column_name: postgres_type} covering the union of all columns
    seen. Columns listed in `json_columns` (originally lists/dicts before
    being JSON-encoded into strings by prepare_row) are forced to JSONB
    regardless of what their now-stringified content looks like.
    """
    json_columns = json_columns or set()
    columns: dict[str, list[Any]] = {}
    for row in rows:
        for col, val in row.items():
            columns.setdefault(col, []).append(val)

    schema = {}
    for col, vals in columns.items():
        schema[col] = "JSONB" if col in json_columns else infer_column_type(vals)
    return schema


def build_create_table_sql(schema: str, table_name: str, columns: dict[str, str], primary_key) -> str:
    """Builds a CREATE TABLE IF NOT EXISTS statement. Primary-key column(s)
    are forced to TEXT NOT NULL regardless of inferred type, so a key is
    always reliably indexable and never rejects an over-long value.
    `primary_key` may be a single name or a list of names (composite key,
    e.g. for lines/detail tables).

    Note: because PK columns are TEXT, the service coerces primary-key
    values to str before binding - Postgres will not implicitly cast an
    integer parameter into a text column the way MySQL did.
    """
    pk_cols = normalize_pk(primary_key)
    pk_set = set(pk_cols)
    col_defs = []
    seen: set[str] = set()

    for col, col_type in columns.items():
        if col in pk_set:
            seen.add(col)
            col_defs.append(f"{quote_ident(col)} TEXT NOT NULL")
        else:
            col_defs.append(f"{quote_ident(col)} {col_type}")

    # Prepend any PK columns that weren't present in the sampled data,
    # preserving the configured key order.
    for col in reversed(pk_cols):
        if col not in seen:
            col_defs.insert(0, f"{quote_ident(col)} TEXT NOT NULL")

    # Bookkeeping columns for observability. Postgres has no
    # "ON UPDATE CURRENT_TIMESTAMP", so _synced_at is refreshed explicitly
    # in the upsert's DO UPDATE clause rather than by the column default.
    col_defs.append(f"{quote_ident(SYNCED_AT_COL)} TIMESTAMPTZ NOT NULL DEFAULT now()")
    col_defs.append(f"{quote_ident(RAW_JSON_COL)} JSONB")

    cols_sql = ",\n  ".join(col_defs)
    pk_clause = ", ".join(quote_ident(c) for c in pk_cols)
    return (
        f"CREATE TABLE IF NOT EXISTS {qualified_table(schema, table_name)} (\n"
        f"  {cols_sql},\n"
        f"  PRIMARY KEY ({pk_clause})\n"
        f");"
    )

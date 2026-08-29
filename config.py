"""
config.py

Centralized configuration loaded from environment variables (.env).
No credentials or tenant-specific values are hardcoded anywhere in the
codebase — everything flows through here.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent
load_dotenv(ROOT_DIR / ".env")

WEB_SERVICES_CONFIG_PATH = ROOT_DIR / "config" / "web_services.json"


class ConfigError(Exception):
    pass


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ConfigError(f"Required environment variable '{name}' is not set.")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.getenv(name, default)


@dataclass(frozen=True)
class BCConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    environment: str
    company_id: str  # company name OR GUID, as configured in BC
    api_base: str = "https://api.businesscentral.dynamics.com/v2.0"
    scope: str = "https://api.businesscentral.dynamics.com/.default"
    request_timeout_seconds: int = 300
    # Server-driven page size, sent via the `Prefer: odata.maxpagesize`
    # header. Unlike $top (which BC treats as a hard total cap and which
    # then suppresses @odata.nextLink), maxpagesize bounds each PAGE while
    # still emitting a nextLink for the remainder — so the full dataset is
    # retrieved across many pages.
    #
    # BC caps this at 20000 server-side: asking for 30000/50000/100000 all
    # return exactly 20000 rows (measured 2026-08-29). Per-row cost, not row
    # count, is what matters — a wide page (Posted_Sales_Invoice_Excel is
    # ~3.9 KB/row) is 20x the payload of a narrow one (Customer Item
    # Reference, ~275 B/row) for the same page size. Almost all of the
    # speed-up lands between 2000 and 5000 (per-request overhead is
    # amortised); beyond that the per-row rate is flat while the cost of a
    # timeout/retry — which re-fetches the whole page — keeps growing.
    # Hence 5000 as the default, raised per service in web_services.json
    # ("max_page_size") for narrow tables where a big page is still cheap.
    max_page_size: int = 5000
    # Default trailing re-scan buffer (days) for incremental syncs that key
    # off a business date (e.g. Posting_Date), to catch back-dated postings.
    # Overridable per service via "lookback_days" in web_services.json.
    default_lookback_days: int = 7

    @property
    def token_url(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token"

    def odata_base_url(self) -> str:
        """Base OData v4 URL up to and including the Company(...) segment."""
        # Must be quote(), NOT quote_plus(): this is a URL PATH segment, where
        # a space is %20. quote_plus() emits "+", which is only interpreted as
        # a space inside a query string - Business Central takes it literally
        # and reports Internal_DataNotFoundFilter / company does not exist.
        company_segment = quote(f"'{self.company_id}'", safe="")
        return f"{self.api_base}/{self.tenant_id}/{self.environment}/ODataV4/Company({company_segment})"

    def api_v2_base_url(self) -> str:
        """Base URL of the standard Business Central API v2.0 (the same
        endpoints the Power BI BC connector reads). Unlike the legacy
        ODataV4 web services, entities hang off companies(<GUID>), so the
        company segment is appended by BCApiService after resolving the
        GUID - see BCApiService._company_guid()."""
        return f"{self.api_base}/{self.tenant_id}/{self.environment}/api/v2.0"


@dataclass(frozen=True)
class SupabaseConfig:
    """
    Connection settings for the Supabase Postgres database.

    Supabase exposes standard Postgres, so this sync talks to it over a
    direct Postgres connection (psycopg 3) rather than the PostgREST/
    supabase-py REST client. That is deliberate: this pipeline creates and
    alters tables at runtime (DDL) and bulk-upserts thousands of rows per
    page, neither of which PostgREST is built for.

    Two ways to configure, in priority order:
      1. SUPABASE_DB_URL  - the full Postgres connection string copied from
         Supabase Dashboard > Project Settings > Database > Connection string.
      2. Discrete SUPABASE_DB_HOST / _PORT / _NAME / _USER / _PASSWORD parts.
    """
    host: str
    port: int
    database: str
    user: str
    password: str
    dsn: str = ""            # optional full connection string override
    schema: str = "public"
    sslmode: str = "require"  # Supabase requires TLS
    sslrootcert: str = ""    # path to a CA bundle, or the literal "system"
    statement_timeout_ms: int = 0   # 0 = no client-imposed limit
    # Disk guard (2026-08-28, after a disk-full outage crash-looped the DB):
    # the run refuses to start / continue once pg_database_size exceeds
    # disk_stop_pct % of disk_limit_gb. 0 disables the guard.
    disk_limit_gb: float = 0.0
    disk_stop_pct: int = 85
    # Tables that should NOT carry the _raw_json copy of the source record.
    # That column duplicates every field the typed columns already hold; at
    # ~1KB/row it is ~90% of a wide table's on-disk size, which is fine for
    # thousands of rows and ruinous for millions. Exclude the high-volume
    # tables here and keep the safety net on the small ones.
    raw_json_exclude_tables: frozenset = frozenset()
    enable_rls: bool = True   # ENABLE ROW LEVEL SECURITY on tables we create
    use_pooler: bool = True   # disable prepared statements (PgBouncer-safe)
    pool_size: int = 5
    max_overflow: int = 5
    pool_timeout: int = 30
    pool_recycle: int = 1800

    @property
    def sqlalchemy_url(self) -> str:
        """SQLAlchemy URL using the psycopg (v3) driver."""
        if self.dsn:
            return _normalize_dsn(self.dsn, self.sslrootcert, self.sslmode)
        url = (
            f"postgresql+psycopg://{quote(self.user, safe='')}:{quote(self.password, safe='')}"
            f"@{self.host}:{self.port}/{self.database}?sslmode={self.sslmode}"
        )
        if self.sslrootcert:
            url = f"{url}&sslrootcert={quote(self.sslrootcert, safe='')}"
        return url

    @property
    def is_transaction_pooler(self) -> bool:
        """Supabase's transaction pooler listens on 6543 and does not support
        session state or server-side prepared statements."""
        return self.port == 6543 or ".pooler.supabase.com" in self.host


def _normalize_dsn(dsn: str, sslrootcert: str = "", sslmode: str = "require") -> str:
    r"""Accepts the connection string exactly as Supabase prints it
    (postgresql://... or postgres://...) and rewrites the scheme so
    SQLAlchemy uses the psycopg 3 driver. Forces sslmode=require when no
    sslmode was supplied, and appends sslrootcert when one is configured.

    Note on sslmode=require: libpq will silently behave as if verify-ca had
    been requested IF a root CA file happens to exist on the machine
    (~/.postgresql/root.crt, or %APPDATA%\postgresql\root.crt on Windows).
    That is a documented backwards-compatibility quirk and a common cause
    of "certificate verify failed" on an otherwise correct connection
    string. Set SUPABASE_DB_SSLROOTCERT to the correct CA - or to the
    literal "system" to use the OS trust store - to resolve it explicitly
    rather than relying on whatever file is lying around."""
    parts = urlsplit(dsn)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+psycopg"
    query = parts.query

    def _add(q: str, kv: str) -> str:
        return f"{q}&{kv}" if q else kv

    if "sslmode=" not in query:
        query = _add(query, f"sslmode={sslmode or 'require'}")
    if sslrootcert and "sslrootcert=" not in query:
        query = _add(query, f"sslrootcert={quote(sslrootcert, safe='')}")
    return urlunsplit((scheme, parts.netloc, parts.path, query, parts.fragment))


def load_bc_config() -> BCConfig:
    return BCConfig(
        tenant_id=_require("BC_TENANT_ID"),
        client_id=_require("BC_CLIENT_ID"),
        client_secret=_require("BC_CLIENT_SECRET"),
        environment=_require("BC_ENVIRONMENT"),
        company_id=_require("BC_COMPANY_ID"),
        request_timeout_seconds=int(_optional("BC_REQUEST_TIMEOUT_SECONDS", "300")),
        max_page_size=int(_optional("BC_MAXPAGESIZE", "5000")),
        default_lookback_days=int(_optional("BC_LOOKBACK_DAYS", "7")),
    )


def load_supabase_config() -> SupabaseConfig:
    dsn = _optional("SUPABASE_DB_URL").strip()

    sslrootcert = _optional("SUPABASE_DB_SSLROOTCERT").strip()
    sslmode = _optional("SUPABASE_DB_SSLMODE", "require").strip() or "require"

    if dsn:
        parts = urlsplit(_normalize_dsn(dsn, sslrootcert, sslmode))
        host = parts.hostname or ""
        port = parts.port or 5432
        database = (parts.path or "/postgres").lstrip("/") or "postgres"
        user = parts.username or "postgres"
        password = parts.password or ""
    else:
        host = _require("SUPABASE_DB_HOST")
        port = int(_optional("SUPABASE_DB_PORT", "5432"))
        database = _optional("SUPABASE_DB_NAME", "postgres")
        user = _require("SUPABASE_DB_USER")
        password = _require("SUPABASE_DB_PASSWORD")

    return SupabaseConfig(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        dsn=dsn,
        schema=_optional("SUPABASE_DB_SCHEMA", "public"),
        sslmode=sslmode,
        sslrootcert=sslrootcert,
        statement_timeout_ms=int(_optional("SUPABASE_STATEMENT_TIMEOUT_MS", "0")),
        disk_limit_gb=float(_optional("SUPABASE_DISK_LIMIT_GB", "0") or 0),
        disk_stop_pct=int(_optional("SUPABASE_DISK_STOP_PCT", "85")),
        raw_json_exclude_tables=frozenset(
            n.strip() for n in _optional("SUPABASE_RAW_JSON_EXCLUDE_TABLES", "").split(",") if n.strip()
        ),
        enable_rls=_optional("SUPABASE_ENABLE_RLS", "true").lower() != "false",
        use_pooler=_optional("SUPABASE_USE_POOLER", "true").lower() != "false",
        pool_size=int(_optional("SUPABASE_POOL_SIZE", "5")),
        max_overflow=int(_optional("SUPABASE_MAX_OVERFLOW", "5")),
    )


def lowercase_columns() -> bool:
    """Whether to fold BC field names to lower case when building Postgres
    column identifiers.

    Default is FALSE, which preserves the exact column names the MySQL
    version produced (e.g. Posting_Date) so migrated data and existing
    downstream queries line up. The cost is that every hand-written query
    must double-quote them: select "Posting_Date" from bc_...

    Set PG_LOWERCASE_COLUMNS=true for a clean start with idiomatic,
    quote-free Postgres identifiers. Changing this on an existing database
    will create a SECOND set of columns - pick one and stay with it.
    """
    return _optional("PG_LOWERCASE_COLUMNS", "false").lower() == "true"


def load_web_services() -> list[dict]:
    """Loads the service definitions generated from the BC web services Excel
    export (see scripts/generate_config_from_excel.py)."""
    if not WEB_SERVICES_CONFIG_PATH.exists():
        raise ConfigError(
            f"{WEB_SERVICES_CONFIG_PATH} not found. Run "
            f"scripts/generate_config_from_excel.py against your BC web "
            f"services export first."
        )
    data = json.loads(WEB_SERVICES_CONFIG_PATH.read_text(encoding="utf-8"))
    services = [s for s in data.get("services", []) if s.get("enabled", True)]
    if not services:
        raise ConfigError("No enabled services found in config/web_services.json")
    return services

"""
diagnose_connection.py

Works out exactly why the Supabase connection is failing, and which SSL
settings do work, instead of changing .env and re-running app.py blind.

Usage:
    python diagnose_connection.py
    python diagnose_connection.py C:/path/to/prod-ca-2021.crt

It reads your .env the same way app.py does, prints the effective settings
(password masked), inspects the libpq root-certificate file that commonly
causes "certificate verify failed", then tries a series of SSL
configurations and reports which succeed.

Nothing is written to the database - it connects and runs SELECT 1.
"""

from __future__ import annotations

import os
import pathlib
import sys
from urllib.parse import urlsplit, urlunsplit

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def hr(title=""):
    print("\n" + "=" * 68)
    if title:
        print(title)
        print("=" * 68)


def mask(url: str) -> str:
    """Hide the password so output is safe to paste."""
    try:
        p = urlsplit(url)
        if p.password:
            netloc = p.netloc.replace(f":{p.password}@", ":***@")
            return urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
    except ValueError:
        pass
    return url


hr("1. ENVIRONMENT")
print("python    :", sys.version.split()[0])
try:
    import psycopg
    print("psycopg   :", psycopg.__version__)
except ImportError:
    sys.exit("psycopg is not installed. Run: pip install -r requirements.txt")
try:
    import sqlalchemy
    print("sqlalchemy:", sqlalchemy.__version__)
except ImportError:
    sys.exit("SQLAlchemy is not installed. Run: pip install -r requirements.txt")

hr("2. IS config.py THE UPDATED VERSION?")
cfg_src = (HERE / "config.py").read_text(encoding="utf-8", errors="replace")
has_rootcert = "SUPABASE_DB_SSLROOTCERT" in cfg_src
print("config.py supports SUPABASE_DB_SSLROOTCERT:", has_rootcert)
if not has_rootcert:
    print("  --> You are still running the OLD config.py. Replace it with the")
    print("      updated one before anything else; the .env settings below")
    print("      will be ignored until you do.")

hr("3. EFFECTIVE CONFIGURATION (from .env)")
try:
    import config as app_config
    sb = app_config.load_supabase_config()
except Exception as exc:  # noqa: BLE001
    sys.exit(f"Could not load config: {type(exc).__name__}: {exc}")

print("host              :", sb.host or "(from DSN)")
print("port              :", sb.port)
print("database          :", sb.database)
print("user              :", sb.user)
print("schema            :", sb.schema)
print("sslmode           :", sb.sslmode)
print("sslrootcert       :", sb.sslrootcert or "(not set)")
print("password present  :", bool(sb.password))
print("\nresolved URL      :", mask(sb.sqlalchemy_url))

if sb.port == 5432 and "pooler.supabase.com" not in (sb.host or sb.sqlalchemy_url):
    print("\nnote: this looks like the DIRECT connection endpoint.")
elif sb.port == 6543:
    print("\nnote: this is the TRANSACTION pooler (port 6543).")

hr("4. libpq ROOT CERTIFICATE FILE")
# libpq auto-loads this file, and its mere existence upgrades
# sslmode=require to verify-ca behaviour.
if os.name == "nt":
    default_root = pathlib.Path(os.environ.get("APPDATA", "")) / "postgresql" / "root.crt"
else:
    default_root = pathlib.Path.home() / ".postgresql" / "root.crt"
print("expected location :", default_root)
if default_root.exists():
    size = default_root.stat().st_size
    print("EXISTS            : yes  (%d bytes)" % size)
    print("  --> This is why sslmode=require is verifying certificates.")
    head = default_root.read_text(encoding="utf-8", errors="replace")[:80].strip()
    print("  first line      :", head.splitlines()[0] if head else "(empty file)")
    if size == 0:
        print("  --> The file is EMPTY, so verification can never succeed.")
else:
    print("EXISTS            : no")
    print("  --> Then the failure is not the root.crt quirk; see results below.")

# ---------------------------------------------------------------- attempts
cert_arg = sys.argv[1] if len(sys.argv) > 1 else ""
cert = cert_arg or sb.sslrootcert
if cert and cert != "system":
    cert_path = pathlib.Path(cert)
    if not cert_path.exists():
        print(f"\nWARNING: certificate not found at {cert_path}")

base = sb.sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def variant(url: str, **over) -> str:
    p = urlsplit(url)
    params = dict(
        kv.split("=", 1) for kv in p.query.split("&") if "=" in kv
    )
    for k, v in over.items():
        if v is None:
            params.pop(k, None)
        else:
            params[k] = v
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return urlunsplit((p.scheme, p.netloc, p.path, q, p.fragment))


attempts = [
    ("as currently configured", base),
    ("sslmode=require, no CA file consulted",
     variant(base, sslmode="require", sslrootcert=None)),
    ("sslmode=verify-full + system trust store",
     variant(base, sslmode="verify-full", sslrootcert="system")),
]
if cert and cert != "system":
    enc = str(pathlib.Path(cert)).replace("\\", "%5C").replace(":", "%3A")
    attempts.append(("sslmode=verify-full + supplied Supabase CA",
                     variant(base, sslmode="verify-full", sslrootcert=enc)))
    attempts.append(("sslmode=verify-ca + supplied Supabase CA",
                     variant(base, sslmode="verify-ca", sslrootcert=enc)))

hr("5. CONNECTION ATTEMPTS")
if not cert:
    print("(no certificate supplied - pass one as an argument to also test it:")
    print(" python diagnose_connection.py C:/Users/you/Downloads/prod-ca-2021.crt)\n")

winners = []
for label, url in attempts:
    try:
        with psycopg.connect(url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                cur.execute("select 1")
                cur.fetchone()
        print(f"  OK    {label}")
        winners.append((label, url))
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"  FAIL  {label}\n          {msg}")

hr("6. WHAT TO DO")
if winners:
    label, url = winners[0]
    p = urlsplit(url)
    params = dict(kv.split("=", 1) for kv in p.query.split("&") if "=" in kv)
    print(f"Working configuration: {label}\n")
    print("Put these in your .env:")
    print(f"  SUPABASE_DB_SSLMODE={params.get('sslmode', 'require')}")
    rc = params.get("sslrootcert", "")
    rc = rc.replace("%5C", "/").replace("%3A", ":")
    print(f"  SUPABASE_DB_SSLROOTCERT={rc}")
    print("\nThen run:  python app.py --setup")
else:
    print("Nothing connected. Most likely next steps:")
    print("  1. Download the certificate from the Supabase dashboard:")
    print("     Project Settings > Database > SSL Configuration")
    print("     then re-run:  python diagnose_connection.py <path-to-cert>")
    print("  2. Confirm the database password is correct (a wrong password")
    print("     reports an authentication error, not an SSL one).")
    print("  3. If the project is on the free tier, check it is not paused.")
print()

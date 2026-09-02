#!/usr/bin/env python3
"""Load Ship-to Addresses into Supabase over BC's SOAP endpoint.

WHY SOAP: BC does not serve page 300 over OData V4 in this tenant. The Web
Services row is Object Type Page, ID 300, Published, and BC even renders an
OData URL for it - but the entity set is absent from both the OData catalogue
(119 sets) and $metadata (120 entity types), and every URL form returns
"Resource not found for the segment". The same token returns 200 for
Item_Card_Excel, and SOAP serves this page fine, so it is neither auth nor
permissions. Toggling Published did not change it. See the README for the full
evidence.

This is therefore the supported way to load this table, not a stopgap. If BC's
behaviour ever changes, enable the existing `Ship_to_Address_Excel` entry in
web_services.json and delete this script and its workflow step.

WHAT IT UNBLOCKS: the register's GSTN column is the customer's registration in
the SHIP-TO state, which lives here and nowhere else the API exposes - BC
records the bill-to GSTIN on the GST ledger instead. Also the ship-to Name,
which the posted document stores with non-ASCII characters replaced by '?'.

Paging is BC's bookmark scheme: ask for setSize rows, then repeat from the last
row's Key until a short page comes back.

Runs on schedule from .github/workflows/bc_sync.yml (step "Load ship-to
addresses (SOAP)"), after the main sync. Also runnable by hand:

    python scripts/load_ship_to_address_soap.py
"""
from __future__ import annotations

import logging
import re
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import requests

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])

from config import load_bc_config, load_supabase_config
from services.auth_service import BCAuthService
from services.supabase_service import SupabaseService
from utils.logger import setup_logger
from utils.retry_helper import RetryableHTTPError, retry_with_backoff

logger = logging.getLogger("bc_sync")

SERVICE = "Ship_to_Address_Excel"
NS = f"urn:microsoft-dynamics-schemas/page/{SERVICE.lower()}"
TABLE = "bc_ship_to_address"
PK = ["Customer_No", "Code"]
PAGE = 500

# Only the fields the register needs, plus enough to identify the row.
WANTED = ["Customer_No", "Code", "Name", "Name_2", "Address", "City", "Post_Code",
          "State", "GST_Registration_No", "Ship_to_GST_Customer_Type",
          "Location_Code", "Country_Region_Code", "Last_Date_Modified",
          "Shortcut_Dimension_1_Code", "Shortcut_Dimension_2_Code"]


def endpoint(cfg) -> str:
    return (f"{cfg.api_base}/{cfg.tenant_id}/{cfg.environment}/WS/"
            f"{requests.utils.quote(cfg.company_id)}/Page/{SERVICE}")


@retry_with_backoff(max_attempts=5, base_delay=2.0)
def read_page(url, token, bookmark=None):
    """One ReadMultiple page.

    BC's SOAP endpoint returns a bare 500 intermittently - a full pass
    succeeded locally and then failed on page 2 from a GitHub runner minutes
    later, same data. Treat 5xx/429 as retryable rather than letting a blip
    fail the whole workflow; a genuine fault still surfaces after 5 attempts.
    """
    body = ('<?xml version="1.0"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            f'<ReadMultiple xmlns="{NS}"><filter/>'
            + (f"<bookmarkKey>{escape(bookmark)}</bookmarkKey>" if bookmark else "")
            + f"<setSize>{PAGE}</setSize></ReadMultiple></soap:Body></soap:Envelope>")
    try:
        r = requests.post(url, timeout=300,
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "text/xml; charset=utf-8",
                                   "SOAPAction": f"{NS}:ReadMultiple"},
                          data=body.encode("utf-8"))
    except requests.RequestException as exc:
        raise RetryableHTTPError(f"Network error calling BC SOAP: {exc}") from exc
    if r.status_code == 429 or r.status_code >= 500:
        raise RetryableHTTPError(
            f"BC SOAP returned {r.status_code} for {SERVICE}", status_code=r.status_code)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    rows, last_key = [], None
    for node in root.iter(f"{{{NS}}}{SERVICE}"):
        row = {}
        for child in node:
            tag = child.tag.split("}", 1)[-1]
            # BC repeats some option fields; first value wins.
            if tag == "Key":
                last_key = child.text
            elif tag in WANTED and tag not in row:
                row[tag] = child.text
        if row.get("Customer_No") and row.get("Code"):
            rows.append(row)
    return rows, last_key


def main() -> int:
    setup_logger()
    cfg = load_bc_config()
    token = BCAuthService(cfg).get_token()
    url = endpoint(cfg)
    db = SupabaseService(load_supabase_config())

    all_rows, bookmark, pages = [], None, 0
    while True:
        rows, last_key = read_page(url, token, bookmark)
        pages += 1
        all_rows.extend(rows)
        logger.info(f"[{SERVICE}] page {pages}: {len(rows)} row(s), {len(all_rows)} total")
        if len(rows) < PAGE or not last_key:
            break
        bookmark = last_key

    if not all_rows:
        logger.error("No ship-to addresses returned - aborting without writing.")
        return 1

    db.ensure_table(TABLE, all_rows, PK)
    ok, failed = db.upsert_rows(TABLE, all_rows, PK)
    logger.info(f"[{SERVICE}] upserted {ok} row(s), {failed} failed, into {TABLE}")
    with_gstin = sum(1 for r in all_rows if (r.get("GST_Registration_No") or "").strip())
    logger.info(f"[{SERVICE}] {with_gstin} of {len(all_rows)} carry a GST registration number")
    return 0 if not failed else 3


if __name__ == "__main__":
    sys.exit(main())

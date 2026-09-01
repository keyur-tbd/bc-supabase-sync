#!/usr/bin/env python3
"""Load Ship-to Addresses into Supabase over BC's SOAP endpoint.

WHY SOAP: `Ship_to_Address_Excel` (page 300) is published in this tenant for
SOAP only - it appears in the SOAP catalogue (78 services) but NOT in the
OData V4 catalogue (119 entity sets), and the OData URL 404s. Everything else
this project syncs is published for both. The sync framework speaks OData, so
this one table gets its own loader until the OData V4 box is ticked on that
Web Services row; after that, delete this script and flip the existing
`Ship_to_Address_Excel` entry in web_services.json to enabled.

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

import requests

sys.path.insert(0, __file__.rsplit("scripts", 1)[0])

from config import load_bc_config, load_supabase_config
from services.auth_service import BCAuthService
from services.supabase_service import SupabaseService
from utils.logger import setup_logger

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


def read_page(url, token, bookmark=None):
    body = ('<?xml version="1.0"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            f'<ReadMultiple xmlns="{NS}"><filter/>'
            + (f"<bookmarkKey>{bookmark}</bookmarkKey>" if bookmark else "")
            + f"<setSize>{PAGE}</setSize></ReadMultiple></soap:Body></soap:Envelope>")
    r = requests.post(url, timeout=300,
                      headers={"Authorization": f"Bearer {token}",
                               "Content-Type": "text/xml; charset=utf-8",
                               "SOAPAction": f"{NS}:ReadMultiple"},
                      data=body.encode("utf-8"))
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

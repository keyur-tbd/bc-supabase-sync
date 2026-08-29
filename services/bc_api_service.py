"""
services/bc_api_service.py

Fetches data from Business Central OData v4 web services, handling:
- pagination via @odata.nextLink
- automatic token refresh (checked before every page request)
- retries with backoff on timeouts / connection errors / 5xx
- 429 throttling, respecting the Retry-After header
- incremental/backfill sync via $filter on a configured date field
- resuming a previously interrupted pull from a stored nextLink

Two BC endpoint families are supported, chosen per service via the "api"
key in web_services.json:
  "odata" (default) - legacy ODataV4 web services published from BC pages
                      (.../ODataV4/Company('<name>')/<Service_Name>)
  "v2.0"            - the standard Business Central API v2.0
                      (.../api/v2.0/companies(<guid>)/<entitySet>), i.e.
                      the entities the Power BI connector reads. Fields are
                      camelCase, the key is the `id` GUID, and every
                      entity carries lastModifiedDateTime for incremental.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator

import requests

from config import BCConfig
from services.auth_service import BCAuthService
from utils.retry_helper import NonRetryableError, RetryableHTTPError, retry_with_backoff

logger = logging.getLogger("bc_sync")


_GUID_RE = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

# BC ignores anything above this in Prefer: odata.maxpagesize and silently
# returns 20000 (verified against the live API, 2026-08-29).
BC_MAX_PAGE_SIZE = 20000


class BCApiService:
    def __init__(self, bc_config: BCConfig, auth_service: BCAuthService,
                 service_apis: dict[str, str] | None = None,
                 service_page_sizes: dict[str, int] | None = None):
        self._config = bc_config
        self._auth = auth_service
        self._session = requests.Session()
        # service name -> "odata" | "v2.0"; anything unlisted is legacy odata.
        self._service_apis = {k: (v or "odata") for k, v in (service_apis or {}).items()}
        # service name -> Prefer: odata.maxpagesize override. Unlisted
        # services use BC_MAXPAGESIZE. Clamped to BC's server-side cap.
        self._service_page_sizes = {
            k: max(1, min(int(v), BC_MAX_PAGE_SIZE))
            for k, v in (service_page_sizes or {}).items() if v
        }
        self._company_guid_cache: str | None = None

    def api_kind(self, service_name: str) -> str:
        return self._service_apis.get(service_name, "odata")

    def page_size_for(self, service_name: str) -> int:
        return self._service_page_sizes.get(service_name, self._config.max_page_size)

    def _company_guid(self) -> str:
        """API v2.0 addresses the company by GUID, not display name. Accept
        either in BC_COMPANY_ID: a GUID is used as-is, a name is resolved
        once per process through the /companies collection."""
        if self._company_guid_cache:
            return self._company_guid_cache
        company_id = self._config.company_id
        if _GUID_RE.match(company_id):
            self._company_guid_cache = company_id
            return company_id
        data = self._get_page(f"{self._config.api_v2_base_url()}/companies")
        wanted = company_id.strip().casefold()
        for c in data.get("value", []):
            if str(c.get("name", "")).strip().casefold() == wanted:
                self._company_guid_cache = c["id"]
                logger.info(f"Resolved BC company '{company_id}' to id {c['id']} for API v2.0.")
                return c["id"]
        names = [c.get("name") for c in data.get("value", [])]
        raise NonRetryableError(
            f"BC_COMPANY_ID '{company_id}' not found among API v2.0 companies {names}"
        )

    def _entity_base_url(self, service_name: str) -> str:
        if self.api_kind(service_name) == "v2.0":
            return f"{self._config.api_v2_base_url()}/companies({self._company_guid()})/{service_name}"
        return f"{self._config.odata_base_url()}/{service_name}"

    def build_initial_url(self, service_name: str, odata_filter: str | None = None,
                          order_by: str | None = None) -> str:
        # NOTE: no $top here, on purpose. $top makes BC cap the result and
        # stop emitting @odata.nextLink, which silently truncates large
        # pulls. Page size is bounded instead via the `Prefer:
        # odata.maxpagesize` header (see _get_page), which keeps pagination
        # working across the full dataset.
        url = self._entity_base_url(service_name)
        params = []
        if odata_filter:
            params.append(f"$filter={odata_filter}")
        # $orderby matters for the series_key strategy: its watermark is the
        # highest key already stored, so pages must arrive in ascending key
        # order for an interrupted run to resume without skipping documents.
        if order_by:
            params.append(f"$orderby={order_by}")
        if params:
            url += "?" + "&".join(params)
        return url

    @retry_with_backoff(max_attempts=5, base_delay=2.0)
    def _get_page(self, url: str, page_size: int | None = None) -> dict:
        headers = {
            **self._auth.auth_header(),
            "Accept": "application/json",
            # Bound each page server-side while still receiving an
            # @odata.nextLink for the rest. Smaller pages also return faster,
            # which avoids read timeouts on heavy entities.
            "Prefer": f"odata.maxpagesize={page_size or self._config.max_page_size}",
        }
        try:
            resp = self._session.get(url, headers=headers, timeout=self._config.request_timeout_seconds)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise RetryableHTTPError(f"Network error calling BC API: {exc}") from exc

        if resp.status_code == 401:
            # Token may have just expired mid-run; force a fresh one and let
            # the retry decorator try again.
            logger.warning("Received 401 from BC API, forcing token refresh and retrying.")
            self._auth._access_token = None  # noqa: SLF001 - intentional forced refresh
            raise RetryableHTTPError("Unauthorized (token expired mid-run)", status_code=401)

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            raise RetryableHTTPError(
                f"BC API throttled/unavailable (status {resp.status_code})",
                status_code=resp.status_code,
                retry_after=float(retry_after) if retry_after else None,
            )

        if resp.status_code != 200:
            raise NonRetryableError(
                f"BC API request failed (status {resp.status_code}) for {url}: {resp.text[:500]}"
            )

        return resp.json()

    def fetch_pages(
        self,
        service_name: str,
        odata_filter: str | None = None,
        resume_url: str | None = None,
        order_by: str | None = None,
    ) -> Iterator[tuple[list[dict], str | None]]:
        """
        Yields (records, next_url) tuples, one per page. `next_url` is None
        on the final page. Callers should persist `next_url` after
        successfully processing each page so a crashed run can resume from
        the exact same point via `resume_url`.
        """
        url = resume_url or self.build_initial_url(service_name, odata_filter, order_by)
        page_size = self.page_size_for(service_name)
        page_num = 0
        while url:
            page_num += 1
            logger.info(f"[{service_name}] Fetching page {page_num} (max {page_size} rows)...")
            data = self._get_page(url, page_size)
            records = data.get("value", [])
            next_url = data.get("@odata.nextLink")
            logger.info(f"[{service_name}] Page {page_num}: {len(records)} record(s).")
            yield records, next_url
            url = next_url

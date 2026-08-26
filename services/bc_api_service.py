"""
services/bc_api_service.py

Fetches data from Business Central OData v4 web services, handling:
- pagination via @odata.nextLink
- automatic token refresh (checked before every page request)
- retries with backoff on timeouts / connection errors / 5xx
- 429 throttling, respecting the Retry-After header
- incremental/backfill sync via $filter on a configured date field
- resuming a previously interrupted pull from a stored nextLink
"""

from __future__ import annotations

import logging
from typing import Iterator

import requests

from config import BCConfig
from services.auth_service import BCAuthService
from utils.retry_helper import NonRetryableError, RetryableHTTPError, retry_with_backoff

logger = logging.getLogger("bc_sync")


class BCApiService:
    def __init__(self, bc_config: BCConfig, auth_service: BCAuthService):
        self._config = bc_config
        self._auth = auth_service
        self._session = requests.Session()

    def build_initial_url(self, service_name: str, odata_filter: str | None = None) -> str:
        # NOTE: no $top here, on purpose. $top makes BC cap the result and
        # stop emitting @odata.nextLink, which silently truncates large
        # pulls. Page size is bounded instead via the `Prefer:
        # odata.maxpagesize` header (see _get_page), which keeps pagination
        # working across the full dataset.
        url = f"{self._config.odata_base_url()}/{service_name}"
        if odata_filter:
            url += f"?$filter={odata_filter}"
        return url

    @retry_with_backoff(max_attempts=5, base_delay=2.0)
    def _get_page(self, url: str) -> dict:
        headers = {
            **self._auth.auth_header(),
            "Accept": "application/json",
            # Bound each page server-side while still receiving an
            # @odata.nextLink for the rest. Smaller pages also return faster,
            # which avoids read timeouts on heavy entities.
            "Prefer": f"odata.maxpagesize={self._config.max_page_size}",
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
    ) -> Iterator[tuple[list[dict], str | None]]:
        """
        Yields (records, next_url) tuples, one per page. `next_url` is None
        on the final page. Callers should persist `next_url` after
        successfully processing each page so a crashed run can resume from
        the exact same point via `resume_url`.
        """
        url = resume_url or self.build_initial_url(service_name, odata_filter)
        page_num = 0
        while url:
            page_num += 1
            logger.info(f"[{service_name}] Fetching page {page_num}...")
            data = self._get_page(url)
            records = data.get("value", [])
            next_url = data.get("@odata.nextLink")
            logger.info(f"[{service_name}] Page {page_num}: {len(records)} record(s).")
            yield records, next_url
            url = next_url

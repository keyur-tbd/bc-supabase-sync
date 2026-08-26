"""
services/auth_service.py

OAuth2 client-credentials authentication against Microsoft Entra ID (Azure AD)
for Business Central API access.

Pattern: lightweight pre-run token fetch, held in memory only for the
lifetime of the process. No tokens are ever written back to disk or to the
.env file — each run (e.g. a GitHub Actions job) authenticates fresh, the
same pattern used in the GRN automation scripts.
"""

from __future__ import annotations

import logging
import time

import requests

from config import BCConfig
from utils.retry_helper import RetryableHTTPError, retry_with_backoff

logger = logging.getLogger("bc_sync")


class AuthenticationError(Exception):
    pass


class BCAuthService:
    def __init__(self, bc_config: BCConfig):
        self._config = bc_config
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        """Returns a valid bearer token, refreshing it if missing or close
        to expiry (60s safety buffer)."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        return self._fetch_token()

    @retry_with_backoff(max_attempts=4, base_delay=2.0)
    def _fetch_token(self) -> str:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
            "scope": self._config.scope,
        }
        logger.info("Requesting new Business Central access token...")
        try:
            resp = requests.post(
                self._config.token_url,
                data=payload,
                timeout=self._config.request_timeout_seconds,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise RetryableHTTPError(f"Network error fetching token: {exc}") from exc

        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            raise RetryableHTTPError(
                f"Token endpoint returned {resp.status_code}",
                status_code=resp.status_code,
                retry_after=float(retry_after) if retry_after else None,
            )

        if resp.status_code != 200:
            raise AuthenticationError(
                f"Failed to authenticate with Business Central "
                f"(status {resp.status_code}): {resp.text[:500]}"
            )

        data = resp.json()
        token = data.get("access_token")
        expires_in = int(data.get("expires_in", 3600))
        if not token:
            raise AuthenticationError(f"Token response missing access_token: {data}")

        self._access_token = token
        self._expires_at = time.time() + expires_in
        logger.info(f"Acquired access token, valid for {expires_in}s.")
        return token

    def auth_header(self) -> dict:
        return {"Authorization": f"Bearer {self.get_token()}"}

"""Client for the NinjaOne public API (v2).

Auth is OAuth 2.0 client_credentials against ``/ws/oauth/token`` on the same
regional host as the API itself; this deployment targets ``us2.ninjarmm.com``.
The access token is cached in memory and refreshed shortly before it expires.

Ticket creation posts to ``/v2/ticketing/ticket``. The ticket body field names
below follow NinjaOne's documented ticketing schema, but verify them against
your tenant's API reference before going to production -- ticketing requires
the Ticketing module to be enabled, and ``ticketFormId`` values are per-tenant.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import NinjaOneConfig
from .errors import ApiError, AuthError
from .http import request

log = logging.getLogger(__name__)

# Refresh a little early so a token never expires mid-request.
TOKEN_EXPIRY_SKEW_SECONDS = 60


class NinjaOneClient:
    """Creates tickets in NinjaOne."""

    def __init__(
        self,
        config: NinjaOneConfig,
        client: Optional[httpx.Client] = None,
        *,
        now: Any = time.time,
    ):
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._now = now
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

    def __enter__(self) -> "NinjaOneClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- auth ------------------------------------------------------------

    def access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid bearer token, fetching or refreshing as needed."""
        if not force_refresh and self._token and self._now() < self._token_expires_at:
            return self._token

        response = request(
            self._client,
            "POST",
            self.config.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "scope": self.config.scope,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )

        if response.status_code >= 400:
            raise AuthError(
                "ninjaone",
                "Token request failed. Check NINJA_CLIENT_ID / NINJA_CLIENT_SECRET, that the "
                "API client's grant type is client_credentials, and that NINJA_SCOPE is granted",
                status=response.status_code,
                body=response.text,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise AuthError(
                "ninjaone", "Token endpoint returned a non-JSON body", body=response.text
            ) from exc

        token = payload.get("access_token")
        if not token:
            raise AuthError("ninjaone", "Token response contained no access_token", body=response.text)

        expires_in = payload.get("expires_in", 3600)
        try:
            lifetime = float(expires_in)
        except (TypeError, ValueError):
            lifetime = 3600.0

        self._token = str(token)
        self._token_expires_at = self._now() + max(lifetime - TOKEN_EXPIRY_SKEW_SECONDS, 30.0)
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    # -- requests --------------------------------------------------------

    def _call(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Issue an authenticated call, retrying once on a 401 with a fresh token."""
        url = f"{self.config.api_base}{path}"
        response = request(self._client, method, url, headers=self._headers(), **kwargs)

        if response.status_code == 401:
            log.info("NinjaOne returned 401; refreshing token and retrying once")
            self.access_token(force_refresh=True)
            response = request(self._client, method, url, headers=self._headers(), **kwargs)

        return response

    def _json_or_raise(self, response: httpx.Response, what: str) -> Any:
        if response.status_code >= 400:
            raise ApiError("ninjaone", what, status=response.status_code, body=response.text)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                "ninjaone", f"{what} returned a non-JSON body", status=response.status_code,
                body=response.text,
            ) from exc

    # -- public API ------------------------------------------------------

    def list_organizations(self, page_size: int = 10) -> List[Dict[str, Any]]:
        """Fetch organizations. Used as a lightweight authenticated smoke test."""
        response = self._call("GET", "/v2/organizations", params={"pageSize": page_size})
        payload = self._json_or_raise(response, "GET /v2/organizations failed")
        return payload if isinstance(payload, list) else []

    def create_ticket(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        """Create a ticket and return the created object (or an empty dict)."""
        response = self._call("POST", "/v2/ticketing/ticket", json=ticket)

        if response.status_code == 404:
            raise ApiError(
                "ninjaone",
                "POST /v2/ticketing/ticket not found -- the Ticketing module may not be "
                "enabled for this tenant, or the API client lacks the ticketing scope",
                status=404,
                body=response.text,
            )

        payload = self._json_or_raise(response, "POST /v2/ticketing/ticket failed")
        return payload if isinstance(payload, dict) else {}

    def ping(self) -> bool:
        """Authenticate and make one read call, to prove end-to-end access."""
        self.access_token(force_refresh=True)
        self.list_organizations(page_size=1)
        return True

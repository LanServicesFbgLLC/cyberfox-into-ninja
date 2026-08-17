"""Client for the CyberFOX AutoElevate Partner API (beta).

Defaults now match the published Partner API reference
(partner-api-docs.autoelevate.com, spec v1.0.0):

* Base URL ``https://partner-api.autoelevate.com``, events at
  ``GET /api/v1/elevation-events`` (requires the ``eventView`` scope).
* Bearer auth with a key created under the **AE-BEARER** scheme.
* Offset paging via ``take`` (max 200) and ``skip``.
* Date filtering via ``start``/``end`` in epoch milliseconds.
* A mandatory beta header on every request:
  ``X-Acknowledgment: i-understand-this-is-beta-and-may-change``.
* Responses arrive as ``{"items": [...], "totalCount": n}``.

The API is still beta, so every one of these remains overridable with an
``AE_*`` environment variable -- see .env.example. Robustness kept from the
pre-docs era:

* Paging stops on an empty page, a repeated page, or ``max_pages``, so a wrong
  parameter name degrades into "one page fetched" rather than an infinite loop.
* The events array is located by probing common envelope keys, so the client
  works whether the API returns a bare list or wraps it in ``data``/``items``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Iterator, List, Mapping, Optional

import httpx

from .config import AutoElevateConfig
from .errors import ApiError, AuthError
from .http import request
from .models import ElevationEvent, format_timestamp

log = logging.getLogger(__name__)

# Envelope keys that commonly hold the array of results.
COLLECTION_KEYS = ("data", "items", "results", "events", "records", "content")


def extract_collection(payload: Any) -> List[Dict[str, Any]]:
    """Pull the list of event objects out of whatever envelope the API used."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if not isinstance(payload, Mapping):
        return []

    for key in COLLECTION_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        # Some APIs nest one level, e.g. {"data": {"items": [...]}}.
        if isinstance(value, Mapping):
            for inner in COLLECTION_KEYS:
                nested = value.get(inner)
                if isinstance(nested, list):
                    return [item for item in nested if isinstance(item, Mapping)]

    # A single object response is treated as a one-element collection.
    if any(key in payload for key in ("id", "eventId", "event_id")):
        return [dict(payload)]
    return []


class AutoElevateClient:
    """Reads events from the AutoElevate Partner API."""

    def __init__(self, config: AutoElevateConfig, client: Optional[httpx.Client] = None):
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=config.timeout_seconds)

    def __enter__(self) -> "AutoElevateClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- auth ------------------------------------------------------------

    def _auth_headers(self) -> Dict[str, str]:
        style = self.config.auth_style
        if style == "bearer":
            return {"Authorization": f"Bearer {self.config.api_key}"}
        if style == "header":
            return {self.config.auth_header: self.config.api_key}
        return {}

    def _auth_params(self) -> Dict[str, str]:
        if self.config.auth_style == "query":
            return {self.config.auth_header: self.config.api_key}
        return {}

    # -- requests --------------------------------------------------------

    def _get(self, path: str, params: Dict[str, Any]) -> Any:
        url = f"{self.config.base_url}{path}"
        merged = dict(params)
        merged.update(self._auth_params())
        headers = {"Accept": "application/json"}
        if self.config.ack_value:
            headers[self.config.ack_header] = self.config.ack_value
        headers.update(self._auth_headers())

        response = request(self._client, "GET", url, params=merged, headers=headers)

        if response.status_code in (401, 403):
            raise AuthError(
                "autoelevate",
                "Partner API rejected the credentials. Check AE_API_KEY and AE_AUTH_STYLE",
                status=response.status_code,
                body=response.text,
            )
        if response.status_code == 404:
            raise ApiError(
                "autoelevate",
                f"No endpoint at {path}. The Partner API is in beta -- confirm AE_BASE_URL "
                "and AE_EVENTS_PATH against the current docs",
                status=404,
                body=response.text,
            )
        if response.status_code >= 400:
            raise ApiError(
                "autoelevate", f"GET {path} failed", status=response.status_code, body=response.text
            )

        try:
            return response.json()
        except ValueError as exc:
            raise ApiError(
                "autoelevate",
                f"GET {path} returned a non-JSON body",
                status=response.status_code,
                body=response.text,
            ) from exc

    # -- public API ------------------------------------------------------

    def _format_since(self, since: datetime) -> Any:
        if self.config.since_format == "epoch_ms":
            return int(since.timestamp() * 1000)
        return format_timestamp(since)

    def iter_events(self, since: Optional[datetime] = None) -> Iterator[ElevationEvent]:
        """Yield events newer than ``since``, walking pages until exhausted."""
        cfg = self.config
        seen_signatures: set = set()
        skip = 0

        for _page in range(1, cfg.max_pages + 1):
            params: Dict[str, Any] = {
                cfg.page_size_param: cfg.page_size,
                cfg.skip_param: skip,
            }
            if since is not None:
                params[cfg.since_param] = self._format_since(since)

            payload = self._get(cfg.events_path, params)
            batch = extract_collection(payload)
            if not batch:
                log.debug("AutoElevate skip=%s returned no events; stopping", skip)
                return

            # Guard against an API that ignores the skip parameter and keeps
            # handing back the same first page forever.
            signature = tuple(
                str(item.get("id") or item.get("eventId") or item.get("event_id") or idx)
                for idx, item in enumerate(batch)
            )
            if signature in seen_signatures:
                log.debug("AutoElevate skip=%s repeated a previous page; stopping", skip)
                return
            seen_signatures.add(signature)

            for item in batch:
                yield ElevationEvent.from_payload(item)

            if len(batch) < cfg.page_size:
                return
            skip += len(batch)

        log.warning(
            "Stopped after AE_MAX_PAGES (%s) pages; raise it if backlogs are being truncated",
            cfg.max_pages,
        )

    def fetch_events(self, since: Optional[datetime] = None, limit: Optional[int] = None) -> List[ElevationEvent]:
        """Collect events into a list, optionally capped at ``limit``."""
        events: List[ElevationEvent] = []
        for event in self.iter_events(since=since):
            events.append(event)
            if limit is not None and len(events) >= limit:
                break
        return events

    def ping(self) -> bool:
        """Cheap connectivity + credential check used by the ``check`` command."""
        self._get(self.config.events_path, {self.config.page_size_param: 1})
        return True

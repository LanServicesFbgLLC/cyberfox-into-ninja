"""Unit-test support library for cyberfox-into-ninja.

Spec-accurate, in-process fakes of both upstream APIs plus payload factories,
so tests (here or in downstream projects) can exercise real client code
without the network and without hand-rolling mock routes.

Built on ``httpx.MockTransport`` only -- no extra test dependencies.

Quick start::

    from cyberfox_into_ninja.testing import FakeNinjaOne, FakePartnerAPI

    api = FakePartnerAPI()
    api.add_event(computerName="WS-042")
    with api.autoelevate_client() as client:
        events = client.fetch_events()

    ninja = FakeNinjaOne()
    with ninja.ninjaone_client() as client:
        client.create_ticket({"subject": "hello"})
    assert ninja.tickets[0]["subject"] == "hello"

The fakes enforce what the real services enforce:

* ``FakePartnerAPI`` -- Bearer auth, the mandatory beta ``X-Acknowledgment``
  header (406 without it, mirroring the spec's NotAcceptableError), offset
  paging via ``take`` (capped at 200) + ``skip``, ``start``/``end`` filtering
  in epoch milliseconds, and the ``{"items": [...], "totalCount": n}``
  envelope.
* ``FakeNinjaOne`` -- OAuth client_credentials token grant, Bearer-token
  checks on API calls (401 on a stale token, so the client's refresh-and-retry
  path is exercised via :meth:`FakeNinjaOne.expire_tokens`), ticket creation,
  and the organizations listing.
"""

from __future__ import annotations

import json
import uuid
from itertools import count
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import parse_qs

import httpx

from .autoelevate import AutoElevateClient
from .config import AutoElevateConfig, NinjaOneConfig
from .ninjaone import NinjaOneClient

__all__ = [
    "BETA_ACK_VALUE",
    "FakeNinjaOne",
    "FakePartnerAPI",
    "partner_api_event",
]

BETA_ACK_VALUE = "i-understand-this-is-beta-and-may-change"

# 2026-08-16T10:00:00Z; each generated event lands one minute after the last.
_BASE_CREATED_AT_MS = 1786874400000

_event_seq: Iterator[int] = count(1)


def _deterministic_uuid(kind: str, n: int) -> str:
    """Stable, readable-in-diffs UUIDs: same test order -> same ids."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cyberfox-into-ninja/{kind}/{n}"))


def partner_api_event(**overrides: Any) -> Dict[str, Any]:
    """One elevation event exactly as the published Partner API spec shapes it.

    Top-level keys are overridable via kwargs; pass ``data={...}`` to replace
    the nested payload wholesale. Ids and timestamps are deterministic per
    process (sequential), so tests get unique but stable events.
    """
    n = next(_event_seq)
    event: Dict[str, Any] = {
        "id": _deterministic_uuid("event", n),
        "computerId": _deterministic_uuid("computer", n),
        "computerName": f"WS-{n:03d}",
        "companyName": "Acme Industries",
        "locationName": "Headquarters",
        "data": {
            "trigger": {
                "path": rf"C:\Temp\setup-{n}.exe",
                "fileName": f"setup-{n}.exe",
                "signer": "Acme Software",
            },
            "user": {"name": "jdoe"},
            "ruleThatApplied": None,
        },
        "createdAt": _BASE_CREATED_AT_MS + n * 60_000,
    }
    event.update(overrides)
    return event


def _json_response(status: int, payload: Any) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload), headers={"Content-Type": "application/json"})


def _error(status: int, message: str) -> httpx.Response:
    return _json_response(status, {"error": {"status": status, "message": message}})


class _RecordingFake:
    """Shared plumbing: request log, queued failures, client construction."""

    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []
        self._failures: List[httpx.Response] = []

    def fail_next(self, status: int, body: str = "injected failure") -> None:
        """Make the next request fail with ``status`` regardless of route."""
        self._failures.append(httpx.Response(status, text=body))

    def _pop_failure(self) -> Optional[httpx.Response]:
        return self._failures.pop(0) if self._failures else None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        failure = self._pop_failure()
        if failure is not None:
            return failure
        return self._route(request)

    def _route(self, request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise NotImplementedError

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def http_client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport())


class FakePartnerAPI(_RecordingFake):
    """In-process stand-in for the AutoElevate Partner API events endpoint."""

    def __init__(
        self,
        *,
        api_key: str = "test-ae-key",
        events: Optional[List[Dict[str, Any]]] = None,
        base_url: str = "https://partner-api.test",
        events_path: str = "/api/v1/elevation-events",
        require_ack: bool = True,
        take_cap: int = 200,
        default_take: int = 50,
    ) -> None:
        super().__init__()
        self.api_key = api_key
        self.events: List[Dict[str, Any]] = list(events or [])
        self.base_url = base_url
        self.events_path = events_path
        self.require_ack = require_ack
        self.take_cap = take_cap
        self.default_take = default_take

    # -- test-side conveniences ------------------------------------------

    def add_event(self, **overrides: Any) -> Dict[str, Any]:
        """Create, store, and return a spec-shaped event."""
        event = partner_api_event(**overrides)
        self.events.append(event)
        return event

    def add_events(self, n: int, **overrides: Any) -> List[Dict[str, Any]]:
        return [self.add_event(**overrides) for _ in range(n)]

    def config(self, **overrides: Any) -> AutoElevateConfig:
        """An AutoElevateConfig pointed at this fake, credentials included."""
        values: Dict[str, Any] = {
            "base_url": self.base_url,
            "events_path": self.events_path,
            "api_key": self.api_key,
        }
        values.update(overrides)
        return AutoElevateConfig(**values)

    def autoelevate_client(self, **config_overrides: Any) -> AutoElevateClient:
        """A real AutoElevateClient wired to this fake over MockTransport."""
        return AutoElevateClient(self.config(**config_overrides), client=self.http_client())

    # -- request handling ------------------------------------------------

    def _route(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != self.events_path:
            return _error(404, f"no route for {request.url.path}")
        if request.method != "GET":
            return _error(405, "method not allowed")

        if request.headers.get("Authorization") != f"Bearer {self.api_key}":
            return _error(401, "missing or invalid bearer token")
        if self.require_ack and request.headers.get("X-Acknowledgment") != BETA_ACK_VALUE:
            return _error(
                406, f"the X-Acknowledgment header must be sent verbatim: {BETA_ACK_VALUE}"
            )

        params = request.url.params
        try:
            take = int(params.get("take", self.default_take))
            skip = int(params.get("skip", 0))
        except ValueError:
            return _error(400, "take and skip must be integers")
        if take < 1 or skip < 0:
            return _error(400, "take must be >= 1 and skip >= 0")
        take = min(take, self.take_cap)

        matching = self.events
        if params.get("start") is not None:
            try:
                start = int(params["start"])
            except ValueError:
                return _error(400, "start must be epoch milliseconds")
            matching = [e for e in matching if e.get("createdAt", 0) >= start]
        if params.get("end") is not None:
            try:
                end = int(params["end"])
            except ValueError:
                return _error(400, "end must be epoch milliseconds")
            matching = [e for e in matching if e.get("createdAt", 0) <= end]

        page = matching[skip : skip + take]
        return _json_response(200, {"items": page, "totalCount": len(matching)})


class FakeNinjaOne(_RecordingFake):
    """In-process stand-in for the NinjaOne OAuth token + v2 API endpoints."""

    def __init__(
        self,
        *,
        client_id: str = "test-ninja-id",
        client_secret: str = "test-ninja-secret",
        host: str = "ninja.test",
        organizations: Optional[List[Dict[str, Any]]] = None,
        token_lifetime: int = 3600,
    ) -> None:
        super().__init__()
        self.client_id = client_id
        self.client_secret = client_secret
        self.host = host
        self.organizations = organizations if organizations is not None else [
            {"id": 1, "name": "Acme Industries"}
        ]
        self.token_lifetime = token_lifetime
        self.tickets: List[Dict[str, Any]] = []
        self.issued_tokens: List[str] = []
        self._token_seq = count(1)
        self._valid_tokens: set = set()

    # -- test-side conveniences ------------------------------------------

    @property
    def tokens_issued(self) -> int:
        return len(self.issued_tokens)

    def requests_for(self, path: str) -> List[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]

    def expire_tokens(self) -> None:
        """Invalidate every issued token: the next API call 401s, which should
        make the real client refresh and retry."""
        self._valid_tokens.clear()

    def config(self, **overrides: Any) -> NinjaOneConfig:
        values: Dict[str, Any] = {
            "host": self.host,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "default_organization_id": 1,
        }
        values.update(overrides)
        return NinjaOneConfig(**values)

    def ninjaone_client(self, **config_overrides: Any) -> NinjaOneClient:
        """A real NinjaOneClient wired to this fake over MockTransport."""
        return NinjaOneClient(self.config(**config_overrides), client=self.http_client())

    # -- request handling ------------------------------------------------

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/ws/oauth/token" and request.method == "POST":
            return self._token_grant(request)
        if path == "/v2/organizations" and request.method == "GET":
            return self._authenticated(request) or _json_response(200, self.organizations)
        if path == "/v2/ticketing/ticket" and request.method == "POST":
            return self._authenticated(request) or self._create_ticket(request)
        return _error(404, f"no route for {request.method} {path}")

    def _token_grant(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("grant_type") != "client_credentials":
            return _error(400, "grant_type must be client_credentials")
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return _error(401, "invalid client credentials")
        token = f"tok-{next(self._token_seq)}"
        self._valid_tokens.add(token)
        self.issued_tokens.append(token)
        return _json_response(
            200, {"access_token": token, "token_type": "bearer", "expires_in": self.token_lifetime}
        )

    def _authenticated(self, request: httpx.Request) -> Optional[httpx.Response]:
        """Return an error response for a bad token, or None to proceed."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[len("Bearer "):] not in self._valid_tokens:
            return _error(401, "invalid or expired token")
        return None

    def _create_ticket(self, request: httpx.Request) -> httpx.Response:
        try:
            body = json.loads(request.content.decode() or "{}")
        except ValueError:
            return _error(400, "ticket body must be JSON")
        ticket = dict(body)
        ticket["id"] = len(self.tickets) + 1
        self.tickets.append(ticket)
        return _json_response(200, ticket)

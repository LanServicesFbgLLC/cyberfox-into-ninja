from __future__ import annotations

import httpx
import pytest
import respx

from cyberfox_into_ninja.errors import ApiError, AuthError
from cyberfox_into_ninja.ninjaone import NinjaOneClient

TOKEN_URL = "https://us2.ninjarmm.com/ws/oauth/token"
TICKET_URL = "https://us2.ninjarmm.com/v2/ticketing/ticket"
ORGS_URL = "https://us2.ninjarmm.com/v2/organizations"


class FakeClock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


@respx.mock
def test_token_request_uses_client_credentials(ninja_config):
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )

    with NinjaOneClient(ninja_config) as client:
        assert client.access_token() == "tok-1"

    body = route.calls[0].request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "client_id=ninja-id" in body
    assert "client_secret=ninja-secret" in body


@respx.mock
def test_token_is_cached_until_it_nears_expiry(ninja_config):
    clock = FakeClock()
    route = respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 300})
    )

    with NinjaOneClient(ninja_config, now=clock) as client:
        client.access_token()
        client.access_token()
        assert route.call_count == 1

        # 300s lifetime minus the 60s skew -> refresh due at t+240.
        clock.now += 241
        client.access_token()
        assert route.call_count == 2


@respx.mock
def test_token_failure_raises_actionable_auth_error(ninja_config):
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, text="invalid_client"))

    with NinjaOneClient(ninja_config) as client:
        with pytest.raises(AuthError, match="NINJA_CLIENT_ID"):
            client.access_token()


@respx.mock
def test_create_ticket_posts_payload_and_returns_created(ninja_config):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
    )
    route = respx.post(TICKET_URL).mock(return_value=httpx.Response(201, json={"id": 991}))

    with NinjaOneClient(ninja_config) as client:
        created = client.create_ticket({"clientId": 7, "subject": "hi"})

    assert created == {"id": 991}
    assert route.calls[0].request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
def test_401_on_api_call_refreshes_token_and_retries_once(ninja_config):
    respx.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "stale", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600}),
        ]
    )
    ticket = respx.post(TICKET_URL)
    ticket.side_effect = [
        httpx.Response(401, text="expired"),
        httpx.Response(201, json={"id": 5}),
    ]

    with NinjaOneClient(ninja_config) as client:
        created = client.create_ticket({"clientId": 7})

    assert created == {"id": 5}
    assert ticket.call_count == 2
    assert ticket.calls[1].request.headers["Authorization"] == "Bearer fresh"


@respx.mock
def test_ticketing_404_explains_module_requirement(ninja_config):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx.post(TICKET_URL).mock(return_value=httpx.Response(404, text="not found"))

    with NinjaOneClient(ninja_config) as client:
        with pytest.raises(ApiError, match="Ticketing module"):
            client.create_ticket({"clientId": 7})


@respx.mock
def test_retries_on_429_then_succeeds(ninja_config, monkeypatch):
    monkeypatch.setattr("cyberfox_into_ninja.http.time.sleep", lambda _s: None)
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    ticket = respx.post(TICKET_URL)
    ticket.side_effect = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(201, json={"id": 12}),
    ]

    with NinjaOneClient(ninja_config) as client:
        assert client.create_ticket({"clientId": 7}) == {"id": 12}

    assert ticket.call_count == 2


@respx.mock
def test_ping_checks_token_and_a_read_endpoint(ninja_config):
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[{"id": 7, "name": "Acme"}]))

    with NinjaOneClient(ninja_config) as client:
        assert client.ping() is True

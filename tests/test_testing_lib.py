"""Tests for the cyberfox_into_ninja.testing support library.

These double as usage examples: every test drives a *real* client against the
fake, so the library is proven compatible with the production code paths.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cyberfox_into_ninja.errors import ApiError, AuthError
from cyberfox_into_ninja.testing import (
    BETA_ACK_VALUE,
    FakeNinjaOne,
    FakePartnerAPI,
    partner_api_event,
)


# -- partner_api_event -----------------------------------------------------


def test_events_are_unique_but_deterministic_in_shape():
    a, b = partner_api_event(), partner_api_event()
    assert a["id"] != b["id"]
    assert a["createdAt"] < b["createdAt"]
    assert set(a) == {
        "id", "computerId", "computerName", "companyName", "locationName", "data", "createdAt",
    }
    assert a["data"]["user"] == {"name": "jdoe"}


def test_event_overrides_apply():
    event = partner_api_event(companyName="Globex", data={"user": {"name": "mburns"}})
    assert event["companyName"] == "Globex"
    assert event["data"] == {"user": {"name": "mburns"}}


# -- FakePartnerAPI --------------------------------------------------------


def test_real_client_fetches_events_from_fake():
    api = FakePartnerAPI()
    api.add_event(computerName="WS-FINANCE-04")

    with api.autoelevate_client() as client:
        events = client.fetch_events()

    assert len(events) == 1
    assert events[0].computer_name == "WS-FINANCE-04"
    assert events[0].user_name == "jdoe"


def test_fake_paginates_with_take_and_skip():
    api = FakePartnerAPI()
    api.add_events(5)

    with api.autoelevate_client(page_size=2) as client:
        events = client.fetch_events()

    assert len(events) == 5
    # 2 + 2 + 1: the short page ends the walk.
    assert [r.url.params["skip"] for r in api.requests] == ["0", "2", "4"]


def test_fake_caps_take_and_client_still_gets_everything():
    """The real API caps take at 200; totalCount lets the client keep going."""
    api = FakePartnerAPI(take_cap=2)
    api.add_events(3)

    with api.autoelevate_client(page_size=100) as client:
        events = client.fetch_events()

    assert len(events) == 3


def test_fake_filters_by_start():
    api = FakePartnerAPI()
    old = api.add_event(createdAt=1000)
    new = api.add_event(createdAt=2_000_000_000_000)

    since = datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
    with api.autoelevate_client() as client:
        events = client.fetch_events(since=since)

    assert [e.event_id for e in events] == [new["id"]]
    assert old["id"] not in {e.event_id for e in events}


def test_fake_rejects_bad_api_key():
    api = FakePartnerAPI()
    with api.autoelevate_client(api_key="wrong") as client:
        with pytest.raises(AuthError):
            client.fetch_events()


def test_fake_rejects_missing_ack_header():
    api = FakePartnerAPI()
    api.add_event()
    with api.autoelevate_client(ack_value="") as client:
        with pytest.raises(ApiError) as excinfo:
            client.fetch_events()
    assert excinfo.value.status == 406
    assert BETA_ACK_VALUE in str(excinfo.value.body)


def test_fake_ack_check_can_be_disabled():
    api = FakePartnerAPI(require_ack=False)
    api.add_event()
    with api.autoelevate_client(ack_value="") as client:
        assert len(client.fetch_events()) == 1


def test_fail_next_injects_one_error():
    # 400 is not retried by the http layer, so a single injected failure
    # surfaces (a 5xx would be transparently retried and absorbed).
    api = FakePartnerAPI()
    api.add_event()
    api.fail_next(400)

    with api.autoelevate_client() as client:
        with pytest.raises(ApiError):
            client.fetch_events()
        assert len(client.fetch_events()) == 1  # back to normal


def test_injected_500_is_absorbed_by_client_retry():
    api = FakePartnerAPI()
    api.add_event()
    api.fail_next(500)

    with api.autoelevate_client() as client:
        assert len(client.fetch_events()) == 1


# -- FakeNinjaOne ----------------------------------------------------------


def test_real_client_authenticates_and_creates_ticket():
    ninja = FakeNinjaOne()

    with ninja.ninjaone_client() as client:
        created = client.create_ticket({"subject": "AutoElevate: setup.exe"})

    assert created["id"] == 1
    assert ninja.tickets == [{"subject": "AutoElevate: setup.exe", "id": 1}]
    assert ninja.tokens_issued == 1


def test_token_is_reused_across_calls():
    ninja = FakeNinjaOne()
    with ninja.ninjaone_client() as client:
        client.create_ticket({"subject": "one"})
        client.create_ticket({"subject": "two"})
    assert ninja.tokens_issued == 1


def test_expired_token_triggers_refresh_and_retry():
    ninja = FakeNinjaOne()
    with ninja.ninjaone_client() as client:
        client.create_ticket({"subject": "one"})
        ninja.expire_tokens()
        client.create_ticket({"subject": "two"})

    assert [t["subject"] for t in ninja.tickets] == ["one", "two"]
    assert ninja.tokens_issued == 2


def test_bad_credentials_raise_auth_error():
    ninja = FakeNinjaOne()
    with ninja.ninjaone_client(client_secret="wrong") as client:
        with pytest.raises(AuthError):
            client.create_ticket({"subject": "nope"})
    assert ninja.tickets == []


def test_list_organizations_returns_configured_orgs():
    ninja = FakeNinjaOne(organizations=[{"id": 42, "name": "Globex"}])
    with ninja.ninjaone_client() as client:
        assert client.list_organizations() == [{"id": 42, "name": "Globex"}]


# -- both fakes together ---------------------------------------------------


def test_fakes_compose_for_an_end_to_end_flow():
    """Fetch from the fake Partner API, file into the fake NinjaOne."""
    api = FakePartnerAPI()
    api.add_event(computerName="WS-042")
    ninja = FakeNinjaOne()

    with api.autoelevate_client() as ae, ninja.ninjaone_client() as no:
        for event in ae.fetch_events():
            no.create_ticket({"subject": event.describe(), "organizationId": 1})

    assert len(ninja.tickets) == 1
    assert "WS-042" in ninja.tickets[0]["subject"]

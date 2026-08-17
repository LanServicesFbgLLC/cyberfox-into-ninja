from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

from cyberfox_into_ninja.autoelevate import AutoElevateClient, extract_collection
from cyberfox_into_ninja.errors import ApiError, AuthError


@pytest.mark.parametrize(
    "payload,expected_ids",
    [
        ([{"id": "a"}], ["a"]),
        ({"data": [{"id": "a"}, {"id": "b"}]}, ["a", "b"]),
        ({"items": [{"id": "a"}]}, ["a"]),
        ({"results": [{"id": "a"}]}, ["a"]),
        ({"data": {"items": [{"id": "a"}]}}, ["a"]),
        ({"id": "solo"}, ["solo"]),
        ({"unrelated": 1}, []),
        ("nope", []),
    ],
)
def test_extract_collection_handles_envelope_shapes(payload, expected_ids):
    assert [item["id"] for item in extract_collection(payload)] == expected_ids


@respx.mock
def test_fetch_events_paginates_until_short_page(ae_config, sample_event):
    route = respx.get("https://ae.test/v1/events")
    route.side_effect = [
        httpx.Response(200, json={"data": [dict(sample_event, id="e1"), dict(sample_event, id="e2")]}),
        httpx.Response(200, json={"data": [dict(sample_event, id="e3")]}),
    ]

    with AutoElevateClient(ae_config) as client:
        events = client.fetch_events()

    assert [e.event_id for e in events] == ["e1", "e2", "e3"]
    assert route.call_count == 2


@respx.mock
def test_fetch_events_sends_since_and_paging_params(ae_config, sample_event):
    route = respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(200, json={"data": [sample_event]})
    )

    since = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
    with AutoElevateClient(ae_config) as client:
        client.fetch_events(since=since)

    params = route.calls[0].request.url.params
    assert params["start"] == str(int(since.timestamp() * 1000))
    assert params["take"] == "2"
    assert params["skip"] == "0"


@respx.mock
def test_fetch_events_iso_since_format(ae_config, sample_event):
    ae_config.since_format = "iso"
    route = respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(200, json={"data": [sample_event]})
    )

    with AutoElevateClient(ae_config) as client:
        client.fetch_events(since=datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc))

    assert route.calls[0].request.url.params["start"] == "2026-08-16T09:00:00Z"


@respx.mock
def test_skip_advances_by_batch_size(ae_config, sample_event):
    route = respx.get("https://ae.test/v1/events")
    route.side_effect = [
        httpx.Response(200, json={"items": [dict(sample_event, id="e1"), dict(sample_event, id="e2")]}),
        httpx.Response(200, json={"items": [dict(sample_event, id="e3")]}),
    ]

    with AutoElevateClient(ae_config) as client:
        client.fetch_events()

    assert route.calls[0].request.url.params["skip"] == "0"
    assert route.calls[1].request.url.params["skip"] == "2"


@respx.mock
def test_beta_acknowledgment_header_sent(ae_config, sample_event):
    route = respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(200, json={"items": [sample_event]})
    )

    with AutoElevateClient(ae_config) as client:
        client.fetch_events()

    headers = route.calls[0].request.headers
    assert headers["X-Acknowledgment"] == "i-understand-this-is-beta-and-may-change"


@respx.mock
def test_acknowledgment_header_can_be_disabled(ae_config, sample_event):
    ae_config.ack_value = ""
    route = respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(200, json={"items": [sample_event]})
    )

    with AutoElevateClient(ae_config) as client:
        client.fetch_events()

    assert "X-Acknowledgment" not in route.calls[0].request.headers


@respx.mock
def test_fetch_events_stops_when_api_ignores_paging(ae_config, sample_event):
    """A beta API that ignores `page` must not spin us into an infinite loop."""
    page = [dict(sample_event, id="e1"), dict(sample_event, id="e2")]
    route = respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(200, json={"data": page})
    )

    with AutoElevateClient(ae_config) as client:
        events = client.fetch_events()

    assert [e.event_id for e in events] == ["e1", "e2"]
    assert route.call_count == 2  # first page, then the repeat that stops it


@respx.mock
def test_fetch_events_respects_limit(ae_config, sample_event):
    respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(
            200, json={"data": [dict(sample_event, id="e1"), dict(sample_event, id="e2")]}
        )
    )

    with AutoElevateClient(ae_config) as client:
        events = client.fetch_events(limit=1)

    assert [e.event_id for e in events] == ["e1"]


@respx.mock
def test_bearer_auth_header(ae_config, sample_event):
    route = respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(200, json={"data": [sample_event]})
    )

    with AutoElevateClient(ae_config) as client:
        client.fetch_events()

    assert route.calls[0].request.headers["Authorization"] == "Bearer ae-key"


@respx.mock
def test_custom_header_auth_style(ae_config, sample_event):
    ae_config.auth_style = "header"
    ae_config.auth_header = "X-Api-Key"
    route = respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(200, json={"data": [sample_event]})
    )

    with AutoElevateClient(ae_config) as client:
        client.fetch_events()

    request = route.calls[0].request
    assert request.headers["X-Api-Key"] == "ae-key"
    assert "Authorization" not in request.headers


@respx.mock
def test_query_auth_style(ae_config, sample_event):
    ae_config.auth_style = "query"
    ae_config.auth_header = "apikey"
    route = respx.get("https://ae.test/v1/events").mock(
        return_value=httpx.Response(200, json={"data": [sample_event]})
    )

    with AutoElevateClient(ae_config) as client:
        client.fetch_events()

    assert route.calls[0].request.url.params["apikey"] == "ae-key"


@respx.mock
def test_401_raises_auth_error(ae_config):
    respx.get("https://ae.test/v1/events").mock(return_value=httpx.Response(401, text="nope"))

    with AutoElevateClient(ae_config) as client:
        with pytest.raises(AuthError, match="AE_API_KEY"):
            client.fetch_events()


@respx.mock
def test_404_mentions_the_beta_paths(ae_config):
    respx.get("https://ae.test/v1/events").mock(return_value=httpx.Response(404, text="missing"))

    with AutoElevateClient(ae_config) as client:
        with pytest.raises(ApiError, match="AE_EVENTS_PATH"):
            client.fetch_events()


@respx.mock
def test_non_json_body_raises(ae_config):
    respx.get("https://ae.test/v1/events").mock(return_value=httpx.Response(200, text="<html/>"))

    with AutoElevateClient(ae_config) as client:
        with pytest.raises(ApiError, match="non-JSON"):
            client.fetch_events()

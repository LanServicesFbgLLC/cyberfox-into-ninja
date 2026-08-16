from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cyberfox_into_ninja.mapper import OrganizationResolver, build_body, build_subject, build_ticket
from cyberfox_into_ninja.models import ElevationEvent
from cyberfox_into_ninja.state import SyncState


# -- mapper --------------------------------------------------------------


def test_subject_includes_type_process_user_and_computer(sample_event):
    event = ElevationEvent.from_payload(sample_event)
    assert build_subject(event) == "[AutoElevate] elevation_request setup.exe - jdoe on WS-042"


def test_subject_falls_back_when_everything_is_missing():
    event = ElevationEvent.from_payload({"id": "x"})
    assert build_subject(event) == "[AutoElevate] event - unknown user on unknown computer"


def test_subject_is_truncated():
    event = ElevationEvent.from_payload({"id": "x", "processName": "a" * 500})
    assert len(build_subject(event)) == 200


def test_body_lists_populated_fields_and_raw_payload(sample_event):
    body = build_body(ElevationEvent.from_payload(sample_event))

    assert "Event ID" in body and "evt-1" in body
    assert "needs to install the plotter driver" in body
    assert "Raw event payload:" in body
    # Empty fields are omitted rather than shown blank.
    assert "File hash" not in body


def test_body_can_omit_raw_payload(sample_event):
    body = build_body(ElevationEvent.from_payload(sample_event), include_raw=False)
    assert "Raw event payload:" not in body


def test_build_ticket_shape(sample_event, ninja_config):
    ticket = build_ticket(ElevationEvent.from_payload(sample_event), ninja_config, organization_id=7)

    assert ticket["clientId"] == 7
    assert ticket["status"] == "NEW"
    assert ticket["priority"] == "MEDIUM"
    assert ticket["type"] == "PROBLEM"
    assert ticket["description"]["public"] is False
    assert ticket["tags"] == ["autoelevate", "elevation_request"]
    # Unset form id must not be sent, so NinjaOne applies the tenant default.
    assert "ticketFormId" not in ticket


def test_build_ticket_includes_form_id_when_set(sample_event, ninja_config):
    ninja_config.ticket_form_id = 3
    ticket = build_ticket(ElevationEvent.from_payload(sample_event), ninja_config, organization_id=7)
    assert ticket["ticketFormId"] == 3


def test_resolver_prefers_company_id_then_name_then_default():
    resolver = OrganizationResolver({"acme-1": 11, "globex": 22}, default_id=99)

    by_id = ElevationEvent.from_payload({"id": "a", "companyId": "acme-1", "companyName": "Acme Corp"})
    by_name = ElevationEvent.from_payload({"id": "b", "companyName": "Globex"})
    unknown = ElevationEvent.from_payload({"id": "c", "companyName": "Initech"})

    assert resolver.resolve(by_id) == 11
    assert resolver.resolve(by_name) == 22
    assert resolver.resolve(unknown) == 99


def test_resolver_returns_none_without_a_default():
    resolver = OrganizationResolver({}, default_id=None)
    assert resolver.resolve(ElevationEvent.from_payload({"id": "c"})) is None


def test_resolver_loads_map_from_file(tmp_path):
    path = tmp_path / "orgs.json"
    path.write_text(json.dumps({"Acme Corp": 11, "bad": "not-an-int"}), encoding="utf-8")

    resolver = OrganizationResolver.from_path(path, default_id=99)
    event = ElevationEvent.from_payload({"id": "a", "companyName": "acme corp"})

    assert resolver.resolve(event) == 11


def test_resolver_survives_a_missing_or_broken_map(tmp_path):
    missing = OrganizationResolver.from_path(tmp_path / "nope.json", default_id=5)
    assert missing.resolve(ElevationEvent.from_payload({"id": "a"})) == 5

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert OrganizationResolver.from_path(broken, default_id=5).resolve(
        ElevationEvent.from_payload({"id": "a"})
    ) == 5


# -- state ---------------------------------------------------------------


def test_state_round_trips_through_disk(tmp_path):
    path = tmp_path / "nested" / "state.json"
    state = SyncState()
    state.advance_cursor(datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc))
    state.mark_processed("evt-1")
    state.save(path)

    reloaded = SyncState.load(path)
    assert reloaded.cursor == datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
    assert reloaded.already_processed("evt-1")


def test_cursor_only_moves_forward():
    state = SyncState()
    later = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)

    state.advance_cursor(later)
    state.advance_cursor(earlier)
    state.advance_cursor(None)

    assert state.cursor == later


def test_since_falls_back_to_lookback_when_cold():
    now = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)
    assert SyncState().since(60, now=now) == now - timedelta(minutes=60)


def test_dedupe_history_is_bounded():
    state = SyncState(max_history=3)
    for index in range(5):
        state.mark_processed(f"evt-{index}")

    assert not state.already_processed("evt-0")
    assert not state.already_processed("evt-1")
    assert state.already_processed("evt-4")
    assert len(state.processed_ids) == 3


def test_corrupt_state_file_starts_clean(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ broken", encoding="utf-8")

    state = SyncState.load(path)
    assert state.cursor is None
    assert len(state.processed_ids) == 0

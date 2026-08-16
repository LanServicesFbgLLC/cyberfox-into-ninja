from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import pytest

from cyberfox_into_ninja.errors import ApiError
from cyberfox_into_ninja.mapper import OrganizationResolver
from cyberfox_into_ninja.models import ElevationEvent
from cyberfox_into_ninja.state import SyncState
from cyberfox_into_ninja.sync import SyncEngine, stable_event_id


class FakeAutoElevate:
    def __init__(self, payloads: List[Dict[str, Any]]):
        self.payloads = payloads
        self.since_values: List[Optional[datetime]] = []

    def fetch_events(self, since=None, limit=None) -> List[ElevationEvent]:
        self.since_values.append(since)
        events = [ElevationEvent.from_payload(p) for p in self.payloads]
        return events[:limit] if limit else events


class FakeNinjaOne:
    def __init__(self, fail_on: Optional[set] = None):
        self.created: List[Dict[str, Any]] = []
        self.fail_on = fail_on or set()
        self._next_id = 100

    def create_ticket(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        if ticket["subject"] in self.fail_on:
            raise ApiError("ninjaone", "boom", status=500)
        self.created.append(ticket)
        self._next_id += 1
        return {"id": self._next_id}


def make_engine(app_config, payloads, *, ninja=None, resolver=None, state=None):
    return SyncEngine(
        app_config,
        FakeAutoElevate(payloads),
        ninja or FakeNinjaOne(),
        resolver or OrganizationResolver({}, default_id=7),
        state or SyncState(),
    )


def event(id_: str, hour: int, **extra) -> Dict[str, Any]:
    payload = {
        "id": id_,
        "occurredAt": f"2026-08-16T{hour:02d}:00:00Z",
        "type": "elevation_request",
        "companyName": "Acme Corp",
        "userName": "jdoe",
        "computerName": "WS-1",
        "processName": "setup.exe",
    }
    payload.update(extra)
    return payload


def test_creates_a_ticket_per_event_and_advances_cursor(app_config):
    ninja = FakeNinjaOne()
    engine = make_engine(app_config, [event("e1", 10), event("e2", 11)], ninja=ninja)

    result = engine.run_once()

    assert result.fetched == 2
    assert result.created == 2
    assert len(ninja.created) == 2
    assert engine.state.cursor == datetime(2026, 8, 16, 11, 0, tzinfo=timezone.utc)
    assert app_config.sync.state_path.is_file()


def test_second_run_skips_already_ticketed_events(app_config):
    ninja = FakeNinjaOne()
    payloads = [event("e1", 10)]
    state = SyncState()

    make_engine(app_config, payloads, ninja=ninja, state=state).run_once()
    second = make_engine(app_config, payloads, ninja=ninja, state=state).run_once()

    assert second.created == 0
    assert second.duplicates == 1
    assert len(ninja.created) == 1


def test_state_reloaded_from_disk_still_dedupes(app_config):
    ninja = FakeNinjaOne()
    payloads = [event("e1", 10)]

    make_engine(app_config, payloads, ninja=ninja).run_once()

    reloaded = SyncState.load(app_config.sync.state_path)
    second = make_engine(app_config, payloads, ninja=ninja, state=reloaded).run_once()

    assert second.duplicates == 1
    assert len(ninja.created) == 1


def test_events_are_processed_oldest_first(app_config):
    ninja = FakeNinjaOne()
    engine = make_engine(app_config, [event("late", 15), event("early", 9)], ninja=ninja)

    engine.run_once()

    subjects = [t["description"]["body"] for t in ninja.created]
    assert "early" in subjects[0]
    assert "late" in subjects[1]


def test_event_type_filter_skips_without_ticketing(app_config):
    app_config.sync.event_types = ["approval"]
    ninja = FakeNinjaOne()
    engine = make_engine(app_config, [event("e1", 10, type="elevation_request")], ninja=ninja)

    result = engine.run_once()

    assert result.filtered == 1
    assert result.created == 0
    assert ninja.created == []
    # Filtered events still move the cursor -- they are handled, not pending.
    assert engine.state.cursor == datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)


def test_severity_filter(app_config):
    app_config.sync.severities = ["high"]
    engine = make_engine(app_config, [event("e1", 10, severity="low"), event("e2", 11, severity="High")])

    result = engine.run_once()

    assert result.filtered == 1
    assert result.created == 1


def test_unmapped_company_is_left_pending(app_config):
    ninja = FakeNinjaOne()
    engine = make_engine(
        app_config,
        [event("e1", 10)],
        ninja=ninja,
        resolver=OrganizationResolver({}, default_id=None),
    )

    result = engine.run_once()

    assert result.unmapped == 1
    assert result.created == 0
    assert result.needs_attention
    # Cursor must not advance past it, so a later run retries once mapped.
    assert engine.state.cursor is None
    assert not engine.state.already_processed("e1")


def test_failure_blocks_the_cursor_but_later_events_still_process(app_config):
    ninja = FakeNinjaOne(fail_on={"[AutoElevate] elevation_request bad.exe - jdoe on WS-1"})
    engine = make_engine(
        app_config,
        [event("e1", 10, processName="bad.exe"), event("e2", 11)],
        ninja=ninja,
    )

    result = engine.run_once()

    assert result.failed == 1
    assert result.created == 1
    # e2 succeeded, but the cursor stays behind e1 so e1 is retried next cycle.
    assert engine.state.cursor is None
    assert not engine.state.already_processed("e1")
    assert engine.state.already_processed("e2")


def test_dry_run_creates_nothing_and_does_not_persist(app_config):
    app_config.sync.dry_run = True
    ninja = FakeNinjaOne()
    engine = make_engine(app_config, [event("e1", 10)], ninja=ninja)

    result = engine.run_once()

    assert result.created == 1
    assert ninja.created == []
    assert not app_config.sync.state_path.exists()


def test_since_uses_lookback_on_cold_start_then_the_cursor(app_config):
    ae = FakeAutoElevate([event("e1", 10)])
    state = SyncState()
    engine = SyncEngine(app_config, ae, FakeNinjaOne(), OrganizationResolver({}, default_id=7), state)

    engine.run_once()
    engine.run_once()

    assert ae.since_values[1] == datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)


def test_max_events_per_run_is_passed_through(app_config):
    app_config.sync.max_events_per_run = 1
    engine = make_engine(app_config, [event("e1", 10), event("e2", 11)])

    assert engine.run_once().fetched == 1


@pytest.mark.parametrize("id_field", ["id", "eventId", "event_id"])
def test_stable_event_id_uses_the_api_id(id_field):
    ev = ElevationEvent.from_payload({id_field: "abc"})
    assert stable_event_id(ev) == "abc"


def test_stable_event_id_hashes_when_the_api_gives_none():
    payload = {"computerName": "WS-1", "userName": "jdoe"}
    first = stable_event_id(ElevationEvent.from_payload(payload))
    second = stable_event_id(ElevationEvent.from_payload(dict(reversed(list(payload.items())))))
    other = stable_event_id(ElevationEvent.from_payload({"computerName": "WS-2"}))

    assert first.startswith("sha256:")
    assert first == second  # key order must not change the identity
    assert first != other


def test_idless_events_still_dedupe(app_config):
    ninja = FakeNinjaOne()
    payloads = [{"occurredAt": "2026-08-16T10:00:00Z", "computerName": "WS-1", "companyName": "Acme"}]
    state = SyncState()

    make_engine(app_config, payloads, ninja=ninja, state=state).run_once()
    second = make_engine(app_config, payloads, ninja=ninja, state=state).run_once()

    assert second.duplicates == 1
    assert len(ninja.created) == 1

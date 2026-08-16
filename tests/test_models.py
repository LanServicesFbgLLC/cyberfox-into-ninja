from __future__ import annotations

from datetime import datetime, timezone

import pytest

from cyberfox_into_ninja.models import ElevationEvent, format_timestamp, parse_timestamp, pluck


def test_normalizes_camel_case_payload(sample_event):
    event = ElevationEvent.from_payload(sample_event)

    assert event.event_id == "evt-1"
    assert event.occurred_at == datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)
    assert event.event_type == "elevation_request"
    assert event.computer_name == "WS-042"
    assert event.user_name == "jdoe"
    assert event.company_name == "Acme Corp"
    assert event.raw == sample_event


def test_normalizes_snake_case_and_nested_payload():
    event = ElevationEvent.from_payload(
        {
            "event_id": "evt-2",
            "created_at": 1786874400,
            "computer": {"name": "SRV-01"},
            "user": {"name": "asmith"},
            "company": {"id": 42, "name": "Globex"},
            "file": {"path": "/usr/bin/thing", "sha256": "abc123"},
        }
    )

    assert event.event_id == "evt-2"
    assert event.computer_name == "SRV-01"
    assert event.user_name == "asmith"
    assert event.company_id == "42"
    assert event.company_name == "Globex"
    assert event.process_path == "/usr/bin/thing"
    assert event.file_hash == "abc123"


def test_missing_fields_become_empty_strings():
    event = ElevationEvent.from_payload({"id": "evt-3"})

    assert event.event_id == "evt-3"
    assert event.occurred_at is None
    assert event.computer_name == ""
    assert event.describe() == "event - unknown user on unknown computer"


@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-08-16T10:00:00Z", datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)),
        ("2026-08-16T10:00:00+00:00", datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)),
        (1786874400, datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)),
        (1786874400000, datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)),
        ("not a date", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_timestamp_accepts_common_formats(value, expected):
    assert parse_timestamp(value) == expected


def test_naive_timestamps_are_assumed_utc():
    parsed = parse_timestamp("2026-08-16T10:00:00")
    assert parsed == datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)


def test_format_timestamp_uses_z_suffix():
    assert format_timestamp(datetime(2026, 8, 16, 10, 0, tzinfo=timezone.utc)) == "2026-08-16T10:00:00Z"


def test_pluck_skips_empty_values():
    data = {"a": "", "b": None, "c": "found"}
    assert pluck(data, ["a", "b", "c"]) == "found"
    assert pluck(data, ["a", "b"], default="fallback") == "fallback"

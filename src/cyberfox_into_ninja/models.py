"""Normalized representation of an AutoElevate event.

The Partner API is in beta and its exact field names are not confirmed, so
normalization reads a list of candidate keys per field rather than one fixed
key. Adding a newly-discovered key name is a one-line change here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence


def dig(data: Any, path: str) -> Any:
    """Walk a dotted path through nested mappings, returning None if absent."""
    current = data
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return None
    return current


def pluck(data: Mapping[str, Any], candidates: Sequence[str], default: Any = None) -> Any:
    """Return the first candidate dotted path that resolves to a non-empty value."""
    for path in candidates:
        value = dig(data, path)
        if value is not None and value != "":
            return value
    return default


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse ISO-8601 strings or epoch seconds/milliseconds into aware datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Values past this threshold are milliseconds, not seconds.
        seconds = value / 1000.0 if value > 1e11 else float(value)
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_timestamp(value: datetime) -> str:
    """Render a datetime as a UTC ISO-8601 string with a trailing Z."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# Candidate key names per normalized field, most-likely first.
FIELD_CANDIDATES: Dict[str, List[str]] = {
    "event_id": ["id", "eventId", "event_id", "uuid", "requestId", "request_id"],
    "occurred_at": [
        "occurredAt", "occurred_at", "createdAt", "created_at", "timestamp",
        "eventTime", "event_time", "dateCreated",
    ],
    "event_type": ["type", "eventType", "event_type", "action", "category"],
    "status": ["status", "state", "decision", "result"],
    "severity": ["severity", "level", "risk", "riskLevel"],
    "computer_name": [
        "computerName", "computer_name", "machineName", "hostname",
        "computer.name", "device.name", "endpoint.name",
    ],
    "computer_id": ["computerId", "computer_id", "machineId", "computer.id", "device.id"],
    "user_name": [
        "userName", "user_name", "requestedBy", "requested_by", "user.name",
        "data.user.name", "user.username", "initiatedBy",
    ],
    "company_name": [
        "companyName", "company_name", "clientName", "organizationName",
        "company.name", "client.name", "organization.name",
    ],
    "company_id": [
        "companyId", "company_id", "clientId", "organizationId",
        "company.id", "client.id", "organization.id",
    ],
    "process_name": [
        "processName", "process_name", "fileName", "file_name",
        "data.trigger.fileName", "data.trigger.name",
        "application", "process.name", "file.name",
    ],
    "process_path": [
        "processPath", "process_path", "filePath", "file_path", "path",
        "data.trigger.path", "data.trigger.filePath",
        "process.path", "file.path",
    ],
    "publisher": [
        "publisher", "signer", "certificateSubject", "data.trigger.signer",
        "data.trigger.publisher", "file.publisher", "vendor",
    ],
    "file_hash": [
        "hash", "sha256", "fileHash", "file_hash", "data.trigger.hash",
        "data.trigger.sha256", "file.hash", "file.sha256",
    ],
    "reason": ["reason", "justification", "comment", "note", "userReason"],
    "rule_name": [
        "ruleName", "rule_name", "policyName", "data.ruleThatApplied.name",
        "rule.name", "policy.name",
    ],
    "command_line": ["commandLine", "command_line", "cmdline", "process.commandLine"],
}


@dataclass
class ElevationEvent:
    """One AutoElevate event, flattened into the fields a ticket needs."""

    event_id: str
    occurred_at: Optional[datetime] = None
    event_type: str = ""
    status: str = ""
    severity: str = ""
    computer_name: str = ""
    computer_id: str = ""
    user_name: str = ""
    company_name: str = ""
    company_id: str = ""
    process_name: str = ""
    process_path: str = ""
    publisher: str = ""
    file_hash: str = ""
    reason: str = ""
    rule_name: str = ""
    command_line: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ElevationEvent":
        values: Dict[str, Any] = {}
        for name, candidates in FIELD_CANDIDATES.items():
            values[name] = pluck(payload, candidates)

        event_id = values.pop("event_id")
        occurred_at = parse_timestamp(values.pop("occurred_at"))

        return cls(
            event_id=str(event_id) if event_id is not None else "",
            occurred_at=occurred_at,
            raw=dict(payload),
            **{key: ("" if value is None else str(value)) for key, value in values.items()},
        )

    @property
    def has_id(self) -> bool:
        return bool(self.event_id)

    def describe(self) -> str:
        """Short one-line label, used in logs and as a ticket subject fallback."""
        subject = self.process_name or self.event_type or "event"
        who = self.user_name or "unknown user"
        where = self.computer_name or "unknown computer"
        return f"{subject} - {who} on {where}"

"""Turn a normalized AutoElevate event into a NinjaOne ticket payload."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import NinjaOneConfig
from .models import ElevationEvent, format_timestamp

log = logging.getLogger(__name__)

MAX_SUBJECT_LENGTH = 200


class OrganizationResolver:
    """Maps an AutoElevate company onto a NinjaOne organization id.

    The map file is JSON: keys are AutoElevate company ids or names (matched
    case-insensitively), values are NinjaOne organization ids::

        {"Acme Corp": 12, "1f4c9d20-...": 45}
    """

    def __init__(self, mapping: Optional[Dict[str, int]] = None, default_id: Optional[int] = None):
        self._mapping = {str(key).strip().lower(): int(value) for key, value in (mapping or {}).items()}
        self._default_id = default_id

    @classmethod
    def from_path(cls, path: Optional[Path], default_id: Optional[int] = None) -> "OrganizationResolver":
        if path is None:
            return cls(default_id=default_id)
        if not path.is_file():
            log.warning("Organization map %s not found; falling back to the default org id", path)
            return cls(default_id=default_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read organization map %s (%s); using the default org id", path, exc)
            return cls(default_id=default_id)
        if not isinstance(data, dict):
            log.warning("Organization map %s is not a JSON object; using the default org id", path)
            return cls(default_id=default_id)
        cleaned: Dict[str, int] = {}
        for key, value in data.items():
            try:
                cleaned[str(key)] = int(value)
            except (TypeError, ValueError):
                log.warning("Skipping organization map entry %r -> %r (not an integer id)", key, value)
        return cls(cleaned, default_id=default_id)

    def resolve(self, event: ElevationEvent) -> Optional[int]:
        for candidate in (event.company_id, event.company_name):
            if candidate:
                found = self._mapping.get(candidate.strip().lower())
                if found is not None:
                    return found
        return self._default_id


def build_subject(event: ElevationEvent) -> str:
    """Human-readable ticket subject, trimmed to NinjaOne's practical limit."""
    parts: List[str] = ["[AutoElevate]"]
    if event.event_type:
        parts.append(event.event_type)
    if event.process_name:
        parts.append(event.process_name)

    detail: List[str] = []
    if event.user_name:
        detail.append(event.user_name)
    if event.computer_name:
        detail.append(f"on {event.computer_name}")

    subject = " ".join(parts)
    if detail:
        subject += " - " + " ".join(detail)
    if subject.strip() == "[AutoElevate]":
        subject = f"[AutoElevate] {event.describe()}"
    return subject[:MAX_SUBJECT_LENGTH]


def build_body(event: ElevationEvent, *, include_raw: bool = True) -> str:
    """Plain-text ticket body listing every populated field, plus the raw JSON."""
    rows: List[Tuple[str, str]] = [
        ("Event ID", event.event_id),
        ("Occurred", format_timestamp(event.occurred_at) if event.occurred_at else ""),
        ("Type", event.event_type),
        ("Status", event.status),
        ("Severity", event.severity),
        ("Company", event.company_name or event.company_id),
        ("Computer", event.computer_name),
        ("User", event.user_name),
        ("Process", event.process_name),
        ("Path", event.process_path),
        ("Command line", event.command_line),
        ("Publisher", event.publisher),
        ("File hash", event.file_hash),
        ("Rule", event.rule_name),
        ("Reason", event.reason),
    ]

    lines = ["AutoElevate event synced from the CyberFOX Partner API.", ""]
    width = max(len(label) for label, _ in rows)
    for label, value in rows:
        if value:
            lines.append(f"{label.ljust(width)} : {value}")

    if include_raw and event.raw:
        lines.extend(["", "Raw event payload:", json.dumps(event.raw, indent=2, sort_keys=True, default=str)])

    return "\n".join(lines)


def build_ticket(
    event: ElevationEvent,
    config: NinjaOneConfig,
    organization_id: int,
    *,
    include_raw: bool = True,
) -> Dict[str, Any]:
    """Assemble the POST /v2/ticketing/ticket body.

    Field names follow NinjaOne's documented ticketing schema. ``ticketFormId``
    is tenant-specific and is omitted when unset, in which case NinjaOne applies
    the tenant's default form.
    """
    ticket: Dict[str, Any] = {
        "clientId": organization_id,
        "subject": build_subject(event),
        "description": {
            "public": config.description_public,
            "body": build_body(event, include_raw=include_raw),
        },
        "status": config.status,
        "type": config.ticket_type,
        "priority": config.priority,
        "severity": config.severity,
    }

    if config.ticket_form_id is not None:
        ticket["ticketFormId"] = config.ticket_form_id

    tags = list(config.tags)
    if event.event_type:
        slug = event.event_type.strip().lower().replace(" ", "-")
        if slug and slug not in tags:
            tags.append(slug)
    if tags:
        ticket["tags"] = tags

    return ticket

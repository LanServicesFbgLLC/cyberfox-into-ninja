"""Orchestration: fetch AutoElevate events, file NinjaOne tickets, record progress."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .autoelevate import AutoElevateClient
from .config import AppConfig
from .errors import ApiError
from .mapper import OrganizationResolver, build_ticket
from .models import ElevationEvent
from .ninjaone import NinjaOneClient
from .state import SyncState

log = logging.getLogger(__name__)

# Events with no timestamp sort last, so a missing timestamp never drags the
# cursor backwards over events we have already handled.
_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


@dataclass
class SyncResult:
    """Per-run counters, returned so the CLI (or a monitor) can report on them."""

    fetched: int = 0
    duplicates: int = 0
    filtered: int = 0
    created: int = 0
    unmapped: int = 0
    failed: int = 0
    ticket_ids: List[str] = field(default_factory=list)
    cursor: Optional[datetime] = None

    @property
    def needs_attention(self) -> bool:
        return bool(self.failed or self.unmapped)

    def summary(self) -> str:
        return (
            f"fetched={self.fetched} created={self.created} duplicates={self.duplicates} "
            f"filtered={self.filtered} unmapped={self.unmapped} failed={self.failed}"
        )


def stable_event_id(event: ElevationEvent) -> str:
    """Return the event's own id, or a content hash when the API gave none."""
    if event.has_id:
        return event.event_id
    digest = hashlib.sha256(
        json.dumps(event.raw, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest[:32]}"


class SyncEngine:
    """Runs one poll cycle: AutoElevate in, NinjaOne tickets out."""

    def __init__(
        self,
        config: AppConfig,
        autoelevate: AutoElevateClient,
        ninjaone: NinjaOneClient,
        resolver: OrganizationResolver,
        state: SyncState,
    ):
        self.config = config
        self.autoelevate = autoelevate
        self.ninjaone = ninjaone
        self.resolver = resolver
        self.state = state

    # -- filtering -------------------------------------------------------

    def _passes_filters(self, event: ElevationEvent) -> bool:
        sync = self.config.sync
        if sync.event_types and event.event_type.strip().lower() not in sync.event_types:
            return False
        if sync.severities and event.severity.strip().lower() not in sync.severities:
            return False
        return True

    # -- main loop -------------------------------------------------------

    def run_once(self) -> SyncResult:
        sync = self.config.sync
        result = SyncResult()

        since = self.state.since(sync.initial_lookback_minutes)
        log.info("Fetching AutoElevate events since %s", since.isoformat())
        events = self.autoelevate.fetch_events(since=since, limit=sync.max_events_per_run)
        result.fetched = len(events)

        # Oldest first, so the cursor can advance incrementally and a mid-run
        # failure leaves everything after it to be retried next cycle.
        events.sort(key=lambda e: e.occurred_at or _FAR_FUTURE)

        cursor_blocked = False

        for event in events:
            event_id = stable_event_id(event)

            if self.state.already_processed(event_id):
                result.duplicates += 1
                if not cursor_blocked:
                    self.state.advance_cursor(event.occurred_at)
                continue

            if not self._passes_filters(event):
                result.filtered += 1
                self.state.mark_processed(event_id)
                if not cursor_blocked:
                    self.state.advance_cursor(event.occurred_at)
                continue

            organization_id = self.resolver.resolve(event)
            if organization_id is None:
                result.unmapped += 1
                cursor_blocked = True
                log.warning(
                    "No NinjaOne organization for AutoElevate company %r (id %r); event %s left "
                    "for a later run. Add it to the org map or set "
                    "NINJA_DEFAULT_ORGANIZATION_ID",
                    event.company_name,
                    event.company_id,
                    event_id,
                )
                continue

            ticket = build_ticket(event, self.config.ninjaone, organization_id)

            if sync.dry_run:
                result.created += 1
                log.info(
                    "[dry-run] would create ticket for %s in org %s: %s",
                    event_id,
                    organization_id,
                    ticket["subject"],
                )
                continue

            try:
                created = self.ninjaone.create_ticket(ticket)
            except ApiError as exc:
                result.failed += 1
                cursor_blocked = True
                log.error("Failed to create ticket for event %s: %s", event_id, exc)
                continue

            ticket_id = str(created.get("id", "")) if isinstance(created, dict) else ""
            result.created += 1
            if ticket_id:
                result.ticket_ids.append(ticket_id)
            self.state.mark_processed(event_id)
            if not cursor_blocked:
                self.state.advance_cursor(event.occurred_at)
            log.info(
                "Created NinjaOne ticket %s for AutoElevate event %s (%s)",
                ticket_id or "(id unknown)",
                event_id,
                event.describe(),
            )

        result.cursor = self.state.cursor

        if sync.dry_run:
            log.info("[dry-run] state not persisted (%s)", result.summary())
        else:
            self.state.save(sync.state_path)

        return result

"""Durable sync state: how far we have read, and what we have already ticketed.

The processed-id set is what keeps a rerun (or an overlapping cursor window)
from filing the same AutoElevate event as a second NinjaOne ticket.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set
from collections import deque

from .models import format_timestamp, parse_timestamp

log = logging.getLogger(__name__)


@dataclass
class SyncState:
    """Cursor plus a bounded FIFO of already-processed event ids."""

    cursor: Optional[datetime] = None
    processed_ids: Deque[str] = field(default_factory=deque)
    _processed_set: Set[str] = field(default_factory=set, repr=False)
    max_history: int = 5000

    def __post_init__(self) -> None:
        self._processed_set = set(self.processed_ids)

    # -- dedupe ----------------------------------------------------------

    def already_processed(self, event_id: str) -> bool:
        return event_id in self._processed_set

    def mark_processed(self, event_id: str) -> None:
        if not event_id or event_id in self._processed_set:
            return
        self.processed_ids.append(event_id)
        self._processed_set.add(event_id)
        while len(self.processed_ids) > self.max_history:
            evicted = self.processed_ids.popleft()
            self._processed_set.discard(evicted)

    # -- cursor ----------------------------------------------------------

    def advance_cursor(self, moment: Optional[datetime]) -> None:
        """Move the cursor forward only -- never backwards, to avoid replays."""
        if moment is None:
            return
        if self.cursor is None or moment > self.cursor:
            self.cursor = moment

    def since(self, initial_lookback_minutes: int, *, now: Optional[datetime] = None) -> datetime:
        """The lower bound for the next fetch, defaulting to a cold-start lookback."""
        if self.cursor is not None:
            return self.cursor
        reference = now or datetime.now(timezone.utc)
        return reference - timedelta(minutes=initial_lookback_minutes)

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> Dict[str, object]:
        return {
            "cursor": format_timestamp(self.cursor) if self.cursor else None,
            "processed_ids": list(self.processed_ids),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, object], max_history: int = 5000) -> "SyncState":
        raw_ids = data.get("processed_ids") or []
        ids: List[str] = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        return cls(
            cursor=parse_timestamp(data.get("cursor")),
            processed_ids=deque(ids[-max_history:]),
            max_history=max_history,
        )

    @classmethod
    def load(cls, path: Path, max_history: int = 5000) -> "SyncState":
        if not path.is_file():
            return cls(max_history=max_history)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("Could not read state file %s (%s); starting from a clean state", path, exc)
            return cls(max_history=max_history)
        if not isinstance(data, dict):
            log.warning("State file %s is not a JSON object; starting from a clean state", path)
            return cls(max_history=max_history)
        return cls.from_dict(data, max_history=max_history)

    def save(self, path: Path) -> None:
        """Write atomically, so an interrupted run cannot corrupt the cursor."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as tmp:
                json.dump(self.to_dict(), tmp, indent=2)
                tmp.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

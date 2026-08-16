"""Environment-driven configuration.

Everything the integration talks to is configurable, because the AutoElevate
Partner API is in beta and its surface is expected to move. Prefer changing an
environment variable over editing code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .errors import ConfigError

# NinjaOne serves each region from its own host. The OAuth token endpoint and
# the v2 API live on the same host.
NINJAONE_REGIONS: Dict[str, str] = {
    "us": "app.ninjarmm.com",
    "us2": "us2.ninjarmm.com",
    "eu": "eu.ninjarmm.com",
    "ca": "ca.ninjarmm.com",
    "oc": "oc.ninjarmm.com",
}


def _get(name: str, default: Optional[str] = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise ConfigError(
            f"Missing required environment variable {name}. See .env.example for the full list."
        )
    return value or ""


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_list(name: str, default: Optional[List[str]] = None) -> List[str]:
    raw = os.environ.get(name, "")
    if not raw.strip():
        return list(default or [])
    return [part.strip() for part in raw.split(",") if part.strip()]


@dataclass
class AutoElevateConfig:
    """Connection details for the CyberFOX AutoElevate Partner API (beta).

    ``base_url`` and ``events_path`` are placeholders until the Partner API
    documentation is reachable from this environment -- override both via env
    vars once the real values are confirmed.
    """

    base_url: str = "https://api.autoelevate.com"
    events_path: str = "/v1/events"
    api_key: str = ""
    # How the API key is presented. "bearer" -> Authorization: Bearer <key>,
    # "header" -> <auth_header>: <key>, "query" -> ?<auth_header>=<key>.
    auth_style: str = "bearer"
    auth_header: str = "X-Api-Key"
    page_size: int = 100
    max_pages: int = 50
    # Query parameter names the API uses for cursoring/paging.
    since_param: str = "since"
    page_param: str = "page"
    page_size_param: str = "limit"
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "AutoElevateConfig":
        cfg = cls(
            base_url=_get("AE_BASE_URL", cls.base_url).rstrip("/"),
            events_path="/" + _get("AE_EVENTS_PATH", cls.events_path).lstrip("/"),
            api_key=_get("AE_API_KEY", required=True),
            auth_style=_get("AE_AUTH_STYLE", cls.auth_style).lower(),
            auth_header=_get("AE_AUTH_HEADER", cls.auth_header),
            page_size=_get_int("AE_PAGE_SIZE", cls.page_size),
            max_pages=_get_int("AE_MAX_PAGES", cls.max_pages),
            since_param=_get("AE_SINCE_PARAM", cls.since_param),
            page_param=_get("AE_PAGE_PARAM", cls.page_param),
            page_size_param=_get("AE_PAGE_SIZE_PARAM", cls.page_size_param),
            timeout_seconds=float(_get_int("AE_TIMEOUT_SECONDS", int(cls.timeout_seconds))),
        )
        if cfg.auth_style not in {"bearer", "header", "query"}:
            raise ConfigError(
                f"AE_AUTH_STYLE must be one of bearer|header|query, got {cfg.auth_style!r}"
            )
        return cfg


@dataclass
class NinjaOneConfig:
    """Connection and ticket-defaults for NinjaOne."""

    host: str = NINJAONE_REGIONS["us2"]
    client_id: str = ""
    client_secret: str = ""
    scope: str = "monitoring management"
    # Ticket defaults. client_id here is the NinjaOne *organization* id that a
    # ticket is filed against when an event cannot be matched to one.
    default_organization_id: Optional[int] = None
    ticket_form_id: Optional[int] = None
    status: str = "NEW"
    priority: str = "MEDIUM"
    severity: str = "MODERATE"
    ticket_type: str = "PROBLEM"
    tags: List[str] = field(default_factory=lambda: ["autoelevate"])
    description_public: bool = False
    timeout_seconds: float = 30.0

    @classmethod
    def from_env(cls) -> "NinjaOneConfig":
        region = _get("NINJA_REGION", "us2").lower()
        host = _get("NINJA_HOST", "")
        if not host:
            if region not in NINJAONE_REGIONS:
                raise ConfigError(
                    f"NINJA_REGION must be one of {sorted(NINJAONE_REGIONS)} "
                    f"(or set NINJA_HOST explicitly), got {region!r}"
                )
            host = NINJAONE_REGIONS[region]

        org_id = os.environ.get("NINJA_DEFAULT_ORGANIZATION_ID", "").strip()
        form_id = os.environ.get("NINJA_TICKET_FORM_ID", "").strip()
        return cls(
            host=host.replace("https://", "").replace("http://", "").rstrip("/"),
            client_id=_get("NINJA_CLIENT_ID", required=True),
            client_secret=_get("NINJA_CLIENT_SECRET", required=True),
            scope=_get("NINJA_SCOPE", cls.scope),
            default_organization_id=int(org_id) if org_id else None,
            ticket_form_id=int(form_id) if form_id else None,
            status=_get("NINJA_TICKET_STATUS", cls.status),
            priority=_get("NINJA_TICKET_PRIORITY", cls.priority),
            severity=_get("NINJA_TICKET_SEVERITY", cls.severity),
            ticket_type=_get("NINJA_TICKET_TYPE", cls.ticket_type),
            tags=_get_list("NINJA_TICKET_TAGS", ["autoelevate"]),
            description_public=_get_bool("NINJA_DESCRIPTION_PUBLIC", cls.description_public),
            timeout_seconds=float(_get_int("NINJA_TIMEOUT_SECONDS", int(cls.timeout_seconds))),
        )

    @property
    def token_url(self) -> str:
        return f"https://{self.host}/ws/oauth/token"

    @property
    def api_base(self) -> str:
        return f"https://{self.host}"


@dataclass
class SyncConfig:
    """How often to poll, what to poll for, and where to keep the cursor."""

    state_path: Path = Path("state.json")
    # Optional JSON file mapping AutoElevate company name/id -> NinjaOne org id.
    org_map_path: Optional[Path] = None
    poll_interval_seconds: int = 300
    # On a cold start with no cursor, how far back to reach.
    initial_lookback_minutes: int = 60
    max_events_per_run: int = 500
    # Optional allow-lists. Empty means "accept everything".
    event_types: List[str] = field(default_factory=list)
    severities: List[str] = field(default_factory=list)
    dry_run: bool = False
    # Cap on remembered event ids, to keep the state file from growing forever.
    dedupe_history: int = 5000

    @classmethod
    def from_env(cls) -> "SyncConfig":
        org_map = _get("SYNC_ORG_MAP_PATH", "")
        return cls(
            state_path=Path(_get("SYNC_STATE_PATH", "state.json")),
            org_map_path=Path(org_map) if org_map else None,
            poll_interval_seconds=_get_int("SYNC_POLL_INTERVAL_SECONDS", cls.poll_interval_seconds),
            initial_lookback_minutes=_get_int(
                "SYNC_INITIAL_LOOKBACK_MINUTES", cls.initial_lookback_minutes
            ),
            max_events_per_run=_get_int("SYNC_MAX_EVENTS_PER_RUN", cls.max_events_per_run),
            event_types=[t.lower() for t in _get_list("SYNC_EVENT_TYPES")],
            severities=[s.lower() for s in _get_list("SYNC_SEVERITIES")],
            dry_run=_get_bool("SYNC_DRY_RUN", cls.dry_run),
            dedupe_history=_get_int("SYNC_DEDUPE_HISTORY", cls.dedupe_history),
        )


@dataclass
class AppConfig:
    autoelevate: AutoElevateConfig
    ninjaone: NinjaOneConfig
    sync: SyncConfig

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            autoelevate=AutoElevateConfig.from_env(),
            ninjaone=NinjaOneConfig.from_env(),
            sync=SyncConfig.from_env(),
        )


def load_dotenv(path: Path = Path(".env")) -> None:
    """Populate os.environ from a .env file without clobbering real env vars.

    Deliberately minimal -- no interpolation, no export syntax -- so the package
    keeps a single runtime dependency.
    """
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

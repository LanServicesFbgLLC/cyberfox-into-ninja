"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import List, Optional

from . import __version__
from .autoelevate import AutoElevateClient
from .config import AppConfig, load_dotenv
from .errors import CyberfoxIntoNinjaError
from .mapper import OrganizationResolver
from .ninjaone import NinjaOneClient
from .state import SyncState
from .sync import SyncEngine

log = logging.getLogger("cyberfox_into_ninja")

_shutdown = threading.Event()


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    # httpx logs every request at INFO, which drowns out our own output.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _install_signal_handlers() -> None:
    def handle(signum, _frame):  # pragma: no cover - signal path
        log.info("Received signal %s; finishing the current cycle then exiting", signum)
        _shutdown.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle)
        except (ValueError, OSError):  # pragma: no cover - non-main thread
            pass


def _build_engine(config: AppConfig, ae: AutoElevateClient, ninja: NinjaOneClient) -> SyncEngine:
    state = SyncState.load(config.sync.state_path, max_history=config.sync.dedupe_history)
    resolver = OrganizationResolver.from_path(
        config.sync.org_map_path, default_id=config.ninjaone.default_organization_id
    )
    return SyncEngine(config, ae, ninja, resolver, state)


# -- commands ------------------------------------------------------------


def cmd_check(config: AppConfig) -> int:
    """Validate credentials and connectivity against both APIs."""
    ok = True

    print(f"NinjaOne host      : {config.ninjaone.host}")
    print(f"AutoElevate base   : {config.autoelevate.base_url}{config.autoelevate.events_path}")
    print(f"State file         : {config.sync.state_path}")
    print(f"Default org id     : {config.ninjaone.default_organization_id or '(none)'}")
    print()

    with NinjaOneClient(config.ninjaone) as ninja:
        try:
            ninja.ping()
            print("NinjaOne     : OK (token acquired, organizations readable)")
        except CyberfoxIntoNinjaError as exc:
            ok = False
            print(f"NinjaOne     : FAILED - {exc}")

    with AutoElevateClient(config.autoelevate) as ae:
        try:
            ae.ping()
            print("AutoElevate  : OK (events endpoint readable)")
        except CyberfoxIntoNinjaError as exc:
            ok = False
            print(f"AutoElevate  : FAILED - {exc}")

    if config.ninjaone.default_organization_id is None and config.sync.org_map_path is None:
        ok = False
        print(
            "\nConfig       : FAILED - set NINJA_DEFAULT_ORGANIZATION_ID or SYNC_ORG_MAP_PATH, "
            "otherwise no event can be assigned to a NinjaOne organization."
        )

    return 0 if ok else 1


def cmd_run_once(config: AppConfig) -> int:
    with AutoElevateClient(config.autoelevate) as ae, NinjaOneClient(config.ninjaone) as ninja:
        engine = _build_engine(config, ae, ninja)
        result = engine.run_once()
    print(result.summary())
    return 1 if result.failed else 0


def cmd_poll(config: AppConfig) -> int:
    _install_signal_handlers()
    interval = config.sync.poll_interval_seconds
    log.info("Polling every %ss. Ctrl-C to stop.", interval)

    exit_code = 0
    with AutoElevateClient(config.autoelevate) as ae, NinjaOneClient(config.ninjaone) as ninja:
        engine = _build_engine(config, ae, ninja)
        while not _shutdown.is_set():
            try:
                result = engine.run_once()
                log.info("Cycle complete: %s", result.summary())
            except CyberfoxIntoNinjaError as exc:
                exit_code = 1
                log.error("Cycle failed: %s", exc)
            except Exception:  # pragma: no cover - keep the daemon alive
                exit_code = 1
                log.exception("Unexpected error during cycle; continuing")
            _shutdown.wait(interval)

    log.info("Stopped.")
    return exit_code


def cmd_show_state(config: AppConfig) -> int:
    state = SyncState.load(config.sync.state_path, max_history=config.sync.dedupe_history)
    payload = state.to_dict()
    print(
        json.dumps(
            {
                "path": str(config.sync.state_path),
                "exists": config.sync.state_path.is_file(),
                "cursor": payload["cursor"],
                "processed_count": len(state.processed_ids),
                "recent_processed_ids": list(state.processed_ids)[-10:],
            },
            indent=2,
        )
    )
    return 0


# -- entry point ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cyberfox-into-ninja",
        description="Poll the CyberFOX AutoElevate Partner API (beta) and file NinjaOne tickets.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--env-file", type=Path, default=Path(".env"), help="dotenv file to load (default: .env)")
    parser.add_argument("--dry-run", action="store_true", help="map and log tickets without creating them")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="verify configuration and connectivity to both APIs")
    sub.add_parser("run-once", help="run a single poll cycle and exit")
    sub.add_parser("poll", help="run cycles continuously on the configured interval")
    sub.add_parser("show-state", help="print the persisted cursor and dedupe history")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)
    load_dotenv(args.env_file)

    try:
        config = AppConfig.from_env()
    except CyberfoxIntoNinjaError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        config.sync.dry_run = True

    handlers = {
        "check": cmd_check,
        "run-once": cmd_run_once,
        "poll": cmd_poll,
        "show-state": cmd_show_state,
    }

    try:
        return handlers[args.command](config)
    except CyberfoxIntoNinjaError as exc:
        log.error("%s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

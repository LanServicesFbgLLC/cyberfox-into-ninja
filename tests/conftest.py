from __future__ import annotations

import pytest

from cyberfox_into_ninja.config import AppConfig, AutoElevateConfig, NinjaOneConfig, SyncConfig


@pytest.fixture
def ae_config() -> AutoElevateConfig:
    return AutoElevateConfig(
        base_url="https://ae.test",
        events_path="/v1/events",
        api_key="ae-key",
        page_size=2,
        max_pages=5,
    )


@pytest.fixture
def ninja_config() -> NinjaOneConfig:
    return NinjaOneConfig(
        host="us2.ninjarmm.com",
        client_id="ninja-id",
        client_secret="ninja-secret",
        default_organization_id=7,
    )


@pytest.fixture
def app_config(ae_config, ninja_config, tmp_path) -> AppConfig:
    return AppConfig(
        autoelevate=ae_config,
        ninjaone=ninja_config,
        sync=SyncConfig(state_path=tmp_path / "state.json", initial_lookback_minutes=60),
    )


@pytest.fixture
def sample_event() -> dict:
    return {
        "id": "evt-1",
        "occurredAt": "2026-08-16T10:00:00Z",
        "type": "elevation_request",
        "status": "approved",
        "severity": "high",
        "computerName": "WS-042",
        "userName": "jdoe",
        "companyName": "Acme Corp",
        "companyId": "acme-1",
        "processName": "setup.exe",
        "processPath": r"C:\Temp\setup.exe",
        "publisher": "Acme Software",
        "reason": "needs to install the plotter driver",
    }

from __future__ import annotations

import os

import httpx
import pytest
import respx

from cyberfox_into_ninja.cli import main
from cyberfox_into_ninja.config import AppConfig, NinjaOneConfig, SyncConfig, load_dotenv
from cyberfox_into_ninja.errors import ConfigError

TOKEN_URL = "https://us2.ninjarmm.com/ws/oauth/token"
ORGS_URL = "https://us2.ninjarmm.com/v2/organizations"
EVENTS_URL = "https://ae.test/api/v1/elevation-events"


@pytest.fixture
def env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("AE_", "NINJA_", "SYNC_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AE_API_KEY", "ae-key")
    monkeypatch.setenv("AE_BASE_URL", "https://ae.test")
    monkeypatch.setenv("NINJA_CLIENT_ID", "ninja-id")
    monkeypatch.setenv("NINJA_CLIENT_SECRET", "ninja-secret")
    return monkeypatch


# -- config --------------------------------------------------------------


def test_region_us2_maps_to_the_right_host(env):
    env.setenv("NINJA_REGION", "us2")
    config = NinjaOneConfig.from_env()

    assert config.host == "us2.ninjarmm.com"
    assert config.token_url == "https://us2.ninjarmm.com/ws/oauth/token"
    assert config.api_base == "https://us2.ninjarmm.com"


def test_explicit_host_overrides_region_and_strips_scheme(env):
    env.setenv("NINJA_REGION", "eu")
    env.setenv("NINJA_HOST", "https://custom.ninjarmm.com/")
    assert NinjaOneConfig.from_env().host == "custom.ninjarmm.com"


def test_unknown_region_is_rejected(env):
    env.setenv("NINJA_REGION", "mars")
    with pytest.raises(ConfigError, match="NINJA_REGION"):
        NinjaOneConfig.from_env()


def test_missing_required_var_names_itself(env):
    env.delenv("AE_API_KEY")
    with pytest.raises(ConfigError, match="AE_API_KEY"):
        AppConfig.from_env()


def test_bad_auth_style_is_rejected(env):
    env.setenv("AE_AUTH_STYLE", "magic")
    with pytest.raises(ConfigError, match="AE_AUTH_STYLE"):
        AppConfig.from_env()


def test_list_and_bool_parsing(env):
    env.setenv("NINJA_TICKET_TAGS", "autoelevate, pam ,")
    env.setenv("NINJA_DESCRIPTION_PUBLIC", "yes")
    config = NinjaOneConfig.from_env()

    assert config.tags == ["autoelevate", "pam"]
    assert config.description_public is True


def test_filters_are_lowercased(env):
    env.setenv("SYNC_EVENT_TYPES", "Elevation_Request, Approval")
    assert SyncConfig.from_env().event_types == ["elevation_request", "approval"]


def test_bad_integer_is_rejected(env):
    env.setenv("SYNC_POLL_INTERVAL_SECONDS", "soon")
    with pytest.raises(ConfigError, match="SYNC_POLL_INTERVAL_SECONDS"):
        SyncConfig.from_env()


def test_load_dotenv_does_not_clobber_real_env(tmp_path, env):
    path = tmp_path / ".env"
    path.write_text('AE_API_KEY=from-file\nNEW_VALUE="quoted"\n# comment\n\n', encoding="utf-8")

    load_dotenv(path)
    try:
        assert os.environ["AE_API_KEY"] == "ae-key"  # real env wins
        assert os.environ["NEW_VALUE"] == "quoted"
    finally:
        os.environ.pop("NEW_VALUE", None)


# -- cli -----------------------------------------------------------------


@respx.mock
def test_check_reports_ok_when_both_apis_answer(env, tmp_path, capsys):
    env.setenv("NINJA_DEFAULT_ORGANIZATION_ID", "7")
    env.setenv("SYNC_STATE_PATH", str(tmp_path / "state.json"))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[{"id": 7}]))
    respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    assert main(["--env-file", str(tmp_path / "absent.env"), "check"]) == 0

    out = capsys.readouterr().out
    assert "NinjaOne     : OK" in out
    assert "AutoElevate  : OK" in out


@respx.mock
def test_check_fails_without_an_organization_target(env, tmp_path, capsys):
    env.setenv("SYNC_STATE_PATH", str(tmp_path / "state.json"))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx.get(ORGS_URL).mock(return_value=httpx.Response(200, json=[]))
    respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    assert main(["--env-file", str(tmp_path / "absent.env"), "check"]) == 1
    assert "NINJA_DEFAULT_ORGANIZATION_ID" in capsys.readouterr().out


@respx.mock
def test_check_reports_a_failing_upstream(env, tmp_path, capsys):
    env.setenv("NINJA_DEFAULT_ORGANIZATION_ID", "7")
    env.setenv("SYNC_STATE_PATH", str(tmp_path / "state.json"))
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401, text="invalid_client"))
    respx.get(EVENTS_URL).mock(return_value=httpx.Response(200, json={"data": []}))

    assert main(["--env-file", str(tmp_path / "absent.env"), "check"]) == 1
    assert "NinjaOne     : FAILED" in capsys.readouterr().out


def test_missing_config_exits_with_code_2(env, tmp_path, capsys):
    env.delenv("NINJA_CLIENT_SECRET")

    assert main(["--env-file", str(tmp_path / "absent.env"), "check"]) == 2
    assert "NINJA_CLIENT_SECRET" in capsys.readouterr().err


@respx.mock
def test_run_once_end_to_end_creates_a_ticket(env, tmp_path, capsys):
    env.setenv("NINJA_DEFAULT_ORGANIZATION_ID", "7")
    env.setenv("SYNC_STATE_PATH", str(tmp_path / "state.json"))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx.get(EVENTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "evt-1",
                        "occurredAt": "2026-08-16T10:00:00Z",
                        "type": "elevation_request",
                        "companyName": "Acme Corp",
                    }
                ]
            },
        )
    )
    ticket = respx.post("https://us2.ninjarmm.com/v2/ticketing/ticket").mock(
        return_value=httpx.Response(201, json={"id": 55})
    )

    assert main(["--env-file", str(tmp_path / "absent.env"), "run-once"]) == 0

    assert ticket.call_count == 1
    assert ticket.calls[0].request.read().decode().count("evt-1") >= 1
    assert "created=1" in capsys.readouterr().out


@respx.mock
def test_dry_run_flag_suppresses_ticket_creation(env, tmp_path):
    env.setenv("NINJA_DEFAULT_ORGANIZATION_ID", "7")
    env.setenv("SYNC_STATE_PATH", str(tmp_path / "state.json"))
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
    )
    respx.get(EVENTS_URL).mock(
        return_value=httpx.Response(200, json={"data": [{"id": "evt-1", "companyName": "Acme"}]})
    )
    ticket = respx.post("https://us2.ninjarmm.com/v2/ticketing/ticket")

    assert main(["--env-file", str(tmp_path / "absent.env"), "--dry-run", "run-once"]) == 0

    assert ticket.call_count == 0
    assert not (tmp_path / "state.json").exists()


def test_show_state_prints_json(env, tmp_path, capsys):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"cursor": "2026-08-16T10:00:00Z", "processed_ids": ["a"]}', encoding="utf-8")
    env.setenv("SYNC_STATE_PATH", str(state_path))

    assert main(["--env-file", str(tmp_path / "absent.env"), "show-state"]) == 0

    out = capsys.readouterr().out
    assert '"processed_count": 1' in out
    assert "2026-08-16T10:00:00Z" in out

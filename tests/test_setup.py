"""Asistente offline: nunca abre navegador, autentica ni envía a plataformas reales."""
import json
import os
import sys
from types import SimpleNamespace

import pytest

from sniper import setup


def config(mode="twitch-chat"):
    return {"version": 2, "mode": mode, "twitch_user": "mi_usuario", "kick_user": "mi_cuenta",
            "psn_online_id": "MiAliasPSN", "channel": "streamer", "interval": 180,
            "raffle_users": [], "raffle_min_users": 2,
            "roi": [0.1, 0.2, 0.9, 0.8] if mode == "kick-codes" else None}


@pytest.mark.parametrize("platform,url", [("kick", "https://kick.com/STREAMER"),
                                          ("twitch", "https://www.twitch.tv/STREAMER/")])
def test_target_extracts_streamer_from_url(platform, url):
    channel, source = setup.target(url, platform)
    assert channel == "streamer" and source.endswith("/streamer")


@pytest.mark.parametrize("url", ["https://kick.com/streamer?clip=1", "https://kick.com/streamer/videos/id",
                                "https://evil.example/streamer", "https://user@kick.com/streamer", "http://kick.com/streamer"])
def test_target_rejects_non_channel_urls(url):
    with pytest.raises(ValueError):
        setup.target(url, "kick")


def test_twitch_com_is_not_accepted():
    with pytest.raises(ValueError):
        setup.target("https://twitch.com/streamer", "twitch")


def test_private_config_roundtrip_and_sensitive_fields_rejected(tmp_path):
    path = tmp_path / ".sniper-local.json"
    setup.save_config(config(), path)
    assert setup.load_config(path) == config()
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    with pytest.raises(ValueError):
        setup.save_config({**config(), "password": "not-real"}, path)
    assert setup.load_config(path) == config()


def test_chat_command_uses_streamer_not_own_username(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    cmd = setup.build_command(config(), real=True)
    assert cmd[cmd.index("--chat") + 1] == "streamer"
    assert cmd[cmd.index("--source") + 1] == "https://www.twitch.tv/streamer"
    assert "mi_usuario" not in cmd and "MiAliasPSN" not in cmd
    assert "--dry-run" not in cmd
    assert "--wait-first" in cmd


def test_preview_never_opens_browser(tmp_path, monkeypatch):
    calls = []
    def forbidden(*args, **kw):
        raise AssertionError("La simulación no necesita Brave ni sesión")
    monkeypatch.setattr(setup, "ensure_browser", forbidden)
    monkeypatch.setattr(setup.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=0))
    assert setup.start(config()) == 0
    assert "--dry-run" in calls[0]


def test_browser_already_connected_is_not_launched(monkeypatch):
    monkeypatch.setattr(setup, "cdp_version", lambda: "Brave/mock")
    def forbidden(*args, **kw):
        raise AssertionError("No lanzar otro Brave si CDP ya está disponible")
    monkeypatch.setattr(setup.subprocess, "Popen", forbidden)
    setup.ensure_browser("https://kick.com/streamer")


def test_browser_launch_waits_for_cdp_without_prompts(monkeypatch):
    calls = []
    probes = iter([OSError("refused"), OSError("starting"), "Brave/mock"])
    def probe():
        result = next(probes)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(setup, "cdp_version", probe)
    monkeypatch.setattr(setup, "browser_command", lambda url: ["mock-brave", url])
    monkeypatch.setattr(setup.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(setup.time, "sleep", lambda _: None)
    setup.ensure_browser("https://kick.com/streamer")
    assert calls == [["mock-brave", "https://kick.com/streamer"]]


def test_browser_timeout_prevents_bot_start(monkeypatch):
    def refused():
        raise OSError("refused")
    monkeypatch.setattr(setup, "cdp_version", refused)
    monkeypatch.setattr(setup, "browser_command", lambda url: ["mock-brave", url])
    monkeypatch.setattr(setup.subprocess, "Popen", lambda *a, **kw: None)
    with pytest.raises(RuntimeError, match="9333"):
        setup.ensure_browser("https://kick.com/streamer", timeout=0)


def test_start_propagates_authentication_failure_without_retry(monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(setup, "ensure_browser", lambda url: calls.append("browser"))
    monkeypatch.setattr(setup.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(returncode=1))
    assert setup.start(config(), real=True) == 1
    assert len(calls) == 2 and calls[0] == "browser"


def test_kick_missing_psn_session_stops_before_capture(tmp_path, monkeypatch, caplog):
    from sniper import kick
    from sniper.browser import BrowserStateError
    closed = []
    class UnauthenticatedRedeemer:
        def __init__(self, **kwargs):
            pass
        async def start(self):
            raise BrowserStateError("session_lost", "Falta iniciar sesión en PSN en el perfil dedicado")
        async def close(self):
            closed.append(True)
    def forbidden(*args, **kwargs):
        raise AssertionError("No iniciar captura sin la sesión de PSN necesaria para canjear")
    monkeypatch.setattr(kick, "RUNS", tmp_path)
    monkeypatch.setattr(kick, "Redeemer", UnauthenticatedRedeemer)
    monkeypatch.setattr(kick, "StreamCapture", forbidden)
    assert kick.main(["--channel", "streamer", "--redeem"]) == 1
    assert closed == [True] and "iniciar sesión en PSN" in caplog.text


def test_kick_real_requires_roi_and_preserves_redeemer_limits(monkeypatch):
    saved = config("kick-codes")
    cmd = setup.build_command(saved, real=True)
    assert "--redeem" in cmd and "--roi" in cmd and "--new-campaign" not in cmd
    saved["roi"] = None
    with pytest.raises(ValueError, match="ROI"):
        setup.build_command(saved, real=True)


def test_windows_launch_uses_dedicated_profile_and_kick(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "User With Spaces"))
    monkeypatch.setattr(setup, "find_brave", lambda: "C:/Program Files/Brave/brave.exe")
    cmd = setup.browser_command("https://kick.com/streamer")
    assert "--remote-debugging-port=9333" in cmd
    assert any(arg.startswith("--user-data-dir=") and "User With Spaces" in arg for arg in cmd)
    assert "--redeem" in setup.build_command(config("kick-codes"), real=True)
    with pytest.raises(ValueError, match="Linux"):
        setup.build_command(config(), real=True)


def test_wizard_records_accounts_separately_from_target(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    answers = iter(["1", "my_twitch", "my_kick", "PSNAlias", "https://www.twitch.tv/STREAMER", "180", "", "3"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    saved, action = setup.wizard()
    assert saved["twitch_user"] == "my_twitch" and saved["channel"] == "streamer"
    assert saved["interval"] == 180 and action == "3"


def test_run_defaults_to_simulation_with_saved_config(monkeypatch, tmp_path):
    path = tmp_path / ".sniper-local.json"
    setup.save_config(config(), path)
    monkeypatch.setattr(setup, "CONFIG", path)
    calls = []
    monkeypatch.setattr(setup, "start", lambda cfg, *, real: calls.append(real) or 0)
    assert setup.main(["--run"]) == 0
    assert setup.main(["--run", "--real"]) == 0
    assert calls == [False, True]


def test_local_config_is_gitignored():
    import subprocess
    result = subprocess.run(["git", "check-ignore", ".sniper-local.json"], cwd=setup.ROOT,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0


def test_legacy_config_migrates_without_losing_accounts_or_rewriting_file(tmp_path):
    legacy = config()
    legacy["version"] = 1
    legacy.pop("raffle_users")
    legacy.pop("raffle_min_users")
    path = tmp_path / ".sniper-local.json"
    path.write_text(json.dumps(legacy))
    saved = setup.load_config(path)
    assert saved == config()
    assert json.loads(path.read_text()) == legacy


def test_wizard_configures_watched_users_and_three_minute_schedule(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    answers = iter(["1", "my_twitch", "", "", "streamer", "300", "mod_two, @MOD_ONE", "2", "3"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    saved, action = setup.wizard()
    assert saved["raffle_users"] == ["mod_one", "mod_two"]
    assert saved["interval"] == 180 and action == "3"
    cmd = setup.build_command(saved)
    assert "--raffle-users" in cmd and "--dry-run" in cmd


def test_own_account_cannot_confirm_its_own_raffle():
    saved = config()
    saved["raffle_users"] = ["mi_usuario", "mod_two"]
    with pytest.raises(ValueError, match="propia cuenta"):
        setup.validate(saved)

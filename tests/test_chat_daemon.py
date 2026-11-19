"""The active daemon is chat-only; all browser/capture/sender services are fake."""
import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper import daemon
from sniper.session import ChatBrowserSession, ChatBrowserUnavailable


@pytest.fixture
def main_logging(capsys):
    yield
    import logging
    from sniper.console import CountdownConsole
    for handler in list(logging.getLogger().handlers):
        if isinstance(handler, CountdownConsole):
            logging.getLogger().removeHandler(handler)
            handler.stream = sys.stderr
            handler.close()


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "RUNS", tmp_path)
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline)


def test_chat_only_defaults_and_channel_source():
    args = daemon.parse_args([])
    assert args.chat == "rubius" and args.chat_interval == 300
    assert args.source == "https://www.twitch.tv/rubius"
    assert not hasattr(args, "stop_on_redeem") and not hasattr(args, "simulate_whisper")
    other = daemon.parse_args(["--chat", "OTRO", "--chat-interval", "420"])
    assert other.chat == "otro" and other.source == "https://www.twitch.tv/otro"


@pytest.mark.parametrize("arguments", [
    ["--stop-on-redeem"], ["--simulate-whisper", "hola"], ["--no-participate"],
    ["--chat-interval", "nan"], ["--chat-interval", "inf"],
])
def test_removed_modes_and_invalid_intervals_fail_before_startup(arguments):
    with pytest.raises(SystemExit):
        daemon.parse_args(arguments)


def test_fresh_import_does_not_load_redemption_ocr_or_whispers():
    code = """import sys
import sniper.daemon
for name in ('sniper.redeemer', 'sniper.detector', 'sniper.chat', 'sniper.whispers', 'onnxruntime', 'rapidocr'):
    assert name not in sys.modules, name
"""
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_chat_session_only_attaches_cdp_and_disconnects_driver(monkeypatch):
    import playwright.async_api
    calls = []
    context = object()
    class Chromium:
        async def connect_over_cdp(self, endpoint, **options):
            calls.append((endpoint, options))
            return SimpleNamespace(contexts=[context])
    class Driver:
        chromium = Chromium()
        async def stop(self):
            calls.append("driver stopped")
    class Manager:
        async def start(self):
            return Driver()
    monkeypatch.setattr(playwright.async_api, "async_playwright", lambda: Manager())
    monkeypatch.setattr("sniper.session.install_click_guard", lambda: None)
    async def scenario():
        session = ChatBrowserSession()
        await session.connect()
        assert session.context is context
        await session.close()
        assert calls == [("http://127.0.0.1:9333", {"timeout": 5000}), "driver stopped"]
        assert session.context is None
    asyncio.run(scenario())


def test_refused_cdp_connection_has_actionable_message_and_cleans_driver(monkeypatch):
    import playwright.async_api
    stopped = []
    class Chromium:
        async def connect_over_cdp(self, *args, **kw):
            raise playwright.async_api.Error("connect ECONNREFUSED 127.0.0.1:9333")
    class Driver:
        chromium = Chromium()
        async def stop(self):
            stopped.append(True)
    class Manager:
        async def start(self):
            return Driver()
    monkeypatch.setattr(playwright.async_api, "async_playwright", Manager)
    monkeypatch.setattr("sniper.session.install_click_guard", lambda: None)
    async def scenario():
        expected_hint = "scripts\\launch_brave.bat --twitch CANAL" if sys.platform == "win32" else "scripts/launch_brave.sh"
        session = ChatBrowserSession()
        with pytest.raises(ChatBrowserUnavailable) as error:
            await session.connect()
        assert expected_hint in str(error.value)
        assert "9333" in str(error.value) and "Twitch" in str(error.value)
        assert session.playwright is session.browser is session.context is None
        assert stopped == [True]
    asyncio.run(scenario())


def test_daemon_reports_missing_brave_with_exit_one_without_traceback(monkeypatch, capsys, main_logging):
    expected_hint = "scripts\\launch_brave.bat --twitch CANAL" if sys.platform == "win32" else "scripts/launch_brave.sh"
    async def unavailable(self):
        raise ChatBrowserUnavailable(f"Abre {expected_hint}; conexión 9333 no disponible")
    monkeypatch.setattr(daemon.ChatDaemon, "main", unavailable)
    assert daemon.main(["--chat", "rubius"]) == 1
    output = capsys.readouterr().err
    assert expected_hint in output and "Traceback" not in output


def test_unauthenticated_twitch_aborts_before_capture_and_has_clear_error(monkeypatch, capsys, main_logging):
    state = services(monkeypatch)
    async def no_auth(self):
        raise daemon.ChatUnavailable("Falta iniciar sesión en Twitch en el perfil dedicado de Brave")
    monkeypatch.setattr(daemon.BraveChatSender, "start", no_auth)
    def forbidden_capture(*args, **kw):
        raise AssertionError("No iniciar captura si falta autenticación")
    monkeypatch.setattr(daemon, "StreamCapture", forbidden_capture)
    assert daemon.main(["--chat", "rubius"]) == 1
    output = capsys.readouterr().err
    assert "iniciar sesión en Twitch" in output and "Traceback" not in output
    assert state["session_closed"] and state["messages"] == []


def services(monkeypatch, *, dry_run=False, fail_login=False):
    state = {"messages": [], "capture_closed": False, "sender_closed": False,
             "session_closed": False, "session_connected": False}
    context = object()
    class Session:
        def __init__(self):
            self.context = context
            assert not dry_run, "Dry-run must never connect to a browser"
        async def connect(self):
            state["session_connected"] = True
        async def close(self):
            state["session_closed"] = True
    class Sender:
        emote_only = False
        def __init__(self, actual_context, channel, *, stop):
            assert actual_context is context and channel == "rubius" and not dry_run
            self.stop = stop
        async def start(self):
            if fail_login:
                raise RuntimeError("Twitch requiere iniciar sesión")
        async def send(self, text):
            state["messages"].append(text)
            self.stop.set()  # End this offline test after one fake dispatch.
        async def close(self):
            state["sender_closed"] = True
    class Capture:
        def __init__(self, source, frames, *, stop, **kwargs):
            assert not fail_login, "Capture must not start if authentication fails"
            self.frames, self.stop = frames, stop
            self.live = threading.Event()
        def start(self):
            self.live.set()
            self.frames.put({})
        def is_alive(self):
            return not self.stop.is_set()
        def close(self):
            state["capture_closed"] = True
    class Composer:
        async def compose(self, *args, **kwargs):
            return "PogChamp", "emote"
        def remember(self, message):
            pass
    monkeypatch.setattr(daemon, "ChatBrowserSession", Session)
    monkeypatch.setattr(daemon, "BraveChatSender", Sender)
    monkeypatch.setattr(daemon, "StreamCapture", Capture)
    monkeypatch.setattr(daemon, "ReactionComposer", lambda model: Composer())
    return state


def test_normal_run_sends_only_chat_and_closes_its_resources(monkeypatch):
    state = services(monkeypatch)
    async def scenario():
        app = daemon.ChatDaemon(daemon.parse_args([]))
        await asyncio.wait_for(app.main(), 2)
        assert state["messages"] == ["PogChamp"]
        assert state["session_connected"] and state["session_closed"]
        assert state["sender_closed"] and state["capture_closed"] and app.events.closed
        entries = [json.loads(line) for line in (app.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(entries) == 1 and entries[0]["kind"] == "chat_message"
        assert entries[0]["message"] == "PogChamp" and not entries[0]["dry_run"]
        assert not (app.run_dir / "whispers.jsonl").exists()
    asyncio.run(scenario())


def test_dry_run_never_opens_browser_or_sends(monkeypatch):
    state = services(monkeypatch, dry_run=True)
    async def scenario():
        app = daemon.ChatDaemon(daemon.parse_args(["--dry-run"]))
        original_event = app.event
        def event(**values):
            original_event(**values)
            app.stop.set()
        app.event = event
        await asyncio.wait_for(app.main(), 2)
        assert state["messages"] == [] and not state["session_connected"]
        assert state["capture_closed"] and app.events.closed
        entries = [json.loads(line) for line in (app.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        assert entries[0]["dry_run"] and entries[0]["message"] == "PogChamp"
    asyncio.run(scenario())


def test_failed_login_leaves_no_background_tasks(monkeypatch):
    state = services(monkeypatch, fail_login=True)
    async def scenario():
        app = daemon.ChatDaemon(daemon.parse_args([]))
        with pytest.raises(RuntimeError, match="iniciar sesión"):
            await app.main()
        assert state["messages"] == [] and state["session_closed"] and state["sender_closed"]
        assert app.events.closed
    asyncio.run(scenario())

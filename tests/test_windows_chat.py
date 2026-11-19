"""Pruebas del bot de chat de Twitch simulando Windows (sys.platform='win32'), 100% offline."""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper import capture, chat_sender, daemon, participation, platform_support, raffle, raffle_chat, setup
from sniper.capture import LatestFrame
from sniper.chat_sender import BraveChatSender, ChatAuthenticationRequired, ChatUnavailable
from sniper.console import CountdownConsole
from sniper.participation import ChatParticipation
from sniper.raffle import ChatMessage, RaffleComposer, RaffleLedger
from sniper.session import ChatBrowserSession, ChatBrowserUnavailable


@pytest.fixture
def clean_console_handlers():
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if isinstance(handler, CountdownConsole):
            root.removeHandler(handler)
            handler.stream = sys.stderr
            handler.close()


def twitch_raffle_config():
    return {
        "version": 2,
        "mode": "twitch-chat",
        "twitch_user": "mi_cuenta",
        "kick_user": "",
        "psn_online_id": "",
        "channel": "streamer",
        "interval": 180,
        "raffle_users": ["mod_one", "mod_two"],
        "raffle_min_users": 2,
        "roi": None,
    }


def test_windows_wizard_chat_with_raffles_builds_real_command(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    answers = iter([
        "1",
        "mi_cuenta",
        "",
        "",
        "https://www.twitch.tv/STREAMER",
        "300",
        "@MOD_TWO, mod_one",
        "2",
        "2",
    ])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))

    saved, action = setup.wizard()

    assert action == "2"
    assert saved["mode"] == "twitch-chat"
    assert saved["twitch_user"] == "mi_cuenta"
    assert saved["channel"] == "streamer"
    assert saved["raffle_users"] == ["mod_one", "mod_two"]
    assert saved["raffle_min_users"] == 2
    assert saved["interval"] == 180

    config_dir = tmp_path / "Carpeta Con Espacios"
    config_dir.mkdir()
    config_path = config_dir / ".sniper-local.json"
    setup.save_config(saved, config_path)
    loaded = setup.load_config(config_path)
    assert loaded == saved

    cmd = setup.build_command(loaded, real=True)
    assert cmd[1:3] == ["-m", "sniper.daemon"]
    assert cmd[cmd.index("--chat") + 1] == "streamer"
    assert cmd[cmd.index("--source") + 1] == "https://www.twitch.tv/streamer"
    assert cmd[cmd.index("--chat-interval") + 1] == "180"
    assert "--wait-first" in cmd
    assert "--dry-run" not in cmd
    raffle_idx = cmd.index("--raffle-users")
    assert cmd[raffle_idx + 1 : raffle_idx + 3] == ["mod_one", "mod_two"]
    assert cmd[cmd.index("--raffle-min-users") + 1] == "2"


def test_windows_autostart_launches_brave_with_spaces_and_runs_twitch_daemon(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    localappdata = tmp_path / "Usuario Con Espacios"
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    brave_exe = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    monkeypatch.setattr(setup, "find_brave", lambda: brave_exe)

    probes = iter([OSError("refused"), OSError("starting"), "Brave/mock"])

    def fake_cdp():
        value = next(probes)
        if isinstance(value, Exception):
            raise value
        return value

    popen_calls = []
    run_calls = []
    monkeypatch.setattr(setup, "cdp_version", fake_cdp)
    monkeypatch.setattr(setup.time, "sleep", lambda _: None)
    monkeypatch.setattr(setup.subprocess, "Popen", lambda cmd, **kw: popen_calls.append((cmd, kw)))
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda cmd, **kw: run_calls.append((cmd, kw)) or SimpleNamespace(returncode=0),
    )

    rc = setup.start(twitch_raffle_config(), real=True)

    assert rc == 0
    assert len(popen_calls) == 1
    brave_cmd, _ = popen_calls[0]
    expected_profile = Path(str(localappdata)) / "twitch-psn-sniper/brave-profile"
    assert brave_cmd[0] == brave_exe
    assert f"--user-data-dir={expected_profile}" in brave_cmd
    assert "--remote-debugging-address=127.0.0.1" in brave_cmd
    assert "--remote-debugging-port=9333" in brave_cmd
    assert brave_cmd[-1] == "https://www.twitch.tv/streamer"

    assert len(run_calls) == 1
    daemon_cmd, run_kw = run_calls[0]
    assert run_kw["cwd"] == setup.ROOT
    assert daemon_cmd[daemon_cmd.index("--chat") + 1] == "streamer"
    assert "--wait-first" in daemon_cmd
    assert "--raffle-users" in daemon_cmd
    assert "--dry-run" not in daemon_cmd


def test_windows_refused_cdp_hint_points_to_twitch_bat_and_stops_driver(monkeypatch):
    import playwright.async_api

    monkeypatch.setattr(sys, "platform", "win32")
    assert platform_support.launch_hint(platform="twitch") == "scripts\\launch_brave.bat --twitch CANAL"

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
        session = ChatBrowserSession()
        with pytest.raises(ChatBrowserUnavailable) as exc:
            await session.connect()
        message = str(exc.value)
        assert "scripts\\launch_brave.bat --twitch CANAL" in message
        assert "9333" in message and "Twitch" in message
        assert session.playwright is session.browser is session.context is None
        assert stopped == [True]

    asyncio.run(scenario())


def test_windows_wait_first_defers_first_message_until_interval_elapses(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    now = [1000.0]
    monkeypatch.setattr("sniper.participation.time.perf_counter", lambda: now[0])
    monkeypatch.setattr("sniper.capture.time.perf_counter", lambda: now[0])

    args = daemon.parse_args(["--chat", "streamer", "--chat-interval", "180", "--wait-first"])
    assert args.wait_first and args.chat_interval == 180

    sent = []
    statuses = []

    class Sender:
        emote_only = False

        async def start(self):
            pass

        async def send(self, text):
            sent.append((now[0], text))

    class Composer:
        async def compose(self, *args, **kw):
            return "PogChamp", "emote"

        def remember(self, text):
            pass

    frames = LatestFrame()
    live = threading.Event()
    live.set()
    participant = ChatParticipation(
        frames=frames,
        capture=SimpleNamespace(live=live),
        stop=threading.Event(),
        sender=Sender(),
        composer=Composer(),
        interval=args.chat_interval,
        wait_first=args.wait_first,
        status_display=statuses.append,
    )

    async def scenario():
        frames.put({"frame": 1})
        await participant.tick()
        participant.report_status()
        assert sent == []
        assert participant.status() == ("countdown", "Próximo mensaje en 03:00 | Enviados: 0")

        now[0] = 1090.0
        frames.put({"frame": 2})
        await participant.tick()
        participant.report_status()
        assert sent == []
        assert "01:30" in statuses[-1]

        now[0] = 1180.0
        frames.put({"frame": 3})
        await participant.tick()
        assert sent == [(1180.0, "PogChamp")]
        assert participant.next_due == 1360.0

    asyncio.run(scenario())


def test_windows_auth_failure_aborts_before_capture_and_raffle_listener(
    monkeypatch, tmp_path, capsys, clean_console_handlers
):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(daemon, "RUNS", tmp_path)
    monkeypatch.setattr("sniper.console.configure_console", lambda: None)

    state = {"session_closed": False, "sender_closed": False}

    class Session:
        def __init__(self):
            self.context = object()

        async def connect(self):
            pass

        async def close(self):
            state["session_closed"] = True

    class UnauthenticatedSender:
        emote_only = False

        def __init__(self, context, channel, *, stop):
            assert channel == "streamer"

        async def start(self):
            raise ChatAuthenticationRequired(
                "Twitch muestra Iniciar sesión en el perfil dedicado de Brave."
            )

        async def close(self):
            state["sender_closed"] = True

    def forbidden_capture(*args, **kw):
        raise AssertionError("No iniciar captura si falta autenticación de Twitch")

    async def forbidden_irc(*args, **kw):
        raise AssertionError("No iniciar lectura IRC de rifas si falta autenticación de Twitch")

    monkeypatch.setattr(daemon, "ChatBrowserSession", Session)
    monkeypatch.setattr(daemon, "BraveChatSender", UnauthenticatedSender)
    monkeypatch.setattr(daemon, "StreamCapture", forbidden_capture)
    monkeypatch.setattr(raffle_chat, "watch_messages", forbidden_irc)

    rc = daemon.main(["--chat", "streamer", "--raffle-users", "mod_one", "mod_two"])

    assert rc == 1
    assert state["session_closed"] and state["sender_closed"]
    output = capsys.readouterr().err
    assert "Iniciar sesión" in output and "Traceback" not in output


def test_windows_setup_autostart_propagates_auth_failure_without_retry(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []
    monkeypatch.setattr(setup, "ensure_browser", lambda url: calls.append(("browser", url)))
    monkeypatch.setattr(
        setup.subprocess,
        "run",
        lambda cmd, **kw: calls.append(("run", cmd)) or SimpleNamespace(returncode=1),
    )

    assert setup.start(twitch_raffle_config(), real=True) == 1
    assert len(calls) == 2
    assert calls[0] == ("browser", "https://www.twitch.tv/streamer")
    assert calls[1][0] == "run"


def test_windows_cancellation_cleans_up_tasks_capture_sender_and_session(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(daemon, "RUNS", tmp_path)

    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline)

    state = {
        "session_closed": False,
        "sender_closed": False,
        "capture_closed": False,
        "listener_started": False,
        "listener_cancelled": False,
        "listener_resets": 0,
    }

    class Session:
        def __init__(self):
            self.context = object()

        async def connect(self):
            pass

        async def close(self):
            state["session_closed"] = True

    class Sender:
        emote_only = False

        def __init__(self, context, channel, *, stop):
            pass

        async def start(self):
            pass

        async def send(self, text):
            raise AssertionError("No debería enviar antes de terminar la cuenta regresiva")

        async def close(self):
            state["sender_closed"] = True

    class Capture:
        def __init__(self, source, frames, *, stop, **kwargs):
            self.stop = stop
            self.live = threading.Event()

        def start(self):
            self.live.set()

        def is_alive(self):
            return not self.stop.is_set()

        def close(self):
            state["capture_closed"] = True

    class Composer:
        async def compose(self, *args, **kw):
            return "PogChamp", "emote"

        def remember(self, message):
            pass

    async def scenario():
        ready = asyncio.Event()

        async def fake_watch_messages(channel, on_message, *, stop, on_reset):
            state["listener_started"] = True
            ready.set()
            try:
                while not stop.is_set():
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                state["listener_cancelled"] = True
                raise
            finally:
                on_reset()
                state["listener_resets"] += 1

        monkeypatch.setattr(daemon, "ChatBrowserSession", Session)
        monkeypatch.setattr(daemon, "BraveChatSender", Sender)
        monkeypatch.setattr(daemon, "StreamCapture", Capture)
        monkeypatch.setattr(daemon, "ReactionComposer", lambda model: Composer())
        monkeypatch.setattr(raffle_chat, "watch_messages", fake_watch_messages)

        args = daemon.parse_args(["--chat", "streamer", "--raffle-users", "mod_one", "mod_two"])
        app = daemon.ChatDaemon(args)
        runner = asyncio.create_task(app.main())
        await asyncio.wait_for(ready.wait(), timeout=2)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert app.stop.is_set()
        assert state["listener_started"] and state["listener_cancelled"]
        assert state["listener_resets"] == 1
        assert state["capture_closed"] and state["sender_closed"] and state["session_closed"]
        assert app.events.closed
        remaining = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        assert remaining == []

    asyncio.run(scenario())


def test_windows_keyboard_interrupt_exits_130_and_clears_console_status(
    monkeypatch, tmp_path, clean_console_handlers
):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(daemon, "RUNS", tmp_path)
    monkeypatch.setattr("sniper.console.configure_console", lambda: None)

    cleared = []
    original_clear = CountdownConsole.clear_status

    def spy_clear(self):
        cleared.append(True)
        original_clear(self)

    async def interrupted(self):
        raise KeyboardInterrupt()

    monkeypatch.setattr(CountdownConsole, "clear_status", spy_clear)
    monkeypatch.setattr(daemon.ChatDaemon, "main", interrupted)

    assert daemon.main(["--chat", "streamer"]) == 130
    assert cleared


def _install_fake_clock(monkeypatch, now):
    clock = SimpleNamespace(
        time=lambda: now[0],
        monotonic=lambda: now[0],
        perf_counter=lambda: now[0],
    )
    monkeypatch.setattr(raffle, "time", clock)
    monkeypatch.setattr(participation, "time", clock)
    monkeypatch.setattr(capture, "time", clock)
    monkeypatch.setattr(chat_sender, "time", clock)


def _install_fake_windows_lock(monkeypatch):
    locks = []
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            LK_NBLCK=2,
            LK_UNLCK=0,
            locking=lambda fd, mode, count: locks.append((mode, count)),
        ),
    )
    return locks


def test_windows_urgent_raffle_cycle_immediate_then_60s_then_normal_180s(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    locks = _install_fake_windows_lock(monkeypatch)
    now = [1000.0]
    _install_fake_clock(monkeypatch, now)

    args = daemon.parse_args(["--chat", "streamer", "--raffle-users", "mod_one", "mod_two"])
    assert args.chat_interval == 180 and args.wait_first

    class Fallback:
        async def compose(self, frame, *, emote_only=False):
            return "PogChamp", "emote"

        def remember(self, message):
            pass

    sent = []

    class Sender:
        emote_only = False
        slow_until = 0.0

        async def start(self):
            pass

        async def send(self, text):
            sent.append((now[0], "normal", text))

        async def send_raffle(self, text):
            sent.append((now[0], "raffle", text))

    ledger = RaffleLedger(tmp_path / "raffle-streamer.json")
    composer = RaffleComposer(
        Fallback(),
        args.raffle_users,
        minimum=args.raffle_min_users,
        ledger=ledger,
    )
    frames = LatestFrame()
    live = threading.Event()
    live.set()
    worker = ChatParticipation(
        frames=frames,
        capture=SimpleNamespace(live=live),
        stop=threading.Event(),
        sender=Sender(),
        composer=composer,
        interval=args.chat_interval,
        wait_first=args.wait_first,
    )

    async def scenario():
        frames.put({"frame": 1})
        await worker.tick()
        assert sent == []
        assert worker.status() == ("countdown", "Próximo mensaje en 03:00 | Enviados: 0")

        # Confirmación de 2 moderadores a los 10 s: primer envío inmediato sin esperar el contador normal.
        now[0] = 1010.0
        composer.observe(ChatMessage("mod_one", "coco", "m1", now[0], True))
        composer.observe(ChatMessage("mod_two", "COCO", "m2", now[0], True))
        assert composer.wakeup.is_set()
        assert worker.status()[0] == "ready"
        assert "Rifa confirmada" in worker.status()[1]

        await worker.tick()
        assert sent == [(1010.0, "raffle", "COCO")]

        # A los +40 s todavía faltan 20 s para la segunda frase.
        now[0] = 1050.0
        assert worker.status() == ("countdown", "Próximo mensaje de rifa en 00:20 | Enviados: 1")
        await worker.tick()
        assert len(sent) == 1

        # A los +60 s se envía la segunda frase por send_raffle.
        now[0] = 1070.0
        await worker.tick()
        assert sent == [(1010.0, "raffle", "COCO"), (1070.0, "raffle", "COCO")]
        assert composer.priority_due() is None
        assert worker.next_due == 1250.0

        # Aunque aparezca otra frase, el tercer turno obligatorio es normal a los +180 s.
        now[0] = 1100.0
        composer.observe(ChatMessage("mod_one", "mango", "m3", now[0], True))
        composer.observe(ChatMessage("mod_two", "mango", "m4", now[0], True))
        assert composer.priority_due() is None
        now[0] = 1150.0
        frames.put({"frame": 2})
        await worker.tick()
        assert len(sent) == 2

        now[0] = 1250.0
        frames.put({"frame": 3})
        await worker.tick()
        assert sent == [
            (1010.0, "raffle", "COCO"),
            (1070.0, "raffle", "COCO"),
            (1250.0, "normal", "PogChamp"),
        ]

    asyncio.run(scenario())
    assert locks and locks[0] == (2, 1) and locks[1] == (0, 1)


def test_windows_raffle_ignores_local_cooldown_but_respects_twitch_slow_mode(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    now = [1000.0]
    _install_fake_clock(monkeypatch, now)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(chat_sender.asyncio, "sleep", no_sleep)

    class FakeElement:
        def __init__(self, *, visible=False, text="", editable=True, enabled=True, on_click=None):
            self.visible = visible
            self.text = text
            self.editable = editable
            self.enabled = enabled
            self.value = ""
            self.on_click = on_click

        async def count(self):
            return 1 if self.visible else 0

        def nth(self, index):
            return self

        async def is_visible(self):
            return self.visible

        async def is_editable(self):
            return self.editable

        async def is_enabled(self):
            return self.enabled

        async def inner_text(self, **kwargs):
            return self.text

        async def fill(self, value, **kwargs):
            self.value = value

        async def evaluate(self, *args):
            return self.value

        async def click(self, **kwargs):
            if self.on_click is not None:
                self.on_click()

    submitted = []
    chat_input = FakeElement(visible=True, editable=True)
    def submit_click():
        submitted.append((now[0], chat_input.value))
        chat_input.value = ""

    send_button = FakeElement(
        visible=True,
        enabled=True,
        on_click=submit_click,
    )
    login_button = FakeElement(visible=False)
    notice_el = FakeElement(visible=False, text="")

    class FakePage:
        url = "https://www.twitch.tv/streamer"

        def is_closed(self):
            return False

        def locator(self, selector):
            if "login-button" in selector:
                return login_button
            if "chat-input-error" in selector:
                return notice_el
            if "chat-send-button" in selector:
                return send_button
            return chat_input

        def get_by_role(self, *args, **kwargs):
            return send_button

    page = FakePage()
    context = SimpleNamespace(pages=[page])
    sender = BraveChatSender(context, "streamer", stop=threading.Event())

    class Fallback:
        async def compose(self, frame, *, emote_only=False):
            return "Kappa", "emote"

        def remember(self, message):
            pass

    composer = RaffleComposer(Fallback(), ["mod_one", "mod_two"])
    frames = LatestFrame()
    live = threading.Event()
    live.set()
    worker = ChatParticipation(
        frames=frames,
        capture=SimpleNamespace(live=live),
        stop=threading.Event(),
        sender=sender,
        composer=composer,
        interval=180,
        wait_first=True,
    )

    async def scenario():
        await sender.start()
        # Un envío normal activa el cooldown local de 30 s, sin modo lento de Twitch.
        await sender.send("PogChamp")
        assert submitted == [(1000.0, "PogChamp")]
        assert sender.next_allowed == 1030.0 and sender.slow_until == 0.0

        # A los 5 s el envío normal respeta los 30 s locales, pero send_raffle no se bloquea por ellos.
        now[0] = 1005.0
        with pytest.raises(ChatUnavailable, match="esperar"):
            await sender.send("Kappa")
        await sender.send_raffle("coco")
        assert submitted == [(1000.0, "PogChamp"), (1005.0, "coco")]

        # Twitch activa modo lento de 20 s: ni send_raffle ni ChatParticipation lo saltan.
        now[0] = 1010.0
        notice_el.visible = True
        notice_el.text = "Espera 20 segundos antes de enviar otro mensaje"
        with pytest.raises(ChatUnavailable, match="esperar"):
            await sender.send_raffle("coco")
        assert sender.slow_until == 1031.0
        assert len(submitted) == 2

        now[0] = 1015.0
        composer.observe(ChatMessage("mod_one", "mango", "m1", now[0], True))
        composer.observe(ChatMessage("mod_two", "mango", "m2", now[0], True))
        assert worker._priority_due() == 1031.0
        assert worker.status() == ("countdown", "Próximo mensaje de rifa en 00:16 | Enviados: 0")
        await worker.tick()
        assert len(submitted) == 2
        assert composer.ledger.memory["attempts"] == {}

        # Una vez vencido el modo lento de Twitch, la frase confirmada se envía.
        notice_el.visible = False
        notice_el.text = ""
        now[0] = 1031.0
        await worker.tick()
        assert submitted == [(1000.0, "PogChamp"), (1005.0, "coco"), (1031.0, "mango")]

    asyncio.run(scenario())

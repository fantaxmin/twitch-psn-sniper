"""Offline tests: no browser, Twitch messages, model requests, or coupon redemption."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sniper.capture import LatestFrame
from sniper.chat_sender import (BraveChatSender, ChatRestricted, ChatUnavailable,
                               ChatAuthenticationRequired, ChatNotReady)
from sniper.participation import ChatParticipation
from sniper.reactions import EMOTES, ReactionComposer, clean_reaction


@pytest.fixture(autouse=True)
def inline_background_workers(monkeypatch):
    # These are coordination tests, not networking/thread-pool integration tests.
    # Keep fake inference and fake capture cleanup deterministic in the sandbox.
    async def inline(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", inline)


class Composer:
    def __init__(self):
        self.history = []
        self.calls = 0

    async def compose(self, frame, *, emote_only=False):
        self.calls += 1
        return "PogChamp", "emote"

    def remember(self, message):
        self.history.append(message)


class Sender:
    def __init__(self):
        self.messages = []
        self.emote_only = False
        self.error = None

    async def start(self):
        pass

    async def send(self, message):
        self.messages.append(message)
        if self.error:
            raise self.error


def participant(**kwargs):
    frames = LatestFrame()
    frames.put(np.zeros((4, 4, 3), dtype=np.uint8))
    live = threading.Event()
    live.set()
    return ChatParticipation(frames=frames, capture=SimpleNamespace(live=live),
                             stop=threading.Event(), composer=Composer(), sender=Sender(), **kwargs)


def test_five_minute_interval_and_no_catchup(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "perf_counter", lambda: now[0])
    async def scenario():
        worker = participant()
        await worker.tick()
        assert worker.sender.messages == ["PogChamp"]
        now[0] += 299
        worker.frames.put(np.zeros((4, 4, 3), dtype=np.uint8))
        await worker.tick()
        assert len(worker.sender.messages) == 1
        now[0] += 1
        worker.frames.put(np.zeros((4, 4, 3), dtype=np.uint8))
        await worker.tick()
        assert len(worker.sender.messages) == 2
        now[0] += 10000
        worker.frames.put(np.zeros((4, 4, 3), dtype=np.uint8))
        await worker.tick()
        await worker.tick()
        assert len(worker.sender.messages) == 3
    asyncio.run(scenario())


def test_wait_first_starts_countdown_without_sending_immediately(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "perf_counter", lambda: now[0])
    async def scenario():
        worker = participant(interval=180, wait_first=True)
        assert "03:00" in worker.status()[1]
        await worker.tick()
        assert worker.sender.messages == []
        now[0] += 180
        worker.frames.put(np.zeros((4, 4, 3), dtype=np.uint8))
        await worker.tick()
        assert worker.sender.messages == ["PogChamp"]
    asyncio.run(scenario())


@pytest.mark.parametrize("condition", ["offline", "stale", "stop"])
def test_no_post_without_fresh_live_frame(condition):
    async def scenario():
        worker = participant()
        if condition == "offline":
            worker.capture.live.clear()
        elif condition == "stale":
            worker.frames.t_frame -= 60
        else:
            worker.stop.set()
        await worker.tick()
        assert not worker.sender.messages and worker.composer.calls == 0
    asyncio.run(scenario())


def test_win_while_composing_cancels_post():
    async def scenario():
        worker = participant()
        async def compose(*args, **kwargs):
            worker.stop.set()
            return "PogChamp", "emote"
        worker.composer.compose = compose
        await worker.tick()
        assert not worker.sender.messages
    asyncio.run(scenario())


def test_dry_run_does_not_send():
    async def scenario():
        worker = participant(dry_run=True)
        await worker.tick()
        assert not worker.sender.messages and not worker.composer.history
    asyncio.run(scenario())


@pytest.mark.parametrize("error", [TimeoutError(), ChatUnavailable("incierto"), ChatRestricted("vetado")])
def test_send_errors_never_immediately_retry(error):
    async def scenario():
        worker = participant()
        worker.sender.error = error
        await worker.tick()
        worker.frames.put(np.zeros((4, 4, 3), dtype=np.uint8))
        await worker.tick()
        assert worker.sender.messages == ["PogChamp"] and not worker.composer.history
        assert worker.disabled == isinstance(error, ChatRestricted)
    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["/ban alguien", ".timeout alguien", "@persona hola", "https://example.com", "CODIGO: 1234-ABCD-5678", "x" * 141, None])
def test_reaction_rejects_commands_links_mentions_and_codes(text):
    assert clean_reaction(text) == ""


def test_local_model_fallback_and_grounded_text():
    async def scenario():
        composer = ReactionComposer()
        composer._generate = lambda frame: {"text": "Ese coche rojo se ve brutal", "emote": "PogChamp"}
        message, source = await composer.compose(np.zeros((4, 4, 3), dtype=np.uint8))
        assert message == "Ese coche rojo se ve brutal PogChamp" and source == "vision"
        composer.remember(message)
        repeated, _ = await composer.compose(np.zeros((4, 4, 3), dtype=np.uint8))
        assert repeated != message
        def unavailable(frame):
            raise ConnectionError("sin modelo")
        composer._generate = unavailable
        fallback, source = await composer.compose(None)
        assert fallback in EMOTES and source == "emote"
    asyncio.run(scenario())


class Input:
    def __init__(self, value=""):
        self.value = value

    async def evaluate(self, *args):
        return self.value

    async def fill(self, value, **kwargs):
        self.value = value


class Button:
    def __init__(self):
        self.clicks = 0
        self.error = None

    async def is_enabled(self):
        return True

    async def click(self, **kwargs):
        self.clicks += 1
        if self.error:
            raise self.error


def fake_sender():
    sender = BraveChatSender(None, "rubius", stop=threading.Event())
    sender.input, sender.send_button = Input(), Button()
    async def refresh():
        pass
    async def notice():
        return ""
    sender.refresh, sender._notice = refresh, notice
    return sender


@pytest.mark.parametrize("draft", ["", "  ", "\n", "\u200b\n", "mi borrador"])
def test_sender_replaces_any_draft_with_daemon_message(draft):
    async def scenario():
        sender = fake_sender()
        sender.input.value = draft
        submitted = []
        original_click = sender.send_button.click
        async def click(**kwargs):
            submitted.append(sender.input.value)
            await original_click(**kwargs)
        sender.send_button.click = click
        await sender.send("PogChamp")
        assert submitted == ["PogChamp"] and sender.send_button.clicks == 1
        assert sender.input.value == ""
    asyncio.run(scenario())


def test_invalid_daemon_message_does_not_erase_existing_text():
    async def scenario():
        sender = fake_sender()
        sender.input.value = "mi borrador"
        with pytest.raises(ValueError):
            await sender.send("/ban alguien")
        assert sender.input.value == "mi borrador" and sender.send_button.clicks == 0
    asyncio.run(scenario())


def test_uncertain_click_is_not_retried_and_clears_own_draft():
    async def scenario():
        sender = fake_sender()
        sender.send_button.error = TimeoutError()
        with pytest.raises(TimeoutError):
            await sender.send("PogChamp")
        assert sender.send_button.clicks == 1 and sender.input.value == ""
    asyncio.run(scenario())


def test_sender_cancels_if_win_occurs_during_fill():
    async def scenario():
        sender = fake_sender()
        original = sender.input.fill
        async def fill(value, **kwargs):
            await original(value, **kwargs)
            if value:
                sender.stop.set()
        sender.input.fill = fill
        with pytest.raises(ChatUnavailable):
            await sender.send("PogChamp")
        assert sender.send_button.clicks == 0 and sender.input.value == ""
    asyncio.run(scenario())


def test_daemon_default_five_minutes_and_seven_minute_option():
    from sniper.daemon import parse_args
    assert parse_args([]).chat_interval == 300
    assert parse_args(["--chat-interval", "420"]).chat_interval == 420
    with pytest.raises(SystemExit):
        parse_args(["--source", "tests/fixtures/sim_1080p60.mp4"])


def test_shutdown_closes_daemon_tabs_and_leaves_user_tabs(tmp_path):
    from sniper.browser import BraveBrowser, RedeemTab
    class Page:
        closed = False
        def is_closed(self):
            return self.closed
        async def close(self):
            self.closed = True
    class Driver:
        stopped = False
        async def stop(self):
            self.stopped = True
    async def scenario():
        browser = BraveBrowser(runs_dir=tmp_path)
        owned, user, driver = Page(), Page(), Driver()
        browser.playwright = driver
        browser.tabs = {"A": RedeemTab("A", owned, owned=True), "B": RedeemTab("B", user)}
        await browser.close(close_tabs=True)
        assert owned.closed and not user.closed and driver.stopped
        chat = BraveChatSender(None, "rubius")
        chat.page = user
        await chat.close()
        assert not user.closed
    asyncio.run(scenario())


def test_start_waits_for_existing_page_to_finish_loading(monkeypatch):
    from sniper import chat_sender
    now = [0.0]
    monkeypatch.setattr(chat_sender.time, "monotonic", lambda: now[0])
    async def sleep(delay):
        now[0] += delay
    monkeypatch.setattr(chat_sender.asyncio, "sleep", sleep)
    page = SimpleNamespace(url="https://www.twitch.tv/rubius", is_closed=lambda: False)
    sender = BraveChatSender(SimpleNamespace(pages=[page]), "rubius")
    calls = []
    async def refresh():
        calls.append(True)
        if len(calls) < 3:
            raise ChatNotReady("El chat está cargando")
    monkeypatch.setattr(sender, "refresh", refresh)
    asyncio.run(sender.start())
    assert sender.page is page and not sender.owned
    assert len(calls) == 3 and now[0] == 0.5


def test_start_missing_auth_fails_immediately_without_waiting(monkeypatch):
    from sniper import chat_sender
    page = SimpleNamespace(url="https://www.twitch.tv/rubius", is_closed=lambda: False)
    sender = BraveChatSender(SimpleNamespace(pages=[page]), "rubius")
    async def refresh():
        raise ChatAuthenticationRequired("Twitch muestra Iniciar sesión")
    async def forbidden(delay):
        raise AssertionError("No esperar ni insistir si falta autenticación explícitamente")
    monkeypatch.setattr(sender, "refresh", refresh)
    monkeypatch.setattr(chat_sender.asyncio, "sleep", forbidden)
    with pytest.raises(ChatAuthenticationRequired):
        asyncio.run(sender.start())


def test_start_loading_timeout_is_not_reported_as_missing_auth(monkeypatch):
    page = SimpleNamespace(url="https://www.twitch.tv/rubius", is_closed=lambda: False)
    sender = BraveChatSender(SimpleNamespace(pages=[page]), "rubius", start_timeout=0)
    async def refresh():
        raise ChatNotReady("Chat cerrado")
    monkeypatch.setattr(sender, "refresh", refresh)
    with pytest.raises(ChatNotReady, match="No se confirmó que falte iniciar sesión"):
        asyncio.run(sender.start())


@pytest.mark.parametrize("login,editable,expected", [(True, False, ChatAuthenticationRequired),
                                                   (False, False, ChatNotReady)])
def test_refresh_distinguishes_login_from_uneditable_input(monkeypatch, login, editable, expected):
    from sniper import chat_sender
    sender = BraveChatSender(None, "rubius")
    sender.page = SimpleNamespace(url="https://www.twitch.tv/rubius", is_closed=lambda: False,
                                  locator=lambda selector: selector)
    async def visible(locator):
        if locator == '[data-a-target="login-button"]':
            return object() if login else None
        return SimpleNamespace(is_editable=editable_value)
    async def editable_value():
        return editable
    monkeypatch.setattr(chat_sender, "visible_first", visible)
    with pytest.raises(expected):
        asyncio.run(sender.refresh())


def test_urgent_raffle_bypasses_only_local_cooldown_and_respects_twitch_slow_mode(monkeypatch):
    from sniper import chat_sender
    now = [1000.0]
    monkeypatch.setattr(chat_sender.time, "perf_counter", lambda: now[0])
    async def no_sleep(delay):
        pass
    monkeypatch.setattr(chat_sender.asyncio, "sleep", no_sleep)
    async def scenario():
        sender = fake_sender()
        sender.next_allowed = 1030
        with pytest.raises(ChatUnavailable):
            await sender.send("coco")
        assert sender.send_button.clicks == 0
        await sender.send_raffle("coco")
        assert sender.send_button.clicks == 1
        sender.slow_until = 1060
        with pytest.raises(ChatUnavailable):
            await sender.send_raffle("coco")
        assert sender.send_button.clicks == 1
    asyncio.run(scenario())

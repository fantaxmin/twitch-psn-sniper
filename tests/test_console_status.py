"""Terminal/countdown tests using a fake clock and fake senders, never Twitch."""
import asyncio
import io
import logging
import threading
from types import SimpleNamespace

import pytest

from sniper.console import CountdownConsole
from sniper.participation import ChatParticipation


class Frames:
    def __init__(self, now):
        self.seq, self.timestamp = 1, now
    def get_newer(self, *args, **kwargs):
        return self.seq, {}, self.timestamp


class Sender:
    emote_only = False
    error = None
    def __init__(self):
        self.messages = []
    async def start(self):
        pass
    async def send(self, text):
        if self.error:
            raise self.error
        self.messages.append(text)


class Composer:
    async def compose(self, *args, **kwargs):
        return "Qué buena carrera PogChamp", "vision"
    def remember(self, text):
        pass


def worker(now, **kwargs):
    live = threading.Event()
    live.set()
    return ChatParticipation(frames=Frames(now), capture=SimpleNamespace(live=live),
                             stop=threading.Event(), sender=Sender(), composer=Composer(), **kwargs)


def test_countdown_uses_real_deadline_and_logs_exact_message(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr("sniper.participation.time.perf_counter", lambda: now[0])
    caplog.set_level(logging.INFO)
    async def scenario():
        chat = worker(now[0])
        await chat.tick()
        assert chat.status() == ("countdown", "Próximo mensaje en 05:00 | Enviados: 1")
        assert "CHAT ENVIADO #1 [vision]: Qué buena carrera PogChamp" in caplog.text
        assert chat.last_message == "Qué buena carrera PogChamp"
        deadline = chat.next_due
        for timestamp, expected in ((1001.0, "04:59"), (1299.8, "00:01")):
            now[0] = chat.frames.timestamp = timestamp
            assert expected in chat.status()[1]
        now[0] = chat.frames.timestamp = 1301
        chat.frames.seq += 1
        assert chat.status()[0] == "ready"
        assert chat.next_due == deadline and len(chat.sender.messages) == 1
    asyncio.run(scenario())


def test_countdown_reports_offline_stale_restricted_and_stopped():
    chat = worker(0)
    chat.capture.live.clear()
    assert chat.status()[0] == "offline"
    chat.capture.live.set()
    assert chat.status()[0] == "stale"
    chat.disabled = True
    assert chat.status()[0] == "restricted"
    chat.stop.set()
    assert chat.status()[0] == "stopped"


@pytest.mark.parametrize("dry_run", [False, True])
def test_failed_or_simulated_message_is_not_reported_as_sent(dry_run, caplog):
    caplog.set_level(logging.INFO)
    async def scenario():
        import time
        chat = worker(time.perf_counter(), dry_run=dry_run)
        chat.sender.error = TimeoutError("resultado incierto")
        await chat.tick()
        assert chat.sent_count == 0 and chat.last_message is None
        assert "CHAT ENVIADO" not in caplog.text
        if dry_run:
            assert chat.preview_count == 1 and "CHAT SIMULADO #1" in caplog.text
            assert "Simulados: 1" in chat.status()[1]
    asyncio.run(scenario())


def test_redirected_status_logs_every_30_seconds_not_every_second(monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr("sniper.participation.time.perf_counter", lambda: now[0])
    caplog.set_level(logging.INFO)
    chat = worker(now[0])
    chat.next_due = now[0] + 300
    chat.report_status()
    now[0] = chat.frames.timestamp = 1001
    chat.report_status()
    assert len(caplog.records) == 1
    now[0] = chat.frames.timestamp = 1030
    chat.report_status()
    assert len(caplog.records) == 2 and "04:30" in caplog.records[-1].message


def test_terminal_keeps_message_log_above_countdown():
    class Terminal(io.StringIO):
        def isatty(self):
            return True
    stream = Terminal()
    console = CountdownConsole(stream)
    console.setFormatter(logging.Formatter("%(message)s"))
    try:
        console.update("Próximo mensaje en 04:59 | Enviados: 1")
        console.handle(logging.LogRecord("chat", logging.INFO, "", 0,
                                        "CHAT ENVIADO #1: PogChamp", (), None))
        output = stream.getvalue()
        assert "\r\x1b[2K" in output
        assert "CHAT ENVIADO #1: PogChamp\nPróximo mensaje en 04:59" in output
        console.clear_status()
    finally:
        console.close()


def test_redirected_output_has_no_terminal_escape_sequences():
    stream = io.StringIO()
    console = CountdownConsole(stream)
    try:
        console.update("Próximo mensaje en 04:59")
        console.handle(logging.LogRecord("chat", logging.INFO, "", 0, "CHAT ENVIADO: PogChamp", (), None))
        assert stream.getvalue() == "CHAT ENVIADO: PogChamp\n"
    finally:
        console.close()


def test_countdown_keeps_showing_preparation_while_composer_waits():
    async def scenario():
        import time
        lines, entered, release = [], asyncio.Event(), asyncio.Event()
        chat = worker(time.perf_counter(), status_display=lines.append)
        async def compose(*args, **kwargs):
            entered.set()
            await release.wait()
            return "PogChamp", "emote"
        chat.composer.compose = compose
        task = asyncio.create_task(chat.run())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0)
            assert any("preparando mensaje" in line for line in lines)
        finally:
            chat.stop.set()
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert lines[-1].startswith("Chat detenido") and not chat.sender.messages
    asyncio.run(scenario())

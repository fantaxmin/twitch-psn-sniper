"""Consola y registro de rifas de Windows, sin consola nativa ni envíos reales."""
import io
import sys
from types import SimpleNamespace

from sniper import platform_support, raffle


class Stream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.options = None

    def reconfigure(self, **kwargs):
        self.options = kwargs


def test_windows_console_uses_utf8_and_initializes_ansi(monkeypatch):
    stdout, stderr = Stream(), Stream()
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.setitem(sys.modules, "colorama", SimpleNamespace(just_fix_windows_console=lambda: calls.append("ansi")))
    platform_support.configure_console()
    assert stdout.options == stderr.options == {"encoding": "utf-8", "errors": "replace"}
    assert calls == ["ansi"]


def test_windows_console_without_optional_module_does_not_crash(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", Stream())
    monkeypatch.setattr(sys, "stderr", Stream())
    monkeypatch.setitem(sys.modules, "colorama", None)
    platform_support.configure_console()


def test_other_platform_console_is_unchanged(monkeypatch):
    stream = Stream()
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "stderr", stream)
    platform_support.configure_console()
    assert stream.options is None


def test_countdown_uses_stream_after_windows_console_wrapping(monkeypatch):
    from sniper import console
    wrapped = Stream()
    monkeypatch.setattr(console, "configure_console", lambda: monkeypatch.setattr(sys, "stderr", wrapped))
    handler = console.CountdownConsole()
    try:
        assert handler.stream is wrapped
    finally:
        handler.close()


def test_windows_raffle_ledger_locks_and_preserves_pair_then_normal(tmp_path, monkeypatch):
    now = [1000.0]
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, count: calls.append((mode, count))))
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    path = tmp_path / "raffle-streamer.json"
    assert raffle.RaffleLedger(path).reserve("coco")
    now[0] += 180
    assert raffle.RaffleLedger(path).reserve("coco")
    now[0] += 180
    assert not raffle.RaffleLedger(path).reserve("new phrase")
    assert not raffle.RaffleLedger(path).reserve("coco")
    assert calls == [(2, 1), (0, 1)] * 4


def test_windows_browser_is_detached_from_bot_console(monkeypatch, tmp_path):
    from sniper import setup
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "User With Spaces"))
    monkeypatch.setattr(setup, "find_brave", lambda: "C:/Program Files/Brave/brave.exe")
    probes = iter([OSError("refused"), "Brave/mock"])
    def cdp():
        result = next(probes)
        if isinstance(result, Exception):
            raise result
        return result
    monkeypatch.setattr(setup, "cdp_version", cdp)
    calls = []
    monkeypatch.setattr(setup.subprocess, "Popen", lambda cmd, **kwargs: calls.append(kwargs))
    setup.ensure_browser("https://www.twitch.tv/streamer")
    assert calls[0]["creationflags"] & 0x00000200
    assert calls[0]["creationflags"] & 0x00000008

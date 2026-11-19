import asyncio
import errno
import sys
from types import SimpleNamespace

import pytest

from sniper import filelock, platform_support


def test_posix_lock_excludes_second_owner_and_releases(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Cobertura POSIX")
    path = tmp_path / ".redeemer.lock"
    with filelock.FileLock(path):
        with pytest.raises(BlockingIOError, match="Ya hay otro proceso"):
            with filelock.FileLock(path):
                pass
    with filelock.FileLock(path):
        pass


def test_windows_lock_uses_same_byte_and_releases_on_exception(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, size: calls.append((fd, mode, size))))
    path = tmp_path / "lock"
    path.write_bytes(b"existing-data")
    with pytest.raises(ValueError):
        with filelock.FileLock(path, readonly=True) as lock:
            lock.stream.seek(4)
            raise ValueError("simulado")
    assert calls[0][1:] == (2, 1) and calls[1] == (calls[0][0], 0, 1)
    assert path.read_bytes() == b"existing-data"


def test_windows_contention_is_nonblocking(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    def busy(*args):
        raise OSError(errno.EACCES, "busy")
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, locking=busy))
    lock = filelock.FileLock(tmp_path / "lock")
    with pytest.raises(BlockingIOError, match="Ya hay otro proceso"):
        lock.__enter__()
    assert lock.stream is None


def test_windows_brave_paths_and_streamlink(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_support.shutil, "which", lambda name: "tool.exe" if name == "streamlink.exe" else None)
    for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "User With Spaces"))
    brave = tmp_path / "User With Spaces/BraveSoftware/Brave-Browser/Application/brave.exe"
    brave.parent.mkdir(parents=True)
    brave.write_bytes(b"")
    assert platform_support.find_brave() == str(brave)
    assert platform_support.find_streamlink() == "tool.exe"
    assert "launch_brave.bat" in platform_support.launch_hint()


@pytest.mark.parametrize("missing", [False, True])
def test_windows_notification_never_uses_subprocess(monkeypatch, missing):
    calls = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winsound", None if missing else SimpleNamespace(MessageBeep=lambda: calls.append("beep")))
    async def forbidden(*args, **kw):
        raise AssertionError("Windows no debe ejecutar notify-send")
    monkeypatch.setattr(platform_support.asyncio, "create_subprocess_exec", forbidden)
    asyncio.run(platform_support.notify("success", "simulado"))
    assert calls == ([] if missing else ["beep"])


def test_absent_linux_notification_tool_is_optional(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(platform_support.shutil, "which", lambda name: None)
    asyncio.run(platform_support.notify("blocked", "simulado"))


def test_capture_close_terminates_and_kills_stalled_child_on_windows(monkeypatch):
    from sniper.capture import LatestFrame, StreamCapture
    calls = []
    class Proc:
        def poll(self):
            return None
        def terminate(self):
            calls.append("terminate")
        def kill(self):
            calls.append("kill")
    monkeypatch.setattr(sys, "platform", "win32")
    capture = StreamCapture("https://kick.com/elded", LatestFrame())
    capture._proc = Proc()
    capture.close()
    assert capture.stop_ev.is_set() and calls == ["terminate", "kill"]


def test_capture_uses_discovered_windows_streamlink(monkeypatch):
    from sniper.capture import streamlink_cmd
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(platform_support.shutil, "which", lambda name: "C:/With Spaces/streamlink.exe" if name == "streamlink.exe" else None)
    command = streamlink_cmd("https://kick.com/elded", "best")
    assert command[0] == "C:/With Spaces/streamlink.exe"
    assert "--kick-low-latency" in command and "--twitch-low-latency" not in command

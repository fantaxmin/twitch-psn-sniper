"""Detección de herramientas y notificaciones opcionales para Linux/Windows."""
import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def find_brave():
    names = ("brave.exe",) if sys.platform == "win32" else ("brave-browser", "brave", "brave-browser-stable")
    for name in names:
        executable = shutil.which(name)
        if executable:
            return executable
    if sys.platform == "win32":
        for variable in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            if os.environ.get(variable):
                candidate = Path(os.environ[variable]) / "BraveSoftware/Brave-Browser/Application/brave.exe"
                if candidate.is_file():
                    return str(candidate)
    return None


def find_streamlink():
    return shutil.which("streamlink.exe" if sys.platform == "win32" else "streamlink")


def launch_hint():
    return "scripts\\launch_brave.bat --kick elded" if sys.platform == "win32" else "scripts/launch_brave.sh --kick elded"


async def notify(status, message):
    log.warning("PSN %s: %s", status, message)
    if sys.platform == "win32":
        try:
            import winsound
            winsound.MessageBeep()
        except (ImportError, OSError, RuntimeError):
            pass
        return
    executable = shutil.which("notify-send")
    if executable is None:
        return
    try:
        process = await asyncio.create_subprocess_exec(
            executable, "PSN: " + status, message[:500],
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), timeout=2)
        except TimeoutError:
            process.kill()
            await process.wait()
    except OSError:
        pass

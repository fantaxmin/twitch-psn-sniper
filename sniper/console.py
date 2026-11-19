"""One terminal countdown line that stays readable alongside normal log messages."""
from __future__ import annotations

import logging
import shutil
from .platform_support import configure_console


class CountdownConsole(logging.StreamHandler):
    def __init__(self, stream=None):
        configure_console()
        super().__init__(stream)
        self.interactive = bool(getattr(self.stream, "isatty", lambda: False)())
        self._status = ""

    def _clear(self):
        if self.interactive and self._status:
            self.stream.write("\r\x1b[2K")

    def _draw(self):
        if self.interactive and self._status:
            width = max(20, shutil.get_terminal_size((100, 24)).columns - 1)
            self.stream.write(self._status[:width])
            self.flush()

    def update(self, text: str):
        if not self.interactive:
            return
        with self.lock:
            if text != self._status:
                self._clear()
                self._status = text
                self._draw()

    def emit(self, record):
        self._clear()
        super().emit(record)
        self._draw()

    def clear_status(self):
        with self.lock:
            self._clear()
            self._status = ""
            self.flush()

    def close(self):
        self.clear_status()
        super().close()

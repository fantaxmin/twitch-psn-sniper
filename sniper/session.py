"""CDP connection for Twitch only: no store pages, PSN modals, or credentials."""
from __future__ import annotations

from typing import Any

from .browser import CDP_URL, install_click_guard


class ChatBrowserSession:
    def __init__(self):
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None

    async def connect(self) -> None:
        from playwright.async_api import async_playwright
        install_click_guard()
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(CDP_URL, timeout=5000)
            if not self.browser.contexts:
                raise RuntimeError("Brave no tiene un contexto disponible; abre el perfil dedicado e inicia sesión en Twitch")
            self.context = self.browser.contexts[0]
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        # Disconnect our driver, never close the user's browser or existing tabs.
        try:
            if self.playwright is not None:
                await self.playwright.stop()
        finally:
            self.playwright = self.browser = self.context = None

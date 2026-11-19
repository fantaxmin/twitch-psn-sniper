"""CDP connection for Twitch only: no store pages, PSN modals, or credentials."""
from __future__ import annotations

from typing import Any

from .browser import CDP_URL, install_click_guard
from .platform_support import launch_hint


class ChatBrowserUnavailable(RuntimeError):
    """La sesión dedicada necesita preparación manual; no iniciar Brave automáticamente."""


class ChatBrowserSession:
    def __init__(self):
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None

    async def connect(self) -> None:
        from playwright.async_api import Error as PlaywrightError, async_playwright
        install_click_guard()
        try:
            self.playwright = await async_playwright().start()
            try:
                self.browser = await self.playwright.chromium.connect_over_cdp(CDP_URL, timeout=5000)
            except PlaywrightError as exc:
                raise ChatBrowserUnavailable(
                    f"No se pudo conectar al Brave dedicado en {CDP_URL}. "
                    f"Ejecuta {launch_hint(platform='twitch')} en otra terminal, inicia sesión en Twitch "
                    "y deja esa ventana abierta antes de volver a ejecutar el bot. "
                    "Abrir Brave de forma normal no activa el puerto 9333.") from exc
            if not self.browser.contexts:
                raise ChatBrowserUnavailable(f"Brave no tiene un contexto disponible; abre el perfil dedicado con {launch_hint(platform='twitch')} e inicia sesión en Twitch")
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

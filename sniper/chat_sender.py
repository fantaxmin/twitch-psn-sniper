"""Write through the user's existing Twitch session in the dedicated Brave context."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit

from .browser import visible_first
from .reactions import clean_reaction


class ChatUnavailable(RuntimeError):
    pass


class ChatRestricted(ChatUnavailable):
    pass


class ChatAuthenticationRequired(ChatUnavailable):
    pass


class ChatNotReady(ChatUnavailable):
    pass


class BraveChatSender:
    def __init__(self, context: Any, channel: str, *, stop: Any = None, start_timeout: float = 15):
        if not re.fullmatch(r"[a-zA-Z0-9_]{1,25}", channel):
            raise ValueError("Canal de Twitch inválido")
        self.context = context
        self.channel = channel.lower()
        self.page: Any = None
        self.owned = False
        self.input: Any = None
        self.send_button: Any = None
        self.emote_only = False
        self.next_allowed = 0.0
        self.restricted = False
        self.stop = stop
        self.start_timeout = start_timeout

    async def start(self) -> None:
        if self.page is None or self.page.is_closed():
            for candidate in self.context.pages:
                parsed = urlsplit(candidate.url)
                if parsed.hostname in {"www.twitch.tv", "twitch.tv"} and parsed.path.rstrip("/").lower() == f"/{self.channel}":
                    self.page = candidate
                    break
            else:
                self.page = await self.context.new_page()
                self.owned = True
                await self.page.goto(f"https://www.twitch.tv/{self.channel}", wait_until="domcontentloaded", timeout=30000)
        # El asistente puede haber abierto esta pestaña hace unos milisegundos.
        # Esperar también cuando se reutiliza; DOMContentLoaded no garantiza chat listo.
        deadline = time.monotonic() + self.start_timeout
        while True:
            if self.stop is not None and self.stop.is_set():
                raise ChatUnavailable("Daemon detenido; preparación del chat cancelada")
            try:
                await self.refresh()
                return
            except ChatNotReady as exc:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ChatNotReady(
                        "El chat no estuvo listo tras esperar su carga. Abre el chat en la "
                        "ventana del Brave dedicado y comprueba si Twitch muestra un aviso. "
                        "No se confirmó que falte iniciar sesión. Detalle: " + str(exc)) from exc
                await asyncio.sleep(min(0.25, remaining))

    async def refresh(self) -> None:
        if self.restricted:
            raise ChatRestricted("Participación pausada por una restricción de Twitch; requiere revisión manual")
        if self.page is None or self.page.is_closed():
            raise ChatUnavailable("La pestaña de Twitch está cerrada")
        parsed = urlsplit(self.page.url)
        if parsed.hostname not in {"www.twitch.tv", "twitch.tv"} or parsed.path.rstrip("/").lower() != f"/{self.channel}":
            raise ChatUnavailable("La pestaña cambió de canal; no se enviará el mensaje")
        if await visible_first(self.page.locator('[data-a-target="login-button"]')) is not None:
            raise ChatAuthenticationRequired(
                "Twitch muestra Iniciar sesión en el perfil dedicado de Brave. "
                "Inicia sesión en esa ventana y vuelve a arrancar el bot. "
                "Guardar tu nombre de usuario en la configuración no autentica la cuenta.")
        # Scope all text checks to the chat input UI, never to messages from viewers.
        area = await visible_first(self.page.locator('[data-a-target="chat-input"], [data-slate-editor="true"][contenteditable="true"]'))
        if area is None:
            raise ChatNotReady("No se encontró un cuadro de chat visible; puede estar cargando o cerrado")
        if not await area.is_editable():
            raise ChatNotReady("El cuadro de chat está visible pero Twitch todavía no permite escribir")
        self.input = area
        self.send_button = await visible_first(self.page.locator('[data-a-target="chat-send-button"]'))
        if self.send_button is None:
            self.send_button = await visible_first(self.page.get_by_role("button", name=re.compile(r"^(?:chat|enviar|send)$", re.I)))
        if self.send_button is None:
            raise ChatNotReady("No se encontró el botón para enviar al chat")
        notice = await self._notice()
        lower = notice.casefold()
        if re.search(r"(?:se te ha|has sido).*(?:vetado|suspendido)|you (?:are|have been).*(?:banned|timed out)|no puedes enviar mensajes", lower):
            self.restricted = True
            raise ChatRestricted(notice)
        self.emote_only = bool(re.search(r"(?:solo|únicamente) (?:emotes|emoticonos)|emote.only", lower))
        slow = re.search(r"(?:espera|wait).*?(\d+)\s*(?:s\b|segundos?|seconds?)", lower)
        if slow:
            self.next_allowed = max(self.next_allowed, time.perf_counter() + int(slow.group(1)) + 1)

    async def _notice(self) -> str:
        parts = []
        notices = self.page.locator('[data-a-target="chat-input-error"], [data-a-target="chat-input-notice"], [data-test-selector="chat-input-notice"]')
        for index in range(await notices.count()):
            notice = notices.nth(index)
            if await notice.is_visible():
                parts.append(await notice.inner_text(timeout=1000))
        return " ".join(parts)

    async def send(self, message: str) -> None:
        if self.stop is not None and self.stop.is_set():
            raise ChatUnavailable("Daemon detenido; mensaje cancelado")
        await self.refresh()
        if time.perf_counter() < self.next_allowed:
            raise ChatUnavailable("Twitch pide esperar; se omite este turno")
        if not clean_reaction(message):
            raise ValueError("Mensaje de chat inválido")
        try:
            # The user explicitly gives daemon messages priority while it runs.
            # fill replaces the entire input, including drafts and empty-editor newlines.
            await self.input.fill(message, timeout=2000)
            if self.stop is not None and self.stop.is_set():
                raise ChatUnavailable("Daemon detenido; mensaje cancelado")
            if not await self.send_button.is_enabled():
                raise ChatUnavailable("Twitch no habilitó el envío; se omite este turno")
            # Exactly one dispatch. A timeout is uncertain, so never retry this message.
            await self.send_button.click(timeout=2000)
            self.next_allowed = time.perf_counter() + 30
            await asyncio.sleep(0.15)
            notice = await self._notice()
            if re.search(r"no (?:se (?:ha )?pudo |se puede |puedes )?(?:enviar|enviado)|not sent|wait.*\d+|espera.*\d+|banned|timed out", notice, re.I):
                await self.refresh()
                raise ChatUnavailable("Twitch mostró un aviso después del envío: " + notice[:300])
        finally:
            # Clear only our own residual draft, including on an uncertain click.
            try:
                value = await self.input.evaluate("el => el.value === undefined ? el.innerText : el.value")
                if value == message:
                    await self.input.fill("", timeout=1000)
            except Exception:
                pass

    async def close(self) -> None:
        if self.owned and self.page is not None and not self.page.is_closed():
            await self.page.close()

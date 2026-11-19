"""Write through the user's existing Twitch session in the dedicated Brave context."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any
from urllib.parse import urlsplit

from .browser import guarded_click, visible_first
from .reactions import clean_reaction

INPUT_SELECTORS = (
    '[data-a-target="chat-input"][contenteditable="true"]',
    '[data-a-target="chat-input"] [data-slate-editor="true"][contenteditable="true"]',
    'textarea[data-a-target="chat-input"]',
    '[data-slate-editor="true"][contenteditable="true"]',
    '[data-a-target="chat-input"]',
)
SEND_BUTTON_SELECTOR = '[data-a-target="chat-send-button"], [data-test-selector="chat-send-button"]'
SEND_BUTTON_NAME = re.compile(
    r"^(?:chat|chatear|enviar(?:\s+(?:al\s+chat|mensaje))?|send(?:\s+(?:a\s+)?message|\s+chat)?)$",
    re.I,
)
REJECTION_NOTICE = re.compile(
    r"no (?:se (?:ha )?pudo |se puede |puedes |se )?(?:enviar|enviado|envi[oó])"
    r"|not sent|wasn['’]t (?:posted|sent)|could not be sent"
    r"|wait.*?\d+\s*(?:s\b|seconds?)|espera.*?\d+\s*(?:s\b|segundos?)"
    r"|banned|timed out|vetado|suspendido",
    re.I,
)


def _normalize_input_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\u200b", "").split())


class ChatUnavailable(RuntimeError):
    pass


class ChatRestricted(ChatUnavailable):
    pass


class ChatAuthenticationRequired(ChatUnavailable):
    pass


class ChatNotReady(ChatUnavailable):
    pass


class BraveChatSender:
    def __init__(self, context: Any, channel: str, *, stop: Any = None,
                 start_timeout: float = 15, send_button_timeout: float = 2.0):
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
        self.slow_until = 0.0
        self.restricted = False
        self.stop = stop
        self.start_timeout = start_timeout
        self.send_button_timeout = send_button_timeout

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
        area = await self._find_input()
        if area is None:
            raise ChatNotReady("No se encontró un cuadro de chat visible; puede estar cargando o cerrado")
        if not await area.is_editable():
            raise ChatNotReady("El cuadro de chat está visible pero Twitch todavía no permite escribir")
        self.input = area
        self.send_button = await self._find_send_button()
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
            self.slow_until = max(self.slow_until, time.perf_counter() + int(slow.group(1)) + 1)
            self.next_allowed = max(self.next_allowed, self.slow_until)

    async def _find_input(self) -> Any | None:
        if self.page is None or self.page.is_closed():
            return self.input
        for selector in INPUT_SELECTORS:
            area = await visible_first(self.page.locator(selector))
            if area is not None:
                return area
        return None

    async def _find_send_button(self) -> Any | None:
        if self.page is None or self.page.is_closed():
            return self.send_button
        button = await visible_first(self.page.locator(SEND_BUTTON_SELECTOR))
        if button is None:
            button = await visible_first(self.page.get_by_role("button", name=SEND_BUTTON_NAME))
        return button

    async def _button_ready(self, button: Any) -> bool:
        is_visible = getattr(button, "is_visible", None)
        if callable(is_visible) and not await is_visible():
            return False
        if not await button.is_enabled():
            return False
        get_attr = getattr(button, "get_attribute", None)
        if callable(get_attr):
            aria_disabled = await get_attr("aria-disabled")
            if isinstance(aria_disabled, str) and aria_disabled.casefold() == "true":
                return False
        return True

    async def _wait_send_button(self, *, timeout: float | None = None) -> Any:
        timeout = self.send_button_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        elapsed = 0.0
        button = None
        while True:
            if self.stop is not None and self.stop.is_set():
                raise ChatUnavailable("Daemon detenido; mensaje cancelado")
            button = await self._find_send_button()
            if button is not None:
                self.send_button = button
                if await self._button_ready(button):
                    return button
            remaining = min(deadline - time.monotonic(), timeout - elapsed)
            if remaining <= 0:
                break
            step = min(0.05, remaining)
            await asyncio.sleep(step)
            elapsed += step
        if button is None:
            raise ChatUnavailable("No se encontró el botón Enviar del chat; se omite este turno")
        raise ChatUnavailable("Twitch no habilitó el botón Enviar; se omite este turno")

    async def _is_slate_input(self) -> bool:
        get_attr = getattr(self.input, "get_attribute", None)
        if not callable(get_attr):
            return False
        try:
            slate = await get_attr("data-slate-editor")
            editable = await get_attr("contenteditable")
        except Exception:
            return False
        return slate == "true" or editable == "true"

    async def _write_input(self, message: str) -> None:
        keyboard = getattr(self.page, "keyboard", None) if self.page is not None else None
        if keyboard is not None and await self._is_slate_input():
            await guarded_click(self.input, timeout=2000)
            await keyboard.press("ControlOrMeta+A")
            await keyboard.press("Backspace")
            await keyboard.insert_text(message)
            return
        await self.input.fill(message, timeout=2000)

    async def _input_text(self) -> str | None:
        try:
            value = await self.input.evaluate("el => el.value === undefined ? el.innerText : el.value")
        except Exception:
            return None
        return _normalize_input_text(value)

    async def _clear_input(self) -> None:
        keyboard = getattr(self.page, "keyboard", None) if self.page is not None else None
        if keyboard is not None and await self._is_slate_input():
            await guarded_click(self.input, timeout=1000)
            await keyboard.press("ControlOrMeta+A")
            await keyboard.press("Backspace")
            return
        await self.input.fill("", timeout=1000)

    async def _confirm_sent(self, message: str, *, notice_delay: float = 0.15, clear_timeout: float = 0.5) -> None:
        await asyncio.sleep(notice_delay)
        wait_budget = max(0.0, clear_timeout - notice_delay)
        deadline = time.monotonic() + wait_budget
        elapsed = 0.0
        while True:
            notice = await self._notice()
            if REJECTION_NOTICE.search(notice):
                await self.refresh()
                raise ChatUnavailable("Twitch mostró un aviso después del envío: " + notice[:300])
            current = await self._input_text()
            if current is None or current != message:
                return
            remaining = min(deadline - time.monotonic(), wait_budget - elapsed)
            if remaining <= 0:
                break
            step = min(0.05, remaining)
            await asyncio.sleep(step)
            elapsed += step
        raise ChatUnavailable("El mensaje siguió en el cuadro de chat tras pulsar Enviar; no se confirmó el envío")

    async def _notice(self) -> str:
        parts = []
        notices = self.page.locator('[data-a-target="chat-input-error"], [data-a-target="chat-input-notice"], [data-test-selector="chat-input-notice"]')
        for index in range(await notices.count()):
            notice = notices.nth(index)
            if await notice.is_visible():
                parts.append(await notice.inner_text(timeout=1000))
        return " ".join(parts)

    async def send(self, message: str) -> None:
        await self._send(message)

    async def send_raffle(self, message: str) -> None:
        # La frase confirmada no espera el intervalo local de las reacciones.
        # El modo lento y las restricciones explícitas de Twitch siguen aplicándose.
        await self._send(message, raffle=True)

    async def _send(self, message: str, *, raffle: bool = False) -> None:
        if self.stop is not None and self.stop.is_set():
            raise ChatUnavailable("Daemon detenido; mensaje cancelado")
        await self.refresh()
        if time.perf_counter() < (self.slow_until if raffle else self.next_allowed):
            raise ChatUnavailable("Twitch pide esperar; se omite este turno")
        message = clean_reaction(message)
        if not message:
            raise ValueError("Mensaje de chat inválido")
        try:
            # The user explicitly gives daemon messages priority while it runs.
            # Slate editors need real keyboard input events so React enables Enviar.
            await self._write_input(message)
            button = await self._wait_send_button()
            if self.stop is not None and self.stop.is_set():
                raise ChatUnavailable("Daemon detenido; mensaje cancelado")
            # Exactly one dispatch. A timeout is uncertain, so never retry this message.
            await guarded_click(button, timeout=2000)
            self.next_allowed = time.perf_counter() + 30
            await self._confirm_sent(message)
        finally:
            # Clear only our own residual draft, including on an uncertain click.
            try:
                if await self._input_text() == message:
                    await self._clear_input()
            except Exception:
                pass

    async def close(self) -> None:
        if self.owned and self.page is not None and not self.page.is_closed():
            await self.page.close()

"""Read incoming whispers from an existing Twitch page. Never send or extract tokens.

Twitch's private Hermes/PubSub envelopes and DOM can change. Unknown payloads are
ignored; only incoming whisper events or a newly increased whisper badge trigger.
CDP supplements page.websocket to observe frames on sockets opened before attach.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


@dataclass
class Whisper:
    sender: str
    text: str
    received_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    message_id: str = ""
    source: str = "websocket"


def parse_whispers(payload: str | bytes, own_login: str = "") -> list[Whisper]:
    """Normalize known received-event envelopes; don't parse arbitrary chat text."""
    if not isinstance(payload, (str, bytes)) or len(payload) > 1_000_000:
        return []
    try:
        root = json.loads(payload)
    except (ValueError, UnicodeError):
        return []
    found: list[Whisper] = []
    received = {"user.whisper.message", "whisper_received", "whisperreceived", "whisper-received"}
    wrappers = {"", "message", "notification", "event", "data", "response", "pubsub"}

    def walk(node: Any, depth: int = 0, hint: bool = False) -> None:
        if depth > 9 or len(found) >= 16:
            return
        if isinstance(node, str):
            try:
                node = json.loads(node)
            except (ValueError, UnicodeError):
                return
        if isinstance(node, list):
            for item in node[:100]:
                walk(item, depth + 1, hint)
            return
        if not isinstance(node, dict):
            return
        kind = str(node.get("type", node.get("event_type", ""))).casefold()
        if kind in {"whisper_sent", "whispers_sent", "whisper_send", "message_sent"}:
            return
        subscription = node.get("subscription", {})
        sub_type = subscription.get("type", "") if isinstance(subscription, dict) else ""
        if sub_type and sub_type != "user.whisper.message":
            return
        topic = node.get("topic", "")
        if isinstance(topic, str) and topic and not topic.startswith(("whispers.", "user-whispers", "user.whisper")):
            return
        is_received = kind in received or sub_type == "user.whisper.message" or hint
        if kind not in wrappers and kind not in received and not is_received:
            return
        event = node.get("event", node.get("data", node))
        if is_received and isinstance(event, dict):
            message = event.get("whisper", {})
            text = message.get("text") if isinstance(message, dict) else None
            text = text if isinstance(text, str) else event.get("body", event.get("text"))
            tags = event.get("tags", {})
            tags = tags if isinstance(tags, dict) else {}
            sender = event.get("from_user_login", event.get("from_user_name", tags.get("login", tags.get("display_name", event.get("sender", "")))))
            recipient = event.get("to_user_login", "")
            outgoing = event.get("direction") in {"outgoing", "sent"} or event.get("is_own") is True
            if own_login and (str(sender).casefold() == own_login.casefold()
                              or (recipient and str(recipient).casefold() != own_login.casefold())):
                outgoing = True
            if isinstance(text, str) and isinstance(sender, str) and sender and not outgoing:
                found.append(Whisper(sender[:100], text[:10000],
                                     message_id=str(event.get("whisper_id", event.get("message_id", event.get("id", ""))))[:200]))
                return
        for key in ("data", "payload", "message", "event", "notification", "notifications", "messages"):
            if key in node:
                walk(node[key], depth + 1, is_received)

    walk(root)
    return found


DOM_SNAPSHOT_JS = """() => {
    const button = document.querySelector('[data-a-target="whisper-box-button"]');
    const badge = button && (button.querySelector('[data-a-target="unread-count"], [data-a-target="number-badge"], [data-test-selector="unread-count"]'));
    const countText = badge ? badge.textContent : button ? (button.getAttribute('aria-label') || button.innerText) : '';
    const match = countText && countText.match(/(\\d+)/);
    const unread = match ? Number(match[1]) : button ? 0 : null;
    const account = document.querySelector('[data-a-target="user-menu-toggle"]');
    const login = account && (account.getAttribute('data-user-login') || account.getAttribute('data-login')) || '';
    const title = document.querySelector('[data-a-target="whisper-thread-title"], [data-test-selector="whisper-thread-title"]');
    const rows = Array.from(document.querySelectorAll('[data-a-target="whisper-message"], [data-test-selector="whisper-message"], [data-a-target="whisper-thread-message"]'));
    const messages = rows.slice(-40).map(row => {
        const author = row.querySelector('[data-a-target="whisper-message-author"], [data-test-selector="whisper-message-author"]');
        const body = row.querySelector('[data-a-target="whisper-message-text"], [data-test-selector="whisper-message-text"]');
        const sender = row.getAttribute('data-sender') || (author && author.textContent) || (title && title.textContent) || '';
        const direction = row.getAttribute('data-direction');
        const own = row.getAttribute('data-is-own');
        const outgoing = direction === 'outgoing' || direction === 'sent' || own === 'true' || (login && sender.toLowerCase().trim() === login.toLowerCase());
        const incoming = !outgoing && (direction === 'incoming' || direction === 'received' || own === 'false' || row.classList.contains('whisper-message--received') || (login && sender));
        return {sender: sender.trim().slice(0, 100), text: (body ? body.textContent : row.innerText).slice(0, 10000),
                id: row.getAttribute('data-message-id') || row.id || '', incoming: Boolean(incoming), outgoing};
    });
    return {unread, login, messages, expanded: Boolean(button && button.getAttribute('aria-expanded') === 'true')};
}"""


class WhisperWatcher:
    def __init__(self, context: Any, channel: str, on_whisper: Any, *, stop: Any):
        self.context, self.channel, self.on_whisper, self.stop = context, channel.lower(), on_whisper, stop
        self.page: Any = None
        self.cdp: Any = None
        self.queue: asyncio.Queue[Whisper] = asyncio.Queue(maxsize=32)
        self.seen: set[str] = set()
        self.dom_seen: set[str] = set()
        self.unread: int | None = None
        self.own_login = ""
        self.sockets: list[tuple[Any, Any]] = []
        self._cdp_handler: Any = None

    @staticmethod
    def _key(sender: str, text: str, message_id: str = "") -> str:
        return message_id or hashlib.sha256((sender + "\0" + text).encode()).hexdigest()

    def _enqueue(self, whisper: Whisper) -> None:
        key = self._key(whisper.sender, whisper.text, whisper.message_id)
        if key in self.seen or self.queue.full() or self.stop.is_set():
            return
        self.seen.add(key)
        self.queue.put_nowait(whisper)

    def _payload(self, payload: str | bytes) -> None:
        for whisper in parse_whispers(payload, self.own_login):
            self._enqueue(whisper)

    def _socket(self, socket: Any) -> None:
        host = urlsplit(socket.url).hostname or ""
        if host != "twitch.tv" and not host.endswith(".twitch.tv"):
            return
        socket.on("framereceived", self._payload)
        self.sockets.append((socket, self._payload))

    async def start(self) -> bool:
        for page in self.context.pages:
            parsed = urlsplit(page.url)
            if parsed.hostname in {"www.twitch.tv", "twitch.tv"} and parsed.path.rstrip("/").lower() == "/" + self.channel:
                self.page = page
                break
        if self.page is None:
            return False
        self.page.on("websocket", self._socket)
        try:
            self.cdp = await self.context.new_cdp_session(self.page)
            def received(event):
                response = event.get("response", {})
                data = response.get("payloadData", "")
                if response.get("opcode") == 2:
                    try:
                        data = base64.b64decode(data, validate=True)
                    except ValueError:
                        return
                self._payload(data)
            self._cdp_handler = received
            self.cdp.on("Network.webSocketFrameReceived", received)
            await self.cdp.send("Network.enable")
        except Exception as exc:
            log.warning("CDP de susurros no disponible (%s); se usa DOM y sockets nuevos", type(exc).__name__)
        try:
            baseline = await self.page.evaluate(DOM_SNAPSHOT_JS)
        except Exception:
            await self.close()
            raise
        self.unread = baseline.get("unread")
        self.own_login = baseline.get("login", "")
        for row in baseline.get("messages", []):
            self.dom_seen.add(self._key(row.get("sender", ""), row.get("text", ""), row.get("id", "")))
        if self.unread is None and not baseline.get("messages"):
            log.warning("Indicador DOM de susurros no identificado; validar los selectores en Twitch. Se mantiene la escucha WebSocket")
        log.info("Escucha de susurros conectada a la pestaña existente; no se leen credenciales")
        return True

    async def poll_dom(self) -> None:
        snapshot = await self.page.evaluate(DOM_SNAPSHOT_JS)
        current = snapshot.get("unread")
        increased = current is not None and self.unread is not None and current > self.unread
        self.unread = current
        if snapshot.get("login"):
            self.own_login = snapshot["login"]
        if increased:
            # Open only Twitch's whisper UI, never message links or a reply box.
            button = self.page.locator('button[data-a-target="whisper-box-button"]')
            if not snapshot.get("expanded") and await button.count():
                await button.first.click(force=True, timeout=1500)
                opened_thread = False
                for _ in range(10):
                    thread = self.page.locator('button[data-a-target="whisper-thread-list-item"][data-unread="true"]')
                    if not opened_thread and await thread.count():
                        await thread.first.click(force=True, timeout=1500)
                        opened_thread = True
                    await asyncio.sleep(0.15)
                    snapshot = await self.page.evaluate(DOM_SNAPSHOT_JS)
                    new_rows = any(self._key(row.get("sender", ""), row.get("text", ""), row.get("id", ""))
                                   not in self.dom_seen for row in snapshot.get("messages", []))
                    if new_rows or not self.queue.empty():
                        break
        emitted = False
        rows = snapshot.get("messages", [])
        for row in rows:
            key = self._key(row.get("sender", ""), row.get("text", ""), row.get("id", ""))
            if key in self.dom_seen:
                continue
            self.dom_seen.add(key)
            incoming = row.get("incoming") or (increased and row is rows[-1])
            if incoming and not row.get("outgoing") and row.get("text"):
                self._enqueue(Whisper(row.get("sender") or "desconocido", row["text"],
                                      message_id=row.get("id", ""), source="dom"))
                emitted = True
        if increased and not emitted and self.queue.empty():
            # Receipt is known, but private UI selectors may have changed. Alert and
            # stop without inventing text or guessing/redeeming a coupon.
            self._enqueue(Whisper("desconocido", "", source="dom_unread_indicator"))

    async def run(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    if self.page is None and not await self.start():
                        await asyncio.sleep(1)
                        continue
                    if self.page.is_closed():
                        await self.close()
                        await asyncio.sleep(1)
                        continue
                    try:
                        whisper = await asyncio.wait_for(self.queue.get(), timeout=0.5)
                    except TimeoutError:
                        await self.poll_dom()
                        continue
                    await self.on_whisper(whisper)
                except Exception as exc:
                    log.warning("Escucha de susurros: %s; se reintentará sin enviar mensajes", type(exc).__name__)
                    await asyncio.sleep(1)
        finally:
            await self.close()

    async def close(self) -> None:
        if self.page is not None:
            self.page.remove_listener("websocket", self._socket)
        for socket, handler in self.sockets:
            socket.remove_listener("framereceived", handler)
        self.sockets.clear()
        if self.cdp is not None:
            if self._cdp_handler is not None:
                self.cdp.remove_listener("Network.webSocketFrameReceived", self._cdp_handler)
            try:
                await self.cdp.detach()
            except Exception:
                pass
        self.cdp = self.page = None

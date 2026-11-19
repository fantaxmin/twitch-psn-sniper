"""Periodic chat participation; no catch-up bursts or posts on stale/offline frames."""
from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

from .chat_sender import ChatAuthenticationRequired, ChatRestricted, ChatUnavailable

log = logging.getLogger(__name__)


class ChatParticipation:
    def __init__(self, *, frames: Any, capture: Any, stop: Any, composer: Any,
                 sender: Any = None, interval: float = 300, dry_run: bool = False,
                 event: Any = None, status_display: Any = None, wait_first: bool = False):
        if interval < 30:
            raise ValueError("El intervalo de chat debe ser al menos 30 segundos")
        self.frames, self.capture, self.stop = frames, capture, stop
        self.composer, self.sender = composer, sender
        self.interval, self.dry_run = interval, dry_run
        self.event = event or (lambda **kwargs: None)
        self.next_due = time.perf_counter() + interval if wait_first else 0.0
        self.last_seq = 0
        self.disabled = False
        self.status_display = status_display
        self.sent_count = 0
        self.preview_count = 0
        self.last_message: str | None = None
        self.phase = ""
        self.retry_at = 0.0
        self._last_report_at = float("-inf")
        self._last_report_kind = ""

    def _priority_due(self):
        method = getattr(self.composer, "priority_due", None)
        due = method() if callable(method) else None
        if due is not None and self.sender is not None:
            due = max(due, getattr(self.sender, "slow_until", 0.0))
        if due is not None:
            due = max(due, self.retry_at)
        return due

    def status(self) -> tuple[str, str]:
        """Read the real send deadline without advancing or changing the schedule."""
        count = f"Simulados: {self.preview_count}" if self.dry_run else f"Enviados: {self.sent_count}"
        if self.stop.is_set():
            return "stopped", f"Chat detenido | {count}"
        if self.disabled:
            return "restricted", f"Chat pausado por restricción de Twitch | {count}"
        if self.phase:
            return self.phase, f"Chat: {self.phase} | {count}"
        if self.retry_at > time.perf_counter():
            seconds = math.ceil(self.retry_at - time.perf_counter())
            return "unavailable", f"Chat no disponible; reintento en {seconds:02d} s | {count}"
        priority_due = self._priority_due()
        if priority_due is not None and priority_due <= self.next_due:
            seconds = max(0, math.ceil(priority_due - time.perf_counter()))
            if seconds:
                minutes, seconds = divmod(seconds, 60)
                return "countdown", f"Próximo mensaje de rifa en {minutes:02d}:{seconds:02d} | {count}"
            return "ready", f"Rifa confirmada: mensaje listo para enviar | {count}"
        if not self.capture.live.is_set():
            return "offline", f"Chat: esperando stream en vivo | {count}"
        seq, frame, timestamp = self.frames.get_newer(self.last_seq, timeout=0)
        if frame is None or time.perf_counter() - timestamp > 15:
            return "stale", f"Chat: esperando imagen reciente del stream | {count}"
        seconds = max(0, math.ceil(self.next_due - time.perf_counter()))
        if seconds:
            minutes, seconds = divmod(seconds, 60)
            return "countdown", f"Próximo mensaje en {minutes:02d}:{seconds:02d} | {count}"
        if seq == self.last_seq:
            return "frame", f"Chat: esperando un fotograma nuevo | {count}"
        return "ready", f"Chat: próximo mensaje listo para preparar | {count}"

    def report_status(self) -> None:
        kind, text = self.status()
        if self.status_display is not None:
            self.status_display(text)
        elif kind != self._last_report_kind or time.perf_counter() - self._last_report_at >= 30:
            # Redirected output stays readable without 300 new lines per interval.
            log.info("%s", text)
            self._last_report_at = time.perf_counter()
            self._last_report_kind = kind

    async def _status_loop(self) -> None:
        while not self.stop.is_set():
            self.report_status()
            await asyncio.sleep(1)

    async def _compose_normal(self, frame, *, emote_only):
        wakeup = getattr(self.composer, "wakeup", None)
        if wakeup is None:
            return await self.composer.compose(frame, emote_only=emote_only)
        composing = asyncio.create_task(self.composer.compose(frame, emote_only=emote_only))
        waking = None
        try:
            while True:
                waking = asyncio.create_task(wakeup.wait())
                done, _ = await asyncio.wait({composing, waking}, return_when=asyncio.FIRST_COMPLETED)
                if composing in done:
                    return await composing
                wakeup.clear()
                due = self._priority_due()
                if due is not None and time.perf_counter() >= due:
                    return None  # La frase confirmada no espera una inferencia de visión lenta.
        finally:
            tasks = [task for task in (composing, waking) if task is not None]
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def tick(self) -> None:
        if self.stop.is_set() or self.disabled or time.perf_counter() < self.retry_at:
            return
        priority_due = self._priority_due()
        priority = priority_due is not None and time.perf_counter() >= priority_due
        if not priority and time.perf_counter() < self.next_due:
            return
        seq, frame, timestamp = self.frames.get_newer(self.last_seq, timeout=0)
        if not priority and (not self.capture.live.is_set() or frame is None
                             or seq == self.last_seq or time.perf_counter() - timestamp > 15):
            return
        if not priority:
            self.last_seq = seq
        normal_composed = False
        message_attempted = False
        try:
            if not self.dry_run:
                self.phase = "conectando al chat"
                await self.sender.start()
            self.phase = "preparando mensaje"
            emote_only = bool(self.sender and self.sender.emote_only)
            if priority:
                priority_due = self._priority_due()
                if priority_due is None or time.perf_counter() < priority_due:
                    return
                result = await self.composer.compose_priority(emote_only=emote_only)
                if result is None:
                    return
                message, source = result
            else:
                normal_composed = True
                result = await self._compose_normal(frame.copy(), emote_only=emote_only)
                # Si la confirmación llegó preparando una reacción, la frase tiene prioridad.
                priority_due = self._priority_due()
                if priority_due is not None and time.perf_counter() >= priority_due:
                    priority = True
                    result = await self.composer.compose_priority(emote_only=emote_only)
                    if result is None:
                        return
                if result is None:
                    return
                message, source = result
            if self.stop.is_set() or (not priority and (not self.capture.live.is_set() or time.perf_counter() - timestamp > 20)):
                self.event(kind="chat_skipped", reason="stopped_or_stale")
                return
            if self.dry_run:
                message_attempted = True
                self.preview_count += 1
                log.info("CHAT SIMULADO #%d [%s]: %s", self.preview_count, source, message)
            else:
                self.phase = "enviando mensaje"
                send = getattr(self.sender, "send_raffle", self.sender.send) if priority else self.sender.send
                message_attempted = True
                await send(message)
                self.composer.remember(message)
                self.sent_count += 1
                self.last_message = message
                log.info("CHAT ENVIADO #%d [%s]: %s", self.sent_count, source, message)
            self.event(kind="chat_message", message=message, source=source, dry_run=self.dry_run)
        except (ChatRestricted, ChatAuthenticationRequired) as exc:
            self.disabled = True
            log.error("Participación en chat pausada: %s", exc)
            self.event(kind="chat_restricted", message=str(exc))
        except ChatUnavailable as exc:
            self.retry_at = time.perf_counter() + 30
            log.warning("Turno de chat omitido: %s", exc)
            self.event(kind="chat_unavailable", message=str(exc))
        except Exception as exc:
            self.retry_at = time.perf_counter() + 30
            # Unknown send failures may already have reached Twitch. Never retry.
            log.warning("Turno de chat falló (%s); no se reintentará este mensaje", type(exc).__name__)
            self.event(kind="chat_error", error=type(exc).__name__)
        finally:
            if normal_composed:
                after_normal = getattr(self.composer, "after_normal_attempt", None)
                if callable(after_normal):
                    after_normal(attempted=message_attempted and not priority)
            if priority:
                after_attempt = getattr(self.composer, "after_priority_attempt", None)
                if callable(after_attempt):
                    after_attempt()
            if message_attempted or not priority:
                self.next_due = time.perf_counter() + self.interval
            self.phase = ""

    async def run(self) -> None:
        status_task = asyncio.create_task(self._status_loop())
        try:
            while not self.stop.is_set():
                await self.tick()
                wakeup = getattr(self.composer, "wakeup", None)
                if wakeup is None:
                    await asyncio.sleep(1)
                else:
                    try:
                        await asyncio.wait_for(wakeup.wait(), timeout=1)
                    except TimeoutError:
                        pass
                    wakeup.clear()
        finally:
            status_task.cancel()
            await asyncio.gather(status_task, return_exceptions=True)
            self.report_status()

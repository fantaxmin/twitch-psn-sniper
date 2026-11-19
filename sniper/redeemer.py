"""Serialized, deduplicated PSN redemption through an already authenticated Brave."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from sniper.browser import REQUEST_SUBMIT_JS, BraveBrowser, BrowserStateError, classify_message
from sniper.filelock import lock_directory
from sniper.platform_support import notify

log = logging.getLogger(__name__)
HALT_STATUSES = {"blocked", "captcha", "session_lost"}
ROUND_MAX_AGE = 90.0
MANUAL_RESOLUTION_STATUSES = ("success", "invalid", "already_used", "region", "blocked", "captcha", "session_lost", "error")


@dataclass
class RedeemResult:
    code: str
    tab: str
    status: str
    message: str
    t_submit: float
    t_result: float
    screenshot: str | None


class Redeemer:
    def __init__(self, *, runs_dir: str | Path = "runs", result_timeout: float = 20.0,
                 browser: BraveBrowser | None = None, ready_lock_timeout: float = 30.0,
                 stop_after_success: bool = False, global_lock_held: bool = False):
        self.runs_dir = Path(runs_dir)
        self.browser = browser or BraveBrowser(runs_dir=self.runs_dir)
        self.result_timeout = result_timeout
        self.ready_lock_timeout = ready_lock_timeout
        self.stop_after_success = stop_after_success
        self.global_lock_held = global_lock_held
        self._lock = asyncio.Lock()
        self._seen: dict[str, RedeemResult] = {}
        self._resolutions: list[dict] = []
        self._last: RedeemResult | None = None
        self._halt: tuple[str, str] | None = None
        self._started = False
        self._round_requested = False
        self._process_lock: Any = None
        self._ledger = self.runs_dir / "redeemer-attempts.json"

    def _load(self) -> None:
        if not self._ledger.exists():
            return
        payload = json.loads(self._ledger.read_text(encoding="utf-8"))
        if payload.get("version") != 1:
            raise RuntimeError("Versión desconocida del registro de canjes; no se enviará nada")
        self._seen = {code: RedeemResult(**result) for code, result in payload["attempts"].items()}
        self._seen = {code: result for code, result in self._seen.items()
                      if not (result.status == "error" and result.t_submit == 0.0)}
        self._resolutions = payload.get("resolutions", [])
        # An interrupted request cannot be treated as a rejection or retried.
        unresolved = [r for r in self._seen.values() if r.status == "timeout"]
        if unresolved:
            self._last = unresolved[-1]
            self._halt = ("timeout", "Hay un canje anterior sin resultado confirmado")
        elif self._seen:
            self._last = next(reversed(self._seen.values()))
            if self._last.status in HALT_STATUSES:
                self._halt = (self._last.status, self._last.message)

    def _save(self, result: RedeemResult | None) -> None:
        if result is not None:
            self._seen[result.code] = result
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "attempts": {code: asdict(r) for code, r in self._seen.items()},
                   "resolutions": self._resolutions}
        fd, temporary = tempfile.mkstemp(prefix=".redeemer-", dir=self.runs_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.flush()
                # Atomic replace, without fsync on the send path. Recent writes may
                # be lost on power failure; process restarts still retain dedup.
            os.replace(temporary, self._ledger)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @property
    def rejection_count(self) -> int:
        return sum(result.status in {"invalid", "already_used", "region"} for result in self._seen.values())

    @property
    def has_success(self) -> bool:
        return any(result.status == "success" for result in self._seen.values())

    @property
    def has_pending_timeout(self) -> bool:
        return any(result.status == "timeout" for result in self._seen.values())

    @classmethod
    def resolve_manual(cls, code: str, *, status: str, runs_dir: str | Path = "runs",
                       message: str = "Resultado verificado manualmente por el usuario") -> RedeemResult:
        """Offline ledger correction only; never connect to Brave or replay a code."""
        if status not in MANUAL_RESOLUTION_STATUSES:
            raise ValueError("Estado de resolución manual no permitido")
        resolver = cls(runs_dir=runs_dir)
        normalized = resolver._normalize(code)
        if normalized is None:
            raise ValueError("Código con formato inválido")
        resolver.runs_dir.mkdir(parents=True, exist_ok=True)
        try:
            locks = lock_directory(resolver.runs_dir)
        except BlockingIOError as exc:
            raise RuntimeError("Detén el daemon antes de resolver manualmente su registro") from exc
        with locks:
            resolver._load()
            previous = resolver._seen.get(normalized)
            if previous is None or previous.status != "timeout":
                raise ValueError("El código no tiene un timeout pendiente en el registro")
            result = replace(previous, status=status, message="Resolución manual: " + message,
                             t_result=time.perf_counter())
            resolver._resolutions.append({"code": normalized, "previous_status": previous.status,
                                          "status": status, "wall": time.time(), "message": message})
            resolver._save(result)
            return result

    async def start(self) -> None:
        if self._started:
            return
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._process_lock = lock_directory(self.runs_dir, include_global=not self.global_lock_held)
            self._load()
            await self.browser.connect()
            # Never replace an unresolved result screen by reopening its modal.
            if self._halt is None and not (self.stop_after_success and self.has_success):
                for tab in self.browser.tabs.values():
                    tab.needs_clean = True
                    await self.browser.prepare(tab)
            self._started = True
        except BrowserStateError as exc:
            await self._notify(exc.status, str(exc))
            await self.close()
            raise
        except BaseException:
            await self.close()
            raise

    async def _notify(self, status: str, message: str) -> None:
        await notify(status, message)

    async def _resolve_timeout(self) -> bool:
        if self._last is None or self._last.status != "timeout":
            return False
        tab = self.browser.tabs[self._last.tab]
        message = await self.browser.result_text(tab)
        status = classify_message(message)
        if status is None:
            guard = await self.browser.guard(tab)
            if guard:
                status, message = guard
        if status is None:
            return False
        result = replace(self._last, status=status, message=message, t_result=time.perf_counter())
        self._save(result)
        self._last = result
        self._halt = (status, message) if status in HALT_STATUSES else None
        unresolved = [r for r in self._seen.values() if r.status == "timeout"]
        if unresolved:
            self._last = unresolved[-1]
            self._halt = ("timeout", "Hay otro canje anterior sin resultado confirmado")
        if self._halt:
            await self._notify(*self._halt)
        return self._halt is None

    def new_round(self) -> None:
        """Request a fresh A/B round; caller then awaits ensure_ready().

        Synchronous so the detector can signal disappearance without browser I/O.
        In-flight requests finish first. Stop states and global dedup survive.
        """
        self._round_requested = True
        if not self._lock.locked() and self._halt is None:
            self._last = None
        for tab in self.browser.tabs.values():
            tab.ready = False
            tab.needs_clean = True

    def _expire_round(self) -> None:
        if (self._last is not None and self._halt is None
                and time.perf_counter() - self._last.t_result > ROUND_MAX_AGE):
            self._last = None
            # A previous health check may already have cleaned both modals.
            # Otherwise require ensure_ready, never inspect DOM in submit.
            if any(not tab.ready for tab in self.browser.tabs.values()):
                self._round_requested = True

    async def ensure_ready(self) -> bool:
        if not self._started:
            return False
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=self.ready_lock_timeout)
        except TimeoutError:
            log.warning("Health check espera un canje en curso; ronda nueva sigue pendiente")
            return False
        try:
            try:
                if self.stop_after_success and self.has_success:
                    return False
                if self._halt:
                    if self._halt[0] != "timeout" or not await self._resolve_timeout():
                        return False
                if self.stop_after_success and self.has_success:
                    return False
                self._expire_round()
                if self._round_requested:
                    self._last = None
                for tab in self.browser.tabs.values():
                    guard = await self.browser.guard(tab)
                    if guard:
                        raise BrowserStateError(*guard)
                    await self.browser.prepare(tab)
                self._round_requested = False
                return all(tab.ready for tab in self.browser.tabs.values())
            except BrowserStateError as exc:
                if exc.status in HALT_STATUSES or exc.status == "timeout":
                    self._halt = (exc.status, str(exc))
                await self._notify(exc.status, str(exc))
                return False
            except Exception as exc:
                log.exception("Health check de PSN falló: %s", exc)
                return False
        finally:
            self._lock.release()

    @staticmethod
    def _normalize(raw: str) -> str | None:
        # Use the shared module when Claude has supplied it; strict fallback for standalone CLI.
        try:
            from sniper.codes import normalize
        except ImportError:
            value = re.sub(r"\s", "", raw).upper()
            if not re.fullmatch(r"(?:[A-Z0-9]{12}|[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4})", value):
                return None
            compact = value.replace("-", "")
            return "-".join(compact[i:i + 4] for i in range(0, 12, 4))
        return normalize(raw)

    def _no_send(self, code: str, status: str, message: str, tab: str = "A") -> RedeemResult:
        # Zero means no fill/submit occurred, unlike an actual attempt's monotonic timestamp.
        return RedeemResult(code, tab, status, message, 0.0, time.perf_counter(), None)

    async def submit(self, code: str, *, source: str, confidence: float) -> RedeemResult:
        normalized = self._normalize(code)
        if normalized is None:
            return self._no_send(code, "invalid", "Formato local inválido; no enviado a PSN")
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            return self._no_send(normalized, "error", "Confianza fuera de [0, 1]; no enviado")
        async with self._lock:
            if not self._started:
                return self._no_send(normalized, "error", "Redeemer.start() debe completarse primero")
            if normalized in self._seen:
                # Return the existing outcome, including for duplicates queued during submit.
                return self._seen[normalized]
            if self.stop_after_success and self.has_success:
                return self._no_send(normalized, "error", "Ya hubo un canje exitoso; participación terminada")
            if self._halt:
                # Timeout resolution and all DOM health checks belong to ensure_ready.
                return self._no_send(normalized, *self._halt)
            self._expire_round()
            if self._round_requested:
                return self._no_send(normalized, "error", "Ronda nueva pendiente: llamar await ensure_ready()")
            name = "A"
            if self._last is not None:
                if self._last.status != "invalid" or self._last.tab != "A":
                    return self._no_send(normalized, "error", "Descartado: ronda terminada o resultado anterior no habilita B", "B")
                name = "B"
            tab = self.browser.tabs[name]
            if not tab.ready:
                return self._no_send(normalized, "error", "Pestaña no preparada: llamar await ensure_ready()", name)
            submitted = False
            t_submit = 0.0
            try:
                # Write-ahead journal: a crash at any subsequent point prohibits replay.
                pending = RedeemResult(normalized, name, "timeout", "Intento iniciado; resultado todavía desconocido", 0.0, time.perf_counter(), None)
                self._save(pending)
                log.info("Canje %s source=%s confidence=%.4f", name, source, confidence)
                tab.ready = False
                tab.needs_clean = True
                t_submit = time.perf_counter()
                pending.t_submit = t_submit
                await tab.input.fill(normalized.replace("-", ""), timeout=1500)
                deadline = time.perf_counter() + 3
                button = None
                while time.perf_counter() < deadline:
                    button = await self.browser.button(tab)
                    if button is not None:
                        break
                    await asyncio.sleep(0.01)
                if button is None:
                    raise BrowserStateError("error", "Continuar no está disponible; código no enviado")
                # A failed click may still have reached PSN: mark uncertainty before dispatch.
                submitted = True
                await self._dispatch_submit(button)
                status, message = await self._wait_result(tab)
            except asyncio.CancelledError:
                if normalized in self._seen:
                    if submitted:
                        result = RedeemResult(normalized, name, "timeout", "Tarea cancelada; verificar el resultado antes de continuar", t_submit, time.perf_counter(), None)
                        self._save(result)
                        self._last = result
                        self._halt = ("timeout", result.message)
                    else:
                        self._seen.pop(normalized, None)
                        self._save(None)
                raise
            except BrowserStateError as exc:
                # A DOM/action failure after dispatch is uncertain, never a safe rejection.
                status = "timeout" if submitted and exc.status == "error" else exc.status
                message = str(exc)
            except Exception as exc:
                log.exception("Fallo durante canje %s", name)
                status = "timeout" if submitted else "error"
                message = f"{type(exc).__name__}: {exc}"
            result = RedeemResult(normalized, name, status, message, t_submit, time.perf_counter(), None)
            if not submitted and status == "error":
                # El diario preventivo no es un intento definitivo si no hubo dispatch.
                result.t_submit = 0.0
                self._seen.pop(normalized, None)
                self._save(None)
                self._last = None
                return result
            self._last = result
            if status in HALT_STATUSES or status == "timeout":
                self._halt = (status, message)
            self._save(result)
        # Release the send lock before screenshots so B can immediately follow an
        # explicit rejection in A. It uses its own page and cannot duplicate A.
        result.screenshot = await self.browser.screenshot(tab, f"redeem-{time.time_ns()}-{name}-{status}")
        if self._seen.get(normalized) is result:
            self._save(result)
        if status in HALT_STATUSES or status in {"timeout", "success"}:
            await self._notify(status, message)
        return result

    async def _dispatch_submit(self, button: Any) -> None:
        # Evaluate in the button's own iframe. Only an explicit unsupported result
        # permits fallback: an exception might mean requestSubmit already reached PSN.
        dispatched = await button.evaluate(REQUEST_SUBMIT_JS)
        if dispatched is False:
            await button.click(force=True, no_wait_after=True, timeout=1500)
        elif dispatched is not True:
            raise BrowserStateError("error", "Resultado incierto al ejecutar requestSubmit")

    async def _wait_result(self, tab: Any) -> tuple[str, str]:
        deadline = time.perf_counter() + self.result_timeout
        confirmations = 0
        last_text = ""
        last_button: Any = None
        while time.perf_counter() < deadline:
            text = await self.browser.result_text(tab)
            status = classify_message(text)
            if status:
                return status, text
            guard = await self.browser.guard(tab)
            if guard:
                return guard
            button = await self.browser.button(tab, confirm=True)
            if button is not None:
                handle = await button.element_handle()
                # Do not click an unchanged confirmation repeatedly while PSN is responding.
                same = last_button is not None and await handle.evaluate("(el, previous) => el === previous", last_button)
                if not same or text != last_text:
                    if confirmations >= 3:
                        return "error", "Más de tres confirmaciones; flujo de canje no reconocido"
                    await button.click(force=True, no_wait_after=True, timeout=1500)
                    confirmations += 1
                    last_button = handle
                    last_text = text
            await asyncio.sleep(0.05)
        return "timeout", "Sin resultado explícito de PSN; no se habilita otro envío. " + last_text[:2000]

    async def close(self, *, close_tabs: bool = False) -> None:
        try:
            if close_tabs and not self.has_pending_timeout:
                await self.browser.close(close_tabs=True)
            else:
                if close_tabs and self.has_pending_timeout:
                    log.warning("Se conservan las pestañas PSN: hay un canje con timeout pendiente")
                await self.browser.close()
        finally:
            self._started = False
            if self._process_lock is not None:
                self._process_lock.close()
                self._process_lock = None

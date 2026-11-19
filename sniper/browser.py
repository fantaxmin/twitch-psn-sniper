"""Attach to the user's dedicated Brave; never launch or log in automatically."""

from __future__ import annotations

import asyncio
import logging
import json
import re
import time
import unicodedata
from functools import wraps
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger(__name__)
CDP_URL = "http://127.0.0.1:9333"
STORE_URL = "https://store.playstation.com/es-co/pages/latest"
PROFILE_SELECTOR = '[data-qa="web-toolbar#profile-container#profile-icon#dropdown-toggler"]'
REDEEM_SELECTOR = '[data-qa="web-toolbar#profile-container#profile-dropdown#item-list#redeem-codes#button"]'
REDEEM_NAME = re.compile(r"^canjear(?:\s+un)?\s+c[oó]digos?$", re.I)
INPUT_NAME = re.compile(r"(?:introduce|introducir|ingres[ae]).*c[oó]digo.*(?:cup[oó]n|12)|12.*caracteres", re.I)
CONTINUE_NAME = re.compile(r"^continuar$", re.I)
CONFIRM_NAME = re.compile(r"^(?:confirmar(?:\s+canje)?|canjear(?:\s+ahora)?|a[nñ]adir\s+(?:al\s+)?monedero)$", re.I)
FORM_PROBE_JS = """button => {
    const form = button.form;
    const input = form && form.querySelector('input[type="text"]');
    return Boolean(!button.disabled && form && input && input.form === form
        && typeof form.requestSubmit === 'function' && input.value.replace(/-/g, '').length === 12);
}"""
REQUEST_SUBMIT_JS = """button => {
    const supported = (""" + FORM_PROBE_JS + """)(button);
    if (!supported) return false;
    button.form.requestSubmit(button);
    return true;
}"""


# Texto que el usuario ve. inner_text() del iframe del voucher incluye el contenido de
# <script> (JSON de sesión con correo/accountId y cadenas de i18n), lo que producía
# falsos session_lost y filtraba datos personales a registros y eventos.
VISIBLE_TEXT_JS = """el => {
    const skip = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE', 'SVG', 'IFRAME']);
    const out = [];
    const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, {acceptNode(node) {
        for (let p = node.parentElement; p; p = p.parentElement) {
            if (skip.has(p.tagName.toUpperCase())) return NodeFilter.FILTER_REJECT;
            if (p === el) break;
        }
        const parent = node.parentElement;
        if (parent && parent.checkVisibility
                && !parent.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) {
            return NodeFilter.FILTER_REJECT;
        }
        return node.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    }});
    while (walker.nextNode()) out.push(walker.currentNode.nodeValue.trim());
    return out.join('\\n');
}"""


async def visible_text(locator: Any, *, timeout: float = 1000) -> str:
    """Visible text only: no script/style/hidden nodes."""
    return await locator.evaluate(VISIBLE_TEXT_JS, timeout=timeout) or ""


def plain(text: str) -> str:
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", text.casefold())
                            if not unicodedata.combining(c)).split())


def classify_message(text: str) -> str | None:
    """Only explicit PSN outcomes count; an available Confirm button is not success."""
    text = plain(text)
    patterns = (
        ("captcha", r"captcha|verifica que eres humano|verifica que no eres un robot|verify.*human"),
        ("blocked", r"demasiad[oa]s? (?:intentos|solicitudes|veces)|too many|access denied|acceso denegado|temporalmente bloquead|temporarily blocked|intentalo (?:de nuevo )?mas tarde|try again later"),
        ("session_lost", r"sesion (?:ha )?(?:caducado|expirado)|session (?:has )?expired|vuelve a iniciar sesion|inicia sesion para continuar|sign in to continue"),
        ("region", r"(?:pais|region).*(?:diferente|distint|coincid|incorrect|correspond)|no (?:se )?puede(?:s)? canjear.*(?:pais|region)|(?:different|incorrect|wrong).*(?:country|region)|country.*does not match"),
        ("already_used", r"(?:codigo|cupon).*(?:ya (?:ha sido |se ha )?(?:canjeado|utilizado|usado))|already (?:been )?(?:redeemed|used)"),
        ("invalid", r"(?:codigo|cupon).*(?:incorrecto|no (?:es )?valid|caducad|no existe)|(?:invalid|incorrect|expired) (?:voucher |coupon )?code|code.*(?:invalid|incorrect|expired)"),
        ("error", r"(?:se ha producido|ocurrio|ha ocurrido) un error|something went wrong|unable to redeem|no (?:se ha podido|se pudo|puedes) canjear"),
        ("success", r"(?:codigo|cupon).*(?:se ha canjeado|canjeado correctamente|canjeado con exito)|(?:se (?:ha|han) |hemos )(?:anadido|agregado|aplicado).*(?:cuenta|monedero|biblioteca)|(?:credito|contenido|fondos).*(?:anadido|agregado|aplicado).*(?:cuenta|monedero|biblioteca)|successfully redeemed|(?:added|applied) to your (?:account|wallet|library)|canje (?:realizado|completado)"),
    )
    for status, pattern in patterns:
        if re.search(pattern, text):
            return status
    return None


class BrowserStateError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


def validate_click_options(options: dict[str, Any]) -> None:
    # force bypasses the interceptor used by trial in affected Playwright builds.
    # Never combine them, even in diagnostics that claim to be read-only.
    if options.get("trial") and options.get("force"):
        raise ValueError("Clic prohibido: trial=True con force=True puede ejecutar un clic real")


async def guarded_click(target: Any, **options: Any) -> None:
    validate_click_options(options)
    await target.click(**options)


def install_click_guard() -> None:
    """Guard every async Playwright click entry point in this process, idempotently."""
    from playwright.async_api import ElementHandle, Frame, Locator, Page

    def protect(original):
        @wraps(original)
        async def click(target, *args, **options):
            validate_click_options(options)
            return await original(target, *args, **options)
        click._sniper_click_guard = True
        return click

    for target_type in (Locator, ElementHandle, Page, Frame):
        if not getattr(target_type.click, "_sniper_click_guard", False):
            target_type.click = protect(target_type.click)


async def visible_first(locator: Any) -> Any | None:
    for index in range(await locator.count()):
        item = locator.nth(index)
        if await item.is_visible():
            return item
    return None


@dataclass
class RedeemTab:
    name: str
    page: Any
    frame: Any = None
    input: Any = None
    scope: Any = None
    continue_button: Any = None
    ready: bool = False
    needs_clean: bool = False
    owned: bool = False


class BraveBrowser:
    def __init__(self, *, cdp_url: str = CDP_URL, runs_dir: str | Path = "runs",
                 ready_timeout: float = 15.0):
        if cdp_url != CDP_URL:
            raise ValueError(f"Solo se permite el CDP local dedicado: {CDP_URL}")
        self.cdp_url = cdp_url
        self.runs_dir = Path(runs_dir)
        self.ready_timeout = ready_timeout
        self.playwright: Any = None
        self.browser: Any = None
        self.tabs: dict[str, RedeemTab] = {}

    async def connect(self) -> None:
        # Lazy import keeps --help and offline unit tests usable without installation.
        from playwright.async_api import async_playwright
        install_click_guard()
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.chromium.connect_over_cdp(self.cdp_url, timeout=5000)
            if not self.browser.contexts:
                raise BrowserStateError("error", "Brave no tiene un contexto persistente disponible")
            context = self.browser.contexts[0]
            for name in ("A", "B"):
                page = None
                owned = False
                for candidate in context.pages:
                    if urlsplit(candidate.url).hostname == "store.playstation.com":
                        if await candidate.evaluate("window.name") == f"psn-sniper-{name}":
                            page = candidate
                            owned = True  # Explicitly tagged daemon tab from a previous run.
                            break
                if page is None:
                    page = await context.new_page()
                    owned = True
                    await page.goto(STORE_URL, wait_until="domcontentloaded", timeout=30000)
                    await page.evaluate("name => { window.name = name; }", f"psn-sniper-{name}")
                page.set_default_timeout(2000)
                self.tabs[name] = RedeemTab(name, page, owned=owned)
        except BaseException:
            await self.close()
            raise

    async def guard(self, tab: RedeemTab) -> tuple[str, str] | None:
        if tab.page.is_closed():
            return "session_lost", "Se cerró la pestaña dedicada de PSN"
        for frame in tab.page.frames:
            host = urlsplit(frame.url).hostname or ""
            if frame == tab.page.main_frame and (host.endswith("sonyentertainmentnetwork.com") or host.startswith("id.sony")):
                return "session_lost", "PSN solicita iniciar sesión manualmente"
            challenge = await visible_first(frame.locator(
                'iframe[src*="recaptcha"][title*="challenge"], iframe[src*="hcaptcha"], '
                '[role="dialog"][aria-label*="CAPTCHA"]'))
            if challenge is not None:
                return "captcha", "PSN muestra un desafío CAPTCHA; requiere intervención manual"
            if frame == tab.page.main_frame:
                # La tienda incrusta en su DOM respuestas de error de recursos sueltos (p. ej.
                # "Access Denied" de Akamai para error-general-fail.svg) aunque la página funcione.
                # En el documento principal solo cuenta una página de error completa.
                title = await frame.evaluate("() => document.title || ''")
                if re.search(r"access denied|acceso denegado", title, re.I):
                    return "blocked", f"Akamai bloqueó la página completa: {title[:200]}"
                continue
            body = frame.locator("body")
            if await body.count():
                text = await visible_text(body)
                status = classify_message(text)
                if status in {"captcha", "blocked", "session_lost"}:
                    return status, text[:4000]
        return None

    async def _find_input(self, tab: RedeemTab) -> bool:
        for frame in tab.page.frames:
            for locator in (frame.get_by_placeholder(INPUT_NAME), frame.get_by_label(INPUT_NAME)):
                field = await visible_first(locator)
                if field is None:
                    continue
                # Keep a stable result scope even after PSN removes the input.
                scope = None
                voucher = urlsplit(frame.url)
                if voucher.hostname == "checkout.playstation.com" and voucher.path.rstrip("/") == "/voucher":
                    scope = frame.locator("body")
                for selector in (() if scope is not None else ('[role="dialog"]', '[aria-modal="true"]', '.psw-modal', 'form')):
                    possible = frame.locator(selector)
                    for index in range(await possible.count()):
                        candidate = possible.nth(index)
                        if await candidate.filter(has=field).count() and await candidate.is_visible():
                            # Save the container, not filter(has=input): the input disappears
                            # during confirmation, but the result scope must still resolve.
                            scope = candidate
                            break
                    if scope is not None:
                        break
                if scope is None and frame != tab.page.main_frame:
                    scope = frame.locator("body")
                if scope is None:
                    raise BrowserStateError("error", "Input encontrado sin contenedor de canje identificable; ejecutar --inspect")
                tab.frame, tab.input, tab.scope = frame, field, scope
                return True
        return False

    async def prepare(self, tab: RedeemTab) -> None:
        tab.ready = False
        if tab.needs_clean:
            # Outside the hot path: discard result/error/confirmation DOM entirely.
            await tab.page.goto(STORE_URL, wait_until="domcontentloaded", timeout=30000)
            tab.frame = tab.input = tab.scope = tab.continue_button = None
        deadline = time.perf_counter() + self.ready_timeout
        profile_opened = False
        while time.perf_counter() < deadline:
            try:
                if await self._find_input(tab):
                    await tab.input.wait_for(state="visible", timeout=2000)
                    if await tab.input.is_editable():
                        message = await self.result_text(tab)
                        if classify_message(message):
                            raise BrowserStateError("error", "El modal conserva un resultado previo; no está limpio")
                        await tab.input.fill("")
                        tab.continue_button = await visible_first(tab.scope.get_by_role("button", name=CONTINUE_NAME))
                        if tab.continue_button is None:
                            await asyncio.sleep(0.1)
                            continue
                        await tab.input.focus()
                        tab.needs_clean = False
                        tab.ready = True
                        return
                guard = await self.guard(tab)
                if guard:
                    raise BrowserStateError(*guard)
                opened = False
                for frame in tab.page.frames:
                    button = await visible_first(frame.locator(REDEEM_SELECTOR))
                    if button is not None:
                        await button.click(timeout=2000)
                        opened = True
                        break
                    for role in ("menuitem", "button", "link"):
                        button = await visible_first(frame.get_by_role(role, name=REDEEM_NAME))
                        if button is not None:
                            await button.click(timeout=2000)
                            opened = True
                            break
                    if opened:
                        break
                if not opened and not profile_opened:
                    for locator in (
                        tab.page.locator(PROFILE_SELECTOR),
                        tab.page.get_by_role("button", name=re.compile(r"perfil|profile|mi cuenta|my account|avatar", re.I)),
                        tab.page.locator('[data-qa="web-toolbar#profile"], [data-qa="web-toolbar#profile-button"]'),
                    ):
                        profile = await visible_first(locator)
                        if profile is not None:
                            await profile.click(timeout=2000)
                            profile_opened = True
                            break
                login = await visible_first(tab.page.get_by_role("button", name=re.compile(r"^iniciar sesi[oó]n$|^sign in$", re.I)))
                if login is not None and not opened:
                    raise BrowserStateError("session_lost", "Inicia sesión manualmente en el perfil dedicado de Brave")
                await asyncio.sleep(0.1)
            except BrowserStateError:
                raise
            except Exception as exc:  # noqa: BLE001 - iframes/menús de PSN se recargan durante la carga
                # Fuera de la ruta caliente: un iframe desprendido o un menú animándose se reintenta.
                log.debug("prepare reintenta tras %s: %s", type(exc).__name__, str(exc)[:200])
                profile_opened = False
                tab.frame = tab.input = tab.scope = tab.continue_button = None
                await asyncio.sleep(0.3)
        raise BrowserStateError("error", "No se pudo preparar Canjear código; ejecutar --inspect para revisar selectores")

    async def result_text(self, tab: RedeemTab) -> str:
        if tab.scope is not None and await tab.scope.count() and await tab.scope.is_visible():
            return (await visible_text(tab.scope))[:8000]
        # Some implementations replace the entire modal on success.
        for frame in tab.page.frames:
            parsed = urlsplit(frame.url)
            if parsed.hostname == "checkout.playstation.com" and parsed.path.rstrip("/") == "/voucher":
                body = frame.locator("body")
                if await body.count():
                    text = await visible_text(body)
                    if classify_message(text):
                        return text[:8000]
            for role in ("alert", "status", "dialog"):
                items = frame.get_by_role(role)
                for index in range(await items.count()):
                    item = items.nth(index)
                    if await item.is_visible():
                        text = await visible_text(item)
                        if classify_message(text):
                            return text[:8000]
        return ""

    async def button(self, tab: RedeemTab, *, confirm: bool = False) -> Any | None:
        if not confirm and tab.continue_button is not None:
            # Locator cached during prepare; only readiness is checked after fill.
            return tab.continue_button if await tab.continue_button.is_enabled() else None
        if tab.scope is None:
            return None
        button = await visible_first(tab.scope.get_by_role("button", name=CONFIRM_NAME if confirm else CONTINUE_NAME))
        if button is not None and await button.is_enabled():
            return button
        return None

    async def screenshot(self, tab: RedeemTab, stem: str) -> str | None:
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.runs_dir / f"{stem}.png"
        try:
            await tab.page.screenshot(path=str(path), timeout=3000)
            path.chmod(0o600)
            return str(path)
        except Exception:
            return None

    async def inspect(self) -> Path:
        """No fill, submit or confirmation. Include metadata, never field values/cookies."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        report: dict[str, Any] = {"tabs": {}, "suggested": {
            "input_placeholder_regex": INPUT_NAME.pattern,
            "continue_role_name": CONTINUE_NAME.pattern,
            "confirm_role_name": CONFIRM_NAME.pattern,
        }}
        for name, tab in self.tabs.items():
            entry: dict[str, Any] = {"frames": []}
            try:
                await self.prepare(tab)
                entry["ready"] = True
            except Exception as exc:
                entry["ready"] = False
                entry["error"] = str(exc)
            for frame in tab.page.frames:
                parsed = urlsplit(frame.url)
                controls = await frame.locator('input, button, [role="button"], [role="menuitem"], [role="alert"], [role="status"]').evaluate_all("""els => els.filter(e => e.getClientRects().length).map(e => ({
                    tag: e.tagName.toLowerCase(), role: e.getAttribute('role'),
                    id: e.id, name: e.getAttribute('name'), type: e.getAttribute('type'),
                    placeholder: e.getAttribute('placeholder'), ariaLabel: e.getAttribute('aria-label'),
                    dataQa: e.getAttribute('data-qa'), maxLength: e.getAttribute('maxlength'),
                    pattern: e.getAttribute('pattern'), disabled: e.disabled || false,
                    text: e.tagName === 'INPUT' ? null : e.innerText.slice(0, 1200)
                }))""")
                entry["frames"].append({"url": urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), "controls": controls})
            entry["messages"] = await self.result_text(tab)
            entry["screenshot"] = await self.screenshot(tab, f"inspect-{time.time_ns()}-{name}")
            report["tabs"][name] = entry
        path = self.runs_dir / f"inspect-{time.time_ns()}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)
        return path

    async def benchmark(self) -> Path:
        """Read-only probes plus one unforced trial on an already open clean modal."""
        tab = self.tabs["A"]
        filled = False
        report: dict[str, Any] = {
            "tab": "A", "actual_click": False, "click_ms": None,
            "request_submit_dispatched": False,
            "note": "requestSubmit mide solo preflight; único clic: trial=True sin force",
        }
        try:
            # Do not prepare(), navigate, reopen or clear a previous result here.
            # In particular, even fill('') is forbidden on a dirty result screen.
            message = await self.result_text(tab)
            if classify_message(message) or tab.needs_clean:
                raise BrowserStateError("error", "Benchmark cancelado: modal con resultado previo o pendiente de limpieza; no se hizo fill")
            if not await self._find_input(tab):
                raise BrowserStateError("error", "El benchmark requiere un modal limpio ya abierto; usa --inspect antes")
            message = await self.result_text(tab)
            if classify_message(message):
                raise BrowserStateError("error", "Benchmark cancelado: el modal conserva un resultado previo; no se hizo fill")
            if not await tab.input.is_editable():
                raise BrowserStateError("error", "Input no editable; benchmark cancelado sin fill")
            tab.continue_button = await visible_first(tab.scope.get_by_role("button", name=CONTINUE_NAME))
            if tab.continue_button is None:
                raise BrowserStateError("error", "Continuar no encontrado; benchmark cancelado sin fill")
            t0 = time.perf_counter()
            filled = True
            await tab.input.fill("000000000000", timeout=1500)
            report["fill_ms"] = (time.perf_counter() - t0) * 1000
            button = None
            deadline = time.perf_counter() + 3
            while time.perf_counter() < deadline:
                button = await self.button(tab)
                if button is not None:
                    break
                await asyncio.sleep(0.01)
            if button is None:
                raise BrowserStateError("error", "Continuar no se habilitó con el valor de prueba; no se pulsó")
            report["fill_to_enabled_ms"] = (time.perf_counter() - t0) * 1000
            probe_start = time.perf_counter()
            report["form_request_submit_supported"] = await button.evaluate(FORM_PROBE_JS)
            report["request_submit_preflight_ms"] = (time.perf_counter() - probe_start) * 1000
            normal_start = time.perf_counter()
            try:
                await guarded_click(button, trial=True, timeout=1500)
                report["normal_trial_ms"] = (time.perf_counter() - normal_start) * 1000
                report["fill_to_trial_click_ms"] = (time.perf_counter() - t0) * 1000
            except Exception as exc:
                report["normal_trial_ms"] = (time.perf_counter() - normal_start) * 1000
                report["normal_trial_error"] = str(exc)
                report["fill_to_trial_click_ms"] = None
            report["status"] = "ok"
        except Exception as exc:
            report.update(status="error", message=str(exc))
        finally:
            # No Enter, submit, actual click, or confirmation in this code path.
            tab.ready = False
            if filled:
                await tab.input.fill("", timeout=1500)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        path = self.runs_dir / f"bench-{time.time_ns()}.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        path.chmod(0o600)
        return path

    async def close(self, *, close_tabs: bool = False) -> None:
        # Opt-in closes only daemon-created/tagged pages, never unrelated user tabs.
        if close_tabs:
            for tab in self.tabs.values():
                if tab.owned and not tab.page.is_closed():
                    try:
                        await tab.page.close()
                    except Exception:
                        pass
        if self.playwright is not None:
            await self.playwright.stop()
        self.playwright = self.browser = None

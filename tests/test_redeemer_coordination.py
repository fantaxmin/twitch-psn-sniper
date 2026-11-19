"""Offline contract tests: no browser, network, notification, or real coupon."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from sniper.browser import BraveBrowser, classify_message
from sniper.redeemer import Redeemer

CODE_A = "A1AA-B2BB-C3CC"
CODE_B = "D4DD-E5EE-F6FF"


class FakeInput:
    def __init__(self, browser, name):
        self.browser, self.name = browser, name

    async def fill(self, value, **kwargs):
        self.browser.fills.append((self.name, value))

    async def is_editable(self):
        return True


class FakeButton:
    def __init__(self, browser, name):
        self.browser, self.name = browser, name

    async def click(self, **kwargs):
        self.browser.sent.append(self.name)
        await asyncio.sleep(0.01)

    async def evaluate(self, script):
        assert "requestSubmit(button)" in script
        await self.click()
        return True


class FakeBrowser:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {"A": "Código canjeado correctamente"}
        self.sent, self.fills, self.prepared = [], [], []
        self.guard_result = None
        self.tabs = {name: SimpleNamespace(name=name, input=FakeInput(self, name), ready=False, needs_clean=False) for name in ("A", "B")}

    async def connect(self):
        pass

    async def close(self):
        pass

    async def prepare(self, tab):
        self.prepared.append(tab.name)
        tab.ready = True
        tab.needs_clean = False

    async def guard(self, tab):
        return self.guard_result

    async def _find_input(self, tab):
        return True

    async def result_text(self, tab):
        return self.outcomes.get(tab.name, "") if tab.name in self.sent and not tab.ready else ""

    async def button(self, tab, *, confirm=False):
        return None if confirm else FakeButton(self, tab.name)

    async def screenshot(self, tab, stem):
        return None


class QuietRedeemer(Redeemer):
    async def _notify(self, status, message):
        pass


def test_fill_failure_is_not_definitive_and_same_code_can_retry(tmp_path):
    async def scenario():
        browser = FakeBrowser()
        original_fill = browser.tabs["A"].input.fill
        async def failed_fill(*args, **kwargs):
            raise RuntimeError("falló fill antes del dispatch")
        browser.tabs["A"].input.fill = failed_fill
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            result = await redeemer.submit(CODE_A, source="kick-ocr", confidence=1)
            assert result.status == "error" and result.t_submit == 0
            assert CODE_A not in redeemer._seen and browser.sent == []
            payload = json.loads((tmp_path / "redeemer-attempts.json").read_text())
            assert CODE_A not in payload["attempts"]
            browser.tabs["A"].input.fill = original_fill
            redeemer.new_round()
            assert await redeemer.ensure_ready()
            result = await redeemer.submit(CODE_A, source="kick-ocr", confidence=1)
            assert result.status == "success" and browser.sent == ["A"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_cancel_before_dispatch_does_not_leave_pending_timeout(tmp_path):
    async def scenario():
        browser = FakeBrowser()
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()
        browser.tabs["A"].input.fill = cancelled
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            with pytest.raises(asyncio.CancelledError):
                await redeemer.submit(CODE_A, source="kick-ocr", confidence=1)
            assert not redeemer.has_pending_timeout and CODE_A not in redeemer._seen
            assert browser.sent == []
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_manual_resolution_takes_global_lock_for_kick_campaign(tmp_path):
    from sniper.filelock import FileLock
    from sniper.redeemer import RedeemResult
    directory = tmp_path / "kick-elded"
    writer = QuietRedeemer(runs_dir=directory, browser=FakeBrowser())
    writer._save(RedeemResult(CODE_A, "A", "timeout", "incierto", 1, 2, None))
    with FileLock(tmp_path / ".redeemer.lock"):
        with pytest.raises(RuntimeError, match="Detén el daemon"):
            Redeemer.resolve_manual(CODE_A, status="invalid", runs_dir=directory)
    result = Redeemer.resolve_manual(CODE_A, status="invalid", runs_dir=directory)
    assert result.status == "invalid"


def test_stop_after_success_survives_new_round_and_restart(tmp_path):
    async def scenario():
        browser = FakeBrowser()
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser, stop_after_success=True)
        await redeemer.start()
        try:
            assert (await redeemer.submit(CODE_A, source="kick-ocr", confidence=1)).status == "success"
            redeemer.new_round()
            assert not await redeemer.ensure_ready()
            refused = await redeemer.submit(CODE_B, source="kick-ocr", confidence=1)
            assert refused.t_submit == 0 and browser.sent == ["A"]
        finally:
            await redeemer.close()
        restarted_browser = FakeBrowser()
        restarted = QuietRedeemer(runs_dir=tmp_path, browser=restarted_browser, stop_after_success=True)
        await restarted.start()
        try:
            assert restarted.has_success
            assert restarted_browser.prepared == []
            assert not await restarted.ensure_ready()
            refused = await restarted.submit(CODE_B, source="kick-ocr", confidence=1)
            assert refused.t_submit == 0 and restarted_browser.sent == []
        finally:
            await restarted.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("prior_status", ["invalid", "already_used", "region"])
@pytest.mark.parametrize("prior_tab", ["A", "B"])
@pytest.mark.parametrize("future_clock", [False, True])
def test_kick_new_round_after_restart_allows_new_code_after_rejection(tmp_path, prior_status, prior_tab, future_clock):
    import time
    from sniper.redeemer import RedeemResult
    async def scenario():
        writer = QuietRedeemer(runs_dir=tmp_path, browser=FakeBrowser())
        # Un reloj monotónico de otro arranque no debe mantener la ronda cerrada.
        t_result = 1e15 if future_clock else time.perf_counter()
        writer._save(RedeemResult(CODE_A, prior_tab, prior_status, prior_status, 1, t_result, None))
        browser = FakeBrowser()
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser, stop_after_success=True)
        await redeemer.start()
        try:
            redeemer.new_round()
            assert await redeemer.ensure_ready()
            assert (await redeemer.submit(CODE_B, source="kick-ocr", confidence=1)).status == "success"
            assert browser.sent == ["A"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


@pytest.mark.parametrize(("message", "status"), [
    ("El código del cupón es incorrecto o ya no es válido.", "invalid"),
    ("Este código ya se ha utilizado.", "already_used"),
    ("El código no se puede canjear en tu región.", "region"),
    ("Has realizado demasiados intentos. Inténtalo más tarde.", "blocked"),
    ("Verifica que eres humano", "captcha"),
    ("Tu sesión ha caducado", "session_lost"),
    ("El cupón se ha canjeado.", "success"),
    ("Los fondos se han añadido a tu monedero.", "success"),
    ("Se ha producido un error", "error"),
    ("Confirma el contenido que vas a canjear. Confirmar", None),
    ("Introduce el código del cupón de 12 caracteres. Continuar", None),
])
def test_explicit_messages(message, status):
    assert classify_message(message) == status


def test_duplicate_concurrent_success_and_discard(tmp_path):
    async def scenario():
        browser = FakeBrowser()
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            a, duplicate, b = await asyncio.gather(
                redeemer.submit(CODE_A, source="ocr", confidence=0.99),
                redeemer.submit("a1aab2bbc3cc", source="chat", confidence=1.0),
                redeemer.submit(CODE_B, source="ocr", confidence=0.99))
            assert a.status == duplicate.status == "success"
            assert a == duplicate
            assert b.t_submit == 0 and b.status == "error"
            assert browser.sent == ["A"]
            assert browser.fills == [("A", "A1AAB2BBC3CC")]
            assert await redeemer.ensure_ready()
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_b_only_after_invalid_a(tmp_path):
    async def scenario():
        browser = FakeBrowser({"A": "Código incorrecto", "B": "Código canjeado correctamente"})
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            a, b = await asyncio.gather(
                redeemer.submit(CODE_A, source="ocr", confidence=1),
                redeemer.submit(CODE_B, source="ocr", confidence=1))
            assert (a.tab, a.status, b.tab, b.status) == ("A", "invalid", "B", "success")
            assert browser.sent == ["A", "B"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("message", ["Este código ya se ha utilizado", "Código de región diferente", "Demasiados intentos", "Verifica que eres humano", "Tu sesión ha caducado"])
def test_other_a_outcomes_never_enable_b(tmp_path, message):
    async def scenario():
        browser = FakeBrowser({"A": message})
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            await redeemer.submit(CODE_A, source="ocr", confidence=1)
            b = await redeemer.submit(CODE_B, source="ocr", confidence=1)
            assert b.t_submit == 0
            assert browser.sent == ["A"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_timeout_blocks_until_explicit_rejection(tmp_path):
    async def scenario():
        browser = FakeBrowser({"A": "", "B": "Código canjeado correctamente"})
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser, result_timeout=0.001)
        await redeemer.start()
        try:
            a = await redeemer.submit(CODE_A, source="ocr", confidence=1)
            b = await redeemer.submit(CODE_B, source="ocr", confidence=1)
            assert a.status == b.status == "timeout" and b.t_submit == 0
            assert browser.sent == ["A"]
            browser.outcomes["A"] = "Código incorrecto"
            assert await redeemer.ensure_ready()
            b = await redeemer.submit(CODE_B, source="ocr", confidence=1)
            assert b.status == "success" and browser.sent == ["A", "B"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_restart_does_not_resend_or_erase_uncertain_result(tmp_path):
    async def scenario():
        browser = FakeBrowser({"A": ""})
        first = QuietRedeemer(runs_dir=tmp_path, browser=browser, result_timeout=0.001)
        await first.start()
        await first.submit(CODE_A, source="ocr", confidence=1)
        await first.close()
        second_browser = FakeBrowser()
        second = QuietRedeemer(runs_dir=tmp_path, browser=second_browser)
        await second.start()
        try:
            result = await second.submit(CODE_A, source="chat", confidence=1)
            assert result.status == "timeout"
            assert not second_browser.sent and not second_browser.prepared
            assert not await second.ensure_ready()
            ledger = json.loads((tmp_path / "redeemer-attempts.json").read_text())
            assert ledger["attempts"][CODE_A]["status"] == "timeout"
        finally:
            await second.close()
    asyncio.run(scenario())


def test_process_lock_and_local_validation(tmp_path):
    async def scenario():
        first = QuietRedeemer(runs_dir=tmp_path, browser=FakeBrowser())
        second = QuietRedeemer(runs_dir=tmp_path, browser=FakeBrowser())
        await first.start()
        try:
            with pytest.raises(BlockingIOError):
                await second.start()
            result = await first.submit("invalid", source="cli", confidence=1)
            assert result.status == "invalid" and result.t_submit == 0
            assert not first.browser.sent
        finally:
            await first.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("complete", [True, False])
def test_confirmation_clicks_once_until_new_state(tmp_path, complete):
    class Confirmation:
        def __init__(self, browser):
            self.browser = browser

        async def element_handle(self):
            return self

        async def evaluate(self, expression, previous):
            return self is previous

        async def click(self, **kwargs):
            assert kwargs.get("force") is True and kwargs.get("no_wait_after") is True
            self.browser.confirmations += 1

    class ConfirmBrowser(FakeBrowser):
        def __init__(self):
            super().__init__()
            self.confirmations = 0
            self.confirm_button = Confirmation(self)

        async def result_text(self, tab):
            if tab.name not in self.sent:
                return ""
            if complete and self.confirmations:
                return "El cupón se ha canjeado"
            return "Comprueba el contenido. Confirmar"

        async def button(self, tab, *, confirm=False):
            return self.confirm_button if confirm else FakeButton(self, tab.name)

    async def scenario():
        browser = ConfirmBrowser()
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser, result_timeout=0.13)
        await redeemer.start()
        try:
            result = await redeemer.submit(CODE_A, source="ocr", confidence=1)
            assert result.status == ("success" if complete else "timeout")
            assert browser.confirmations == 1
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_a_screenshot_does_not_delay_b(tmp_path):
    async def scenario():
        release = asyncio.Event()
        capturing = asyncio.Event()

        class SlowScreenshotBrowser(FakeBrowser):
            async def screenshot(self, tab, stem):
                if tab.name == "A":
                    capturing.set()
                    await release.wait()
                return None

        browser = SlowScreenshotBrowser({"A": "Código incorrecto", "B": "Código canjeado correctamente"})
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        task = asyncio.create_task(redeemer.submit(CODE_A, source="ocr", confidence=1))
        try:
            await asyncio.wait_for(capturing.wait(), 1)
            b = await asyncio.wait_for(redeemer.submit(CODE_B, source="ocr", confidence=1), 1)
            assert b.status == "success" and browser.sent == ["A", "B"]
            release.set()
            await task
        finally:
            release.set()
            await task
            await redeemer.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["Código canjeado correctamente", "Este código ya se ha utilizado", "Código incorrecto"])
def test_new_round_preserves_dedup_and_rearms_a(tmp_path, outcome):
    async def scenario():
        browser = FakeBrowser({"A": outcome, "B": "Código incorrecto"})
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            old = await redeemer.submit(CODE_A, source="ocr", confidence=1)
            if old.status == "invalid":
                await redeemer.submit(CODE_B, source="ocr", confidence=1)
            assert await redeemer.ensure_ready()  # Finished round is healthy, not halted.
            before = len(browser.prepared)
            redeemer.new_round()
            assert redeemer._last is None
            assert not any(tab.ready for tab in browser.tabs.values())
            assert await redeemer.ensure_ready()
            assert browser.prepared[before:] == ["A", "B"]
            assert await redeemer.submit(CODE_A, source="chat", confidence=1) == old
            browser.outcomes["A"] = "Código canjeado correctamente"
            result = await redeemer.submit("G7GG-H8HH-J9JJ", source="ocr", confidence=1)
            assert result.tab == "A" and result.status == "success"
            assert browser.sent.count("A") == 2
            ledger = json.loads((tmp_path / "redeemer-attempts.json").read_text())
            assert CODE_A in ledger["attempts"] and result.code in ledger["attempts"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["Demasiados intentos", "Verifica que eres humano", "Tu sesión ha caducado", ""])
def test_new_round_and_age_never_clear_halts(tmp_path, outcome):
    async def scenario():
        browser = FakeBrowser({"A": outcome})
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser, result_timeout=0.001)
        await redeemer.start()
        try:
            result = await redeemer.submit(CODE_A, source="ocr", confidence=1)
            result.t_result -= 91
            redeemer.new_round()
            assert redeemer._last is result
            assert not await redeemer.ensure_ready()
            next_result = await redeemer.submit(CODE_B, source="ocr", confidence=1)
            assert next_result.status == result.status and next_result.t_submit == 0
            assert browser.sent == ["A"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_auto_reset_after_90_seconds_with_clean_tabs(tmp_path):
    async def scenario():
        browser = FakeBrowser()
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            first = await redeemer.submit(CODE_A, source="ocr", confidence=1)
            assert await redeemer.ensure_ready()
            first.t_result -= 91
            second = await redeemer.submit(CODE_B, source="ocr", confidence=1)
            assert second.tab == "A" and second.status == "success"
            assert browser.sent == ["A", "A"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_auto_reset_prepares_dirty_tabs_in_health_check(tmp_path):
    async def scenario():
        browser = FakeBrowser()
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            first = await redeemer.submit(CODE_A, source="ocr", confidence=1)
            first.t_result -= 91
            assert await redeemer.ensure_ready()
            second = await redeemer.submit(CODE_B, source="ocr", confidence=1)
            assert second.tab == "A" and second.status == "success"
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_new_round_during_submit_waits_for_result(tmp_path):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        browser = FakeBrowser()

        class WaitingButton(FakeButton):
            async def click(self, **kwargs):
                self.browser.sent.append(self.name)
                started.set()
                await release.wait()

        async def button(tab, *, confirm=False):
            return None if confirm else WaitingButton(browser, tab.name)

        browser.button = button
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        task = asyncio.create_task(redeemer.submit(CODE_A, source="ocr", confidence=1))
        try:
            await asyncio.wait_for(started.wait(), 1)
            redeemer.new_round()
            ready = asyncio.create_task(redeemer.ensure_ready())
            await asyncio.sleep(0)
            assert not ready.done()
            release.set()
            assert (await task).status == "success"
            assert await asyncio.wait_for(ready, 1)
            assert redeemer._last is None
            assert CODE_A in redeemer._seen
        finally:
            release.set()
            await task
            await redeemer.close()
    asyncio.run(scenario())


def test_hot_path_has_no_dom_health_checks_or_fsync(tmp_path, monkeypatch):
    async def scenario():
        class StrictBrowser(FakeBrowser):
            async def guard(self, tab):
                assert self.fills, "guard before fill"
                return None

            async def result_text(self, tab):
                assert self.fills, "result_text before fill"
                return await super().result_text(tab)

            async def _find_input(self, tab):
                raise AssertionError("_find_input in submit")

        def forbidden_fsync(*args):
            raise AssertionError("fsync on hot path")

        browser = StrictBrowser()
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        monkeypatch.setattr("os.fsync", forbidden_fsync)
        try:
            result = await redeemer.submit(CODE_A, source="ocr", confidence=1)
            assert result.status == "success" and browser.sent == ["A"]
            assert browser.prepared == ["A", "B"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_benchmark_never_dispatches_click_and_clears_input(tmp_path):
    click_calls = []
    class TrialButton:
        async def count(self):
            return 1

        def nth(self, index):
            return self

        async def is_visible(self):
            return True

        async def evaluate(self, script):
            assert "requestSubmit(button)" not in script
            return True

        async def click(self, **kwargs):
            assert kwargs.get("trial") is True
            assert "force" not in kwargs, "A forced trial can send the coupon"
            click_calls.append(kwargs)

    class BenchmarkBrowser(BraveBrowser):
        def __init__(self):
            super().__init__(runs_dir=tmp_path)
            self.fills = []
            scope = SimpleNamespace(get_by_role=lambda *args, **kwargs: TrialButton())
            self.tabs = {"A": SimpleNamespace(name="A", input=FakeInput(self, "A"), ready=False,
                                              needs_clean=False, scope=scope)}

        async def prepare(self, tab):
            raise AssertionError("Bench must not prepare/reopen a modal or clear existing results")

        async def result_text(self, tab):
            return ""

        async def _find_input(self, tab):
            return True

        async def button(self, tab):
            return TrialButton()

    async def scenario():
        browser = BenchmarkBrowser()
        path = await browser.benchmark()
        report = json.loads(path.read_text())
        assert report["actual_click"] is False and report["click_ms"] is None
        assert report["status"] == "ok" and report["fill_to_trial_click_ms"] >= report["fill_ms"]
        assert report["request_submit_dispatched"] is False
        assert report["form_request_submit_supported"] is True
        assert "force_trial_ms" not in report and "fill_to_force_trial_click_ms" not in report
        assert report["normal_trial_ms"] >= 0
        assert len(click_calls) == 1 and "force" not in click_calls[0]
        assert browser.fills == [("A", "000000000000"), ("A", "")]
    asyncio.run(scenario())


def test_health_lock_timeout_keeps_round_pending(tmp_path):
    async def scenario():
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=FakeBrowser(), ready_lock_timeout=0.001)
        await redeemer.start()
        try:
            await redeemer._lock.acquire()
            redeemer.new_round()
            assert not await redeemer.ensure_ready()
            assert redeemer._round_requested
            assert redeemer._lock.locked()
            redeemer._lock.release()
            assert await redeemer.ensure_ready()
            assert not redeemer._round_requested
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_timeout_preserves_tabs_and_resolves_after_restart(tmp_path):
    class RecoverableBrowser(FakeBrowser):
        close_tabs = None
        async def close(self, *, close_tabs=False):
            self.close_tabs = close_tabs

    async def scenario():
        browser = RecoverableBrowser({"A": ""})
        first = QuietRedeemer(runs_dir=tmp_path, browser=browser, result_timeout=0.001)
        await first.start()
        await first.submit(CODE_A, source="ocr", confidence=1)
        assert first.has_pending_timeout
        await first.close(close_tabs=True)
        assert browser.close_tabs is False
        recovered = RecoverableBrowser({"A": "Código canjeado correctamente"})
        recovered.sent.append("A")  # Existing result page, not a new submission.
        second = QuietRedeemer(runs_dir=tmp_path, browser=recovered)
        await second.start()
        assert not recovered.prepared
        assert await second.ensure_ready()
        assert not second.has_pending_timeout
        assert second._seen[CODE_A].status == "success"
        assert recovered.fills == []
        await second.close(close_tabs=True)
        assert recovered.close_tabs is True
    asyncio.run(scenario())


def test_manual_resolve_updates_ledger_offline_and_keeps_dedup(tmp_path, monkeypatch, capsys):
    from argparse import Namespace
    from scripts import redeem_cli
    from sniper.redeemer import RedeemResult
    now = __import__("time").perf_counter()
    journal = QuietRedeemer(runs_dir=tmp_path, browser=FakeBrowser())
    journal._save(RedeemResult(CODE_A, "A", "timeout", "sin respuesta", now, now, None))
    async def forbidden_connect(self):
        raise AssertionError("manual resolve must not connect to Brave")
    monkeypatch.setattr(BraveBrowser, "connect", forbidden_connect)
    args = Namespace(resolve=CODE_A, resolve_as="already_used", runs_dir=tmp_path,
                     message="Comprobado en la cuenta PSN")
    assert asyncio.run(redeem_cli.run(args)) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "already_used"
    payload = json.loads((tmp_path / "redeemer-attempts.json").read_text())
    assert payload["attempts"][CODE_A]["status"] == "already_used"
    assert payload["resolutions"][0]["previous_status"] == "timeout"
    loaded = QuietRedeemer(runs_dir=tmp_path, browser=FakeBrowser())
    loaded._load()
    assert not loaded.has_pending_timeout and CODE_A in loaded._seen
    loaded._save(loaded._seen[CODE_A])
    assert len(json.loads((tmp_path / "redeemer-attempts.json").read_text())["resolutions"]) == 1
    with pytest.raises(ValueError):
        Redeemer.resolve_manual(CODE_A, status="invalid", runs_dir=tmp_path)


def test_manual_resolution_refuses_live_daemon_lock(tmp_path):
    async def scenario():
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=FakeBrowser())
        await redeemer.start()
        try:
            with pytest.raises(RuntimeError, match="Detén el daemon"):
                Redeemer.resolve_manual(CODE_A, status="invalid", runs_dir=tmp_path)
        finally:
            await redeemer.close()
    asyncio.run(scenario())


def test_native_submit_and_force_fallback_never_double_dispatch(tmp_path):
    async def scenario():
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=FakeBrowser())
        class Button:
            def __init__(self, supported):
                self.supported, self.clicks, self.evaluations = supported, 0, 0
            async def evaluate(self, script):
                assert "requestSubmit(button)" in script
                self.evaluations += 1
                if isinstance(self.supported, Exception):
                    raise self.supported
                return self.supported
            async def click(self, **kwargs):
                assert kwargs["force"] and kwargs["no_wait_after"]
                self.clicks += 1
        native = Button(True)
        await redeemer._dispatch_submit(native)
        assert native.clicks == 0 and native.evaluations == 1
        fallback = Button(False)
        await redeemer._dispatch_submit(fallback)
        assert fallback.clicks == 1 and fallback.evaluations == 1
        uncertain = Button(RuntimeError("context destroyed after dispatch"))
        with pytest.raises(RuntimeError):
            await redeemer._dispatch_submit(uncertain)
        assert uncertain.clicks == 0 and uncertain.evaluations == 1
    asyncio.run(scenario())


def test_native_submit_uncertainty_is_timeout_without_force_retry(tmp_path):
    async def scenario():
        browser = FakeBrowser()
        class UncertainButton:
            async def evaluate(self, script):
                browser.sent.append("A")
                raise RuntimeError("context destroyed immediately after requestSubmit")
            async def click(self, **kwargs):
                raise AssertionError("Must not retry a possibly dispatched form")
        async def button(tab, *, confirm=False):
            return None if confirm else UncertainButton()
        browser.button = button
        redeemer = QuietRedeemer(runs_dir=tmp_path, browser=browser)
        await redeemer.start()
        try:
            result = await redeemer.submit(CODE_A, source="whisper", confidence=1)
            assert result.status == "timeout" and redeemer.has_pending_timeout
            assert browser.sent == ["A"]
        finally:
            await redeemer.close()
    asyncio.run(scenario())

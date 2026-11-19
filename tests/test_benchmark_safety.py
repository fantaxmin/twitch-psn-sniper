"""Offline regressions for an unintended PSN submission by a forced trial click."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from sniper.browser import BraveBrowser, guarded_click, install_click_guard


def test_click_guard_rejects_forced_trial_before_underlying_click():
    calls = []
    class Target:
        async def click(self, **options):
            calls.append(options)
    async def scenario():
        target = Target()
        with pytest.raises(ValueError, match="trial=True con force=True"):
            await guarded_click(target, trial=True, force=True)
        assert calls == []
        await guarded_click(target, trial=True)
        await guarded_click(target, force=True)
        assert calls == [{"trial": True}, {"force": True}]
    asyncio.run(scenario())


def test_all_playwright_async_click_entrypoints_are_guarded_without_browser(monkeypatch):
    from playwright.async_api import ElementHandle, Frame, Locator, Page
    calls = []
    async def original(target, *args, **options):
        calls.append(options)
    types = (Locator, ElementHandle, Page, Frame)
    for target_type in types:
        monkeypatch.setattr(target_type, "click", original)
    install_click_guard()
    first = [target_type.click for target_type in types]
    install_click_guard()
    assert first == [target_type.click for target_type in types]
    async def scenario():
        for target_type in types:
            with pytest.raises(ValueError, match="trial=True con force=True"):
                await target_type.click(object(), trial=True, force=True)
        assert calls == []
        await Locator.click(object(), force=True)
        assert calls == [{"force": True}]
    asyncio.run(scenario())


@pytest.mark.parametrize(("message", "dirty"), [
    ("El código de cupón no es correcto o ya no es válido", False),
    ("El cupón se ha canjeado correctamente", False),
    ("", True),
])
def test_dirty_modal_prevents_every_fill_including_cleanup(tmp_path, message, dirty):
    fills = []
    class Input:
        async def fill(self, value, **kwargs):
            fills.append(value)
            raise AssertionError("No fill is allowed on a result screen")
    class Browser(BraveBrowser):
        async def result_text(self, tab):
            return message
        async def prepare(self, tab):
            raise AssertionError("Must not prepare or erase the prior result")
        async def _find_input(self, tab):
            raise AssertionError("Initial dirty state must stop the benchmark immediately")
    async def scenario():
        browser = Browser(runs_dir=tmp_path)
        browser.tabs = {"A": SimpleNamespace(ready=True, needs_clean=dirty, input=Input())}
        path = await browser.benchmark()
        report = json.loads(path.read_text())
        assert report["status"] == "error" and "no se hizo fill" in report["message"]
        assert fills == [] and "fill_ms" not in report
        assert report["actual_click"] is False and report["request_submit_dispatched"] is False
    asyncio.run(scenario())


def test_result_appearing_during_read_only_location_also_prevents_fill(tmp_path):
    fills = []
    class Input:
        async def fill(self, value, **kwargs):
            fills.append(value)
    class Browser(BraveBrowser):
        reads = 0
        async def result_text(self, tab):
            self.reads += 1
            return "" if self.reads == 1 else "El código del cupón no es válido"
        async def _find_input(self, tab):
            return True
    async def scenario():
        browser = Browser(runs_dir=tmp_path)
        browser.tabs = {"A": SimpleNamespace(ready=False, needs_clean=False, input=Input())}
        report = json.loads((await browser.benchmark()).read_text())
        assert report["status"] == "error" and browser.reads == 2 and fills == []
    asyncio.run(scenario())

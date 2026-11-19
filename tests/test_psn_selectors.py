"""Model the live-validated PSN attributes without connecting to its website."""
import asyncio
from types import SimpleNamespace

import pytest

from sniper.browser import BraveBrowser, RedeemTab, STORE_URL


class Element:
    def __init__(self, visible=True, click=None, text=""):
        self.visible, self.action, self.text = visible, click, text

    async def count(self):
        return int(self.visible)

    def nth(self, index):
        return self

    async def is_visible(self):
        return self.visible

    async def click(self, **kwargs):
        if self.action:
            self.action()

    async def inner_text(self, **kwargs):
        return self.text


class Field(Element):
    async def wait_for(self, **kwargs):
        pass

    async def is_editable(self):
        return True

    async def fill(self, value):
        assert value == ""  # Preparing a modal must never enter a coupon.

    async def focus(self):
        pass


def test_live_data_qa_selectors_take_priority_over_unnamed_avatar(tmp_path):
    async def scenario():
        state = {"profile": False, "modal": False}
        clicks = []
        def profile():
            state["profile"] = True
            clicks.append("profile")
        def redeem():
            state["modal"] = True
            clicks.append("redeem")
        continuing = Element()
        scope = SimpleNamespace(get_by_role=lambda *args, **kwargs: continuing)
        class UnnamedProfileFallback(Element):
            async def count(self):
                raise AssertionError("Unnamed avatar must be located by data-qa first")
        class Page:
            frames = []
            def locator(self, selector):
                if selector == '[data-qa="web-toolbar#profile-container#profile-icon#dropdown-toggler"]':
                    return Element(click=profile)
                if selector == '[data-qa="web-toolbar#profile-container#profile-dropdown#item-list#redeem-codes#button"]':
                    return Element(visible=state["profile"], click=redeem)
                return Element(False)
            def get_by_role(self, role, *, name):
                if "perfil" in name.pattern:
                    return UnnamedProfileFallback()
                return Element(False)
        page = Page()
        page.frames = [page]
        tab = RedeemTab("A", page)
        browser = BraveBrowser(runs_dir=tmp_path, ready_timeout=1)
        async def find_input(tab):
            if not state["modal"]:
                return False
            tab.input, tab.scope = Field(), scope
            return True
        async def guard(tab):
            return None
        async def result_text(tab):
            return ""
        browser._find_input = find_input
        browser.guard = guard
        browser.result_text = result_text
        await browser.prepare(tab)
        assert clicks == ["profile", "redeem"] and tab.ready
        assert tab.continue_button is continuing
    asyncio.run(scenario())


def test_voucher_iframe_without_dialog_keeps_body_scope_and_recovers_result(tmp_path):
    async def scenario():
        body = Element(text="Introduce el código del cupón de 12 caracteres")
        field = Field()
        class Frame:
            url = "https://checkout.playstation.com/voucher?session=redacted"
            def locator(self, selector):
                assert selector == "body"
                return body
            def get_by_placeholder(self, pattern):
                assert pattern.search("Introduce el código del cupón de 12 caracteres")
                return field
            def get_by_label(self, pattern):
                return Element(False)
        frame = Frame()
        page = SimpleNamespace(frames=[frame], main_frame=object(), url=STORE_URL)
        tab = RedeemTab("A", page)
        browser = BraveBrowser(runs_dir=tmp_path)
        assert await browser._find_input(tab)
        assert tab.scope is body and tab.frame is frame
        tab.scope = None  # The next process reattaches without cached locators.
        body.text = "El código se ha canjeado correctamente"
        assert await browser.result_text(tab) == body.text
    asyncio.run(scenario())


@pytest.mark.parametrize(("title", "voucher_text", "expected"), [
    ("PlayStation Store", "Introduce el código del cupón", None),
    ("ACCESS DENIED", "", "blocked"),
    ("Acceso denegado", "", "blocked"),
    ("PlayStation Store", "Access Denied", "blocked"),
    ("PlayStation Store", "Verifica que eres humano", "captcha"),
    ("PlayStation Store", "Tu sesión ha caducado", "session_lost"),
])
def test_guard_ignores_resource_errors_in_store_body_but_keeps_real_blocks(tmp_path, title, voucher_text, expected):
    async def scenario():
        reads = {"store": 0, "voucher": 0}
        class Body(Element):
            def __init__(self, name, text):
                super().__init__(text=text)
                self.name = name
            async def inner_text(self, **kwargs):
                reads[self.name] += 1
                return self.text
        class Frame:
            def __init__(self, url, body):
                self.url, self.body = url, body
            def locator(self, selector):
                return self.body if selector == "body" else Element(False)
            async def evaluate(self, script):
                assert script == "() => document.title || ''"
                return title
        main = Frame(STORE_URL, Body("store", "Access Denied: error-general-fail.svg (Akamai)"))
        voucher = Frame("https://checkout.playstation.com/voucher", Body("voucher", voucher_text))
        page = SimpleNamespace(main_frame=main, frames=[main, voucher], is_closed=lambda: False)
        result = await BraveBrowser(runs_dir=tmp_path).guard(RedeemTab("A", page))
        assert (result[0] if result else None) == expected
        assert reads["store"] == 0
        assert reads["voucher"] == (0 if title.casefold() in {"access denied", "acceso denegado"} else 1)
    asyncio.run(scenario())

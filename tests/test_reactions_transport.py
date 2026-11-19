import io
import json
import sys

from sniper import reactions


def test_windows_ollama_requests_bypass_system_proxies(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    calls = []
    class Opener:
        def open(self, request, *, timeout):
            calls.append((request.full_url, json.loads(request.data), timeout))
            return io.BytesIO(b'{"ok": true}')
    def build_opener(handler):
        assert isinstance(handler, reactions.urllib.request.ProxyHandler)
        assert handler.proxies == {}
        return Opener()
    monkeypatch.setattr(reactions.urllib.request, "build_opener", build_opener)
    composer = reactions.ReactionComposer()
    assert composer._request("/api/chat", {"model": "local-test"}) == {"ok": True}
    assert calls == [("http://127.0.0.1:11434/api/chat", {"model": "local-test"}, 10)]

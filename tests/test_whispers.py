"""Offline whisper fixtures and lifecycle tests. No public/private messages or PSN calls."""
import asyncio
import json
import stat
from types import SimpleNamespace


from sniper.whispers import DOM_SNAPSHOT_JS, Whisper, WhisperWatcher, parse_whispers

CODE = "A1AA-B2BB-C3CC"


def envelope(text="hola", *, sender="rubius", recipient="viewer", message_id="w1"):
    return {"subscription": {"type": "user.whisper.message"}, "event": {
        "from_user_login": sender, "to_user_login": recipient,
        "whisper_id": message_id, "whisper": {"text": text}}}


def test_parser_received_pubsub_and_eventsub_only():
    message = envelope(CODE)
    parsed = parse_whispers(json.dumps({"type": "notification", "payload": message}), "viewer")
    assert len(parsed) == 1 and parsed[0].sender == "rubius" and parsed[0].text == CODE
    legacy = {"type": "MESSAGE", "data": {"topic": "whispers.123", "message": json.dumps({
        "type": "whisper_received", "data": {"id": "w2", "body": "premio " + CODE,
                                              "tags": {"login": "mod"}}})}}
    assert parse_whispers(json.dumps(legacy))[0].sender == "mod"
    legacy["data"]["topic"] = "chat.channel.123"
    assert parse_whispers(json.dumps(legacy)) == []
    assert parse_whispers(json.dumps(envelope(sender="viewer")), "viewer") == []
    assert parse_whispers(json.dumps(envelope(recipient="otra_cuenta")), "viewer") == []
    assert parse_whispers(json.dumps({"type": "whisper_sent", "data": message["event"]})) == []
    assert parse_whispers(json.dumps({"type": "channel.chat.message", "message": json.dumps(message)})) == []
    assert parse_whispers(b"\x00\xffbinary") == []
    assert parse_whispers('{"auth_token":"do-not-save"}') == []


class Emitter:
    def __init__(self):
        self.listeners = {}
    def on(self, event, handler):
        self.listeners.setdefault(event, []).append(handler)
    def remove_listener(self, event, handler):
        if handler in self.listeners.get(event, []):
            self.listeners[event].remove(handler)
    def emit(self, event, payload):
        for handler in tuple(self.listeners.get(event, [])):
            handler(payload)


class Page(Emitter):
    url = "https://www.twitch.tv/rubius"
    def __init__(self):
        super().__init__()
        self.snapshot = {"unread": 0, "login": "viewer", "messages": []}
    async def evaluate(self, script):
        assert script == DOM_SNAPSHOT_JS
        assert "document.cookie" not in script and "localStorage" not in script
        return self.snapshot
    def is_closed(self):
        return False
    def locator(self, selector):
        class Absent:
            async def count(self):
                return 0
        return Absent()


class CDP(Emitter):
    detached = False
    async def send(self, method):
        assert method == "Network.enable"  # Instrumentation only, never whisper/chat requests.
    async def detach(self):
        self.detached = True


def watcher_fixture():
    page, cdp = Page(), CDP()
    class Context:
        pages = [page]
        async def new_cdp_session(self, selected):
            assert selected is page
            return cdp
    import threading
    return WhisperWatcher(Context(), "rubius", None, stop=threading.Event()), page, cdp


def test_existing_socket_via_cdp_new_sockets_and_dedup():
    async def scenario():
        watcher, page, cdp = watcher_fixture()
        assert await watcher.start()
        payload = json.dumps(envelope())
        cdp.emit("Network.webSocketFrameReceived", {"response": {"opcode": 1, "payloadData": payload}})
        socket = Emitter()
        socket.url = "wss://hermes.twitch.tv/v1"
        page.emit("websocket", socket)
        socket.emit("framereceived", payload)
        assert watcher.queue.qsize() == 1
        assert watcher.queue.get_nowait().sender == "rubius"
        await watcher.close()
        assert cdp.detached and not page.listeners["websocket"]
        assert not socket.listeners["framereceived"]
    asyncio.run(scenario())


def test_dom_ignores_history_and_outgoing_then_detects_new_received_message():
    async def scenario():
        watcher, page, _ = watcher_fixture()
        old = {"sender": "old", "text": "historial", "id": "old", "incoming": True}
        page.snapshot["messages"] = [old]
        assert await watcher.start()
        await watcher.poll_dom()
        assert watcher.queue.empty()
        outgoing = {"sender": "viewer", "text": "saliente", "id": "sent", "incoming": False, "outgoing": True}
        page.snapshot["messages"] = [old, outgoing]
        await watcher.poll_dom()
        assert watcher.queue.empty()
        incoming = {"sender": "rubius", "text": CODE, "id": "new", "incoming": True, "outgoing": False}
        page.snapshot.update(unread=1, messages=[old, outgoing, incoming])
        await watcher.poll_dom()
        received = watcher.queue.get_nowait()
        assert received.sender == "rubius" and received.text == CODE and received.source == "dom"
        await watcher.close()
    asyncio.run(scenario())


def test_unread_indicator_without_readable_text_never_invents_a_coupon():
    async def scenario():
        watcher, page, _ = watcher_fixture()
        await watcher.start()
        page.snapshot["unread"] = 1
        await watcher.poll_dom()
        received = watcher.queue.get_nowait()
        assert received.source == "dom_unread_indicator" and received.text == ""
        await watcher.close()
    asyncio.run(scenario())



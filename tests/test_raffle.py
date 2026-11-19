import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from sniper import raffle
from sniper.raffle import ChatMessage, PhraseDetector, RaffleComposer, RaffleLedger, parse_message


def message(user="mod_one", text="coco", id="one", timestamp=1000, moderator=True):
    return ChatMessage(user, text, id, timestamp, moderator)


def test_distinct_watched_mods_required_duplicate_messages_and_accounts_do_not_count():
    detector = PhraseDetector(["mod_one", "mod_two"])
    assert detector.observe(message(), now=1000) is None
    assert detector.observe(message(id="again"), now=1001) is None
    assert detector.observe(message(user="stranger", id="outsider"), now=1001) is None
    assert detector.observe(message(user="mod_two", id="not-mod", moderator=False), now=1001) is None
    assert detector.observe(message(user="mod_two", id="two", text=" COCO "), now=1001) == "COCO"


def test_optional_three_distinct_users_and_no_old_confirmations():
    detector = PhraseDetector(["mod_one", "mod_two", "mod_three"], minimum=3)
    assert detector.observe(message(), now=1000) is None
    assert detector.observe(message(user="mod_two", id="two"), now=1000) is None
    assert detector.observe(message(user="mod_three", id="three", timestamp=1061), now=1061) is None
    assert detector.observe(message(id="late", timestamp=1000), now=1100) is None


def test_parser_uses_irc_login_not_display_name_and_checks_channel():
    line = "@display-name=mod_one;id=x;tmi-sent-ts=1000000;mod=1;badges=moderator/1 :stranger!s@s.tmi.twitch.tv PRIVMSG #streamer :coco"
    parsed = parse_message(line, "streamer")
    assert parsed.user == "stranger" and parsed.moderator
    assert parse_message(line, "other") is None
    assert parse_message(line.replace("1000000", "NaN"), "streamer") is None
    assert parse_message(line.replace(";id=x", ""), "streamer") is None


@pytest.mark.parametrize("text", ["/ban test", ".timeout test", "@somebody hola", "https://example.com", "A1B2-C3D4-E5F6", "x" * 81])
def test_unsafe_or_long_messages_never_become_phrases(text):
    detector = PhraseDetector(["mod_one", "mod_two"])
    assert detector.observe(message(text=text), now=1000) is None
    assert detector.observe(message(user="mod_two", text=text, id="two"), now=1000) is None


def test_two_phrase_turns_then_normal_even_if_another_phrase_is_confirmed(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    fallback_calls = []
    class Fallback:
        async def compose(self, *a, **kw):
            fallback_calls.append(True)
            return "PogChamp", "emote"
        def remember(self, text):
            pass
    composer = RaffleComposer(Fallback(), ["mod_one", "mod_two"], ledger=RaffleLedger(tmp_path / "raffle.json"))
    composer.observe(message())
    composer.observe(message(user="mod_two", id="two"))
    async def scenario():
        assert await composer.compose_priority() == ("coco", "raffle")
        composer.after_priority_attempt()
        now[0] += 60
        assert await composer.compose_priority() == ("coco", "raffle")
        composer.after_priority_attempt()
        composer.observe(message(text="mango", id="new-one", timestamp=now[0]))
        composer.observe(message(user="mod_two", text="mango", id="new-two", timestamp=now[0]))
        now[0] += 180
        assert await composer.compose(None) == ("PogChamp", "emote")
        assert len(fallback_calls) == 1
        # La frase coco ya no vuelve a enviarse, aunque otros la repitan después.
        composer.observe(message(id="repeat-one", timestamp=now[0]))
        composer.observe(message(user="mod_two", id="repeat-two", timestamp=now[0]))
        now[0] += 180
        assert await composer.compose(None) == ("PogChamp", "emote")
    asyncio.run(scenario())


def test_persistent_reservations_and_interval_survive_restart(tmp_path, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    path = tmp_path / "raffle.json"
    assert RaffleLedger(path).reserve("coco")
    assert not RaffleLedger(path).reserve("COCO")  # No segundo envío inmediato desde otro proceso.
    now[0] += 60
    assert RaffleLedger(path).reserve("COCO")
    now[0] += 60
    assert not RaffleLedger(path).reserve("otra")  # Tercer turno normal obligatorio.
    assert not RaffleLedger(path).reserve(None)
    assert not RaffleLedger(path).reserve("coco")
    assert RaffleLedger(path).reserve("otra")


def test_corrupt_ledger_fails_closed(tmp_path):
    path = tmp_path / "raffle.json"
    path.write_text('{"version": 99}')
    with pytest.raises(RuntimeError):
        RaffleLedger(path).reserve("coco")


def test_disabled_or_disconnected_and_emote_only_use_normal_composer(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    class Fallback:
        async def compose(self, *a, **kw):
            return "Kappa", "emote"
        def remember(self, text):
            pass
    composer = RaffleComposer(Fallback(), ["mod_one", "mod_two"])
    composer.observe(message())
    composer.observe(message(user="mod_two", id="two"))
    assert asyncio.run(composer.compose(None, emote_only=True)) == ("Kappa", "emote")
    composer.reset()
    assert asyncio.run(composer.compose(None)) == ("Kappa", "emote")
    assert composer.ledger.memory["attempts"] == {}


def test_listener_only_reads_chat_and_handles_ping_without_public_sends(monkeypatch):
    from sniper import raffle_chat
    stop = threading.Event()
    writes = []
    lines = iter([b"PING :tmi.twitch.tv\r\n",
                  b"@id=one;tmi-sent-ts=1000000;mod=1 :mod_one!m@m.tmi.twitch.tv PRIVMSG #streamer :coco\r\n"])
    class Reader:
        async def readline(self):
            return next(lines)
    class Writer:
        def write(self, value):
            writes.append(value)
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass
    async def connect(*a, **kw):
        return Reader(), Writer()
    monkeypatch.setattr(raffle_chat.asyncio, "open_connection", connect)
    seen = []
    def observe(value):
        seen.append(value)
        stop.set()
    asyncio.run(raffle_chat.watch_messages("streamer", observe, stop=stop, on_reset=lambda: None))
    assert seen[0].user == "mod_one"
    assert any(value.startswith(b"PONG") for value in writes)
    assert all(b"PRIVMSG" not in value for value in writes)


def test_cli_enforces_three_minute_schedule_and_enough_users():
    from sniper.daemon import parse_args
    args = parse_args(["--raffle-users", "mod_one,mod_two", "--chat-interval", "30"])
    assert args.raffle_users == ["mod_one", "mod_two"] and args.chat_interval == 180 and args.wait_first
    with pytest.raises(SystemExit):
        parse_args(["--raffle-users", "mod_one"])


def test_chat_schedule_is_immediate_then_one_minute_then_normal(monkeypatch):
    import numpy as np
    from sniper.capture import LatestFrame
    from sniper.participation import ChatParticipation
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(raffle.time, "perf_counter", lambda: now[0])
    class Fallback:
        async def compose(self, *a, **kw):
            return "Kappa", "emote"
        def remember(self, message):
            pass
    sent = []
    class Sender:
        emote_only = False
        async def start(self):
            pass
        async def send(self, message):
            sent.append((now[0], message))
    composer = RaffleComposer(Fallback(), ["mod_one", "mod_two"])
    composer.observe(message())
    composer.observe(message(user="mod_two", id="two"))
    frames = LatestFrame()
    live = threading.Event()
    live.set()
    participant = ChatParticipation(frames=frames, capture=SimpleNamespace(live=live), stop=threading.Event(),
                                    composer=composer, sender=Sender(), interval=180, wait_first=True)
    async def scenario():
        frames.put(np.zeros((4, 4, 3), dtype=np.uint8))
        await participant.tick()
        assert sent == [(1000.0, "coco")]
        for delay in (59, 1, 179, 1):
            now[0] += delay
            frames.put(np.zeros((4, 4, 3), dtype=np.uint8))
            await participant.tick()
    asyncio.run(scenario())
    assert sent == [(1000.0, "coco"), (1060.0, "coco"), (1240.0, "Kappa")]


def test_third_normal_message_cannot_repeat_keyword_when_keyword_is_an_emote(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    class Fallback:
        async def compose(self, *a, **kw):
            return "Kappa", "emote"
        def remember(self, message):
            pass
    composer = RaffleComposer(Fallback(), ["mod_one", "mod_two"])
    composer.observe(message(text="Kappa"))
    composer.observe(message(user="mod_two", id="two", text="Kappa"))
    async def scenario():
        assert (await composer.compose_priority())[0] == "Kappa"
        composer.after_priority_attempt()
        now[0] += 60
        assert (await composer.compose_priority())[0] == "Kappa"
        composer.after_priority_attempt()
        now[0] += 180
        assert (await composer.compose(None))[0] != "Kappa"
    asyncio.run(scenario())


def test_second_phrase_is_due_one_minute_after_first_send_finishes(monkeypatch):
    from sniper.capture import LatestFrame
    from sniper.participation import ChatParticipation
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(raffle.time, "perf_counter", lambda: now[0])
    class Fallback:
        async def compose(self, *a, **kw):
            raise AssertionError("Las frases no esperan OCR ni generación de reacciones")
        def remember(self, message):
            pass
    sent = []
    class Sender:
        emote_only = False
        async def start(self):
            pass
        async def send(self, value):
            now[0] += 5  # Preparación/envío lento del navegador.
            sent.append((now[0], value))
    composer = RaffleComposer(Fallback(), ["mod_one", "mod_two"])
    composer.observe(message())
    composer.observe(message(user="mod_two", id="two"))
    participant = ChatParticipation(frames=LatestFrame(), capture=SimpleNamespace(live=threading.Event()),
                                    stop=threading.Event(), composer=composer, sender=Sender(),
                                    interval=180, wait_first=True)
    async def scenario():
        # Sin esperar al vídeo ni al contador normal, la confirmación IRC basta para la frase.
        await participant.tick()
        assert sent == [(1005.0, "coco")]
        now[0] = 1064
        await participant.tick()
        assert len(sent) == 1
        now[0] = 1065
        await participant.tick()
        assert sent == [(1005.0, "coco"), (1070.0, "coco")]
    asyncio.run(scenario())


def test_confirmed_phrase_wakes_loop_without_waiting_for_normal_tick(monkeypatch):
    from sniper.capture import LatestFrame
    from sniper.participation import ChatParticipation
    class Fallback:
        async def compose(self, *a, **kw):
            raise AssertionError("No debe esperar al compositor normal")
        def remember(self, message):
            pass
    async def scenario():
        stop = threading.Event()
        sent = asyncio.Event()
        class Sender:
            emote_only = False
            async def start(self):
                pass
            async def send(self, value):
                assert value == "coco"
                sent.set()
                stop.set()
        composer = RaffleComposer(Fallback(), ["mod_one", "mod_two"])
        participant = ChatParticipation(frames=LatestFrame(), capture=SimpleNamespace(live=threading.Event()),
                                        stop=stop, composer=composer, sender=Sender(), interval=180, wait_first=True)
        task = asyncio.create_task(participant.run())
        try:
            await asyncio.sleep(0)  # El bucle queda esperando su próximo plazo.
            now = raffle.time.time()
            composer.observe(message(timestamp=now))
            composer.observe(message(user="mod_two", id="two", timestamp=now))
            await asyncio.wait_for(sent.wait(), timeout=0.5)
            await asyncio.wait_for(task, timeout=0.5)
        finally:
            stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_new_phrase_cannot_preempt_required_third_normal_turn(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(raffle.time, "perf_counter", lambda: now[0])
    class Fallback:
        async def compose(self, *args, **kwargs):
            composer.observe(message(text="mango", id="new-one", timestamp=now[0]))
            composer.observe(message(user="mod_two", text="mango", id="new-two", timestamp=now[0]))
            assert composer.priority_due() is None
            return "Kappa", "emote"
        def remember(self, text):
            pass
    composer = RaffleComposer(Fallback(), ["mod_one", "mod_two"])
    composer.observe(message())
    composer.observe(message(user="mod_two", id="two"))
    async def scenario():
        assert (await composer.compose_priority())[0] == "coco"
        composer.after_priority_attempt()
        now[0] += 60
        assert (await composer.compose_priority())[0] == "coco"
        composer.after_priority_attempt()
        now[0] += 180
        assert await composer.compose(None) == ("Kappa", "emote")
        assert composer.priority_due() is None
        composer.after_normal_attempt(attempted=True)
        assert composer.priority_due() == 0
        assert (await composer.compose_priority())[0] == "mango"
    asyncio.run(scenario())


def test_failed_reservation_disables_priority_instead_of_retrying_each_second(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    composer = RaffleComposer(object(), ["mod_one", "mod_two"])
    composer.observe(message())
    composer.observe(message(user="mod_two", id="two"))
    def failed(*args, **kwargs):
        raise PermissionError("No se puede escribir el registro")
    monkeypatch.setattr(composer.ledger, "reserve", failed)
    assert asyncio.run(composer.compose_priority()) is None
    assert composer.failed and composer.priority_due() is None


def test_confirmed_phrase_interrupts_slow_normal_reaction():
    from sniper.capture import LatestFrame
    from sniper.participation import ChatParticipation
    async def scenario():
        generating = asyncio.Event()
        cancelled = asyncio.Event()
        class SlowFallback:
            async def compose(self, *args, **kwargs):
                generating.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            def remember(self, value):
                pass
        sent = []
        class Sender:
            emote_only = False
            async def start(self):
                pass
            async def send(self, value):
                sent.append(value)
        composer = RaffleComposer(SlowFallback(), ["mod_one", "mod_two"])
        frames = LatestFrame()
        frames.put({})
        live = threading.Event()
        live.set()
        participant = ChatParticipation(frames=frames, capture=SimpleNamespace(live=live), stop=threading.Event(),
                                        composer=composer, sender=Sender(), interval=180)
        task = asyncio.create_task(participant.tick())
        try:
            await asyncio.wait_for(generating.wait(), timeout=0.5)
            now = raffle.time.time()
            composer.observe(message(timestamp=now))
            composer.observe(message(user="mod_two", id="two", timestamp=now))
            await asyncio.wait_for(task, timeout=0.5)
            assert sent == ["coco"] and cancelled.is_set()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_unavailable_chat_backs_off_without_spinning_or_postponing_normal_turn(monkeypatch):
    from sniper.capture import LatestFrame
    from sniper.chat_sender import ChatUnavailable
    from sniper.participation import ChatParticipation
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(raffle.time, "perf_counter", lambda: now[0])
    composer = RaffleComposer(object(), ["mod_one", "mod_two"])
    composer.observe(message())
    composer.observe(message(user="mod_two", id="two"))
    composer.wakeup.clear()
    calls = []
    class Sender:
        async def start(self):
            calls.append(True)
            raise ChatUnavailable("Pestaña no disponible")
    participant = ChatParticipation(frames=LatestFrame(), capture=SimpleNamespace(live=threading.Event()),
                                    stop=threading.Event(), composer=composer, sender=Sender(), interval=180,
                                    wait_first=True)
    async def scenario():
        original_due = participant.next_due
        await participant.tick()
        for _ in range(10):
            await participant.tick()
        assert calls == [True]
        assert participant.retry_at == 1030 and participant.next_due == original_due
        assert composer.ledger.memory["attempts"] == {} and not composer.wakeup.is_set()
        now[0] = 1030
        await participant.tick()
        assert len(calls) == 2
    asyncio.run(scenario())


def test_busy_ledger_read_is_transient_and_does_not_disable_raffles(tmp_path, monkeypatch):
    from sniper.filelock import FileLock
    now = [1000.0]
    monkeypatch.setattr(raffle.time, "time", lambda: now[0])
    monkeypatch.setattr(raffle.time, "monotonic", lambda: now[0])
    path = tmp_path / "raffle.json"
    ledger = RaffleLedger(path)
    assert ledger.reserve("coco")
    composer = RaffleComposer(object(), ["mod_one", "mod_two"], ledger=ledger)
    composer.observe(message())
    composer.observe(message(user="mod_two", id="two"))
    with FileLock(path.with_suffix(".lock")):
        assert composer.priority_due() is None and not composer.failed
    now[0] += 60
    assert composer.priority_due() == 0 and not composer.failed

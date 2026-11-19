"""Pruebas offline de captura, confirmación y parada de la alternativa Kick."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from sniper.capture import LatestFrame, StreamCapture, streamlink_cmd
from sniper.detector import Read
from sniper.kick import Confirmations, OneSuccess, campaign_finished, parse_args, run
from sniper.kick_detector import KickDetector
from sniper.redeemer import RedeemResult

A = "A1AA-B2BB-C3CC"
B = "D4DD-E5EE-F6FF"


def read(code=A):
    return Read(code, 0.99, code, time.perf_counter())


def test_capture_platform_flags():
    kick = streamlink_cmd("https://kick.com/elded", "best")
    assert "--kick-low-latency" in kick and "--twitch-low-latency" not in kick
    twitch = streamlink_cmd("https://www.twitch.tv/rubius", "best")
    assert "--twitch-low-latency" in twitch and "--kick-low-latency" not in twitch
    assert not StreamCapture("kick.com/elded", LatestFrame())._is_file()
    assert StreamCapture("test.mp4", LatestFrame())._is_file()


def test_confirmations_require_consecutive_scans_and_dedup():
    confirmations = Confirmations()
    assert confirmations.update([read(), read()]) == []  # misma imagen no cuenta dos veces
    assert confirmations.update([]) == []
    assert confirmations.update([read()]) == []
    assert [r.code for r in confirmations.update([read()])] == [A]
    assert confirmations.update([read()]) == []
    confirmations.update([])
    confirmations.update([read()])
    assert confirmations.update([read()]) == []


def test_only_one_candidate_consumed_per_scan_other_requires_fresh_confirmation():
    confirmations = Confirmations()
    confirmations.update([read(), read(B)])
    assert [r.code for r in confirmations.update([read(), read(B)])] == [A]
    confirmations.update([])
    assert confirmations.update([read(B)]) == []
    assert [r.code for r in confirmations.update([read(B)])] == [B]


@pytest.mark.parametrize("required", [2, 3])
def test_run_resets_round_on_start_and_requires_fresh_scans_after_rejection(tmp_path, monkeypatch, required):
    import sniper.kick as kick

    calls = []
    scanned = 0

    class FakeRedeemer:
        def __init__(self, **kwargs):
            pass

        async def start(self):
            calls.append("start")

        def new_round(self):
            calls.append("new_round")

        async def ensure_ready(self):
            calls.append("ensure_ready")
            return True

        async def submit(self, code, **kwargs):
            calls.append(("submit", code, scanned))
            return RedeemResult(code, "A", "already_used", "rechazo simulado", 1, 2, None)

        async def close(self):
            calls.append("close")

    class FakeFrames:
        def get_newer(self, seq, timeout):
            if seq >= 2 * required:
                return seq, None, time.perf_counter()
            return seq + 1, np.zeros((2, 2, 3), dtype=np.uint8), time.perf_counter()

    class FakeCapture:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def is_alive(self):
            return False

        def close(self):
            calls.append("capture_closed")

    class FakeDetector:
        def __init__(self, **kwargs):
            pass

        def read_frame(self, frame):
            nonlocal scanned
            scanned += 1
            return [read(), read(B)] if scanned <= required else [read(B)]

    monkeypatch.setattr(kick, "RUNS", tmp_path)
    monkeypatch.setattr(kick, "Redeemer", FakeRedeemer)
    monkeypatch.setattr(kick, "LatestFrame", FakeFrames)
    monkeypatch.setattr(kick, "StreamCapture", FakeCapture)
    monkeypatch.setattr(kick, "KickDetector", FakeDetector)
    async def inline_thread(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(kick.asyncio, "to_thread", inline_thread)
    args = parse_args(["--redeem", "--confirmations", str(required), "--scan-interval", "0.001"])
    async def scenario():
        try:
            await asyncio.wait_for(run(args), timeout=2)
        except TimeoutError:
            pytest.fail(f"La captura simulada no terminó: {scanned=}, {calls=}")
    asyncio.run(scenario())

    assert calls[:3] == ["start", "new_round", "ensure_ready"]
    assert [call for call in calls if isinstance(call, tuple)] == [
        ("submit", A, required), ("submit", B, 2 * required)]
    assert "capture_closed" in calls and calls[-1] == "close"


def test_detector_has_no_label_or_color_dependency_and_rejects_low_confidence():
    def engine(frame, **kwargs):
        assert frame.shape == (50, 100, 3)
        return SimpleNamespace(txts=[A, B, "quanttum3171", A], scores=[.99, .5, 1, float("nan")])
    detector = KickDetector(engine=engine, roi=(0, 0, .5, .5))
    reads = detector.read_frame(np.zeros((100, 200, 3), dtype=np.uint8))
    assert [r.code for r in reads] == [A]


@pytest.mark.parametrize("status", ["success", "timeout", "captcha", "blocked", "session_lost", "error"])
def test_stop_prevents_queued_candidate_after_terminal_result(status):
    async def scenario():
        calls = []
        class FakeRedeemer:
            async def ensure_ready(self):
                return True
            async def submit(self, code, **kwargs):
                calls.append(code)
                await asyncio.sleep(0)
                return RedeemResult(code, "A", status, status, 1, 2, None)
            def new_round(self):
                raise AssertionError("No abrir otra ronda tras un resultado terminal")
        stop = threading.Event()
        coordinator = OneSuccess(FakeRedeemer(), stop, lambda **kw: None)
        await asyncio.gather(coordinator.handle(read()), coordinator.handle(read(B)))
        assert calls == [A] and stop.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["invalid", "already_used", "region"])
def test_rejection_allows_different_code(status):
    async def scenario():
        class FakeRedeemer:
            rounds = 0
            async def ensure_ready(self):
                return True
            async def submit(self, code, **kwargs):
                return RedeemResult(code, "A", status, status, 1, 2, None)
            def new_round(self):
                self.rounds += 1
        redeemer = FakeRedeemer()
        stop = threading.Event()
        await OneSuccess(redeemer, stop, lambda **kw: None).handle(read())
        assert redeemer.rounds == 1 and not stop.is_set()
    asyncio.run(scenario())


def test_finished_campaign_returns_before_browser_or_capture(tmp_path, monkeypatch):
    import sniper.kick as kick
    monkeypatch.setattr(kick, "RUNS", tmp_path)
    directory = tmp_path / "kick-elded"
    directory.mkdir()
    (directory / "redeemer-attempts.json").write_text(json.dumps({
        "version": 1, "attempts": {A: {"status": "success"}}}))
    def forbidden(*a, **kw):
        raise AssertionError("No abrir navegador/captura tras éxito persistido")
    monkeypatch.setattr(kick, "Redeemer", forbidden)
    monkeypatch.setattr(kick, "StreamCapture", forbidden)
    asyncio.run(run(parse_args(["--redeem"])))
    assert campaign_finished(directory)


def test_unknown_ledger_fails_closed(tmp_path):
    (tmp_path / "redeemer-attempts.json").write_text('{"version": 2}')
    with pytest.raises(RuntimeError):
        campaign_finished(tmp_path)


def test_candidate_expiring_during_readiness_never_submits():
    async def scenario():
        candidate = read()
        class FakeRedeemer:
            async def ensure_ready(self):
                candidate.t -= 10
                return True
            async def submit(self, *a, **kw):
                raise AssertionError("El código ya caducó mientras PSN preparaba la pestaña")
        events = []
        stop = threading.Event()
        await OneSuccess(FakeRedeemer(), stop, lambda **kw: events.append(kw)).handle(candidate)
        assert events == [{"type": "expired", "code": A}] and not stop.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("argv", [
    ["--redeem", "--source", "https://kick.com/other"],
    ["--redeem", "--source", "https://kick.com/elded?clip=123"],
    ["--redeem", "--source", "test.mp4"],
    ["--min-score", "nan"], ["--scan-interval", "inf"],
    ["--roi", "0", "0", "nan", "1"], ["--confirmations", "1"],
    ["--channel", "../other"],
])
def test_invalid_options(argv):
    with pytest.raises(SystemExit):
        parse_args(argv)


def test_defaults_are_kick_simulation():
    args = parse_args([])
    assert args.source == "https://kick.com/elded" and not args.redeem


def test_no_send_errors_retry_and_stop_after_three_consecutive_failures():
    async def scenario():
        class FakeRedeemer:
            rounds = 0
            async def ensure_ready(self):
                return True
            async def submit(self, code, **kwargs):
                return RedeemResult(code, "A", "error", "no enviado", 0, 1, None)
            def new_round(self):
                self.rounds += 1
        redeemer = FakeRedeemer()
        stop = threading.Event()
        coordinator = OneSuccess(redeemer, stop, lambda **kw: None)
        assert await coordinator.handle(read()) is False and not stop.is_set()
        assert await coordinator.handle(read()) is False and not stop.is_set()
        assert await coordinator.handle(read()) is True and stop.is_set()
        assert redeemer.rounds == 2
    asyncio.run(scenario())


def test_rejection_limit_stops_without_another_submit():
    async def scenario():
        class FakeRedeemer:
            rejection_count = 2
            sent = []
            async def ensure_ready(self):
                return True
            async def submit(self, code, **kwargs):
                self.sent.append(code)
                self.rejection_count += 1
                return RedeemResult(code, "A", "invalid", "rechazado", 1, 2, None)
            def new_round(self):
                raise AssertionError("No abrir ronda después del límite")
        redeemer = FakeRedeemer()
        stop = threading.Event()
        coordinator = OneSuccess(redeemer, stop, lambda **kw: None)
        await coordinator.handle(read())
        await coordinator.handle(read(B))
        assert stop.is_set() and redeemer.sent == [A]
    asyncio.run(scenario())


@pytest.mark.parametrize("previous", ["cli", "other-channel", "same-channel"])
def test_success_across_ledgers_stops_unless_explicit_new_campaign(tmp_path, monkeypatch, previous):
    import sniper.kick as kick
    monkeypatch.setattr(kick, "RUNS", tmp_path)
    directory = {"cli": tmp_path, "other-channel": tmp_path / "kick-other", "same-channel": tmp_path / "kick-elded"}[previous]
    directory.mkdir(exist_ok=True)
    (directory / "redeemer-attempts.json").write_text(json.dumps({"version": 1, "attempts": {A: {"status": "success"}}}))
    calls = []
    class FakeRedeemer:
        def __init__(self, **kwargs):
            calls.append(kwargs["runs_dir"])
        async def start(self):
            pass
        def new_round(self):
            pass
        async def ensure_ready(self):
            return False
        async def close(self):
            pass
    monkeypatch.setattr(kick, "Redeemer", FakeRedeemer)
    asyncio.run(run(parse_args(["--redeem"])))
    assert calls == []
    asyncio.run(run(parse_args(["--redeem", "--new-campaign"])))
    assert len(calls) == 1 and calls[0].name.startswith("kick-elded-")
    assert json.loads((directory / "redeemer-attempts.json").read_text())["attempts"][A]["status"] == "success"


def test_persisted_rejections_stop_before_browser(tmp_path, monkeypatch):
    import sniper.kick as kick
    monkeypatch.setattr(kick, "RUNS", tmp_path)
    directory = tmp_path / "kick-elded"
    directory.mkdir()
    (directory / "redeemer-attempts.json").write_text(json.dumps({"version": 1, "attempts": {
        "one": {"status": "invalid"}, "two": {"status": "already_used"}, "three": {"status": "region"}}}))
    def forbidden(*a, **kw):
        raise AssertionError("El límite persistido debe detenerse antes de Brave")
    monkeypatch.setattr(kick, "Redeemer", forbidden)
    asyncio.run(run(parse_args(["--redeem"])))


def test_require_label_rejects_unlabelled_ocr():
    def engine(*args, **kw):
        return SimpleNamespace(txts=[A, "CODIGO: " + B], scores=[1, 1], boxes=[
            [[0, 0], [100, 0], [100, 10], [0, 10]],
            [[0, 20], [100, 20], [100, 30], [0, 30]]])
    detector = KickDetector(engine=engine, require_label=True)
    assert [r.code for r in detector.read_frame(np.zeros((30, 200, 3), dtype=np.uint8))] == [B]


def test_kick_lock_contention_returns_exit_three(tmp_path, monkeypatch, caplog):
    import sniper.kick as kick
    from sniper.filelock import FileLock
    monkeypatch.setattr(kick, "RUNS", tmp_path)
    with FileLock(tmp_path / ".redeemer.lock"):
        assert kick.main(["--redeem"]) == 3
    assert "Ya hay otro proceso de canje corriendo" in caplog.text

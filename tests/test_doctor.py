import json
from types import SimpleNamespace

import pytest

from sniper import doctor
from sniper.filelock import FileLock


def mock_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor, "RUNS", tmp_path / "runs")
    monkeypatch.setattr(doctor, "package_version", lambda *a: "1.0.mock")
    monkeypatch.setattr(doctor, "find_streamlink", lambda: "mock-streamlink")
    monkeypatch.setattr(doctor, "find_brave", lambda: "mock-brave")
    monkeypatch.setattr(doctor, "cdp_version", lambda: "Brave/mock")
    monkeypatch.setattr(doctor, "ocr_probe", lambda: "Código sintético reconocido en 10 ms")
    commands = []
    def command(args):
        commands.append(args)
        return SimpleNamespace(returncode=0, stdout="streamlink mock --kick-low-latency", stderr="")
    monkeypatch.setattr(doctor, "command", command)
    return commands


def test_doctor_offline_report_is_readonly_and_never_requests_a_browser(tmp_path, monkeypatch, capsys):
    commands = mock_environment(monkeypatch, tmp_path)
    assert doctor.main(["--kick", "elded", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["fail"] == 0
    assert not (tmp_path / "runs").exists()
    url_command = next(c for c in commands if "--can-handle-url" in c)
    assert url_command[-1] == "https://kick.com/elded"
    assert url_command[url_command.index("--webbrowser") + 1] == "no"
    assert all("--json" not in c for c in commands)


def test_doctor_failures_exit_nonzero_with_fix_and_continue(tmp_path, monkeypatch, capsys):
    mock_environment(monkeypatch, tmp_path)
    def missing(*args):
        raise ImportError("paquete ausente")
    monkeypatch.setattr(doctor, "package_version", missing)
    monkeypatch.setattr(doctor, "cdp_version", lambda: (_ for _ in ()).throw(ConnectionError("offline")))
    assert doctor.main(["--kick", "elded"]) == 1
    output = capsys.readouterr().out
    assert "✗" in output and "Sugerencia:" in output and "Canal Kick" in output


@pytest.mark.parametrize("state,expected", [("success", "warn"), ("timeout", "fail"), ("captcha", "fail"), ("invalid", "ok")])
def test_doctor_ledger_state_and_lock_are_readonly(tmp_path, state, expected):
    ledger = tmp_path / "redeemer-attempts.json"
    ledger.write_text(json.dumps({"version": 1, "attempts": {"code": {"status": state}}}))
    before = ledger.read_bytes()
    assert doctor.ledger_state(tmp_path)[0] == expected
    assert ledger.read_bytes() == before
    lock = tmp_path / ".redeemer.lock"
    lock.write_bytes(b"preserve")
    assert doctor.lock_state(lock)[0] == "ok"
    assert lock.read_bytes() == b"preserve"


def test_busy_lock_fails_without_writing(tmp_path):
    path = tmp_path / ".redeemer.lock"
    with FileLock(path):
        with pytest.raises(BlockingIOError):
            doctor.lock_state(path)


def test_streamlink_timeout_is_reported_and_other_checks_continue(tmp_path, monkeypatch):
    import subprocess
    mock_environment(monkeypatch, tmp_path)
    def timeout(args):
        raise subprocess.TimeoutExpired(args, 10)
    monkeypatch.setattr(doctor, "command", timeout)
    report, code = doctor.diagnose("elded")
    assert code == 1 and report["checks"][-1]["status"] == "fail"
    assert any(c["name"] == "OCR" and c["status"] == "ok" for c in report["checks"])


def test_ocr_probe_uses_synthetic_image_and_explicit_local_models(tmp_path, monkeypatch):
    import numpy as np
    import sys
    root = tmp_path / "rapidocr"
    models = root / "models"
    models.mkdir(parents=True)
    for name in ("det.onnx", "rec.onnx"):
        (models / name).write_bytes(b"mock")
    seen = {}
    class Engine:
        def __init__(self, *, params):
            seen.update(params)
        def __call__(self, image, **kw):
            assert image.shape == (120, 1100, 3) and np.any(image != 255)
            return SimpleNamespace(txts=["CODIGO: A1B2-C3D4-E5F6"])
    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(__file__=str(root / "__init__.py"), RapidOCR=Engine))
    assert "reconocido" in doctor.ocr_probe()
    assert seen["Det.model_path"] == str(models / "det.onnx")
    assert seen["Rec.model_path"] == str(models / "rec.onnx")


def test_cdp_probe_only_gets_version_endpoint(monkeypatch):
    from io import BytesIO
    calls = []
    class Opener:
        def open(self, url, timeout):
            calls.append((url, timeout))
            return BytesIO(b'{"Browser":"Brave/mock","webSocketDebuggerUrl":"ws://127.0.0.1:9333/devtools/browser/mock"}')
    monkeypatch.setattr(doctor.urllib.request, "build_opener", lambda *a: Opener())
    assert doctor.cdp_version() == "Brave/mock"
    assert calls == [("http://127.0.0.1:9333/json/version", 2)]


def test_windows_doctor_uses_windows_tools_and_hint(tmp_path, monkeypatch):
    import sys
    mock_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(doctor, "cdp_version", lambda: (_ for _ in ()).throw(ConnectionError("offline")))
    report, code = doctor.diagnose("elded")
    assert code == 1
    cdp = next(c for c in report["checks"] if c["name"] == "CDP local")
    assert "launch_brave.bat" in cdp["fix"]


def test_missing_local_ocr_models_does_not_construct_engine(tmp_path, monkeypatch):
    import sys
    def forbidden(**kw):
        raise AssertionError("No crear motor que pudiera descargar modelos")
    monkeypatch.setitem(sys.modules, "rapidocr", SimpleNamespace(__file__=str(tmp_path / "__init__.py"), RapidOCR=forbidden))
    with pytest.raises(RuntimeError, match="Faltan modelos OCR locales"):
        doctor.ocr_probe()

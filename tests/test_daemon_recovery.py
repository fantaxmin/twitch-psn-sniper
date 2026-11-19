"""PSN/capture recovery only; no browser connections or public actions."""
import asyncio
import io
import subprocess
from types import SimpleNamespace


from sniper.capture import LatestFrame, StreamCapture

CODE = "A1AA-B2BB-C3CC"


def test_streamlink_stderr_is_a_file_not_an_undrained_pipe(tmp_path, monkeypatch):
    path = tmp_path / "streamlink.log"
    seen = {}
    class Proc:
        stdout = io.BytesIO(b"video")
        def poll(self):
            return 0
        def wait(self):
            return 0
    def spawn(command, *, stdout, stderr, **kwargs):
        assert stdout == subprocess.PIPE and stderr != subprocess.PIPE
        seen["file"] = stderr
        stderr.write(b"warning\n" * 20000)  # More than a typical pipe buffer.
        return Proc()
    monkeypatch.setattr("sniper.capture.subprocess.Popen", spawn)
    monkeypatch.setattr("sniper.capture.av.open", lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
    capture = StreamCapture("https://www.twitch.tv/rubius", LatestFrame(), once=True, stderr_log=path)
    monkeypatch.setattr(capture, "_decode", lambda container: None)
    capture.run()
    assert path.stat().st_size > 65536 and seen["file"].closed

"""Captura del stream: streamlink (baja latencia) -> stdout -> PyAV. Solo se conserva el último frame."""
from __future__ import annotations

import logging
from contextlib import suppress
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit
from .platform_support import find_streamlink

import av
import numpy as np

log = logging.getLogger(__name__)


class LatestFrame:
    """Buzón de un solo slot: el productor sobrescribe, el consumidor espera un frame nuevo."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._seq = 0
        self.t_frame = 0.0

    def put(self, frame: np.ndarray):
        with self._cond:
            self._frame = frame
            self._seq += 1
            self.t_frame = time.perf_counter()
            self._cond.notify_all()

    def get_newer(self, last_seq: int, timeout: float = 1.0):
        with self._cond:
            self._cond.wait_for(lambda: self._seq != last_seq, timeout=timeout)
            return self._seq, self._frame, self.t_frame


def streamlink_cmd(source: str, quality: str) -> list[str]:
    host = urlsplit(source if "://" in source else "https://" + source).hostname
    latency = (["--kick-low-latency"] if host in {"kick.com", "www.kick.com"}
               else ["--twitch-low-latency"] if host in {"twitch.tv", "www.twitch.tv"} else [])
    return [
        find_streamlink() or "streamlink", "--stdout", "--loglevel", "warning",
        *latency, "--hls-live-edge", "2",
        "--stream-segment-threads", "2",
        "--retry-open", "1",
        source, quality,
    ]


class StreamCapture(threading.Thread):
    """Lee `source` (URL de Twitch/Kick en vivo/VOD, o archivo local) y publica frames BGR en `out`.

    - Twitch/Kick: lanza streamlink como subproceso y decodifica su stdout.
    - Archivo local (para pruebas): lo decodifica a velocidad real si `realtime=True`.
    Reintenta con backoff cuando el canal está offline o el stream se corta.
    """

    def __init__(self, source: str, out: LatestFrame, quality: str = "1080p60,best",
                 realtime: bool = True, stop: threading.Event | None = None, once: bool = False,
                 stderr_log: str | Path | None = None):
        super().__init__(daemon=True, name="capture")
        self.source, self.out, self.quality = source, out, quality
        self.realtime, self.once = realtime, once
        self.stop_ev = stop or threading.Event()
        self.live = threading.Event()
        self.frames = 0
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self.stderr_log = Path(stderr_log) if stderr_log is not None else Path(__file__).resolve().parents[1] / "runs" / "streamlink-stderr.log"

    def close(self) -> None:
        """Stop capture, including a Streamlink child stalled on network input."""
        self.stop_ev.set()
        with self._proc_lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                with suppress(ProcessLookupError):
                    proc.terminate()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=3)
        with self._proc_lock:
            proc = self._proc
            if proc is not None and proc.poll() is None:
                with suppress(ProcessLookupError):
                    proc.kill()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=2)

    def _is_file(self) -> bool:
        return not self.source.startswith(("http://", "https://", "twitch.tv/", "www.twitch.tv/",
                                           "kick.com/", "www.kick.com/"))

    def run(self):
        backoff = 5
        while not self.stop_ev.is_set():
            proc = None
            container = None
            stderr_file = None
            try:
                if self._is_file():
                    container = av.open(self.source)
                else:
                    self.stderr_log.parent.mkdir(parents=True, exist_ok=True)
                    stderr_file = self.stderr_log.open("ab", buffering=0)
                    proc = subprocess.Popen(streamlink_cmd(self.source, self.quality),
                                            stdout=subprocess.PIPE, stderr=stderr_file, bufsize=0)
                    with self._proc_lock:
                        self._proc = proc
                        if self.stop_ev.is_set():
                            with suppress(ProcessLookupError):
                                proc.terminate()
                    container = av.open(proc.stdout, options={"fflags": "nobuffer", "probesize": "500000", "analyzeduration": "0"})
                self._decode(container)
                backoff = 5
            except Exception as e:  # noqa: BLE001 - cualquier fallo -> reconectar
                log.info("captura detenida (%s) %s; diagnóstico: %s", type(e).__name__, e,
                         self.stderr_log if not self._is_file() else "archivo local")
            finally:
                self.live.clear()
                if proc and proc.poll() is None:
                    with suppress(ProcessLookupError):
                        proc.kill()
                if container is not None:
                    try:
                        container.close()
                    except Exception:
                        log.debug("Error al cerrar el decodificador", exc_info=True)
                if proc:
                    proc.wait()
                    if proc.stdout:
                        proc.stdout.close()
                    with self._proc_lock:
                        self._proc = None
                if stderr_file is not None:
                    stderr_file.close()
            if self.once or self.stop_ev.is_set():
                break
            # offline o corte -> esperar y reintentar (máx 60 s)
            self.stop_ev.wait(backoff)
            backoff = min(backoff * 2, 60)

    def _decode(self, container):
        vs = container.streams.video[0]
        vs.thread_type = "AUTO"
        t0_wall = t0_pts = None
        self.live.set()
        log.info("stream abierto: %sx%s %s", vs.codec_context.width, vs.codec_context.height, vs.average_rate)
        for frame in container.decode(vs):
            if self.stop_ev.is_set():
                return
            if self.realtime and self._is_file() and frame.time is not None:
                if t0_wall is None:
                    t0_wall, t0_pts = time.perf_counter(), frame.time
                delay = (frame.time - t0_pts) - (time.perf_counter() - t0_wall)
                if delay > 0:
                    time.sleep(delay)
            self.out.put(frame.to_ndarray(format="bgr24"))
            self.frames += 1
        raise EOFError("fin del stream")

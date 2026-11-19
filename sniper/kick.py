"""Alternativa Kick: OCR de vídeo y un máximo de un canje PSN exitoso.

La ejecución predeterminada es una simulación. --redeem activa el canje real.
No envía mensajes al chat. El registro de la campaña persiste entre reinicios.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import re
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from .capture import LatestFrame, StreamCapture
from .kick_detector import KickDetector
from .redeemer import Redeemer
from .campaign import REJECTIONS, attempts, campaign_directories, campaign_finished, rejection_count
from .filelock import FileLock

log = logging.getLogger("sniper.kick")
RUNS = Path(__file__).resolve().parent.parent / "runs"


class Confirmations:
    """Exige el mismo código en escaneos consecutivos; nunca acumula lecturas viejas."""
    def __init__(self, required=2):
        self.required = required
        self.counts = {}
        self.seen = set()

    def update(self, reads):
        current = {read.code: read for read in reads}
        self.counts = {code: self.counts.get(code, 0) + 1 for code in current}
        ready = []
        for code, read in current.items():
            if self.counts[code] >= self.required and code not in self.seen:
                self.seen.add(code)
                ready.append(read)
                # Un canje puede tardar segundos; los demás requieren imágenes nuevas.
                break
        return ready


class OneSuccess:
    """Serializa candidatos y termina ante éxito o resultado que requiere revisión."""
    def __init__(self, redeemer, stop, event, max_age=3.0, max_rejections=3):
        self.redeemer, self.stop, self.event = redeemer, stop, event
        self.max_age = max_age
        self.max_rejections = max_rejections
        self.rejections = getattr(redeemer, "rejection_count", 0)
        self.no_send_failures = 0
        self.lock = asyncio.Lock()

    async def handle(self, read):
        async with self.lock:
            if self.stop.is_set():
                return True
            if self.rejections >= self.max_rejections:
                self.stop.set()
                self.event(type="halt", reason="Límite de rechazos alcanzado")
                return True
            if time.perf_counter() - read.t > self.max_age:
                self.event(type="expired", code=read.code)
                return False
            if not await self.redeemer.ensure_ready():
                self.stop.set()
                self.event(type="halt", reason="PSN no está listo o la campaña ya terminó")
                return True
            if time.perf_counter() - read.t > self.max_age:
                self.event(type="expired", code=read.code)
                return False
            result = await self.redeemer.submit(read.code, source="kick-ocr", confidence=read.score)
            self.event(type="result", **asdict(result))
            log.info("PSN: %s — %s", result.status, result.message)
            if result.status == "error" and result.t_submit == 0.0:
                self.no_send_failures += 1
                if self.no_send_failures >= 3:
                    self.stop.set()
                    self.event(type="halt", reason="Tres errores consecutivos antes del envío")
                    return True
                self.redeemer.new_round()
                return False  # liberar el candidato para confirmarlo de nuevo
            self.no_send_failures = 0
            if result.status not in REJECTIONS:
                self.stop.set()
                return True
            self.rejections = getattr(self.redeemer, "rejection_count", self.rejections + 1)
            if self.rejections >= self.max_rejections:
                self.stop.set()
                self.event(type="halt", reason="Límite de rechazos alcanzado")
                return True
            # Solo después de un rechazo explícito se permite un código diferente.
            self.redeemer.new_round()
            return True


async def run(args):
    campaign_dir = RUNS / ("kick-" + args.channel)
    if args.new_campaign:
        campaign_dir = RUNS / ("kick-" + args.channel + "-" + uuid.uuid4().hex)
    run_dir = campaign_dir / (time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns()}")
    stop = threading.Event()
    capture = redeemer = lock = events = None
    try:
        if args.redeem:
            RUNS.mkdir(parents=True, exist_ok=True)
            # El CLI PSN y la alternativa Kick comparten la exclusión del navegador.
            lock = FileLock(RUNS / ".redeemer.lock")
            lock.__enter__()
            if campaign_finished(campaign_dir, include_others=not args.new_campaign):
                log.info("Ya hubo un canje exitoso en esta campaña. Nos retiramos.")
                return
            for directory in campaign_directories(RUNS):
                if directory != campaign_dir and any(r["status"] == "timeout" for r in attempts(directory).values()):
                    raise RuntimeError(f"Hay un canje sin resolver en {directory}; revisarlo antes de continuar")
            if rejection_count(campaign_dir) >= args.max_rejections:
                log.warning("Límite de rechazos persistido alcanzado; no se inicia captura")
                return
            redeemer = Redeemer(runs_dir=campaign_dir, stop_after_success=True, global_lock_held=True)
            await redeemer.start()
            # Una ronda anterior rechazada no condiciona los códigos de este arranque.
            # new_round conserva los bloqueos y los resultados inciertos del registro.
            redeemer.new_round()
            if not await redeemer.ensure_ready():
                log.warning("PSN necesita revisión o la campaña terminó; no se inicia captura")
                return
        run_dir.mkdir(parents=True, exist_ok=True)
        events = (run_dir / "events.jsonl").open("a", encoding="utf-8")

        def event(**values):
            events.write(json.dumps({"wall": time.time(), **values}, ensure_ascii=False) + "\n")
            events.flush()

        detector = await asyncio.to_thread(KickDetector, min_score=args.min_score, roi=args.roi,
                                           require_label=args.require_label)
        frames = LatestFrame()
        capture = StreamCapture(args.source, frames, quality=args.quality, stop=stop,
                                once=args.once, stderr_log=run_dir / "streamlink.log")
        confirmations = Confirmations(args.confirmations)
        coordinator = OneSuccess(redeemer, stop, event, args.max_frame_age, args.max_rejections) if redeemer else None
        capture.start()
        log.info("Kick /%s: %s. Registro: %s", args.channel,
                 "canje real, máximo un éxito" if args.redeem else "simulación sin canjes", run_dir)
        seq = 0
        while not stop.is_set():
            next_seq, frame, captured_at = await asyncio.to_thread(frames.get_newer, seq, 0.5)
            if next_seq == seq or frame is None:
                if not capture.is_alive():
                    break
                # No se confirma un candidato a partir de una captura detenida.
                confirmations.update([])
                continue
            seq = next_seq
            if time.perf_counter() - captured_at > args.max_frame_age:
                confirmations.update([])
                continue
            reads = await asyncio.to_thread(detector.read_frame, frame)
            if time.perf_counter() - captured_at > args.max_frame_age:
                confirmations.update([])
                continue
            for read in confirmations.update(reads):
                read.t = captured_at
                event(type="candidate", code=read.code, score=read.score, text=read.text)
                log.info("Código confirmado: %s (%.3f)", read.code, read.score)
                if coordinator:
                    if not await coordinator.handle(read):
                        confirmations.seen.discard(read.code)
                    confirmations.update([])
                    break
            await asyncio.sleep(args.scan_interval)
    finally:
        stop.set()
        try:
            if capture is not None:
                await asyncio.to_thread(capture.close)
        finally:
            try:
                if redeemer is not None:
                    await redeemer.close()
            finally:
                if events is not None:
                    events.close()
                if lock is not None:
                    lock.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="elded")
    parser.add_argument("--source", help="Vídeo local para simulación; en real debe ser el canal de Kick")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--redeem", action="store_true", help="Activa canjes reales de PSN hasta el primer éxito")
    mode.add_argument("--dry-run", action="store_true", help="Simulación (comportamiento predeterminado)")
    parser.add_argument("--quality", default="1080p60,1080p,best")
    parser.add_argument("--min-score", type=float, default=0.95)
    parser.add_argument("--confirmations", type=int, default=2)
    parser.add_argument("--scan-interval", type=float, default=0.25)
    parser.add_argument("--max-frame-age", type=float, default=3.0)
    parser.add_argument("--max-rejections", type=int, default=3, help="Tope persistido de rechazos PSN por campaña (3)")
    parser.add_argument("--new-campaign", action="store_true", help="Crear otro registro de campaña y permitir participar pese a éxitos previos")
    parser.add_argument("--require-label", action="store_true", help="Exigir CODIGO/CÓDIGO junto al código en la misma línea OCR")
    parser.add_argument("--roi", nargs=4, type=float, default=(0., 0., 1., 1.), metavar=("X0", "Y0", "X1", "Y1"),
                        help="En real, ajustar al widget para excluir chat y alertas (coordenadas relativas 0–1)")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,99}", args.channel):
        parser.error("Nombre de canal inválido")
    args.channel = args.channel.lower()
    args.source = args.source or "https://kick.com/" + args.channel
    if args.redeem:
        source = urlsplit(args.source)
        if (source.scheme != "https" or source.netloc not in {"kick.com", "www.kick.com"}
                or source.path.rstrip("/").lower() != "/" + args.channel or source.query or source.fragment):
            parser.error("El canje real requiere la URL HTTPS del mismo canal de Kick, sin clips ni VOD")
    if not math.isfinite(args.min_score) or not 0 < args.min_score <= 1:
        parser.error("--min-score debe estar en (0, 1]")
    if args.confirmations < 2:
        parser.error("--confirmations debe ser al menos 2")
    if args.max_rejections < 1:
        parser.error("--max-rejections debe ser al menos 1")
    if any(not math.isfinite(v) or v <= 0 for v in (args.scan_interval, args.max_frame_age)):
        parser.error("Los tiempos deben ser números finitos positivos")
    x0, y0, x1, y1 = args.roi
    if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
        parser.error("--roi requiere 0 <= X0 < X1 <= 1 y 0 <= Y0 < Y1 <= 1")
    return args


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except BlockingIOError:
        log.error("Ya hay otro proceso de canje corriendo")
        return 3
    except Exception as exc:
        log.error("Kick detenido: %s: %s", type(exc).__name__, exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Envío periódico al chat de Twitch, con contador y registro de mensajes.

  uv run python -m sniper.daemon                       # un mensaje cada 5 minutos
  uv run python -m sniper.daemon --chat-interval 420   # cada 7 minutos
  uv run python -m sniper.daemon --dry-run             # vista previa, sin navegador/envíos

La captura solo aporta contexto visual a las reacciones. No busca ni canjea
códigos y no escucha susurros. Ctrl+C detiene la ejecución.
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
from pathlib import Path
from urllib.parse import urlsplit

from .capture import LatestFrame, StreamCapture
from .chat_sender import BraveChatSender, ChatUnavailable
from .participation import ChatParticipation
from .reactions import ReactionComposer
from .session import ChatBrowserSession, ChatBrowserUnavailable

log = logging.getLogger("sniper")
RUNS = Path(__file__).resolve().parent.parent / "runs"


class ChatDaemon:
    def __init__(self, args, *, status_display=None):
        self.args = args
        self.status_display = status_display
        self.frames = LatestFrame()
        self.stop = threading.Event()
        self.run_dir = RUNS / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = (self.run_dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, **values):
        values["wall"] = time.time()
        self.events.write(json.dumps(values, ensure_ascii=False) + "\n")
        self.events.flush()

    async def main(self):
        session = None
        sender = None
        capture = None
        task = None
        try:
            if not self.args.dry_run:
                session = ChatBrowserSession()
                await session.connect()
                sender = BraveChatSender(session.context, self.args.chat, stop=self.stop)
                # Verify the Twitch session before starting capture. This doesn't send.
                await sender.start()
            capture = StreamCapture(self.args.source, self.frames, quality=self.args.quality,
                                    stop=self.stop, once=self.args.once,
                                    stderr_log=self.run_dir / "streamlink.log")
            participation = ChatParticipation(
                frames=self.frames, capture=capture, stop=self.stop, sender=sender,
                composer=ReactionComposer(self.args.vision_model), interval=self.args.chat_interval,
                dry_run=self.args.dry_run, event=self.event, status_display=self.status_display,
                wait_first=self.args.wait_first)
            capture.start()
            task = asyncio.create_task(participation.run())
            log.info("Solo chat #%s, cada %.0f s. Ctrl+C para detener. Registro: %s",
                     self.args.chat, self.args.chat_interval, self.run_dir)
            while not self.stop.is_set() and capture.is_alive():
                if task.done():
                    await task
                    break
                await asyncio.sleep(0.5)
        finally:
            self.stop.set()
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            try:
                if capture is not None:
                    await asyncio.to_thread(capture.close)
            finally:
                try:
                    if sender is not None:
                        await sender.close()
                finally:
                    try:
                        if session is not None:
                            await session.close()
                    finally:
                        self.events.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Envía mensajes periódicos al chat de Twitch con cuenta regresiva")
    parser.add_argument("--chat", default="rubius", help="Canal de Twitch (por defecto rubius)")
    parser.add_argument("--chat-interval", type=float, default=300, help="Segundos entre mensajes (300=5 min, 420=7 min)")
    parser.add_argument("--wait-first", action="store_true", help="Inicia con la cuenta regresiva antes del primer mensaje")
    parser.add_argument("--source", help="Stream para contexto visual; por defecto el canal elegido")
    parser.add_argument("--quality", default="1080p60,1080p,best")
    parser.add_argument("--vision-model", help="Modelo de visión local de Ollama; si falta, se usan emotes")
    parser.add_argument("--dry-run", action="store_true", help="Muestra mensajes simulados sin conectar a Brave ni enviar")
    parser.add_argument("--once", action="store_true", help="Termina si acaba la captura en lugar de reconectar")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,25}", args.chat):
        parser.error("--chat debe ser un nombre de canal válido")
    args.chat = args.chat.lower()
    if not math.isfinite(args.chat_interval) or args.chat_interval < 30:
        parser.error("--chat-interval debe ser un número finito de al menos 30 segundos")
    args.source = args.source or "https://www.twitch.tv/" + args.chat
    source = urlsplit(args.source if "://" in args.source else "https://" + args.source)
    if not args.dry_run and (source.hostname not in {"www.twitch.tv", "twitch.tv"}
                            or source.path.rstrip("/").lower() != "/" + args.chat):
        parser.error("La participación real requiere ver en vivo el mismo canal indicado en --chat")
    return args


def main(argv=None):
    args = parse_args(argv)
    from .console import CountdownConsole
    console = CountdownConsole()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s.%(msecs)03d %(levelname).1s %(threadName)s: %(message)s",
                        datefmt="%H:%M:%S", handlers=[console], force=True)
    try:
        asyncio.run(ChatDaemon(args, status_display=console.update if console.interactive else None).main())
    except KeyboardInterrupt:
        return 130
    except ChatBrowserUnavailable as exc:
        log.error("%s", exc)
        if args.verbose:
            log.debug("Detalle del fallo de conexión a Brave", exc_info=True)
        return 1
    except ChatUnavailable as exc:
        log.error("No se puede iniciar el chat: %s", exc)
        return 1
    finally:
        console.clear_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Lectura IRC de frases en vivo; no publica PRIVMSG ni extrae tokens de sesión."""
import asyncio
import logging
import random
import re

from .raffle import parse_message

log = logging.getLogger(__name__)


async def watch_messages(channel, on_message, *, stop, on_reset):
    backoff = 2
    while not stop.is_set():
        writer = None
        on_reset()
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("irc.chat.twitch.tv", 6697, ssl=True), timeout=10)
            nick = f"justinfan{random.randint(10000, 99999)}"
            writer.write(("CAP REQ :twitch.tv/tags twitch.tv/commands\r\n"
                          f"PASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel}\r\n").encode())
            await writer.drain()
            log.info("Lectura de rifas conectada a #%s", channel)
            backoff = 2
            while not stop.is_set():
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=1)
                except TimeoutError:
                    continue
                if not line:
                    raise ConnectionError("IRC cerrado")
                line = line.decode(errors="replace").rstrip("\r\n")
                if line.startswith("PING "):
                    writer.write(("PONG " + line[5:] + "\r\n").encode())
                    await writer.drain()
                    continue
                if re.fullmatch(r"(?::\S+ )?RECONNECT", line):
                    raise ConnectionError("IRC solicita reconexión")
                message = parse_message(line, channel)
                if message is not None:
                    on_message(message)
        except (OSError, ConnectionError) as exc:
            log.warning("Lectura de rifas desconectada (%s); reintento en %s s", type(exc).__name__, backoff)
            on_reset()
            for _ in range(backoff * 4):
                if stop.is_set():
                    break
                await asyncio.sleep(0.25)
            backoff = min(backoff * 2, 60)
        finally:
            on_reset()
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=2)
                except (OSError, TimeoutError):
                    pass

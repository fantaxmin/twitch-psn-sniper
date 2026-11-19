"""Chat de Twitch por IRC anónimo. Solo acepta códigos del broadcaster, mods o usuarios en lista blanca."""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .codes import find_codes

log = logging.getLogger(__name__)


def _tags(raw: str) -> dict[str, str]:
    out = {}
    for kv in raw.lstrip("@").split(";"):
        k, _, v = kv.partition("=")
        out[k] = v
    return out


async def watch_chat(channel: str, on_code, trusted: set[str] = frozenset(), stop: asyncio.Event | None = None):
    """Llama `await on_code(code, user, t)` por cada código válido de un emisor de confianza."""
    channel = channel.lower()
    backoff = 2
    while not (stop and stop.is_set()):
        writer = None
        try:
            reader, writer = await asyncio.open_connection("irc.chat.twitch.tv", 6697, ssl=True)
            nick = f"justinfan{random.randint(10000, 99999)}"
            writer.write(f"CAP REQ :twitch.tv/tags twitch.tv/commands\r\nPASS SCHMOOPIIE\r\nNICK {nick}\r\nJOIN #{channel}\r\n".encode())
            await writer.drain()
            log.info("chat conectado a #%s", channel)
            backoff = 2
            while not (stop and stop.is_set()):
                line = await reader.readline()
                if not line:
                    raise ConnectionError("chat cerrado")
                msg = line.decode(errors="ignore").rstrip("\r\n")
                if msg.startswith("PING"):
                    writer.write(b"PONG :tmi.twitch.tv\r\n")
                    await writer.drain()
                    continue
                if " PRIVMSG " not in msg:
                    continue
                t = time.perf_counter()
                tags_raw, _, rest = msg.partition(" ") if msg.startswith("@") else ("", "", msg)
                tags = _tags(tags_raw)
                user = tags.get("display-name", "").lower() or rest.split("!", 1)[0].lstrip(":")
                text = rest.split(" :", 1)[1] if " :" in rest else ""
                badges = tags.get("badges", "")
                ok = "broadcaster/" in badges or tags.get("mod") == "1" or user in trusted
                codes = find_codes(text, strict=True)
                if codes:
                    log.info("chat %s%s: %s", user, "" if ok else " (ignorado, no confiable)", text)
                if ok:
                    for c in codes:
                        await on_code(c, user, t)
        except (OSError, ConnectionError, asyncio.IncompleteReadError) as e:
            log.warning("chat desconectado: %s; reintento en %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=2)
                except (OSError, TimeoutError):
                    pass

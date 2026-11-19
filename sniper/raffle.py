"""Frases confirmadas por usuarios vigilados; dos turnos y luego una reacción normal."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .filelock import FileLock
from .reactions import EMOTES, clean_reaction


def watched_users(values):
    users = sorted(set(value.strip().lstrip("@").lower() for value in values))
    if len(users) > 50 or any(not re.fullmatch(r"[a-z0-9_]{1,25}", value) for value in users):
        raise ValueError("Usa hasta 50 nombres de Twitch válidos, separados por comas")
    return users


def phrase(text):
    clean = clean_reaction(text)
    if not clean or len(clean) > 80 or len(clean.split()) > 12:
        return None
    # Solo una frase literal, no convertir anuncios largos en supuestas instrucciones.
    return clean


@dataclass
class ChatMessage:
    user: str
    text: str
    id: str
    timestamp: float
    moderator: bool


def parse_message(line, channel):
    if not line.startswith("@"):
        return None
    raw_tags, _, rest = line.partition(" ")
    tags = dict(part.partition("=")[::2] for part in raw_tags[1:].split(";"))
    match = re.fullmatch(r":([A-Za-z0-9_]+)![^ ]+ PRIVMSG #([A-Za-z0-9_]+) :(.*)", rest)
    if match is None or match[2].lower() != channel.lower():
        return None
    try:
        timestamp = float(tags.get("tmi-sent-ts", "")) / 1000
    except ValueError:
        return None
    if not tags.get("id") or not math.isfinite(timestamp):
        return None
    badges = {item.split("/", 1)[0] for item in tags.get("badges", "").split(",")}
    return ChatMessage(match[1].lower(), match[3], tags["id"], timestamp,
                       tags.get("mod") == "1" or "broadcaster" in badges)


class PhraseDetector:
    def __init__(self, users, *, minimum=2, window=60):
        self.users = set(watched_users(users))
        if minimum not in {2, 3} or len(self.users) < minimum:
            raise ValueError("La rifa requiere al menos 2 o 3 usuarios distintos en la lista")
        self.minimum, self.window = minimum, window
        self.observations = {}
        self.ids = {}

    def reset(self):
        self.observations.clear()
        self.ids.clear()

    def observe(self, message, *, now=None):
        now = time.time() if now is None else now
        if (message.user not in self.users or not message.moderator
                or not -5 <= now - message.timestamp <= self.window):
            return None
        candidate = phrase(message.text)
        if candidate is None:
            return None
        self.ids = {key: t for key, t in self.ids.items() if now - t <= self.window}
        if message.id in self.ids:
            return None
        self.ids[message.id] = now
        self.observations = {key: {user: t for user, t in observations.items() if now - t <= self.window}
                             for key, observations in self.observations.items()}
        self.observations = {key: users for key, users in self.observations.items() if users}
        key = candidate.casefold()
        self.observations.setdefault(key, {})[message.user] = message.timestamp
        # Los mensajes repetidos de una sola cuenta nunca sustituyen otra confirmación.
        if len(self.observations[key]) >= self.minimum:
            return candidate
        return None


class RaffleLedger:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else None
        self.blocked_normal_key = None
        self.memory = {"version": 1, "attempts": {}, "normal_due": False, "last_phrase_at": 0.0}

    def _turn(self, data, candidate):
        if (data.get("version") != 1 or not isinstance(data.get("attempts"), dict)
                or not isinstance(data.get("normal_due"), bool)
                or not isinstance(data.get("last_phrase_at"), (int, float))
                or not math.isfinite(data["last_phrase_at"])
                or any(type(count) is not int or not 0 <= count <= 2 for count in data["attempts"].values())):
            raise RuntimeError("Registro de rifas inválido; no se repetirán frases")
        if data["normal_due"]:
            data["normal_due"] = False
            return False, True
        if candidate is None:
            return False, False
        if time.time() - data["last_phrase_at"] < 180:
            return False, False
        key = hashlib.sha256(candidate.casefold().encode()).hexdigest()
        count = data["attempts"].get(key, 0)
        if count >= 2:
            return False, False
        data["attempts"][key] = count + 1
        data["last_phrase_at"] = time.time()
        data["last_phrase_key"] = key
        data["normal_due"] = count + 1 == 2
        return True, True

    def reserve(self, candidate):
        if self.path is None:
            result = self._turn(self.memory, candidate)[0]
            self.blocked_normal_key = self.memory.get("last_phrase_key")
            return result
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(self.path.with_suffix(".lock")):
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
                "version": 1, "attempts": {}, "normal_due": False, "last_phrase_at": 0.0}
            use_phrase, changed = self._turn(data, candidate)
            self.blocked_normal_key = data.get("last_phrase_key")
            if changed:
                fd, temporary = tempfile.mkstemp(prefix=".raffle-", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as stream:
                        json.dump(data, stream)
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            return use_phrase


class RaffleComposer:
    """Usa el mismo contador y remitente; el lector nunca envía mensajes por su cuenta."""
    def __init__(self, fallback, users, *, minimum=2, ledger=None, event=None):
        self.fallback = fallback
        self.detector = PhraseDetector(users, minimum=minimum)
        self.ledger = ledger if ledger is not None else RaffleLedger()
        self.event = event or (lambda **kw: None)
        self.active = None
        self.confirmed_at = 0
        self.last_raffle = False

    def reset(self):
        self.detector.reset()
        self.active = None

    def observe(self, message):
        candidate = self.detector.observe(message)
        if candidate:
            # Mantener la frase elegida durante el par; las siguientes esperan otra confirmación.
            if self.active is None or time.monotonic() - self.confirmed_at > 600:
                self.active = candidate
                self.event(kind="raffle_confirmed", phrase=candidate)
            if candidate.casefold() == self.active.casefold():
                self.confirmed_at = time.monotonic()

    async def compose(self, frame, *, emote_only=False):
        candidate = self.active if time.monotonic() - self.confirmed_at <= 600 else None
        self.last_raffle = False
        use_phrase = self.ledger.reserve(None if emote_only else candidate)
        if not emote_only and use_phrase:
            self.last_raffle = True
            self.event(kind="raffle_reserved")
            return candidate, "raffle"
        # Aunque aparezca otra frase, el turno posterior al segundo es siempre normal.
        if candidate is not None:
            self.active = None
        message, source = await self.fallback.compose(frame, emote_only=emote_only)
        # Si la palabra clave era un emote, el turno normal debe usar otro distinto.
        if hashlib.sha256(message.casefold().encode()).hexdigest() == self.ledger.blocked_normal_key:
            message = next(emote for emote in EMOTES
                           if hashlib.sha256(emote.casefold().encode()).hexdigest() != self.ledger.blocked_normal_key)
            source = "emote"
        return message, source

    def remember(self, message):
        if not self.last_raffle:
            self.fallback.remember(message)

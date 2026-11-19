"""Asistente local: cuentas de referencia, canal y arranque con sesiones guardadas."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from .doctor import cdp_version
from .platform_support import find_brave

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".sniper-local.json"


def target(value, platform):
    value = value.strip()
    if "://" in value:
        parsed = urlsplit(value)
        domain = "twitch.tv" if platform == "twitch" else "kick.com"
        if (parsed.scheme != "https" or parsed.netloc not in {domain, "www." + domain}
                or parsed.query or parsed.fragment):
            raise ValueError("Usa la URL HTTPS del canal, sin clips, VOD ni parámetros")
        value = parsed.path.strip("/")
    pattern = r"[a-zA-Z0-9_]{1,25}" if platform == "twitch" else r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,99}"
    if not re.fullmatch(pattern, value):
        raise ValueError("Nombre de canal inválido para " + platform)
    channel = value.lower()
    domain = "www.twitch.tv" if platform == "twitch" else "kick.com"
    return channel, f"https://{domain}/{channel}"


def validate(config):
    keys = {"version", "mode", "twitch_user", "kick_user", "psn_online_id", "channel", "interval", "roi"}
    if not isinstance(config, dict) or set(config) != keys or config.get("version") != 1:
        raise ValueError("Configuración desconocida; vuelve a ejecutar el asistente")
    if config["mode"] not in {"twitch-chat", "kick-codes"}:
        raise ValueError("Modo desconocido")
    for key, platform in (("twitch_user", "twitch"), ("kick_user", "kick")):
        value = config[key]
        if not isinstance(value, str):
            raise ValueError("Usuario inválido")
        if value and target(value, platform)[0] != value:
            raise ValueError("Guarda solo el nombre de usuario, sin URL")
    if not isinstance(config["psn_online_id"], str) or not re.fullmatch(r"[a-zA-Z0-9_-]{0,64}", config["psn_online_id"]):
        raise ValueError("Usa un ID público/alias de PSN, no correo, contraseña ni accountId interno")
    platform = "twitch" if config["mode"] == "twitch-chat" else "kick"
    if not isinstance(config["channel"], str) or target(config["channel"], platform)[0] != config["channel"]:
        raise ValueError("Canal inválido")
    interval = config["interval"]
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not math.isfinite(interval) or interval < 30:
        raise ValueError("Intervalo inválido; usa al menos 30 segundos")
    roi = config["roi"]
    if roi is not None:
        if not isinstance(roi, list) or len(roi) != 4 or any(isinstance(v, bool) or not isinstance(v, (float, int)) for v in roi):
            raise ValueError("ROI inválida")
        x0, y0, x1, y1 = roi
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("ROI fuera de rango")
    return config


def load_config(path=None):
    path = Path(path) if path is not None else CONFIG
    return validate(json.loads(path.read_text(encoding="utf-8")))


def save_config(config, path=None):
    validate(config)
    path = Path(path) if path is not None else CONFIG
    fd, temporary = tempfile.mkstemp(prefix=".sniper-local-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(config, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def browser_command(url):
    executable = find_brave()
    if executable is None:
        raise RuntimeError("No se encontró Brave; instálalo antes de arrancar")
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if not base:
            raise RuntimeError("LOCALAPPDATA no está definido")
        profile = Path(base) / "twitch-psn-sniper/brave-profile"
    else:
        profile = Path.home() / ".local/share/twitch-psn-sniper/brave-profile"
    return [executable, "--user-data-dir=" + str(profile), "--remote-debugging-address=127.0.0.1",
            "--remote-debugging-port=9333", "--no-first-run", "--no-default-browser-check", url]


def ensure_browser(url, *, timeout=15):
    try:
        cdp_version()
        return
    except (OSError, ValueError):
        pass
    subprocess.Popen(browser_command(url), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            cdp_version()
            return
        except (OSError, ValueError):
            time.sleep(0.25)
    raise RuntimeError("Brave no activó CDP en 127.0.0.1:9333; revisa el perfil dedicado y vuelve a intentar")


def build_command(config, *, real=False):
    validate(config)
    if config["mode"] == "twitch-chat":
        if sys.platform == "win32":
            raise ValueError("El bot de chat está disponible para Linux; Windows cubre los códigos de Kick")
        channel, url = target(config["channel"], "twitch")
        cmd = [sys.executable, "-m", "sniper.daemon", "--chat", channel, "--source", url,
               "--chat-interval", str(config["interval"]), "--wait-first"]
        if not real:
            cmd.append("--dry-run")
        return cmd
    channel, url = target(config["channel"], "kick")
    cmd = [sys.executable, "-m", "sniper.kick", "--channel", channel, "--source", url]
    if real:
        if config["roi"] is None:
            raise ValueError("Configura la ROI del widget antes de activar el canje real")
        cmd.append("--redeem")
    else:
        cmd.append("--dry-run")
    if config["roi"] is not None:
        cmd.extend(["--roi", *map(str, config["roi"])])
    return cmd


def start(config, *, real=False):
    cmd = build_command(config, real=real)
    if real:
        platform = "twitch" if config["mode"] == "twitch-chat" else "kick"
        _, url = target(config["channel"], platform)
        print("Reutilizando las sesiones guardadas del perfil dedicado de Brave...", flush=True)
        print("Las cuentas guardadas son referencias; se utiliza la sesión autenticada de Brave.", flush=True)
        ensure_browser(url)
    if real:
        print("Preparando " + ("Twitch: comprobar sesión y cargar el chat antes de iniciar el contador."
                                 if config["mode"] == "twitch-chat"
                                 else "PSN: comprobar sesión antes de iniciar la captura de códigos."), flush=True)
    else:
        print("Arrancando " + ("chat con contador" if config["mode"] == "twitch-chat" else "captura de códigos")
              + " en vista previa.", flush=True)
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


def ask(prompt, default="", *, check=lambda v: v):
    while True:
        value = input(prompt + (f" [{default}]" if default else "") + ": ").strip() or default
        try:
            return check(value)
        except (ValueError, TypeError) as exc:
            print("Dato inválido: " + str(exc))


def wizard(previous=None):
    previous = previous or {}
    print("Configuración local. No escribas contraseñas, correos, cookies ni tokens.")
    print("1. Chat de Twitch (Linux)   2. Códigos PSN del vídeo de Kick")
    def choice(value):
        if value not in {"1", "2"}:
            raise ValueError("Elige 1 o 2")
        if value == "1" and sys.platform == "win32":
            raise ValueError("El bot de chat conserva su alcance de Linux")
        return value
    selected = ask("Modo", "2" if previous.get("mode") == "kick-codes" or sys.platform == "win32" else "1", check=choice)
    config = {"version": 1, "mode": "twitch-chat" if selected == "1" else "kick-codes"}
    for key, platform, label in (("twitch_user", "twitch", "Tu usuario de Twitch"),
                                 ("kick_user", "kick", "Tu usuario de Kick")):
        config[key] = ask(label + " (opcional, referencia)", previous.get(key, ""),
                          check=lambda v, p=platform: target(v, p)[0] if v else "")
    def psn_alias(value):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{0,64}", value):
            raise ValueError("Solo ID público/alias; no correo ni accountId interno")
        return value
    config["psn_online_id"] = ask("Tu ID público/alias de PSN (opcional, referencia)",
                                  previous.get("psn_online_id", ""), check=psn_alias)
    platform = "twitch" if selected == "1" else "kick"
    config["channel"] = ask("Canal DEL STREAMER o URL de " + platform, previous.get("channel", ""),
                             check=lambda v: target(v, platform)[0])
    config["interval"] = 300
    if selected == "1":
        def interval(value):
            number = float(value)
            if not math.isfinite(number) or number < 30:
                raise ValueError("Usa al menos 30 segundos")
            return number
        config["interval"] = ask("Segundos entre mensajes", str(previous.get("interval", 300)), check=interval)
    config["roi"] = None
    if selected == "2":
        def roi(value):
            coords = [float(v) for v in value.split()] if value else None
            test = {**config, "roi": coords}
            validate(test)
            return coords
        previous_roi = previous.get("roi")
        config["roi"] = ask("ROI X0 Y0 X1 Y1 (vacío para explorar en simulación)",
                             " ".join(map(str, previous_roi)) if previous_roi else "", check=roi)
    validate(config)
    print(f"Destino: {platform}/{config['channel']}. Las cuentas propias no cambian el canal del streamer.")
    print("1. Iniciar vista previa   2. Iniciar REAL (envía chat o canjea)   3. Solo guardar")
    def action_choice(value):
        if value not in {"1", "2", "3"}:
            raise ValueError("Elige 1, 2 o 3")
        return value
    action = ask("Acción", "1", check=action_choice)
    if action == "2":
        build_command(config, real=True)  # Validar antes de guardar o abrir nada.
    return config, action


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Arranca con la configuración guardada; simulación por defecto")
    parser.add_argument("--real", action="store_true", help="Con --run: envío/canje real; reutiliza sesiones de Brave")
    args = parser.parse_args(argv)
    if args.real and not args.run:
        parser.error("--real requiere --run; el asistente interactivo permite elegir modo real")
    try:
        if args.run:
            return start(load_config(), real=args.real)
        previous = load_config() if CONFIG.exists() else None
        config, action = wizard(previous)
        save_config(config)
        print("Configuración guardada en .sniper-local.json (excluido de Git).")
        return 0 if action == "3" else start(config, real=action == "2")
    except (KeyboardInterrupt, EOFError):
        print("Configuración cancelada.")
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print("No se pudo iniciar: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

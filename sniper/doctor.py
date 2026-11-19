"""Diagnóstico de solo lectura. Nunca inicia Brave ni se conecta mediante Playwright."""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .campaign import HALTS, REJECTIONS, attempts, campaign_directories
from .filelock import FileLock
from .platform_support import find_brave, find_streamlink, launch_hint

RUNS = Path(__file__).resolve().parent.parent / "runs"
PACKAGES = {"av": "av", "numpy": "numpy", "cv2": "opencv-python-headless",
            "rapidocr": "rapidocr", "playwright": "playwright"}


def package_version(module, distribution):
    imported = importlib.import_module(module)
    return getattr(imported, "__version__", None) or importlib.metadata.version(distribution)


def command(args):
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=10, check=False)


def cdp_version():
    # No usar proxies de entorno para este endpoint estrictamente local.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("http://127.0.0.1:9333/json/version", timeout=2) as response:
        payload = json.load(response)
    if not isinstance(payload.get("Browser"), str) or not payload.get("webSocketDebuggerUrl"):
        raise ValueError("El endpoint no parece un CDP de Chromium")
    return payload["Browser"]


def ocr_probe():
    import cv2
    import numpy as np
    import rapidocr
    from rapidocr import RapidOCR
    from .codes import find_codes

    # Rutas explícitas existentes: doctor no descarga pesos ni crea cachés.
    models = Path(rapidocr.__file__).resolve().parent / "models"
    det = next(iter(sorted(models.glob("*det*.onnx"))), None)
    rec = next(iter(sorted(models.glob("*rec*.onnx"))), None)
    if det is None or rec is None:
        raise RuntimeError("Faltan modelos OCR locales; preparar RapidOCR fuera de doctor")
    image = np.full((120, 1100, 3), 255, dtype=np.uint8)
    cv2.putText(image, "CODIGO: A1B2-C3D4-E5F6", (20, 80), cv2.FONT_HERSHEY_SIMPLEX,
                1.8, (0, 0, 0), 3, cv2.LINE_AA)
    started = time.perf_counter()
    engine = RapidOCR(params={"Global.use_cls": False, "Global.log_level": "error",
                              "Det.model_path": str(det), "Rec.model_path": str(rec)})
    result = engine(image, use_cls=False)
    elapsed = (time.perf_counter() - started) * 1000
    texts = getattr(result, "txts", None)
    text = " ".join(texts) if texts is not None else ""
    if "A1B2-C3D4-E5F6" not in find_codes(text):
        raise RuntimeError(f"OCR no reconoció el código sintético ({elapsed:.0f} ms): {text}")
    return f"Código sintético reconocido en {elapsed:.0f} ms (incluye carga del motor)"


def writable_directory(directory):
    parent = directory
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise PermissionError(f"Sin permiso de escritura en {parent}")
    return f"{directory}: permisos de escritura disponibles; no se creó ningún archivo"


def ledger_state(directory):
    records = list(attempts(directory).values())
    states = [r["status"] for r in records]
    if "timeout" in states:
        return "fail", "Hay un timeout pendiente; verificar resultado y resolver manualmente"
    if states and states[-1] in HALTS:
        return "fail", "Hay un bloqueo/CAPTCHA/sesión perdida en el registro; revisar antes de participar"
    if "success" in states:
        return "warn", "Éxito previo; la participación normal queda terminada"
    rejected = sum(status in REJECTIONS for status in states)
    if rejected >= 3:
        return "warn", f"{rejected} rechazos; límite predeterminado alcanzado"
    return "ok", f"{len(records)} intentos, {rejected} rechazos, sin resultado incierto"


def lock_state(path):
    if not path.exists():
        return "warn", "Candado aún no creado; no se creó para mantener el diagnóstico de solo lectura"
    with FileLock(path, readonly=True):
        return "ok", "Candado libre al comprobarlo; adquirido y liberado sin modificar el archivo"


def diagnose(kick=None):
    checks = []

    def check(name, action, fix):
        try:
            result = action()
            status, detail = result if isinstance(result, tuple) else ("ok", str(result))
        except Exception as exc:
            status, detail = "fail", f"{type(exc).__name__}: {exc}"
        checks.append({"name": name, "status": status, "detail": detail,
                       "fix": fix if status != "ok" else ""})

    def python_version():
        if sys.version_info < (3, 12):
            raise RuntimeError("Se requiere Python 3.12 o posterior")
        return sys.version.split()[0]

    check("Python", python_version, "Instalar Python 3.12+ y recrear el entorno virtual")
    for module, distribution in PACKAGES.items():
        check(f"Paquete {module}", lambda m=module, d=distribution: package_version(m, d),
              "Ejecutar uv sync desde la carpeta del proyecto")
    executable = find_streamlink()

    def streamlink_version():
        if not executable:
            raise FileNotFoundError("streamlink no está en PATH")
        result = command([executable, "--version"])
        if result.returncode:
            raise RuntimeError(result.stderr[:500])
        return result.stdout.strip()

    check("Streamlink", streamlink_version, "Ejecutar uv sync y lanzar el diagnóstico con uv run python -m sniper.doctor")
    if kick:
        def kick_flags():
            if not executable:
                raise FileNotFoundError("streamlink no está en PATH")
            result = command([executable, "--no-config", "--help"])
            if result.returncode or "--kick-low-latency" not in result.stdout:
                raise RuntimeError("Esta versión no anuncia --kick-low-latency")
            return "--kick-low-latency disponible"
        check("Kick baja latencia", kick_flags, "Actualizar streamlink")
    check("OCR", ocr_probe, "Instalar onnxruntime y preparar los modelos locales de RapidOCR; doctor no los descarga")

    def brave():
        executable = find_brave()
        if not executable:
            raise FileNotFoundError("Brave no encontrado")
        return executable

    check("Brave", brave, "Instalar Brave o añadir su ejecutable a PATH")
    check("CDP local", cdp_version, "Ejecutar manualmente " + launch_hint())
    check("Carpeta runs", lambda: writable_directory(RUNS), "Corregir los permisos de runs/ o de su carpeta padre")
    directories = campaign_directories(RUNS)
    selected = RUNS / ("kick-" + kick) if kick else RUNS
    if selected not in directories:
        directories.append(selected)
    lock_paths = {RUNS / ".redeemer.lock"}
    for directory in directories:
        check(f"Registro {directory.name}", lambda d=directory: ledger_state(d),
              "Revisar el registro; resolver timeouts con redeem_cli.py --resolve antes de continuar")
        lock_paths.add(directory / ".redeemer.lock")
    for path in sorted(lock_paths):
        check(f"Lock {path.parent.name}", lambda p=path: lock_state(p),
              "Si está ocupado, detener el proceso de canje; si no existe, se creará al participar")

    def platform_url():
        if not executable:
            raise FileNotFoundError("streamlink no está en PATH")
        url = "https://kick.com/" + kick if kick else "https://www.twitch.tv/rubius"
        result = command([executable, "--no-config", "--webbrowser", "no", "--can-handle-url", url])
        if result.returncode:
            raise RuntimeError("Streamlink no reconoce la URL: " + result.stderr[:500])
        return "warn", "URL compatible. Estado en vivo no consultado; no se inició navegador"

    check("Canal Kick" if kick else "Canal Twitch", platform_url,
          "La prueba comprueba compatibilidad de URL; confirmar el directo manualmente")
    summary = {status: sum(c["status"] == status for c in checks) for status in ("ok", "warn", "fail")}
    return {"checks": checks, "summary": summary}, 1 if summary["fail"] else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kick", metavar="CANAL")
    parser.add_argument("--json", action="store_true", dest="json_output")
    args = parser.parse_args(argv)
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.kick and not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,99}", args.kick):
        parser.error("Nombre de canal inválido")
    report, code = diagnose(args.kick.lower() if args.kick else None)
    if args.json_output:
        print(json.dumps(report, ensure_ascii=True, indent=2))
    else:
        for check in report["checks"]:
            symbol = {"ok": "✓", "warn": "!", "fail": "✗"}[check["status"]]
            print(f"{symbol} {check['name']}: {check['detail']}")
            if check["fix"]:
                print("  Sugerencia: " + check["fix"])
        summary = report["summary"]
        print(f"{summary['ok']} correctos, {summary['warn']} avisos, {summary['fail']} errores")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

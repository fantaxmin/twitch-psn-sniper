#!/usr/bin/env python3
"""Manual PSN inspection or an explicitly requested real redemption."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sniper.browser import BraveBrowser, BrowserStateError  # noqa: E402
from sniper.redeemer import MANUAL_RESOLUTION_STATUSES, Redeemer  # noqa: E402
from sniper.filelock import lock_directory  # noqa: E402
from sniper.platform_support import launch_hint  # noqa: E402


async def run(args: argparse.Namespace) -> int:
    if args.resolve:
        result = Redeemer.resolve_manual(args.resolve, status=args.resolve_as,
                                         runs_dir=args.runs_dir, message=args.message)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0
    if args.inspect or args.bench:
        browser = BraveBrowser(runs_dir=args.runs_dir)
        args.runs_dir.mkdir(parents=True, exist_ok=True)
        lock = lock_directory(args.runs_dir)
        try:
            await browser.connect()
            if args.bench:
                path = await browser.benchmark()
                report = json.loads(path.read_text(encoding="utf-8"))
                print(json.dumps({"benchmark": str(path), **report}, ensure_ascii=False, indent=2))
                return 0 if report["status"] == "ok" else 1
            path = await browser.inspect()
            print(json.dumps({"inspection": str(path)}, ensure_ascii=False))
            return 0
        finally:
            await browser.close()
            lock.close()
    redeemer = Redeemer(runs_dir=args.runs_dir)
    if redeemer._normalize(args.code) is None:
        print("Código inválido: se esperan 12 caracteres alfanuméricos, con guiones opcionales", file=sys.stderr)
        return 2
    try:
        await redeemer.start()
        result = await redeemer.submit(args.code, source="cli", confidence=1.0)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0 if result.status == "success" else 1
    finally:
        await redeemer.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Brave debe estar abierto con el perfil dedicado y PSN autenticado.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--inspect", action="store_true", help="Abre el modal y guarda selectores/mensajes/capturas; no introduce ni canjea códigos")
    mode.add_argument("--bench", action="store_true", help="Preflight de requestSubmit y clic trial sin force; requiere un modal limpio ya abierto")
    mode.add_argument("--code", metavar="XXXX-XXXX-XXXX", help="CANJE REAL: introduce el código y acepta las confirmaciones de canje")
    mode.add_argument("--resolve", metavar="XXXX-XXXX-XXXX", help="Resuelve un timeout del registro; no conecta al navegador ni envía el código")
    parser.add_argument("--as", dest="resolve_as", choices=MANUAL_RESOLUTION_STATUSES,
                        help="Estado comprobado manualmente para --resolve")
    parser.add_argument("--message", default="Resultado verificado manualmente por el usuario",
                        help="Evidencia o explicación para el registro de resolución manual")
    parser.add_argument("--runs-dir", type=Path, default=ROOT / "runs")
    args = parser.parse_args()
    if bool(args.resolve) != bool(args.resolve_as):
        parser.error("--resolve CODE y --as STATUS deben usarse juntos")
    try:
        return asyncio.run(run(args))
    except BrowserStateError as exc:
        print(json.dumps({"status": exc.status, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except BlockingIOError:
        print("Ya hay otro proceso de canje corriendo", file=sys.stderr)
        return 3
    except (ImportError, OSError, RuntimeError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"No se pudo conectar/operar Brave: {exc}. Abre {launch_hint()} e inicia sesión manualmente.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

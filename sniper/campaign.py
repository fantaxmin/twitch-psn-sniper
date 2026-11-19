"""Consultas de registros, sin navegador ni escrituras."""
import json
from pathlib import Path

REJECTIONS = {"invalid", "already_used", "region"}
HALTS = {"blocked", "captcha", "session_lost"}


def attempts(directory):
    ledger = Path(directory) / "redeemer-attempts.json"
    if not ledger.exists():
        return {}
    payload = json.loads(ledger.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("attempts"), dict):
        raise RuntimeError(f"Registro de campaña desconocido: {ledger}")
    for result in payload["attempts"].values():
        if not isinstance(result, dict) or not isinstance(result.get("status"), str):
            raise RuntimeError(f"Resultado de campaña inválido: {ledger}")
    return payload["attempts"]


def campaign_directories(root):
    root = Path(root)
    return [root, *sorted(path for path in root.glob("kick-*") if path.is_dir())]


def rejection_count(directory):
    return sum(result["status"] in REJECTIONS for result in attempts(directory).values())


def campaign_finished(directory, *, include_others=True):
    directory = Path(directory)
    directories = campaign_directories(directory.parent) if include_others else [directory]
    if directory not in directories:
        directories.append(directory)
    return any(result["status"] == "success" for folder in directories
               for result in attempts(folder).values())

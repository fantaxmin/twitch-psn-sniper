"""Normalización y validación de códigos de cupón PSN (12 caracteres)."""
from __future__ import annotations

import re

# 12 alfanuméricos, separados opcionalmente en grupos de 4 por guion/espacio/punto.
_CODE_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{4})[\s\-–—.·]?([A-Z0-9]{4})[\s\-–—.·]?([A-Z0-9]{4})(?![A-Z0-9])"
)
# Estricto: grupos separados obligatoriamente. Un nombre de usuario de 12 caracteres
# ("quanttum3171") no tiene guiones; un cupón mostrado en pantalla/chat sí.
_STRICT_RE = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{4})\s*[\-–—.·]\s*([A-Z0-9]{4})\s*[\-–—.·]\s*([A-Z0-9]{4})(?![A-Z0-9])"
)
# Etiqueta del widget: "CODIGO:" / "CÓDIGO" (tolerando confusiones O/0 e I/1 del OCR)
_LABEL_RE = re.compile(r"C[OÓ0]D[IÍ1L]G[O0]", re.I)


def has_code_label(text: str) -> bool:
    return bool(_LABEL_RE.search(text))


def find_codes(text: str, *, strict: bool = False) -> list[str]:
    """Devuelve los códigos XXXX-XXXX-XXXX de `text`.

    strict=True exige guiones/separadores entre los grupos (OCR y chat): evita tomar
    nombres de usuario de 12 caracteres como cupones.
    """
    up = text.upper()
    # El OCR a veces pega la etiqueta: "CODIGO:34MP..." -> separamos por ':'
    up = up.replace(":", " ")
    out = []
    for m in (_STRICT_RE if strict else _CODE_RE).finditer(up):
        code = "-".join(m.groups())
        # Exigir al menos un dígito y una letra: evita palabras de 12 letras y números puros
        raw = code.replace("-", "")
        if any(c.isdigit() for c in raw) and any(c.isalpha() for c in raw):
            out.append(code)
    return out


def normalize(raw: str) -> str | None:
    found = find_codes(raw)
    return found[0] if found else None


def compact(code: str) -> str:
    return code.replace("-", "")

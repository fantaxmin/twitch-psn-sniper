"""Localiza el widget "CODIGO: XXXX-XXXX-XXXX" y lee la línea con OCR solo-reconocimiento.

Ruta rápida (cada frame):
  1. Recorta la ROI (abajo-derecha por defecto) y la escala a una altura fija.
  2. Máscara de color del texto del código (degradado azul->magenta, saturación media, brillo alto).
  3. Si hay suficientes píxeles, agrupa en líneas horizontales (morfología) y para cada línea
     candidata ejecuta solo el reconocedor (~7 ms en CPU).
Ruta lenta (cada `full_scan_every` s, en otro hilo): det+rec sobre el frame completo, por si el
widget cambia de posición. Si encuentra el código, ajusta la ROI.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from .codes import find_codes, has_code_label

log = logging.getLogger(__name__)

# ROI relativa al frame de vídeo (x0, y0, x1, y1). Captura de referencia: widget en x .70-.98, y .64-.77
DEFAULT_ROI = (0.55, 0.45, 1.0, 0.92)
# Rango HSV (OpenCV: H 0-179) del texto del código
CODE_HSV_LO = np.array([95, 30, 140], np.uint8)
CODE_HSV_HI = np.array([175, 160, 255], np.uint8)
WORK_H = 540  # la ROI se escala para que el frame equivalga a 1080p*0.5 -> tamaños estables


@dataclass
class Read:
    code: str
    score: float
    text: str
    t: float
    crop: np.ndarray | None = field(default=None, repr=False)


def _make_engine():
    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    from rapidocr import RapidOCR

    return RapidOCR(params={"Global.use_cls": False, "Global.log_level": "warning"})


class Detector:
    def __init__(self, roi=DEFAULT_ROI, min_pixels: int = 60, min_score: float = 0.85):
        self.roi = roi
        self.min_pixels = min_pixels
        self.min_score = min_score
        self.engine = _make_engine()
        self._full_engine = None  # motor aparte: RapidOCR no es seguro entre hilos
        # Calentar
        self.engine(np.full((48, 320, 3), 30, np.uint8), use_det=False, use_cls=False, use_rec=True)

    # ---------- ruta rápida ----------
    def _roi(self, frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = self.roi
        return frame[int(h * y0):int(h * y1), int(w * x0):int(w * x1)]

    def line_boxes(self, roi_bgr: np.ndarray, scale: float) -> list[tuple[int, int, int, int]]:
        hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, CODE_HSV_LO, CODE_HSV_HI)
        if cv2.countNonZero(mask) < self.min_pixels * scale * scale:
            return []
        k = max(3, int(round(14 * scale)))
        closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 3)))
        n, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
        boxes = []
        for i in range(1, n):
            x, y, w, h, area = stats[i]
            # Línea de código: ancha, de altura de texto (~8-30 px a 540p)
            if w >= 60 * scale and 6 * scale <= h <= 34 * scale and w > 3 * h and area > 40 * scale * scale:
                boxes.append((x, y, w, h))
        boxes.sort(key=lambda b: -b[2])
        return boxes[:4]

    def read_frame(self, frame: np.ndarray, keep_crop: bool = False) -> list[Read]:
        """Devuelve lecturas válidas (formato de código) en este frame. Vacío si no hay widget."""
        roi = self._roi(frame)
        # Umbrales calibrados a 540p; escalan con la altura real del frame
        boxes = self.line_boxes(roi, scale=frame.shape[0] / WORK_H)
        reads = []
        now = time.perf_counter()
        for x, y, w, h in boxes:
            # Margen; se extiende a la izquierda para incluir "CODIGO:" (ayuda al reconocedor)
            pad = int(h * 0.6)
            xa = max(0, x - int(w * 0.6))
            crop = roi[max(0, y - pad):y + h + pad, xa:x + w + pad]
            if crop.size == 0:
                continue
            r = self.engine(crop, use_det=False, use_cls=False, use_rec=True)
            if not r.txts:
                continue
            text, score = r.txts[0], float(r.scores[0])
            # Solo la línea "CODIGO: XXXX-XXXX-XXXX"; el mismo widget muestra "Gracias <usuario>"
            if not has_code_label(text):
                continue
            for code in find_codes(text, strict=True):
                if score >= self.min_score:
                    reads.append(Read(code, score, text, now, crop.copy() if keep_crop else None))
        return reads

    # ---------- ruta lenta ----------
    def full_scan(self, frame: np.ndarray) -> list[Read]:
        """det+rec sobre el frame completo (~300+ ms). Si encuentra un código, recoloca la ROI."""
        if self._full_engine is None:
            self._full_engine = _make_engine()
        r = self._full_engine(frame, use_cls=False)
        out = []
        if getattr(r, "txts", None) is None or getattr(r, "boxes", None) is None:
            return out
        h, w = frame.shape[:2]
        for box, text, score in zip(r.boxes, r.txts, r.scores):
            if not has_code_label(text):
                continue
            for code in find_codes(text, strict=True):
                out.append(Read(code, float(score), text, time.perf_counter()))
                xs, ys = box[:, 0], box[:, 1]
                bw, bh = xs.max() - xs.min(), ys.max() - ys.min()
                new = (
                    max(0.0, (xs.min() - bw * 0.8) / w), max(0.0, (ys.min() - bh * 4) / h),
                    min(1.0, (xs.max() + bw * 0.5) / w), min(1.0, (ys.max() + bh * 4) / h),
                )
                tight = (xs.min() / w, ys.min() / h, xs.max() / w, ys.max() / h)
                if not self._inside(tight):
                    log.warning("widget fuera de la ROI, ampliando ROI a %s", tuple(round(float(v), 3) for v in new))
                    self.roi = tuple(float(v) for v in self._union(self.roi, new))
        return out

    def _inside(self, r):
        x0, y0, x1, y1 = self.roi
        return r[0] >= x0 and r[1] >= y0 and r[2] <= x1 and r[3] <= y1

    @staticmethod
    def _union(a, b):
        return (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))

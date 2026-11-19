"""OCR para un diseño de sorteo aún desconocido, sin depender del widget de Twitch."""
from __future__ import annotations

import math
import time

from .codes import find_codes, has_code_label
from .detector import Read, _make_engine


def _bounds(box):
    """Convierte un cuadrilátero OCR válido a sus límites, o falla cerrado."""
    try:
        points = [(float(point[0]), float(point[1])) for point in box]
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if len(points) != 4 or not all(math.isfinite(v) for point in points for v in point):
        return None
    xs, ys = zip(*points)
    bounds = (min(xs), min(ys), max(xs), max(ys))
    return bounds if bounds[0] < bounds[2] and bounds[1] < bounds[3] else None


def _adjacent_label(code, label):
    x0, y0, x1, y1 = code
    lx0, ly0, lx1, ly1 = label
    height = y1 - y0
    vertical_overlap = min(y1, ly1) - max(y0, ly0)
    left = lx1 <= x0 and x0 - lx1 <= 3 * height and vertical_overlap >= 0.5 * height
    horizontal_overlap = min(x1, lx1) - max(x0, lx0)
    above = ly1 <= y0 and y0 - ly1 <= 1.5 * height and horizontal_overlap > 0
    return left or above


class KickDetector:
    def __init__(self, *, min_score=0.95, roi=(0.0, 0.0, 1.0, 1.0), engine=None, require_label=False):
        self.min_score = min_score
        self.roi = roi
        self.require_label = require_label
        self.engine = engine if engine is not None else _make_engine()

    def read_frame(self, frame):
        h, w = frame.shape[:2]
        x0, y0, x1, y1 = self.roi
        crop = frame[int(y0 * h):int(y1 * h), int(x0 * w):int(x1 * w)]
        if crop.size == 0:
            return []
        result = self.engine(crop, use_cls=False)
        texts, scores = getattr(result, "txts", None), getattr(result, "scores", None)
        if texts is None or scores is None:
            return []
        bounds = []
        labels = []
        if self.require_label:
            boxes = getattr(result, "boxes", None)
            if boxes is None:
                return []
            bounds = [_bounds(box) for box in boxes]
            for index, (text, score) in enumerate(zip(texts, scores)):
                score = float(score)
                if (index < len(bounds) and bounds[index] is not None and has_code_label(text)
                        and math.isfinite(score) and score >= self.min_score):
                    labels.append(bounds[index])
        reads = []
        for index, (text, score) in enumerate(zip(texts, scores)):
            score = float(score)
            if not math.isfinite(score) or score < self.min_score:
                continue
            if self.require_label:
                if index >= len(bounds) or bounds[index] is None:
                    continue
                if not has_code_label(text) and not any(_adjacent_label(bounds[index], label) for label in labels):
                    continue
            for code in find_codes(text, strict=True):
                reads.append(Read(code, score, text, time.perf_counter()))
        return reads

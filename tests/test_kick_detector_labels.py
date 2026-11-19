from types import SimpleNamespace

import numpy as np
import pytest

from sniper.kick_detector import KickDetector

CODE = "A1B2-C3D4-E5F6"


def box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def detect(texts, boxes, *, require_label=True, scores=None):
    def engine(*args, **kwargs):
        return SimpleNamespace(txts=texts, boxes=boxes,
                               scores=scores if scores is not None else [0.99] * len(texts))
    detector = KickDetector(require_label=require_label, engine=engine)
    return detector.read_frame(np.zeros((400, 800, 3), dtype=np.uint8))


def test_same_box_accepts_code():
    assert [r.code for r in detect(["CODIGO: " + CODE], [box(100, 100, 400, 120)])] == [CODE]


def test_separate_left_label_accepts_code_with_half_vertical_overlap():
    boxes = np.array([box(20, 90, 80, 110), box(100, 100, 350, 120)])
    assert [r.code for r in detect(["CODIGO:", CODE], boxes)] == [CODE]


def test_label_above_accepts_code():
    assert [r.code for r in detect(["CÓDIGO:", CODE], [
        box(100, 65, 180, 85), box(100, 100, 350, 120)])] == [CODE]


@pytest.mark.parametrize("label_box", [
    box(0, 0, 60, 20),             # Otra esquina.
    box(400, 100, 460, 120),       # A la derecha.
    box(100, 130, 180, 150),       # Debajo.
    box(10, 100, 39, 120),         # Distancia horizontal >3 alturas.
    box(20, 89, 80, 109),          # Solape vertical <50%.
    box(100, 49, 180, 69),         # Distancia vertical >1.5 alturas.
    box(10, 70, 80, 90),           # Encima sin solape horizontal.
])
def test_non_adjacent_labels_rejected(label_box):
    assert detect(["CODIGO:", CODE], [label_box, box(100, 100, 350, 120)]) == []


def test_no_label_rejected():
    assert detect([CODE], [box(100, 100, 350, 120)]) == []


def test_missing_boxes_fail_closed_even_for_same_line_label():
    assert detect(["CODIGO: " + CODE], None) == []


def test_label_alone_does_not_produce_a_read():
    assert detect(["CODIGO:"], [box(100, 100, 180, 120)]) == []


@pytest.mark.parametrize("boxes", [None, [], [box(0, 0, 0, 0)]])
def test_without_require_label_boxes_do_not_change_detection(boxes):
    assert [r.code for r in detect([CODE], boxes, require_label=False)] == [CODE]


@pytest.mark.parametrize("code_box", [None, [], box(100, 100, 100, 120),
                                    box(100, 100, float("nan"), 120)])
def test_invalid_code_geometry_fails_closed(code_box):
    assert detect(["CODIGO: " + CODE], [code_box]) == []


def test_low_confidence_label_cannot_authorize_code():
    assert detect(["CODIGO:", CODE], [box(20, 100, 80, 120), box(100, 100, 350, 120)],
                  scores=[0.2, 0.99]) == []

import os, time, cv2
import pytest
from sniper.detector import Detector
from sniper.codes import find_codes

FIX = "tests/fixtures/widget_34MP-XF9A-H4RN.png"
needs_fixture = pytest.mark.skipif(not os.path.exists(FIX), reason="captura del stream no incluida en el repo")

def test_codes():
    assert find_codes("CODIGO: 34MP-XF9A-H4RN") == ["34MP-XF9A-H4RN"]
    assert find_codes("CODIGO:34MPXF9AH4RN") == ["34MP-XF9A-H4RN"]
    assert find_codes("SUSCRIBETE AHORA") == []

@needs_fixture
def test_fixture_and_scales():
    d = Detector()
    img = cv2.imread(FIX)
    for h in (720, 751, 1080):
        fr = cv2.resize(img, (int(img.shape[1] * h / img.shape[0]), h))
        d.read_frame(fr)
        t = time.perf_counter(); reads = d.read_frame(fr); dt = (time.perf_counter() - t) * 1000
        print(h, round(dt, 1), "ms", reads)
        assert [r.code for r in reads][:1] == ["34MP-XF9A-H4RN"]
        assert dt < 60

@needs_fixture
def test_no_widget():
    d = Detector()
    img = cv2.imread(FIX)
    img[470:590, 930:1310] = img[470:590, 500:880]  # tapar el widget con escena
    assert d.read_frame(img) == []

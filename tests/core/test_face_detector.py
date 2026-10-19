import pytest

from minavio.core import reframe

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")


def test_yunet_model_is_bundled_and_loads():
    assert reframe.YUNET_MODEL.is_file()
    detect = reframe._yunet_detector()
    assert detect is not None
    assert detect(np.zeros((720, 1280, 3), np.uint8)) is None  # pas de visage

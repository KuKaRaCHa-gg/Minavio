"""Candidats de miniatures : notation, dédoublonnage, plafond par highlight (vidéos lavfi)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from minavio.core.models import Highlight
from minavio.core.thumbs import MAX_PER_HIGHLIGHT, extract_candidates
from tests.core.helpers import ff

FACE = Path(__file__).parents[1] / "fixtures" / "face.jpg"
ENC = ("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("thumbs_media")
    size = "s=640x360:r=10:d=4"
    specs = {
        "sharp": ["-f", "lavfi", "-i", f"testsrc2={size}"],
        "blur": ["-f", "lavfi", "-i", f"testsrc2={size}", "-vf", "boxblur=25"],
        "black": ["-f", "lavfi", "-i", f"color=c=black:{size}"],
        "noise": [
            *("-f", "lavfi", "-i", f"color=c=gray:{size},noise=alls=100:allf=t"),
        ],
        "face": [
            *("-loop", "1", "-framerate", "10", "-t", "4", "-i", str(FACE)),
            *("-vf", "scale=-2:360,pad=640:360:(ow-iw)/2:0"),
        ],
    }
    out = {}
    for name, args in specs.items():
        out[name] = d / f"{name}.mp4"
        ff(*args, *ENC, str(out[name]))
    return out


def hl(src: Path, score: float = 0.6, label: str = "kill") -> Highlight:
    return Highlight(
        source=src,
        start=0.5,
        end=3.5,
        score=score,
        label=label,
        detector="test",
        signals={"peak_t": 2.0},
    )


def test_ordering_black_penalised_and_png_written(clips, profile, tmp_path):
    out = tmp_path / "thumbs"
    cands = extract_candidates(
        [hl(clips["sharp"]), hl(clips["blur"]), hl(clips["black"])], profile, out, n=12
    )
    assert cands
    scores = [c.score for c in cands]
    assert scores == sorted(scores, reverse=True)
    assert all(0 <= s <= 1 for s in scores)
    for c in cands:
        assert c.image.suffix == ".png" and c.image.is_file() and c.image.parent == out
        assert c.label == "kill"
        assert {"sharpness", "face", "peak", "highlight"} <= set(c.signals)
    assert all(c.source.stem != "black" or c.score < 0.1 for c in cands)
    assert cands[0].source.stem == "sharp"
    assert [p.name for p in out.iterdir() if p.name.startswith(".")] == []


def test_sharp_beats_blurred_and_black(clips, profile, tmp_path):
    def top(name):
        found = extract_candidates([hl(clips[name])], profile, tmp_path / name, n=1)
        return found[0].score if found else 0.0

    assert top("sharp") > top("blur") > top("black")
    assert top("black") < 0.1


def test_duplicates_collapsed(clips, profile, tmp_path):
    cands = extract_candidates([hl(clips["face"])], profile, tmp_path, n=12)
    assert len(cands) == 1  # image fixe => toutes les frames sont des doublons


def test_face_bonus(clips, profile, tmp_path):
    face = extract_candidates([hl(clips["face"])], profile, tmp_path / "a", n=1)[0]
    blur = extract_candidates([hl(clips["blur"])], profile, tmp_path / "b", n=1)[0]
    assert face.signals["face_height"] >= 0.08
    assert face.signals["face"] >= 0.6
    assert blur.signals["face"] == 0
    assert face.score > blur.score


def test_cap_per_highlight_and_n(clips, profile, tmp_path):
    one = extract_candidates([hl(clips["noise"])], profile, tmp_path / "a", n=12)
    assert 1 < len(one) <= MAX_PER_HIGHLIGHT
    two = extract_candidates([hl(clips["noise"]), hl(clips["sharp"])], profile, tmp_path / "b", n=2)
    assert len(two) == 2
    default_n = extract_candidates([hl(clips["noise"])], profile, tmp_path / "c")
    assert len(default_n) <= profile.thumbnails.candidates


def test_default_window_is_centre_and_peak_priority(clips, profile, tmp_path):
    h = Highlight(
        source=clips["noise"], start=0.0, end=4.0, score=0.5, label="x", detector="t"
    )  # pas de peak_t : centre = 2.0 s
    cands = extract_candidates([h], profile, tmp_path, n=3)
    assert cands and all(0.5 <= c.t <= 3.0 for c in cands)


def test_unreadable_source_is_skipped(clips, profile, tmp_path, caplog):
    bad = tmp_path / "missing.mp4"
    with caplog.at_level(logging.WARNING):
        cands = extract_candidates([hl(bad, 0.9), hl(clips["sharp"])], profile, tmp_path / "o")
    assert cands and all(c.source == clips["sharp"] for c in cands)
    assert "illisible" in caplog.text


def test_empty(profile, tmp_path):
    assert extract_candidates([], profile, tmp_path) == []


def test_partial_flash_frame_is_penalised():
    np = pytest.importorskip("numpy")
    pytest.importorskip("cv2")
    from minavio.core import thumbs

    rng = np.random.default_rng(0)
    normal = rng.integers(40, 200, (360, 640, 3), dtype=np.uint8)
    flash = normal.copy()
    flash[:, :300] = 255  # ~47 % brûlé, moyenne encore sous BRIGHT_LUMA
    a = thumbs._Frame(path=Path("a.png"), t=0.0, hl=0)
    b = thumbs._Frame(path=Path("b.png"), t=0.0, hl=0)
    thumbs._measure(normal, a, None, None)
    thumbs._measure(flash, b, None, None)
    assert a.exposure == 1.0 and b.exposure < 0.2

import json
import subprocess
from pathlib import Path

import pytest

from minavio.config.schema import ThumbnailFormat
from minavio.core.models import ThumbnailCandidate, ThumbnailChoice
from minavio.motion import render, thumbnail

PIL = pytest.importorskip("PIL.Image")

YT = ThumbnailFormat(name="youtube", width=1280, height=720)
SHORT = ThumbnailFormat(name="short", width=1080, height=1920)
THEME = {"primary": "#FF3B30", "accent": "#FFD60A"}
FACE = Path(__file__).parents[1] / "fixtures" / "face.jpg"


def _choice(layout: str, n: int, tmp_path: Path) -> ThumbnailChoice:
    cands = []
    for i in range(n):
        p = tmp_path / f"c{i}.png"
        PIL.new("RGB", (640, 360), (40 * i + 30, 90, 200 - 40 * i)).save(p)
        cands.append(ThumbnailCandidate(source=Path("a.mp4"), t=i, image=p, score=0.5))
    return ThumbnailChoice(layout=layout, images=cands, text="INSANE CLUTCH")


@pytest.fixture
def fake_motion(tmp_path, monkeypatch):
    mdir = tmp_path / "motion"
    (mdir / "node_modules").mkdir(parents=True)
    monkeypatch.setenv("MINAVIO_MOTION_DIR", str(mdir))
    monkeypatch.setenv("MINAVIO_BROWSER_EXECUTABLE", "/opt/chrome")
    monkeypatch.setattr(render.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[dict] = []

    def fake_run(cmd, cwd=None, **kw):
        props = Path(next(c for c in cmd if c.startswith("--props=")).split("=", 1)[1])
        calls.append({"cmd": cmd, "cwd": cwd, "props": json.loads(props.read_text())})
        PIL.new("RGB", (1280, 720), (1, 2, 3)).save(Path(cmd[6]))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(thumbnail.subprocess, "run", fake_run)
    return mdir, calls


def test_remotion_still_command_and_props(fake_motion, tmp_path):
    mdir, calls = fake_motion
    out = thumbnail.render_thumbnail(
        _choice("split", 2, tmp_path),
        YT,
        THEME,
        tmp_path / "o" / "t.png",
        badge="MINIVITCH",
        kills=3,
    )
    assert out.exists()
    cmd = calls[0]["cmd"]
    assert calls[0]["cwd"] == mdir
    assert cmd[:6] == ["npx", "--no-install", "remotion", "still", "src/index.ts", "thumbnail"]
    assert "--browser-executable=/opt/chrome" in cmd
    props = calls[0]["props"]
    assert (props["layout"], props["width"], props["height"]) == ("split", 1280, 720)
    assert props["badge"] == "MINIVITCH" and props["kills"] == 3
    assert len(props["images"]) == 2 and props["images"][0].startswith("data:image/jpeg;base64,")
    assert props["theme"] == THEME
    assert not (tmp_path / "o" / "t.jpg").exists()  # PNG < 2 Mo : pas de JPEG
    assert not (tmp_path / "o" / "t.props.json").exists()


def test_large_png_gets_jpeg_companion(fake_motion, tmp_path, monkeypatch):
    monkeypatch.setattr(thumbnail, "YOUTUBE_MAX_BYTES", 1000)
    out = thumbnail.render_thumbnail(_choice("hero", 1, tmp_path), YT, THEME, tmp_path / "t.png")
    assert thumbnail.jpeg_companion(out).exists()
    assert thumbnail.upload_path(out).suffix == ".jpg"


def test_remotion_failure_falls_back_to_pillow(fake_motion, tmp_path, monkeypatch):
    monkeypatch.setattr(
        thumbnail.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "boom"),
    )
    out = thumbnail.render_thumbnail(_choice("hero", 1, tmp_path), YT, THEME, tmp_path / "t.png")
    assert PIL.open(out).size == (1280, 720)


@pytest.mark.parametrize("fmt", [YT, SHORT])
@pytest.mark.parametrize("layout,n", [("hero", 1), ("split", 2), ("collage", 3)])
def test_pillow_fallback_without_node(layout, n, fmt, tmp_path, monkeypatch):
    monkeypatch.setenv("MINAVIO_MOTION_DIR", str(tmp_path / "nomotion"))
    monkeypatch.setattr(render.shutil, "which", lambda name: None)
    out = thumbnail.render_thumbnail(
        _choice(layout, n, tmp_path),
        fmt,
        THEME,
        tmp_path / "t.png",
        badge="MINIVITCH",
        kills=7,
        highlight=(0.5, 0.4),
    )
    assert out.is_file()
    with PIL.open(out) as im:
        assert im.size == (fmt.width, fmt.height)


def test_pillow_uses_real_photo(tmp_path):
    cand = ThumbnailCandidate(source=Path("a.mp4"), t=0, image=FACE, score=1)
    choice = ThumbnailChoice(layout="hero", images=[cand], text="GG")
    out = thumbnail.render_thumbnail_pillow(choice, YT, THEME, tmp_path / "f.png")
    assert out.stat().st_size > 5000


def test_fit_text_balances_lines():
    lines, size = thumbnail.fit_text(["5", "KILLS", "IN", "10S"], 600, 300, 200)
    assert 1 <= len(lines) <= 3 and size > 0
    assert " ".join(lines) == "5 KILLS IN 10S"


def test_missing_image_raises(tmp_path):
    cand = ThumbnailCandidate(source=Path("a.mp4"), t=0, image=tmp_path / "nope.png", score=1)
    choice = ThumbnailChoice(layout="hero", images=[cand], text="GG")
    with pytest.raises(Exception, match="introuvable"):
        thumbnail.render_thumbnail_pillow(choice, YT, THEME, tmp_path / "x.png")

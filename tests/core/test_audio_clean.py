"""Séparation et débruitage : vidéos lavfi, Demucs et DeepFilterNet mockés (sans modèle)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from minavio.config import load_profile
from minavio.core import ffmpeg
from minavio.core.audio import denoise as denoise_mod
from minavio.core.audio import separate as separate_mod
from minavio.core.audio import tracks as tracks_mod
from minavio.core.audio.tracks import prepare_audio_tracks
from tests.core.helpers import ff


@pytest.fixture(scope="module")
def noisy_video(tmp_path_factory) -> Path:
    """4 s : « voix » (sinus 300 Hz) de 0 à 2 s, bruit rose seul de 2 à 4 s."""
    out = tmp_path_factory.mktemp("clean") / "noisy.mp4"
    ff(
        *("-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=4"),
        *("-f", "lavfi", "-i", "sine=f=300:r=44100:d=4"),
        *("-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.2:r=44100:d=4"),
        *(
            "-filter_complex",
            "[1:a]volume='lt(t,2)':eval=frame[v];[v][2:a]amix=inputs=2:normalize=0[a]",
        ),
        *("-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast"),
        *("-pix_fmt", "yuv420p", "-c:a", "aac", str(out)),
    )
    return out


def _profile(*, sep=False, den=False, backend="arnndn", remove_music=True):
    p = load_profile("minivitch")
    audio = p.audio.model_copy(
        update={
            "separation": p.audio.separation.model_copy(
                update={"enabled": sep, "remove_music": remove_music}
            ),
            "denoise": p.audio.denoise.model_copy(update={"enabled": den, "backend": backend}),
        }
    )
    return p.model_copy(update={"audio": audio})


def _rms_db(path: Path, af: str) -> float:
    err = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            f"{af},astats=metadata=0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    line = [ln for ln in err.splitlines() if "RMS level dB" in ln][-1]
    return float(line.split(":")[-1])


def _band_db(path: Path, freq: int) -> float:
    # 3 passe-bandes en cascade : un seul (ordre 2) laisse fuir un sinus voisin de ~13 dB seulement.
    band = f"bandpass=f={freq}:width_type=h:w=60"
    return _rms_db(path, ",".join([band] * 3))


def _duration_ts(path: Path) -> tuple[int, int, int]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-of",
            "json",
            "-show_entries",
            "stream=sample_rate,channels,duration_ts",
            str(path),
        ],
        capture_output=True,
        check=True,
    ).stdout
    s = json.loads(out)["streams"][0]
    return int(s["sample_rate"]), int(s["channels"]), int(s["duration_ts"])


def _assert_format(out: Path, source: Path) -> None:
    rate, channels, n = _duration_ts(out)
    assert (rate, channels) == (48000, 2)
    assert abs(n - round(ffmpeg.probe(source).duration * 48000)) <= 1


def test_disabled_returns_nothing(noisy_video, tmp_path):
    assert prepare_audio_tracks([noisy_video], _profile(), tmp_path) == {}


def test_arnndn_reduces_noise_and_keeps_duration(noisy_video, tmp_path):
    out = prepare_audio_tracks([noisy_video], _profile(den=True), tmp_path)[noisy_video]
    assert out.parent == tmp_path / "audio"
    assert out.name.startswith("noisy_") and out.suffix == ".wav"
    _assert_format(out, noisy_video)
    window = "atrim=2.2:3.8"  # hors voix
    assert _rms_db(out, window) < _rms_db(noisy_video, window) - 3


def test_cache_by_hash(noisy_video, tmp_path):
    p = _profile(den=True)
    first = prepare_audio_tracks([noisy_video], p, tmp_path)[noisy_video]
    mtime = first.stat().st_mtime_ns
    again = prepare_audio_tracks([noisy_video], p, tmp_path)[noisy_video]
    assert again == first and again.stat().st_mtime_ns == mtime
    h1 = tracks_mod.settings_hash(noisy_video, p)
    h2 = tracks_mod.settings_hash(noisy_video, _profile(den=True, backend="deepfilternet"))
    assert h1 != h2


def test_failure_means_no_entry(noisy_video, tmp_path, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(denoise_mod, "denoise_arnndn", boom)
    with caplog.at_level("WARNING"):
        res = prepare_audio_tracks([noisy_video], _profile(den=True), tmp_path)
    assert res == {}
    assert "kaboom" in caplog.text
    assert not list((tmp_path / "audio").glob("*.wav"))


def test_missing_demucs_is_not_an_exception(noisy_video, tmp_path, monkeypatch):
    monkeypatch.setattr(separate_mod, "demucs_available", lambda: False)
    assert prepare_audio_tracks([noisy_video], _profile(sep=True), tmp_path) == {}


def test_source_without_audio_skipped(silent_video, tmp_path):
    assert prepare_audio_tracks([silent_video], _profile(den=True), tmp_path) == {}


def _fake_demucs(calls):
    def fake(wav, out_dir, model):
        calls.append(model)
        d = out_dir / model / wav.stem
        d.mkdir(parents=True)
        stems = {}
        for name, f in {"vocals": 300, "drums": 1000, "bass": 100, "other": 2000}.items():
            p = d / f"{name}.wav"
            ff(*("-f", "lavfi", "-i", f"sine=f={f}:r=44100:d=4"), "-ac", "2", str(p))
            stems[name] = p
        return stems

    return fake


def test_demucs_remove_music(noisy_video, tmp_path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(separate_mod, "run_demucs", _fake_demucs(calls))
    out = prepare_audio_tracks([noisy_video], _profile(sep=True), tmp_path)[noisy_video]
    assert calls == ["htdemucs"]
    vocals, other = _band_db(out, 300), _band_db(out, 2000)
    assert abs((vocals - other) - 6) < 1.5  # other atténué de 6 dB
    assert _band_db(out, 1000) < vocals - 25  # drums retirés
    assert _band_db(out, 100) < vocals - 25  # bass retirée
    _assert_format(out, noisy_video)


def test_demucs_keep_music(noisy_video, tmp_path, monkeypatch):
    monkeypatch.setattr(separate_mod, "run_demucs", _fake_demucs([]))
    p = _profile(sep=True, remove_music=False)
    out = prepare_audio_tracks([noisy_video], p, tmp_path)[noisy_video]
    assert _band_db(out, 1000) > _band_db(out, 300) - 3


def test_deepfilternet_mocked_and_fallback(noisy_video, tmp_path, monkeypatch):
    used = []

    def fake_dfn(src, dst):
        used.append("dfn")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), str(dst)], check=True)

    monkeypatch.setattr(denoise_mod, "deepfilternet_available", lambda: True)
    monkeypatch.setattr(denoise_mod, "denoise_deepfilternet", fake_dfn)
    p = _profile(den=True, backend="auto")
    assert noisy_video in prepare_audio_tracks([noisy_video], p, tmp_path / "a")
    assert used == ["dfn"]

    def broken(src, dst):
        raise ImportError("df")

    monkeypatch.setattr(denoise_mod, "denoise_deepfilternet", broken)
    assert noisy_video in prepare_audio_tracks([noisy_video], p, tmp_path / "b")  # repli arnndn


def test_arnndn_model_embedded():
    assert denoise_mod.RNNOISE_MODEL.exists()
    assert "arnndn=m=" in denoise_mod.arnndn_filter()

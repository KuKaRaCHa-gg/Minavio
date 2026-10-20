"""Tests de `minavio watch` : file, stabilité, lots, état persistant, échecs. Horloge factice."""

from __future__ import annotations

import os
import time
from pathlib import Path

from typer.testing import CliRunner

from minavio import pipeline
from minavio.cli import app
from minavio.errors import MinavioError
from minavio.watch import Watcher


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _make(tmp_path: Path, calls: list, clock: FakeClock, *, fail: bool = False) -> Watcher:
    def run(profile_id: str, paths: list[Path]) -> None:
        calls.append([p.name for p in paths])
        if fail:
            raise MinavioError("boom")

    folder = tmp_path / "clips"
    folder.mkdir(exist_ok=True)
    return Watcher(
        "minivitch",
        folder,
        stable_s=30,
        batch_s=600,
        state_path=tmp_path / "state.json",
        run=run,
        clock=clock,
    )


def _clip(tmp_path: Path, name: str, data: bytes = b"abc") -> Path:
    p = tmp_path / "clips" / name
    p.parent.mkdir(exist_ok=True)
    p.write_bytes(data)
    return p


def _advance(w: Watcher, clock: FakeClock, t: float) -> None:
    clock.t = t
    w.tick()


def test_size_change_delays_launch(tmp_path: Path) -> None:
    calls: list = []
    clock = FakeClock()
    w = _make(tmp_path, calls, clock)
    clip = _clip(tmp_path, "a.mp4")
    w.offer(clip)
    for t in (10, 20, 29):
        _advance(w, clock, t)
    clip.write_bytes(b"abcdef")  # taille modifiée à t=29 : compteur de stabilité repart
    for t in (30, 40, 58, 59):  # le tick de t=30 voit le changement : stable à t=60
        _advance(w, clock, t)
    assert calls == []
    _advance(w, clock, 60)  # stable, lot daté de 60
    _advance(w, clock, 659)
    assert calls == []
    _advance(w, clock, 660)
    assert calls == [["a.mp4"]]


def test_stable_file_launched_after_batch_delay(tmp_path: Path) -> None:
    calls: list = []
    clock = FakeClock()
    w = _make(tmp_path, calls, clock)
    w.offer(_clip(tmp_path, "a.mp4"))
    _advance(w, clock, 30)  # stable, entre dans le lot
    assert calls == []
    _advance(w, clock, 629)
    assert calls == []
    _advance(w, clock, 630)
    assert calls == [["a.mp4"]]


def test_two_files_grouped_in_one_job(tmp_path: Path) -> None:
    calls: list = []
    clock = FakeClock()
    w = _make(tmp_path, calls, clock)
    w.offer(_clip(tmp_path, "a.mp4"))
    _advance(w, clock, 10)
    w.offer(_clip(tmp_path, "b.mkv"))
    _advance(w, clock, 40)  # a et b stables ; dernier arrivé à 40
    _advance(w, clock, 639)
    assert calls == []
    _advance(w, clock, 640)
    assert len(calls) == 1
    assert sorted(calls[0]) == ["a.mp4", "b.mkv"]


def test_processed_file_skipped_after_restart(tmp_path: Path) -> None:
    calls: list = []
    clock = FakeClock()
    clip = _clip(tmp_path, "a.mp4")
    w = _make(tmp_path, calls, clock)
    w.offer(clip)
    _advance(w, clock, 30)
    _advance(w, clock, 630)
    assert calls == [["a.mp4"]]

    clock2 = FakeClock()
    w2 = _make(tmp_path, calls, clock2)  # relit state.json
    assert w2.offer(clip) is False
    _advance(w2, clock2, 30)
    _advance(w2, clock2, 630)
    assert calls == [["a.mp4"]]


def test_failing_run_does_not_stop_loop_and_marks_failed(tmp_path: Path) -> None:
    calls: list = []
    clock = FakeClock()
    w = _make(tmp_path, calls, clock, fail=True)
    w.offer(_clip(tmp_path, "a.mp4"))
    _advance(w, clock, 30)
    _advance(w, clock, 630)  # lève dans run : ne doit pas remonter
    assert calls == [["a.mp4"]]

    import json

    state = json.loads((tmp_path / "state.json").read_text("utf-8"))
    entry = next(iter(state["files"].values()))
    assert entry["status"] == "failed"
    assert entry["error"] == "boom"

    # échec non relancé automatiquement, mais la boucle continue
    assert w.offer(tmp_path / "clips" / "a.mp4") is False  # même version : pas de relance
    _advance(w, clock, 700)
    _advance(w, clock, 1400)
    assert calls == [["a.mp4"]]
    w.offer(_clip(tmp_path, "b.mp4"))
    _advance(w, clock, 1430)
    _advance(w, clock, 2030)
    assert calls == [["a.mp4"], ["b.mp4"]]


def test_part_temp_and_wrong_extensions_ignored(tmp_path: Path) -> None:
    calls: list = []
    clock = FakeClock()
    w = _make(tmp_path, calls, clock)
    names = ["x.mp4.part", "x.part", "~x.mp4", ".x.mp4", "y.txt", "z.tmp", "notes.mp3"]
    for n in names:
        assert w.offer(_clip(tmp_path, n)) is False
    _advance(w, clock, 30)
    _advance(w, clock, 630)
    assert calls == []


def test_watch_once_cli_with_fake_run_job(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    clips = tmp_path / "clips"
    clips.mkdir()
    old = clips / "old.mp4"
    old.write_bytes(b"x" * 10)
    past = time.time() - 3600
    os.utime(old, (past, past))  # déjà stable
    (clips / "fresh.mp4").write_bytes(b"y")  # modifiée à l'instant : pas encore stable

    calls: list = []

    def fake_run_job(profile_id, inputs, **kwargs):
        calls.append((profile_id, [p.name for p in inputs], kwargs.get("music")))

    monkeypatch.setattr(pipeline, "run_job", fake_run_job)
    music = tmp_path / "m.mp3"
    result = CliRunner().invoke(
        app, ["watch", "minivitch", str(clips), "--once", "--music", str(music)]
    )
    assert result.exit_code == 0, result.output
    assert calls == [("minivitch", ["old.mp4"], music)]

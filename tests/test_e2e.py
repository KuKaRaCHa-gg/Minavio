"""Bout en bout : vidéo synthétique -> `minavio run` -> MP4 16:9 et 9:16.

Tout tourne en local : FFmpeg/OpenCV réels, LLM simulé par un vrai serveur HTTP sur localhost
(API OpenAI-compatible, comme Ollama/vLLM). Seule l'ASR est simulée (pas de modèle Whisper à
télécharger) et Remotion est désactivé pour utiliser le repli FFmpeg.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from minavio import pipeline
from minavio.core.ffmpeg import probe
from minavio.core.models import Highlight, Timeline, TranscriptSegment, Word

pytest.importorskip("cv2")
pytest.importorskip("numpy")
pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg requis")


def _ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="module")
def media(tmp_path_factory) -> tuple[Path, Path, Path]:
    d = tmp_path_factory.mktemp("media")
    clips = []
    # Deux clips « Outplayed » : flash blanc + cri (pic audio) à des instants différents.
    for i, (flash, dur) in enumerate([(5, 14), (8, 12)]):
        out = d / f"clip{i}.mp4"
        _ff(
            "-f", "lavfi", "-i", f"testsrc2=size=1280x720:rate=30:duration={dur}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
            "-filter_complex",
            f"[0:v]drawbox=color=white:t=fill:enable='between(t,{flash},{flash + 0.3})'[v];"
            f"[1:a]volume='if(between(t,{flash},{flash + 1}),1,0.03)':eval=frame[a]",
            "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(out),
        )  # fmt: skip
        clips.append(out)
    music = d / "music.wav"
    # Clic toutes les 0.5 s (120 BPM) pour le beat-sync.
    _ff(
        "-f", "lavfi", "-i",
        "aevalsrc='0.8*sin(2*PI*1000*t)*lt(mod(t,0.5),0.03)':s=44100:d=30", str(music),
    )  # fmt: skip
    return clips[0], clips[1], music


class _FakeQwen(BaseHTTPRequestHandler):
    hits: ClassVar[list[str]] = []

    def log_message(self, *a):
        pass

    def _send(self, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.hits.append(self.path)
        self._send({"data": [{"id": "qwen3:30b-a3b"}]})

    def do_POST(self):
        self.hits.append(self.path)
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = " ".join(m["content"] for m in req["messages"])
        if '"hook"' in prompt:
            content = {
                "title": "Deux clutchs de fou",
                "hook": "Il a pas vu venir",
                "description": "Best-of.",
                "tags": ["apex"],
                "popups": [{"at": 1.0, "text": "+1 KILL"}, {"at": 99.0, "text": "hors champ"}],
            }
        else:
            content = {"items": [{"id": i, "score": 0.9, "label": "clutch"} for i in range(20)]}
        # Qwen3 émet souvent un bloc de réflexion avant le JSON.
        text = "<think>analyse...</think>\n" + json.dumps(content)
        self._send({"choices": [{"message": {"role": "assistant", "content": text}}]})


@pytest.fixture
def fake_qwen(monkeypatch):
    _FakeQwen.hits = []
    server = HTTPServer(("127.0.0.1", 0), _FakeQwen)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("MINAVIO_LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    yield _FakeQwen.hits
    server.shutdown()


SPEECH = ["NON", "MAIS", "ATTENDS", "QUOI"]


def _fake_transcribe(source: Path, language=None) -> list[TranscriptSegment]:
    words = [Word(start=4.0 + 0.4 * i, end=4.4 + 0.4 * i, text=w) for i, w in enumerate(SPEECH)]
    return [TranscriptSegment(start=4.0, end=5.6, text=" ".join(SPEECH), words=words)]


def test_full_pipeline_offline(media, fake_qwen, tmp_path, monkeypatch):
    clip0, clip1, music = media
    monkeypatch.setenv("MINAVIO_MOTION_DIR", str(tmp_path / "no-remotion"))  # repli FFmpeg
    monkeypatch.setitem(pipeline._API, "transcribe", "tests.test_e2e:_fake_transcribe")

    result = pipeline.run_job(
        "thelostchat",
        [clip0.parent],  # dossier de clips, comme un export Outplayed
        music=music,
        work_root=tmp_path / "work",
        out_dir=tmp_path / "out",
    )

    # Le LLM local a bien été interrogé (classement + copy), et lui seul.
    assert any(h.endswith("/chat/completions") for h in fake_qwen)

    timeline = Timeline.model_validate_json(result.timeline_path.read_text())
    assert timeline.clips and timeline.beats
    assert {c.source for c in timeline.clips} <= {clip0, clip1}
    # Captions rattachées à leur source : aucun mot hors des clips qui la contiennent.
    assert timeline.captions and all(s.source in {clip0, clip1} for s in timeline.captions)

    overlays = json.loads((result.work_dir / "overlays.json").read_text())
    templates = [o["template"] for o in overlays]
    assert "title" in templates and "captions" in templates
    title = next(o for o in overlays if o["template"] == "title")
    assert title["props"]["position"] == "top"  # thelostchat : captions au centre
    popups = [o for o in overlays if o["template"] == "popup"]
    assert [p["props"]["text"] for p in popups] == ["+1 KILL"]  # le popup hors champ est retiré
    assert popups[0]["start"] < timeline.duration

    sizes = {}
    for out in result.outputs:
        info = probe(out)
        sizes[out.stem.rsplit("_", 1)[1]] = (info.width, info.height)
        assert info.has_audio and 0 < info.duration <= 59.5
    assert sizes == {"shorts": (1080, 1920), "youtube": (1920, 1080)}

    # Reprise : rien n'est recalculé, mêmes sorties.
    again = pipeline.run_job(
        "thelostchat", [clip0.parent], music=music,
        work_root=tmp_path / "work", out_dir=tmp_path / "out", resume=True,
    )  # fmt: skip
    assert again.outputs == result.outputs


def test_remap_popups_follows_montage_order():
    a, b = Path("a.mp4"), Path("b.mp4")
    hl = [
        Highlight(source=b, start=20, end=24, score=0.9, label="kill", detector="x"),
        Highlight(source=a, start=10, end=12, score=0.8, label="kill", detector="x"),
    ]
    # Le montage remet a avant b (ordre chronologique par source) avec pre-roll.
    tl = Timeline(
        profile_id="t",
        fps=30,
        clips=[
            {"source": a, "start": 9, "end": 13},
            {"source": b, "start": 19, "end": 25},
        ],
    )
    popups = [{"at": 1.0, "text": "B"}, {"at": 4.5, "text": "A"}, {"at": 50, "text": "X"}]
    # B : 21 s dans b -> 4 (clip a) + 2 = 6 ; A : 10.5 s dans a -> 1.5
    assert pipeline.remap_popups(popups, hl, tl) == [(1.5, "A"), (6.0, "B")]


def test_remap_popups_snaps_out_of_autocut_gap():
    a = Path("a.mp4")
    hl = [Highlight(source=a, start=10, end=20, score=0.9, label="kill", detector="x")]
    # L'autocut a retiré 12..14 : le popup à 13 s glisse au début du passage gardé suivant.
    tl = Timeline(
        profile_id="t",
        fps=30,
        clips=[{"source": a, "start": 10, "end": 12}, {"source": a, "start": 14, "end": 20}],
    )
    assert pipeline.remap_popups([{"at": 3.0, "text": "K"}], hl, tl) == [(2.0, "K")]
    assert pipeline.remap_popups([{"at": 9.5, "text": "far"}], hl, tl) == [(7.5, "far")]

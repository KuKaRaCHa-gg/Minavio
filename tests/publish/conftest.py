"""Faux service googleapiclient : aucun réseau, aucun module Google requis."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest
from PIL import Image

from minavio.config import load_profile, secrets
from minavio.publish import youtube


class FakeHttpError(Exception):
    """Même interface utile que googleapiclient.errors.HttpError (resp.status, content)."""

    def __init__(self, status: int, reason: str = ""):
        super().__init__(f"HTTP {status} {reason}")
        self.resp = SimpleNamespace(status=status)
        self.content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()


class FakeProgress:
    def progress(self) -> float:
        return 0.5


class FakeRequest:
    def __init__(self, service, kind, kwargs, outcomes=None, result=None):
        self.service, self.kind, self.kwargs = service, kind, kwargs
        self.outcomes = list(outcomes or [])
        self.result = result if result is not None else {}

    def execute(self):
        self.service.calls.append((self.kind, self.kwargs))
        err = self.service.errors.get(self.kind)
        if err is not None:
            raise err
        return self.result

    def next_chunk(self):
        self.service.calls.append((self.kind + ".chunk", None))
        if self.outcomes:
            out = self.outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return FakeProgress(), None
        self.service.calls.append((self.kind, self.kwargs))
        return None, {"id": self.service.next_id()}


class FakeResource:
    def __init__(self, service, name, method):
        self.service, self.name, self.method = service, name, method

    def __call__(self):
        return self

    def insert(self, **kw):
        outcomes = self.service.chunk_outcomes if self.name == "videos" else None
        return FakeRequest(self.service, f"{self.name}.insert", kw, outcomes)

    def set(self, **kw):
        return FakeRequest(self.service, f"{self.name}.set", kw)


class FakeService:
    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.errors: dict[str, Exception] = {}
        self.chunk_outcomes: list[object] = []
        self._n = 0

    def next_id(self) -> str:
        self._n += 1
        return f"vid{self._n}"

    def videos(self):
        return FakeResource(self, "videos", "insert")

    def thumbnails(self):
        return FakeResource(self, "thumbnails", "set")

    def playlistItems(self):
        return FakeResource(self, "playlistItems", "insert")

    def inserted(self, kind="videos.insert"):
        return [kw for k, kw in self.calls if k == kind]


@pytest.fixture(autouse=True)
def isolated_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("MINAVIO_SECRETS_FILE", str(tmp_path / "cfg" / "secrets.env"))
    for k in secrets.KNOWN_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(secrets, "_loaded", False)


@pytest.fixture
def sleeps(monkeypatch):
    calls: list[float] = []
    monkeypatch.setattr(youtube, "_sleep", calls.append)
    return calls


@pytest.fixture
def service(monkeypatch, sleeps):
    svc = FakeService()
    monkeypatch.setattr(youtube, "_build_service", lambda: svc)
    monkeypatch.setattr(
        youtube,
        "_media",
        lambda path, mimetype, *, resumable: SimpleNamespace(
            path=str(path), mimetype=mimetype, resumable=resumable
        ),
    )
    return svc


@pytest.fixture
def profile():
    return load_profile("minivitch")


@pytest.fixture
def job(tmp_path):
    """work/job1 (copy, chapitres, miniatures) + out/minivitch (youtube + 2 shorts)."""
    work = tmp_path / "work" / "job1"
    out = tmp_path / "out" / "minivitch"
    work.mkdir(parents=True)
    out.mkdir(parents=True)
    (work / "copy.json").write_text(
        json.dumps(
            {
                "title": "Clutch impossible sur Apex",
                "hook": "1v3",
                "description": "Un clutch fou. On y croit à fond !",
                "tags": ["apex", "clutch"],
                "popups": [],
            }
        )
    )
    (work / "chapters.txt").write_text("0:00 Intro\n0:30 Le clutch\n1:10 Fin")
    for name in ("youtube", "shorts", "short1", "short2"):  # shorts = compilation 9:16
        (out / f"job1_{name}.mp4").write_bytes(b"video")
    thumbs = tmp_path / "out" / "minivitch"
    Image.new("RGB", (1280, 720), "red").save(thumbs / "job1_thumb1_youtube.png")
    Image.new("RGB", (1080, 1920), "blue").save(thumbs / "job1_thumb1_short.png")
    (work / "thumbnails.json").write_text(
        json.dumps(
            [
                {"index": 1, "format": "short", "file": str(thumbs / "job1_thumb1_short.png")},
                {"index": 1, "format": "youtube", "file": str(thumbs / "job1_thumb1_youtube.png")},
            ]
        )
    )
    return SimpleNamespace(root=tmp_path, work=work, out=out, work_root=tmp_path / "work")


def noise_png(path, size=(1920, 1080)):
    Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3)).save(path)

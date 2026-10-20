from __future__ import annotations

import json
from pathlib import Path

import httpx

from minavio.core.models import Chapter, Clip, Highlight, Timeline, TranscriptSegment
from minavio.errors import LLMUnavailableError
from minavio.llm.chapters import format_chapters, generate_chapters
from minavio.llm.client import LocalLLM
from tests.llm.test_llm import mock_llm, profile, reply

SRC = Path("a.mp4")


def spaced(durations: list[float], labels: list[str | None], cold_open: int = 0) -> Timeline:
    """Un clip par durée, écartés de 100 s en source : un moment par clip."""
    clips, t = [], 0.0
    for d, lbl in zip(durations, labels, strict=True):
        clips.append(Clip(source=SRC, start=t, end=t + d, highlight_label=lbl))
        t += d + 100
    return Timeline(profile_id="x", fps=30, clips=clips, cold_open_clips=cold_open)


def items(titles: list[str], first_id: int = 0) -> httpx.Response:
    return reply(
        json.dumps({"items": [{"id": first_id + i, "title": t} for i, t in enumerate(titles)]})
    )


def test_fallback_without_llm_numbers_repeats():
    t = spaced([20, 20, 20, 20], ["kill", "kill", "laugh", "double_kill"])
    chapters = generate_chapters(t, [], profile(enabled=False))
    assert [c.start for c in chapters] == [0, 20, 40, 60]
    assert [c.title for c in chapters] == ["Kill", "Kill (2)", "Gros rire", "Double kill"]


def test_unavailable_llm_falls_back():
    def handler(req):
        raise httpx.ConnectError("down")

    t = spaced([20, 20, 20], ["kill", None, "laugh"])
    chapters = generate_chapters(t, [], profile(), mock_llm(handler))
    assert [c.title for c in chapters] == ["Kill", "Moment chaud", "Gros rire"]


def test_llm_error_type_is_handled():
    class Down(LocalLLM):
        def chat_json(self, *a, **k):
            raise LLMUnavailableError("down")

    t = spaced([20, 20, 20], ["kill", "kill", "kill"])
    chapters = generate_chapters(t, [], profile(), Down("http://x/v1", "m"))
    assert [c.title for c in chapters] == ["Kill", "Kill (2)", "Kill (3)"]


def test_llm_titles_cleaned_and_deduped():
    t = spaced([20, 20, 20], ["clutch", "clutch", "laugh"])
    raw = ["Chapitre 1 : Clutch 1v3 \U0001f525", "Clutch 1v3", "Un très long titre qui dépasse"]
    chapters = generate_chapters(t, [], profile(), mock_llm(lambda r: items(raw)))
    assert [c.title for c in chapters] == ["Clutch 1v3", "Clutch 1v3 (2)", "Un très long titre qui"]


def test_llm_context_uses_overlapping_highlight_and_captions():
    seen = {}

    def handler(req):
        body = json.loads(req.content)
        seen["system"] = body["messages"][0]["content"]
        seen["user"] = body["messages"][-1]["content"]
        return items(["A", "B", "C"])

    t = spaced([20, 20, 20], ["a", "b", "c"])
    t.captions.append(TranscriptSegment(start=21, end=25, text="oh le gros clutch"))
    hs = [
        Highlight(
            source=SRC, start=120, end=140, score=0.9, label="a", detector="d", reason="triple frag"
        ),
        Highlight(
            source=Path("other.mp4"),
            start=0,
            end=20,
            score=1,
            label="z",
            detector="d",
            reason="mauvaise source",
        ),
    ]
    generate_chapters(t, hs, profile(), mock_llm(handler))
    assert "titres de chapitres" in seen["system"]
    assert "gros clutch" in seen["user"]
    assert "triple frag" in seen["user"]
    assert "mauvaise source" not in seen["user"]


def test_invalid_then_valid_response():
    calls = []

    def handler(req):
        calls.append(1)
        return reply("pas du json") if len(calls) == 1 else items(["Titre 0", "Titre 1", "Titre 2"])

    t = spaced([20, 20, 20], ["kill", "kill", "kill"])
    chapters = generate_chapters(t, [], profile(), mock_llm(handler))
    assert len(calls) == 2
    assert [c.title for c in chapters] == ["Titre 0", "Titre 1", "Titre 2"]


def test_invalid_twice_falls_back():
    t = spaced([20, 20, 20], ["kill", "laugh", "kill"])
    chapters = generate_chapters(t, [], profile(), mock_llm(lambda r: reply("nope")))
    assert [c.title for c in chapters] == ["Kill", "Gros rire", "Kill (2)"]


def test_partial_response_fills_missing_with_fallback():
    def handler(req):
        return reply(
            json.dumps({"items": [{"id": 0, "title": "Premier"}, {"id": 99, "title": "Fantôme"}]})
        )

    t = spaced([20, 20, 20], ["kill", "laugh", "fail"])
    chapters = generate_chapters(t, [], profile(), mock_llm(handler))
    assert [c.title for c in chapters] == ["Premier", "Gros rire", "Fail"]


def test_fewer_than_three_returns_empty(caplog):
    t = spaced([20, 20], ["kill", "kill"])
    with caplog.at_level("INFO"):
        assert generate_chapters(t, [], profile(enabled=False)) == []
    assert "Chapitres ignorés" in caplog.text


def test_short_moments_merged_keep_strongest_title():
    # 5 s (kill, faible) + 20 s (laugh, fort) fusionnés, puis deux chapitres de 20 s.
    t = spaced([5, 20, 20, 20], ["kill", "laugh", "fail", "clutch"])
    hs = [
        Highlight(source=SRC, start=0, end=5, score=0.2, label="kill", detector="d"),
        Highlight(source=SRC, start=105, end=125, score=0.9, label="laugh", detector="d"),
    ]
    chapters = generate_chapters(t, hs, profile(enabled=False))
    assert [(c.start, c.title) for c in chapters] == [
        (0, "Gros rire"),
        (25, "Fail"),
        (45, "Clutch"),
    ]


def test_short_last_moment_merged_into_previous():
    t = spaced([20, 20, 20, 4], ["kill", "laugh", "fail", "clutch"])
    chapters = generate_chapters(t, [], profile(enabled=False))
    assert [c.start for c in chapters] == [0, 20, 40]


def test_first_chapter_starts_at_zero():
    t = spaced([20, 20, 20], ["a", "b", "c"])
    assert generate_chapters(t, [], profile(enabled=False))[0].start == 0


def test_cold_open_becomes_intro():
    t = spaced([12, 20, 20, 20], ["clutch", "kill", "laugh", "fail"], cold_open=1)
    seen = {}

    def handler(req):
        seen["user"] = json.loads(req.content)["messages"][-1]["content"]
        return items(["T1", "T2", "T3"], first_id=1)

    chapters = generate_chapters(t, [], profile(), mock_llm(handler))
    assert chapters[0] == Chapter(start=0, title="Intro")
    assert [c.start for c in chapters] == [0, 12, 32, 52]
    assert [c.title for c in chapters][1:] == ["T1", "T2", "T3"]
    assert '"id": 0' not in seen["user"]  # l'Intro n'est pas soumis au LLM


def test_short_cold_open_merged_stays_intro():
    t = spaced([6, 20, 20, 20], ["clutch", "kill", "laugh", "fail"], cold_open=1)
    chapters = generate_chapters(t, [], profile(enabled=False))
    assert chapters[0] == Chapter(start=0, title="Intro")
    assert [c.start for c in chapters] == [0, 26, 46]


def test_llm_not_built_when_disabled(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("LLM ne doit pas être construit")

    monkeypatch.setattr(LocalLLM, "from_settings", boom)
    generate_chapters(spaced([20, 20, 20], ["a", "b", "c"]), [], profile(enabled=False))


def test_format_chapters_with_hours():
    chapters = [
        Chapter(start=0, title="Intro"),
        Chapter(start=65.9, title="Kill"),
        Chapter(start=3599, title="Fin"),
        Chapter(start=3600, title="Heure"),
        Chapter(start=3725, title="Après"),
    ]
    assert format_chapters(chapters).splitlines() == [
        "0:00 Intro",
        "1:05 Kill",
        "59:59 Fin",
        "1:00:00 Heure",
        "1:02:05 Après",
    ]

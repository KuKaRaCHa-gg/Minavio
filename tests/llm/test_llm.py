from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from minavio.config.schema import ChannelProfile
from minavio.core.models import Highlight, TranscriptSegment
from minavio.errors import LLMUnavailableError
from minavio.llm.client import LocalLLM
from minavio.llm.copywriting import VideoCopy, generate_copy
from minavio.llm.prompts import load_prompt
from minavio.llm.rank import _RankResponse, rank_highlights

ROOT = Path(__file__).resolve().parents[2]


def profile(**llm_over) -> ChannelProfile:
    data = yaml.safe_load((ROOT / "configs/channels/minivitch.yaml").read_text())
    data["llm"].update(llm_over)
    return ChannelProfile.model_validate(data)


def mock_llm(handler) -> LocalLLM:
    return LocalLLM("http://llm.test/v1", "qwen", transport=httpx.MockTransport(handler))


def reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def hl(start: float, end: float, score: float = 0.5, label: str = "kill") -> Highlight:
    return Highlight(
        source=Path("a.mp4"), start=start, end=end, score=score, label=label, detector="ui"
    )


SEGS = [TranscriptSegment(start=i * 10.0, end=i * 10.0 + 9, text=f"phrase {i}") for i in range(10)]


def test_chat_json_clean_and_think():
    def handler(req):
        body = json.loads(req.content)
        assert body["response_format"] == {"type": "json_object"}
        return reply('<think>hmm {"x": 1}</think>{"items": [{"id": 0, "score": 0.9}]}')

    out = mock_llm(handler).chat_json([{"role": "user", "content": "x"}], _RankResponse)
    assert out.items[0].score == 0.9


def test_chat_json_in_text_and_fence():
    llm = mock_llm(lambda r: reply('Voici :\n```json\n{"items": []}\n```\nBonne journée'))
    assert llm.chat_json([], _RankResponse).items == []
    llm = mock_llm(lambda r: reply('Résultat {"items": [{"id": 1, "score": 0.2}]} fin'))
    assert llm.chat_json([], _RankResponse).items[0].id == 1


def test_chat_json_falls_back_without_response_format():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, text="unsupported")
        return reply('{"items": []}')

    llm = mock_llm(handler)
    llm.chat_json([], _RankResponse)
    llm.chat_json([], _RankResponse)
    assert seen == [True, False, False]


def test_chat_json_retry_with_error_message():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["messages"])
        return reply("n'importe quoi" if len(calls) == 1 else '{"items": []}')

    mock_llm(handler).chat_json([{"role": "user", "content": "x"}], _RankResponse)
    assert len(calls) == 2
    assert "invalide" in calls[1][-1]["content"]
    assert calls[1][-2]["content"] == "n'importe quoi"


def test_chat_json_fails_after_retry():
    with pytest.raises(ValueError):
        mock_llm(lambda r: reply("nope")).chat_json([], _RankResponse)


def test_server_down_raises_unavailable():
    def handler(req):
        raise httpx.ConnectError("refused")

    llm = mock_llm(handler)
    with pytest.raises(LLMUnavailableError, match=r"llm\.test"):
        llm.chat([])
    assert llm.ping() is False


def test_ping_ok():
    assert mock_llm(lambda r: httpx.Response(200, json={"data": []})).ping() is True


def test_load_prompt():
    for name in ("gaming", "tech_facts", "stream_bestof"):
        assert "JSON" in load_prompt(name, "rank")
        assert "hook" in load_prompt(name, "copy")
        assert load_prompt(name)


def test_rank_scores_and_truncates():
    hs = [hl(10, 15, 0.4), hl(30, 35, 0.8), hl(50, 55, 0.6)]
    items = [{"id": 0, "score": 1.0, "label": "clutch", "reason": "top"}]
    items += [{"id": 1, "score": 0.0}, {"id": 2, "score": 0.2}]
    llm = mock_llm(lambda r: reply(json.dumps({"items": items})))
    out = rank_highlights(hs, SEGS, profile(max_highlights=2), llm)
    assert len(out) == 2
    assert out[0].start == 10 and out[0].label == "clutch"
    assert out[0].score == pytest.approx(0.7)
    assert out[1].score == pytest.approx(0.4)  # id 1 : 0.5*0.8 + 0


def test_rank_bounds_outside_window_rejected_inside_accepted():
    hs = [hl(20, 25), hl(60, 65)]
    items = [
        {"id": 0, "score": 0.5, "start": 10.0, "end": 40.0},  # hors fenêtre [15, 30]
        {"id": 1, "score": 0.5, "start": 58.0, "end": 66.0},  # dans [55, 70]
    ]
    llm = mock_llm(lambda r: reply(json.dumps({"items": items})))
    out = {h.start: h for h in rank_highlights(hs, SEGS, profile(), llm)}
    assert (20, 25) in {(h.start, h.end) for h in out.values()}
    assert (58.0, 66.0) in {(h.start, h.end) for h in out.values()}


def test_rank_batching(monkeypatch):
    monkeypatch.setattr("minavio.llm.rank.TOKEN_BUDGET", 1000)
    long_segs = [TranscriptSegment(start=0, end=500, text="x" * 1200)]
    hs = [hl(10 + i, 15 + i) for i in range(6)]
    prompts = []

    def handler(req):
        user = json.loads(req.content)["messages"][-1]["content"]
        prompts.append(user)
        ids = [json.loads(line)["id"] for line in user.splitlines()[1:]]
        return reply(json.dumps({"items": [{"id": i, "score": 0.5} for i in ids]}))

    out = rank_highlights(hs, long_segs, profile(max_highlights=10), mock_llm(handler))
    assert len(prompts) > 1
    assert len(out) == 6


def test_rank_fallback_when_server_down():
    def handler(req):
        raise httpx.ConnectError("down")

    hs = [hl(0, 5, 0.2), hl(10, 15, 0.9), hl(20, 25, 0.5)]
    out = rank_highlights(hs, SEGS, profile(max_highlights=2), mock_llm(handler))
    assert [h.score for h in out] == [0.9, 0.5]


def test_rank_disabled_does_not_call_llm():
    def handler(req):
        raise AssertionError("ne doit pas être appelé")

    hs = [hl(0, 5, 0.2), hl(10, 15, 0.9)]
    out = rank_highlights(hs, SEGS, profile(enabled=False), mock_llm(handler))
    assert out[0].score == 0.9


def test_copy_from_llm():
    payload = {
        "title": "Clutch fou",
        "hook": "un deux trois quatre cinq six sept huit neuf dix",
        "description": "d",
        "tags": ["#apex", "apex", "clutch"],
        "popups": [{"at": 1.0, "text": "+1 KILL"}, {"at": 999, "text": "TROP LOIN"}],
    }
    out = generate_copy(
        [hl(0, 5), hl(10, 15)], SEGS, profile(), mock_llm(lambda r: reply(json.dumps(payload)))
    )
    assert isinstance(out, VideoCopy)
    assert len(out.hook.split()) == 8
    assert out.tags == ["apex", "clutch"]
    assert [p.text for p in out.popups] == ["+1 KILL"]


def test_copy_fallback_when_down():
    def handler(req):
        raise httpx.ConnectError("down")

    out = generate_copy([hl(0, 5), hl(10, 14, label="clutch")], SEGS, profile(), mock_llm(handler))
    assert out.title and out.hook
    assert [p.at for p in out.popups] == [0.0, 5.0]
    assert out.popups[0].text == "+1 KILL"

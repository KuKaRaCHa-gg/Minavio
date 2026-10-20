"""vision_scan : confidentialité des fournisseurs, échantillonnage, budget, seuil, mapping JSON."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from minavio.config import load_profile
from minavio.config.schema import ChannelProfile, LlmProvider
from minavio.core.detect import DETECTORS, DetectorUnavailable, run_detectors
from minavio.core.models import Highlight
from minavio.errors import LLMUnavailableError
from minavio.llm import vision_scan as vs
from minavio.llm.client import BaseLLM, LocalLLM
from minavio.llm.prompts import load_prompt
from minavio.llm.providers import LLMChain

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg requis")


class FakeVLM(BaseLLM):
    def __init__(self, replies=None, vision="qwen3-vl:8b", fail=False):
        self.name = "fake"
        self.vision_model = vision
        self.replies = replies if replies is not None else []
        self.default = '{"excitement": 8, "events": ["kill"], "kills_visible": 2, "reason": "ok"}'
        self.calls: list[list[Path]] = []
        self.fail = fail

    def chat(self, messages, images=None):
        if self.fail:
            raise LLMUnavailableError("down")
        self.calls.append(list(images or []))
        i = len(self.calls) - 1
        return self.replies[i] if i < len(self.replies) else self.default

    def ping(self):
        return True


@pytest.fixture(autouse=True)
def _clear_cache():
    vs._MEM_CACHE.clear()
    yield
    vs._MEM_CACHE.clear()


@pytest.fixture(scope="module")
def video(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("vscan") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
         "testsrc2=s=1280x720:r=15:d=60", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", str(out)],
        check=True,
    )  # fmt: skip
    return out


def _profile(providers=None, prompt="gaming") -> ChannelProfile:
    p = load_profile("minivitch")
    p.llm.prompt = prompt
    p.llm.providers = providers or []
    return p


def _chain(llm) -> LLMChain:
    return LLMChain([llm])


# ---------------------------------------------------------------- fournisseurs


def test_default_uses_only_local_providers():
    prof = _profile(
        [
            LlmProvider(kind="anthropic", vision_model="claude-x"),
            LlmProvider(kind="local"),
            LlmProvider(kind="google", vision_model="gem-v"),
        ]
    )
    chain = vs.select_vision_providers(prof)
    assert len(chain.providers) == 1
    assert isinstance(chain.providers[0], LocalLLM)
    assert chain.vision_model == "qwen3-vl:8b"


def test_cloud_needs_allow_cloud_and_explicit_vision_model(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    prof = _profile(
        [
            LlmProvider(kind="google"),  # sans vision_model : jamais
            LlmProvider(kind="google", vision_model="gem-v", model="gem"),
        ]
    )
    assert vs.select_vision_providers(prof, allow_cloud=False).providers == []
    chain = vs.select_vision_providers(prof, allow_cloud=True)
    assert [p.vision_model for p in chain.providers] == ["gem-v"]


def test_implicit_local_provider_when_no_providers():
    assert len(vs.select_vision_providers(_profile()).providers) == 1


def test_no_vision_provider_is_detector_unavailable(video, monkeypatch):
    prof = _profile([LlmProvider(kind="anthropic", vision_model="c")])
    with pytest.raises(DetectorUnavailable):
        DETECTORS["vision_scan"]([video], {}, prof)
    prof.llm.enabled = False
    with pytest.raises(LLMUnavailableError):
        vs.run_vision_scan([video], {}, prof)


def test_server_down_is_detector_unavailable_and_skipped(video):
    prof = _profile([LlmProvider(kind="local", base_url="http://127.0.0.1:9/v1")])
    with pytest.raises(DetectorUnavailable):
        DETECTORS["vision_scan"]([video], {"max_calls": 1}, prof)
    prof.detection = [d for d in prof.detection][:0]
    assert run_detectors([video], prof) == []


def test_detector_registered_via_package():
    assert "vision_scan" in DETECTORS


# ---------------------------------------------------------------- échantillonnage / budget


def test_plan_full_every_3s():
    assert vs.plan_times(30, "full", {"max_calls": 100}) == [
        pytest.approx(1.5 + 3 * i) for i in range(10)
    ]


def test_plan_candidates_includes_regular_points_and_dedupes():
    cands = [(10.0, 0.9), (11.0, 0.8), (40.0, 0.5)]  # 11.0 trop proche de 10.0
    times = vs.plan_times(60, "candidates", {"max_calls": 60}, cands)
    assert 10.0 in times and 40.0 in times and 11.0 not in times
    assert {10.0, 30.0, 50.0} <= set(times)  # points réguliers toutes les 20 s (milieu de pas)


def test_candidates_fewer_calls_than_full():
    full = vs.plan_times(600, "full", {"max_calls": 10_000})
    cand = vs.plan_times(600, "candidates", {"max_calls": 10_000}, [(100.0, 0.9)])
    assert len(cand) < len(full) / 3


def test_budget_caps_calls_and_keeps_regular_coverage():
    cands = [(float(t), 1 - t / 1000) for t in range(5, 600, 6)]
    times = vs.plan_times(600, "candidates", {"max_calls": 12}, cands)
    assert len(times) == 12
    assert any(abs(t % 20 - 10) < 1e-6 for t in times)  # au moins un point régulier


def test_max_calls_limits_vlm_calls(video):
    llm = FakeVLM()
    vs.scan_source(video, _profile(), {"mode": "full", "max_calls": 5}, _chain(llm))
    assert len(llm.calls) == 5


def test_candidate_detectors_missing_or_failing_are_ignored(video, monkeypatch):
    def boom(sources, params, profile):
        raise RuntimeError("x")

    def fake_peak(sources, params, profile):
        return [
            Highlight(
                source=sources[0], start=20, end=24, score=0.9, label="l", detector="audio_peak"
            )
        ]

    monkeypatch.setitem(DETECTORS, "scene_change", boom)
    monkeypatch.setitem(DETECTORS, "audio_peak", fake_peak)
    monkeypatch.delitem(DETECTORS, "motion_intensity", raising=False)
    pts = vs.candidate_points(video, 60, {}, _profile())
    assert pts == [(22.0, 0.9)]


# ---------------------------------------------------------------- extraction d'images


def test_three_frames_per_window_real_video(video, tmp_path):
    paths = vs.extract_frames(video, 30.0, 60.0, tmp_path, "w")
    assert len(paths) == 3 and all(p.stat().st_size > 0 for p in paths)
    from PIL import Image

    for p in paths:
        with Image.open(p) as im:
            assert im.width == 448
    # bord de vidéo : jamais d'échec
    assert len(vs.extract_frames(video, 0.2, 60.0, tmp_path, "a")) == 3
    assert len(vs.extract_frames(video, 59.9, 60.0, tmp_path, "b")) == 3


def test_one_call_with_three_images(video):
    llm = FakeVLM()
    vs.scan_source(video, _profile(), {"mode": "full", "max_calls": 2}, _chain(llm))
    assert [len(c) for c in llm.calls] == [3, 3]


# ---------------------------------------------------------------- seuil / mapping / invalide


def test_mapping_json_to_highlight(video):
    llm = FakeVLM(
        ['{"excitement": 9, "events": ["none", "clutch"], "kills_visible": 3, "reason": "1v3"}']
    )
    out = vs.scan_source(video, _profile(), {"mode": "full", "max_calls": 1}, _chain(llm))
    (h,) = out
    assert h.detector == "vision_scan" and h.label == "clutch"
    assert h.score == pytest.approx(0.9) and h.reason == "1v3"
    peak = h.signals["peak_t"]
    assert h.signals["excitement"] == 9 and h.signals["kills"] == 3
    assert h.start == pytest.approx(max(0, peak - 3.0)) and h.end == pytest.approx(peak + 1.5)
    assert h.source == video


def test_label_hype_when_no_event_and_window_clamped(video):
    llm = FakeVLM(['{"excitement": 7, "events": ["none"], "kills_visible": 0, "reason": ""}'])
    prm = {"mode": "full", "max_calls": 1, "every_s": 3, "pre_s": 10, "post_s": 99}
    (h,) = vs.scan_source(video, _profile(), prm, _chain(llm))
    assert h.label == "hype" and h.start == 0.0 and h.end == pytest.approx(60.0, abs=0.5)
    assert h.reason is None


def test_min_excitement_threshold(video):
    replies = [
        f'{{"excitement": {x}, "events": ["kill"], "kills_visible": 1, "reason": "r"}}'
        for x in (5, 6, 7.5, 2)
    ]
    llm = FakeVLM(replies)
    out = vs.scan_source(video, _profile(), {"mode": "full", "max_calls": 4}, _chain(llm))
    assert sorted(h.signals["excitement"] for h in out) == [6, 7.5]
    llm2 = FakeVLM(replies)
    vs._MEM_CACHE.clear()
    out2 = vs.scan_source(
        video, _profile(), {"mode": "full", "max_calls": 4, "min_excitement": 8}, _chain(llm2)
    )
    assert out2 == []


def test_invalid_json_window_ignored(video):
    llm = FakeVLM(["pas du json", "toujours pas"])  # 1re fenêtre : essai + réparation, échec
    out = vs.scan_source(video, _profile(), {"mode": "full", "max_calls": 2}, _chain(llm))
    assert len(out) == 1  # la 2e fenêtre (réponse par défaut) est conservée


def test_unknown_events_filtered_and_excitement_clamped():
    v = vs.VisionVerdict.model_validate(
        {"excitement": 14, "events": ["KILL", "bogus"], "kills_visible": 2.0, "reason": "x"}
    )
    assert v.excitement == 10 and v.events == ["kill"] and v.kills_visible == 2


def test_memory_cache_avoids_second_call(video):
    llm = FakeVLM()
    prm = {"mode": "full", "max_calls": 3}
    a = vs.scan_source(video, _profile(), prm, _chain(llm))
    n = len(llm.calls)
    b = vs.scan_source(video, _profile(), prm, _chain(llm))
    assert len(llm.calls) == n and [h.model_dump() for h in a] == [h.model_dump() for h in b]


def test_disk_cache(video, tmp_path):
    prm = {"mode": "full", "max_calls": 2, "cache_dir": str(tmp_path / "cache")}
    llm = FakeVLM()
    vs.scan_source(video, _profile(), prm, _chain(llm))
    assert list((tmp_path / "cache").glob("vision_scan_*.json"))
    vs._MEM_CACHE.clear()
    llm2 = FakeVLM()
    vs.scan_source(video, _profile(), prm, _chain(llm2))
    assert llm2.calls == []


def test_vlm_down_midway_returns_partial(video):
    class Flaky(FakeVLM):
        def chat(self, messages, images=None):
            if len(self.calls) >= 1:
                raise LLMUnavailableError("coupé")
            return super().chat(messages, images)

    llm = Flaky()
    out = vs.scan_source(video, _profile(), {"mode": "full", "max_calls": 4}, _chain(llm))
    assert len(out) == 1


# ---------------------------------------------------------------- bout en bout (MockTransport)


def test_end_to_end_mock_transport_sends_three_images(video, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        content = '{"excitement": 8, "events": ["win"], "kills_visible": 1, "reason": "CHAMPION"}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    llm = LocalLLM(
        "http://x/v1", "m", vision_model="qwen3-vl:8b", transport=httpx.MockTransport(handler)
    )
    out = vs.run_vision_scan([video], {"mode": "full", "max_calls": 2}, _profile(), LLMChain([llm]))
    assert len(out) == 2 and seen[0]["model"] == "qwen3-vl:8b"
    parts = seen[0]["messages"][-1]["content"]
    assert sum(1 for p in parts if p["type"] == "image_url") == 3


# ---------------------------------------------------------------- prompts


@pytest.mark.parametrize("name", ["gaming", "tech_facts", "stream_bestof"])
def test_prompt_sections_exist(name):
    text = load_prompt(name, "vision_scan")
    assert "excitement" in text and "kills_visible" in text


def test_prompt_themes():
    assert "CHAMPION" in load_prompt("gaming", "vision_scan")
    assert "waouh" in load_prompt("tech_facts", "vision_scan")
    assert "faciale" in load_prompt("stream_bestof", "vision_scan")

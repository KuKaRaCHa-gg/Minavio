from __future__ import annotations

import base64
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
import yaml
from pydantic import BaseModel

from minavio.config.schema import ChannelProfile, LlmProvider, LlmSettings
from minavio.core.models import ThumbnailCandidate
from minavio.errors import LLMUnavailableError
from minavio.llm import providers as prov
from minavio.llm.anthropic_client import AnthropicLLM
from minavio.llm.client import BaseLLM, LocalLLM
from minavio.llm.prompts import load_prompt
from minavio.llm.providers import LLMChain, build_chain, build_provider
from minavio.llm.rank import TOKEN_BUDGET
from minavio.llm.vision import pick_thumbnails

ROOT = Path(__file__).resolve().parents[2]
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class Out(BaseModel):
    ok: bool


class Fake(BaseLLM):
    def __init__(self, name, reply='{"ok": true}', fail=False, vision=None):
        self.name = name
        self.reply = reply
        self.fail = fail
        self.vision_model = vision
        self.calls = 0

    def chat(self, messages, images=None):
        self.calls += 1
        if self.fail:
            raise LLMUnavailableError(f"{self.name} down")
        return self.reply

    def ping(self):
        return not self.fail


def msgs():
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]


# ---------------------------------------------------------------- chaîne


def test_chain_order_fallback_and_always_retries_first():
    a, b = Fake("a", fail=True), Fake("b")
    chain = LLMChain([a, b])
    assert chain.chat(msgs()) == '{"ok": true}'
    assert (a.calls, b.calls) == (1, 1)
    chain.chat(msgs())  # l'ordre configuré est toujours respecté : a est retenté d'abord
    assert (a.calls, b.calls) == (2, 2)
    assert chain.last_used is b
    assert chain.chat_json(msgs(), Out).ok is True


def test_chain_all_down_recaps_every_failure():
    chain = LLMChain([Fake("a", fail=True), Fake("b", fail=True)])
    with pytest.raises(LLMUnavailableError) as ei:
        chain.chat(msgs())
    assert "a : " in str(ei.value) and "b : " in str(ei.value)


def test_chain_single_provider_reraises_original():
    with pytest.raises(LLMUnavailableError, match="a down"):
        LLMChain([Fake("a", fail=True)]).chat(msgs())


def test_chain_vision_skips_providers_without_vision_model(tmp_path):
    img = tmp_path / "i.png"
    img.write_bytes(PNG)
    a, b = Fake("a", vision=None), Fake("b", vision="v")
    chain = LLMChain([a, b])
    chain.chat(msgs(), images=[img])
    assert (a.calls, b.calls) == (0, 1)
    with pytest.raises(LLMUnavailableError, match="vision"):
        LLMChain([a]).chat(msgs(), images=[img])


def test_missing_key_provider_is_skipped(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    prov._warned.clear()
    settings = LlmSettings(
        prompt="gaming",
        providers=[LlmProvider(kind="anthropic"), LlmProvider(kind="google"), LlmProvider()],
    )
    chain = build_chain(settings)
    assert [type(p) for p in chain.providers] == [LocalLLM]
    assert chain.providers[0].name.startswith("Qwen local")


def test_defaults(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    s = LlmSettings(prompt="gaming")
    g = build_provider(LlmProvider(kind="google"), s)
    assert g.model == "gemini-2.5-flash"
    assert g.vision_model is None  # cloud : vision seulement si demandée explicitement
    assert g.base_url.startswith("https://generativelanguage.googleapis.com")
    gh = build_provider(LlmProvider(kind="github"), s)
    assert gh.model == "openai/gpt-4.1" and gh.base_url == "https://models.github.ai/inference"
    assert gh.context_budget_tokens == 6000
    assert g.context_budget_tokens == 8000
    c = build_provider(LlmProvider(kind="anthropic"), s)
    assert c.name == "Claude claude-opus-5-5" and c.vision_model is None
    cv = build_provider(LlmProvider(kind="anthropic", vision_model="claude-opus-5-5"), s)
    assert cv.vision_model == "claude-opus-5-5"
    local = build_provider(LlmProvider(), s)
    assert local.vision_model == "qwen3-vl:8b" and local.model == s.model


def test_implicit_local_chain_and_budget():
    chain = LocalLLM.from_settings(LlmSettings(prompt="gaming"))
    assert isinstance(chain, LLMChain) and len(chain.providers) == 1
    assert chain.context_budget_tokens == TOKEN_BUDGET
    gh = LLMChain([Fake("x"), _gh()])
    assert gh.context_budget_tokens == 6000


def _gh():
    return LocalLLM("http://x", "m", context_budget_tokens=6000)


# ---------------------------------------------------------------- OpenAI-compatible


def test_openai_client_images_auth_and_429(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "yo"}}]})

    llm = LocalLLM(
        "http://x/v1",
        "txt",
        transport=httpx.MockTransport(handler),
        api_key="k",
        vision_model="vis",
    )
    assert llm.chat(msgs(), images=[img]) == "yo"
    assert seen["auth"] == "Bearer k"
    body = seen["body"]
    assert body["model"] == "vis"
    content = body["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "hi"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    llm.chat(msgs())
    assert seen["body"]["model"] == "txt"

    limited = LocalLLM(
        "http://x/v1",
        "m",
        transport=httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")),
    )
    with pytest.raises(LLMUnavailableError, match="429"):
        limited.chat(msgs())
    # 429 sur le 1er fournisseur -> le suivant de la chaîne répond
    assert LLMChain([limited, Fake("b")]).chat(msgs()) == '{"ok": true}'


# ---------------------------------------------------------------- Anthropic (faux SDK)


def fake_sdk(response=None, raises=None):
    mod = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class APIConnectionError(APIError):
        pass

    class APIStatusError(APIError):
        status_code = 500

    class AuthenticationError(APIStatusError):
        status_code = 401

    class RateLimitError(APIStatusError):
        status_code = 429

    mod.APIConnectionError = APIConnectionError
    mod.APIStatusError = APIStatusError
    mod.AuthenticationError = AuthenticationError
    mod.RateLimitError = RateLimitError
    mod.calls = []

    class Messages:
        def create(self, **kw):
            mod.calls.append(kw)
            if raises:
                raise getattr(mod, raises)("boom")
            return response

    class Client:
        def __init__(self, api_key=None):
            mod.api_key = api_key
            self.beta = types.SimpleNamespace(messages=Messages())
            self.models = types.SimpleNamespace(retrieve=lambda m: {"id": m})

    mod.Anthropic = Client
    return mod


def resp(blocks, stop="end_turn", category=None):
    return types.SimpleNamespace(
        stop_reason=stop,
        stop_details=types.SimpleNamespace(category=category),
        content=[types.SimpleNamespace(type=t, text=x) for t, x in blocks],
    )


def test_anthropic_request_shape(monkeypatch, tmp_path):
    sdk = fake_sdk(resp([("thinking", "hmm"), ("text", "a"), ("text", "b")]))
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    llm = AnthropicLLM("claude-opus-5-5", "sk", effort="medium")
    assert llm.chat(msgs(), images=[img]) == "ab"
    kw = sdk.calls[0]
    assert sdk.api_key == "sk"
    assert kw["betas"] == ["server-side-fallback-2026-07-01"]
    assert kw["fallbacks"] == "default"
    assert kw["output_config"] == {"effort": "medium"}
    assert kw["max_tokens"] == 16000
    assert "thinking" not in kw and "temperature" not in kw
    assert kw["system"] == "sys"
    content = kw["messages"][0]["content"]
    assert content[0]["type"] == "image"
    assert content[0]["source"]["type"] == "base64"
    assert content[0]["source"]["media_type"] == "image/png"
    assert content[-1] == {"type": "text", "text": "hi"}
    assert AnthropicLLM("m", "k").effort == "low"
    assert llm.ping() is True


def test_anthropic_json(monkeypatch):
    sdk = fake_sdk(resp([("text", '```json\n{"ok": true}\n```')]))
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    assert AnthropicLLM("m", "k").chat_json(msgs(), Out).ok is True


def test_anthropic_refusal(monkeypatch):
    sdk = fake_sdk(resp([("text", "no")], stop="refusal", category="cyber"))
    monkeypatch.setitem(sys.modules, "anthropic", sdk)
    with pytest.raises(LLMUnavailableError, match="cyber"):
        AnthropicLLM("m", "k").chat(msgs())


@pytest.mark.parametrize(
    ("exc", "match"),
    [
        ("AuthenticationError", "invalide"),
        ("RateLimitError", "débit"),
        ("APIStatusError", "HTTP 500"),
        ("APIConnectionError", "injoignable"),
    ],
)
def test_anthropic_errors_converted(monkeypatch, exc, match):
    monkeypatch.setitem(sys.modules, "anthropic", fake_sdk(raises=exc))
    with pytest.raises(LLMUnavailableError, match=match):
        AnthropicLLM("m", "k").chat(msgs())


def test_anthropic_sdk_missing(monkeypatch):
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import -> ImportError
    with pytest.raises(LLMUnavailableError, match="uv sync --extra cloud"):
        AnthropicLLM("m", "k").chat(msgs())
    assert AnthropicLLM("m", "k").ping() is False


# ---------------------------------------------------------------- miniatures


def profile(**thumb) -> ChannelProfile:
    data = yaml.safe_load((ROOT / "configs/channels/minivitch.yaml").read_text())
    data.setdefault("thumbnails", {}).update({"enabled": True, **thumb})
    return ChannelProfile.model_validate(data)


def cands(tmp_path, n=6) -> list[ThumbnailCandidate]:
    out = []
    for i in range(n):
        p = tmp_path / f"c{i}.png"
        p.write_bytes(PNG)
        out.append(
            ThumbnailCandidate(
                source=Path("a.mp4"),
                t=float(i),
                image=p,
                score=round(0.9 - i * 0.1, 2),
                label=["clutch", "kill", "fail"][i % 3],
            )
        )
    return out


def test_thumbnails_no_vision_fallback(tmp_path):
    p = profile(count=3, use_vision_llm=False)
    res = pick_thumbnails(
        cands(tmp_path), p, copy={"hook": "UN SUPER HOOK QUI EST BEAUCOUP TROP LONG"}
    )
    assert [c.layout for c in res] == ["hero", "split", "collage"]
    assert [len(c.images) for c in res] == [1, 2, 3]
    assert res[0].images[0].score == 0.9  # meilleure note d'abord
    ids = [im.t for c in res for im in c.images]
    assert len(ids) == len(set(ids))  # pas de doublon
    assert all(len(c.text.split()) <= p.thumbnails.max_words for c in res)
    assert res[0].text == "UN SUPER HOOK QUI"
    assert res[1].text == _upper(res[1].text)


def _upper(t):
    return t.upper()


def test_thumbnails_fallback_when_llm_down(tmp_path):
    res = pick_thumbnails(cands(tmp_path), profile(count=2), llm=Fake("x", fail=True, vision="v"))
    assert len(res) == 2 and res[0].reason


def test_thumbnails_single_candidate_downgrades_layout(tmp_path):
    res = pick_thumbnails(cands(tmp_path, 1), profile(count=3, use_vision_llm=False))
    assert all(c.layout == "hero" and len(c.images) == 1 for c in res)


def test_thumbnails_with_vision(tmp_path):
    sent = {}

    class Judge(Fake):
        def chat(self, messages, images=None):
            sent["images"] = images
            sent["user"] = messages[-1]["content"]
            return json.dumps(
                {
                    "items": [
                        {"layout": "hero", "images": [2], "text": "squad wiped 😱", "reason": "r1"},
                        {
                            "layout": "split",
                            "images": [0, 99],
                            "text": "1V3 CLUTCH",
                            "reason": "r2",
                        },
                        {"layout": "collage", "images": [2, 3, 4], "text": "", "reason": "r3"},
                    ]
                }
            )

    p = profile(count=3, max_words=3)
    res = pick_thumbnails(cands(tmp_path), p, copy={"hook": "HOOK"}, llm=Judge("j", vision="v"))
    assert len(sent["images"]) == 6
    assert all(Path(i).suffix == ".jpg" for i in sent["images"])  # redimensionnées (Pillow)
    assert res[0].text == "SQUAD WIPED" and res[0].reason == "r1"
    assert res[0].images[0].t == 2.0
    assert [len(c.images) for c in res] == [1, 2, 3]
    assert 99 not in [im.t for c in res for im in c.images]
    flat = [im.t for c in res for im in c.images]
    assert len(flat) == len(set(flat))  # id 2 réutilisé -> remplacé
    assert res[2].text  # texte vide -> repli
    assert "max_words = 3" in sent["user"]


def test_thumbnails_prompt_sections_exist():
    for name in ("gaming", "tech_facts", "stream_bestof"):
        assert "JSON" in load_prompt(name, "thumbnails")


def test_heuristic_texts_are_distinct_and_never_invent_numbers(tmp_path):
    from minavio.config import load_profile
    from minavio.core.models import ThumbnailCandidate
    from minavio.llm.vision import pick_thumbnails

    profile = load_profile("minivitch")
    profile.thumbnails.use_vision_llm = False
    cands = [
        ThumbnailCandidate(
            source=Path("a.mp4"),
            t=i,
            image=tmp_path / f"{i}.png",
            score=0.9 - i / 10,
            label="clutch",
        )
        for i in range(4)
    ]
    choices = pick_thumbnails(cands, profile, copy=None, llm=None)
    texts = [c.text for c in choices]
    assert len(set(texts)) == len(texts)
    assert not any(ch.isdigit() for t in texts for ch in t)


def test_openai_compat_key_only_when_explicit_and_never_over_http(monkeypatch):
    from minavio.errors import ConfigError

    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-openai")
    monkeypatch.setenv("MY_VLLM_KEY", "k")
    s = LlmSettings(prompt="gaming")
    p = build_provider(
        LlmProvider(kind="openai_compat", base_url="http://192.168.1.20:8000/v1", model="m"), s
    )
    assert "authorization" not in p._client.headers  # clé OpenAI jamais envoyée par défaut
    with pytest.raises(ConfigError, match="non chiffrée"):
        build_provider(
            LlmProvider(
                kind="openai_compat",
                base_url="http://192.168.1.20:8000/v1",
                model="m",
                api_key_env="MY_VLLM_KEY",
            ),
            s,
        )
    ok = build_provider(
        LlmProvider(
            kind="openai_compat",
            base_url="http://localhost:8000/v1",
            model="m",
            api_key_env="MY_VLLM_KEY",
        ),
        s,
    )
    assert ok._client.headers["authorization"] == "Bearer k"


def test_vision_json_repair_does_not_resend_images(tmp_path):
    from PIL import Image

    img = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(img)
    seen = []

    class Two(LocalLLM):
        def _chat_maybe_json(self, messages, images):
            seen.append(bool(images))
            return "pas du json" if len(seen) == 1 else '{"ok": true}'

    llm = Two("http://localhost:1/v1", "m", vision_model="v")
    assert llm.chat_json(msgs(), Out, images=[img]).ok is True
    assert seen == [True, False]


def test_pick_thumbnails_no_llm_never_builds_a_client(monkeypatch, tmp_path):
    from minavio.config import load_profile
    from minavio.core.models import ThumbnailCandidate
    from minavio.llm import client as client_mod
    from minavio.llm.vision import pick_thumbnails

    def boom(*a, **k):
        raise AssertionError("aucun client LLM ne doit être construit avec use_llm=False")

    monkeypatch.setattr(client_mod.LocalLLM, "from_settings", classmethod(boom))
    cands = [
        ThumbnailCandidate(source=Path("a.mp4"), t=i, image=tmp_path / f"{i}.png", score=0.5)
        for i in range(3)
    ]
    assert pick_thumbnails(cands, load_profile("minivitch"), use_llm=False)

"""Base de connaissances : index, BM25, filtres, budget, fiches utilisateur, injection."""

from __future__ import annotations

import itertools
import json
import socket
from pathlib import Path

import pytest
import yaml
from PIL import Image

from minavio.config.schema import ChannelProfile
from minavio.core.models import Highlight, ThumbnailCandidate, TranscriptSegment
from minavio.errors import LLMUnavailableError
from minavio.llm import kb
from minavio.llm.chapters import generate_chapters
from minavio.llm.copywriting import generate_copy
from minavio.llm.rank import rank_highlights
from minavio.llm.vision import pick_thumbnails
from tests.llm.test_chapters import items, spaced
from tests.llm.test_llm import mock_llm, reply

ROOT = Path(__file__).resolve().parents[2]
HEADER_START = "Connaissances utiles"


def load_profile(name: str) -> ChannelProfile:
    return ChannelProfile.model_validate(
        yaml.safe_load((ROOT / f"configs/channels/{name}.yaml").read_text())
    )


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Pas de fiches utilisateur du poste, et aucun accès réseau."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("MINAVIO_KNOWLEDGE_DIR", raising=False)
    monkeypatch.delenv("MINAVIO_KB", raising=False)

    def no_network(*a, **k):
        raise AssertionError("appel réseau interdit dans les tests de la base de connaissances")

    monkeypatch.setattr(socket.socket, "connect", no_network)


# ------------------------------------------------------------------ index


def test_index_has_all_cards_and_tags():
    ps = kb.builtin_passages()
    sources = {p.source for p in ps}
    assert {
        "games/apex_legends.md",
        "games/fps_battle_royale.md",
        "channels/minivitch.md",
        "channels/bytetricks.md",
        "channels/thelostchat.md",
        "youtube/titles_thumbnails.md",
        "youtube/retention_shorts.md",
        "youtube/chapters_metadata.md",
        "editing/highlight_criteria.md",
        "libraries.md",
    } <= sources
    by_src = {p.source: p for p in ps}
    assert "game:apex_legends" in by_src["games/apex_legends.md"].tags
    assert "channel:minivitch" in by_src["channels/minivitch.md"].tags
    assert {"topic:youtube", "topic:thumbnails"} <= by_src["youtube/titles_thumbnails.md"].tags


def test_passages_are_short_and_sources_not_indexed():
    for p in kb.builtin_passages():
        assert len(p.text.split()) <= 150, f"{p.id} ({p.title}) dépasse 150 mots"
        assert not p.title.lower().startswith("sources")


def test_tokenize_accents_stopwords_and_plurals():
    assert kb.tokenize("Les ÉQUIPES arrivent") == kb.tokenize("equipe arrive")
    assert "le" not in kb.tokenize("le the of un")
    assert kb.tokenize("Squads") == kb.tokenize("squad")


# ------------------------------------------------------------------ BM25


def test_bm25_third_party_for_natural_french_query():
    p = load_profile("minivitch")
    out = kb.retrieve(
        "une autre équipe arrive pendant le combat", profile=p, task="rank", budget_tokens=400
    )
    first = out.split("### ")[1]
    assert first.startswith("Apex Legends - Third party")


def test_search_ranks_by_relevance():
    res = kb.search("chapitres horodatage 0:00 dix secondes", k=3)
    assert res and res[0][1].source == "youtube/chapters_metadata.md"
    assert all(a[0] >= b[0] for a, b in itertools.pairwise(res))


def test_empty_query_returns_only_core_passages():
    p = load_profile("minivitch")
    out = kb.retrieve("", profile=p, task="rank", budget_tokens=2000)
    assert "Ce que cherche la chaîne" in out
    assert "Third party" not in out


# ------------------------------------------------------------------ filtres


def titles(out: str) -> list[str]:
    return [b.split("\n", 1)[0] for b in out.split("### ")[1:]]


def test_channel_and_game_filters():
    q = "clutch squad wipe killfeed titre miniature streamer fun fact"
    mv = kb.retrieve(q, profile=load_profile("minivitch"), task="rank", budget_tokens=6000)
    bt = kb.retrieve(q, profile=load_profile("bytetricks"), task="rank", budget_tokens=6000)
    tlc = kb.retrieve(q, profile=load_profile("thelostchat"), task="rank", budget_tokens=6000)
    assert "Minivitch -" in mv and "Byte Tricks -" not in mv and "The Lost Chat -" not in mv
    assert "Apex Legends -" in mv
    assert "Byte Tricks -" in bt and "Minivitch -" not in bt
    assert "Apex Legends -" not in bt and "Apex Legends -" not in tlc  # jeu absent du profil
    assert "The Lost Chat -" in tlc


def test_content_type_generic_game_card():
    q = "killfeed ace clutch round"
    assert "FPS et battle royale" in kb.retrieve(
        q, profile=load_profile("minivitch"), task="rank", budget_tokens=3000
    )
    assert "FPS et battle royale" not in kb.retrieve(
        q, profile=load_profile("bytetricks"), task="rank", budget_tokens=3000
    )


def test_variant_profile_uses_channel_family(tmp_path):
    out = kb.retrieve("", profile=load_profile("minivitch_top10"), task="copy", budget_tokens=2000)
    assert "Minivitch -" in out


def test_task_filter():
    p = load_profile("minivitch")
    chapters = kb.retrieve(
        "chapitres horodatage 0:00", profile=p, task="chapters", budget_tokens=2000
    )
    rank = kb.retrieve("chapitres horodatage 0:00", profile=p, task="rank", budget_tokens=2000)
    assert "Règles des chapitres" in chapters
    assert "Règles des chapitres" not in rank
    thumbs = kb.retrieve(
        "miniature texte contraste", profile=p, task="thumbnails", budget_tokens=2000
    )
    assert "Miniature : texte et contraste" in thumbs
    # le catalogue de bibliothèques n'est jamais injecté dans un prompt
    assert "Bibliothèques" not in kb.retrieve(
        "RapidOCR Demucs licence", profile=p, task="rank", budget_tokens=3000
    )


# ------------------------------------------------------------------ budget


@pytest.mark.parametrize("tokens", [150, 400, 1000])
def test_budget_respected(tokens):
    out = kb.retrieve(
        "clutch squad wipe third party killfeed titre",
        profile=load_profile("minivitch"),
        task="rank",
        budget_tokens=tokens,
    )
    assert out.startswith(HEADER_START)
    assert len(out) <= tokens * 4


def test_zero_budget_and_disable_env(monkeypatch):
    p = load_profile("minivitch")
    assert kb.retrieve("clutch", profile=p, task="rank", budget_tokens=0) == ""
    monkeypatch.setenv("MINAVIO_KB", "0")
    assert kb.retrieve("clutch", profile=p, task="rank", budget_tokens=500) == ""


def test_budget_for_is_quarter_of_context_and_capped():
    class L:
        context_budget_tokens = 2000

    assert kb.budget_for(L()) == 500
    L.context_budget_tokens = 100_000
    assert kb.budget_for(L()) == kb.KB_MAX_TOKENS
    assert kb.budget_for(None) == min(kb.KB_MAX_TOKENS, 2000)


# ------------------------------------------------------------------ fiches utilisateur


def test_user_cards_are_indexed_and_take_priority():
    d = Path("configs/knowledge/games")
    d.mkdir(parents=True)
    (d / "apex_legends.md").write_text(
        "# Mes notes Apex\n\n## Third party chez nous\n"
        "Third party : on appelle ça un « sandwich » dans notre communauté, énorme moment.\n",
        encoding="utf-8",
    )
    p = load_profile("minivitch")
    out = kb.retrieve("third party", profile=p, task="rank", budget_tokens=600)
    assert titles(out)[0] == "Mes notes Apex - Third party chez nous"
    assert "Apex Legends - Third party" in out  # la fiche intégrée reste disponible


def test_user_card_in_home_config_and_loose_file():
    home = Path.cwd() / "xdg" / "minavio" / "knowledge"
    home.mkdir(parents=True)
    (home / "maison.md").write_text(
        "# Maison\n\n## Règle zorglub\nToujours vérifier le zorglub avant publication.\n",
        encoding="utf-8",
    )
    out = kb.retrieve("zorglub", profile=load_profile("bytetricks"), task="copy", budget_tokens=300)
    assert "Règle zorglub" in out


def test_user_card_respects_game_filter():
    d = Path("configs/knowledge/games")
    d.mkdir(parents=True)
    (d / "valorant.md").write_text("# Valorant\n\n## Ace\nUn ace élimine cinq joueurs.\n")
    assert "Valorant - Ace" not in kb.retrieve(
        "ace", profile=load_profile("minivitch"), task="rank", budget_tokens=500
    )
    p = load_profile("minivitch").model_copy(update={"games": ["valorant"]})
    assert "Valorant - Ace" in kb.retrieve("ace", profile=p, task="rank", budget_tokens=500)


# ------------------------------------------------------------------ injection dans les prompts


def hl(start, end, label="clutch", reason="une autre équipe arrive pendant le combat"):
    return Highlight(
        source=Path("a.mp4"),
        start=start,
        end=end,
        score=0.6,
        label=label,
        detector="ui",
        reason=reason,
    )


SEGS = [TranscriptSegment(start=i * 10.0, end=i * 10.0 + 9, text=f"phrase {i}") for i in range(6)]


def capture(responder):
    seen: list[dict] = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(body)
        return responder(body)

    return seen, handler


def test_rank_prompt_contains_knowledge():
    seen, handler = capture(
        lambda b: reply(json.dumps({"items": [{"id": 0, "score": 0.9, "label": "clutch"}]}))
    )
    rank_highlights([hl(10, 20)], SEGS, load_profile("minivitch"), mock_llm(handler))
    system = seen[0]["messages"][0]["content"]
    assert HEADER_START in system
    assert "Third party" in system
    assert "monteur en chef" in system  # le prompt d'origine est conservé


def test_rank_without_matching_card_still_works_and_stays_small():
    seen, handler = capture(lambda b: reply(json.dumps({"items": []})))
    llm = mock_llm(handler)
    llm.context_budget_tokens = 2000
    rank_highlights([hl(10, 20)], SEGS, load_profile("minivitch"), llm)
    system = seen[0]["messages"][0]["content"]
    block = system[system.index(HEADER_START) :]
    assert len(block) <= 2000 * kb.KB_FRACTION * 4


def test_copy_prompt_contains_knowledge():
    seen, handler = capture(lambda b: reply(json.dumps({"title": "t", "hook": "h"})))
    generate_copy([hl(0, 5), hl(10, 15)], SEGS, load_profile("minivitch"), mock_llm(handler))
    system = seen[0]["messages"][0]["content"]
    assert HEADER_START in system and "Minivitch -" in system


def test_chapters_prompt_contains_knowledge():
    seen, handler = capture(lambda b: items(["A", "B", "C"]))
    t = spaced([20, 20, 20], ["clutch", "kill", "fail"])
    generate_chapters(t, [], load_profile("minivitch"), mock_llm(handler))
    system = seen[0]["messages"][0]["content"]
    assert HEADER_START in system
    assert "Règles des chapitres" in system or "Titres de chapitres" in system


def test_vision_prompt_contains_knowledge(tmp_path):
    class Spy:
        context_budget_tokens = 8000
        vision_model = "v"

        def __init__(self):
            self.messages = None

        def chat_json(self, messages, model, images=None):
            self.messages = messages
            raise LLMUnavailableError("test")

    cands = []
    for i in range(3):
        img = tmp_path / f"c{i}.png"
        Image.new("RGB", (64, 36), (i * 40, 10, 10)).save(img)
        cands.append(
            ThumbnailCandidate(source=Path("a.mp4"), t=float(i), image=img, score=0.9 - i * 0.1)
        )
    data = yaml.safe_load((ROOT / "configs/channels/minivitch.yaml").read_text())
    data["thumbnails"] = {"enabled": True, "count": 2}
    spy = Spy()
    pick_thumbnails(cands, ChannelProfile.model_validate(data), llm=spy)
    system = spy.messages[0]["content"]
    assert HEADER_START in system
    assert "Miniature" in system


def test_knowledge_for_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("index cassé")

    monkeypatch.setattr(kb, "retrieve", boom)
    assert kb.knowledge_for(load_profile("minivitch"), "rank", "clutch") == ""


def test_knowledge_for_default_budget_uses_llm():
    class L:
        context_budget_tokens = 800  # petit (GitHub Models) -> 200 tokens

    out = kb.knowledge_for(load_profile("minivitch"), "rank", "clutch third party", llm=L())
    assert 0 < len(out) <= 200 * 4

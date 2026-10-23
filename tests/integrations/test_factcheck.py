"""Fact-check : extraction/jugement via LLM factice, Wikipédia/Wikidata simulés, CLI."""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from minavio.errors import LLMUnavailableError, MinavioError
from minavio.integrations import cli_stats, factcheck
from minavio.integrations.cli_stats import stats_app

CLAIM_TRUE = "La Terre tourne autour du Soleil"
CLAIM_FALSE = "Le Soleil tourne autour de la Terre"
CLAIM_NONE = "Byte Tricks a gagné un prix mondial"

ARTICLES = {
    "Terre": "La Terre est la troisième planète du Système solaire ; elle tourne autour du Soleil.",
    "Soleil": "Le Soleil est l'étoile au centre du Système solaire.",
}


class FakeLLM:
    """Renvoie l'extraction fixée, puis un verdict selon l'affirmation jugée."""

    def __init__(self, verdicts: dict[str, str]) -> None:
        self.verdicts = verdicts
        self.judged: list[str] = []

    def chat_json(self, messages, model):
        if model.__name__ == "_Extraction":
            return model.model_validate(
                {
                    "claims": [
                        {"claim": CLAIM_TRUE, "entities": ["Terre"]},
                        {"claim": CLAIM_FALSE, "entities": ["Soleil"]},
                        {"claim": CLAIM_NONE, "entities": ["Byte Tricks"]},
                    ]
                }
            )
        user = json.loads(messages[-1]["content"])
        self.judged.append(user["affirmation"])
        verdict = self.verdicts.get(user["affirmation"], "unverifiable")
        return model.model_validate({"verdict": verdict, "note": "d'après l'extrait"})


class BrokenLLM:
    def chat_json(self, messages, model):
        raise ValueError("pas du JSON")


def _wiki_handler(request: httpx.Request) -> httpx.Response:
    if "wikidata" in request.url.host:
        return httpx.Response(200, json={"search": []})
    params = request.url.params
    if params.get("list") == "search":
        query = params["srsearch"]
        hits = [{"title": query}] if query in ARTICLES else []
        return httpx.Response(200, json={"query": {"search": hits}})
    title = params["titles"]
    return httpx.Response(
        200, json={"query": {"pages": {"1": {"title": title, "extract": ARTICLES[title]}}}}
    )


@pytest.fixture(autouse=True)
def _wiki(monkeypatch):
    monkeypatch.setattr(factcheck, "_TRANSPORT", httpx.MockTransport(_wiki_handler))


def test_check_claims_supported_contradicted_unverifiable():
    llm = FakeLLM({CLAIM_TRUE: "supported", CLAIM_FALSE: "contradicted"})
    claims = factcheck.check_claims("script", llm)

    assert [c.verdict for c in claims] == ["supported", "contradicted", "unverifiable"]
    assert claims[0].url == "https://fr.wikipedia.org/wiki/Terre"
    assert claims[0].source and "Wikipédia" in claims[0].source
    # Pas d'extrait trouvé : le juge n'est pas appelé pour la 3e affirmation.
    assert CLAIM_NONE not in llm.judged
    assert claims[2].source is None and "Aucune source" in claims[2].note


def test_report_is_markdown_with_labels_and_links():
    claims = factcheck.check_claims("script", FakeLLM({CLAIM_TRUE: "supported"}))
    report = factcheck.factcheck_report(claims)
    assert report.startswith("# Fact-check")
    for label in ("Confirmée", "Non vérifiable", "https://fr.wikipedia.org/wiki/Terre"):
        assert label in report


def test_report_empty():
    assert "Aucune affirmation" in factcheck.factcheck_report([])


def test_without_llm_returns_explicit_message():
    with pytest.raises(LLMUnavailableError, match="sans LLM"):
        factcheck.check_claims("script", None)


def test_invalid_extraction_is_actionable():
    with pytest.raises(MinavioError, match="réponse LLM invalide"):
        factcheck.check_claims("script", BrokenLLM())


def test_cli_factcheck_writes_report(monkeypatch, tmp_path):
    script = tmp_path / "script.txt"
    script.write_text(CLAIM_TRUE + ".", encoding="utf-8")
    out = tmp_path / "out" / "factcheck.md"
    llm = FakeLLM({CLAIM_TRUE: "supported"})

    class _Loader:
        @staticmethod
        def from_settings(settings):
            return llm

    monkeypatch.setattr(
        cli_stats, "load_profile", lambda name: SimpleNamespace(llm=SimpleNamespace(enabled=True))
    )
    monkeypatch.setattr(cli_stats, "LocalLLM", _Loader)

    result = CliRunner().invoke(
        stats_app, ["factcheck", "bytetricks", "--file", str(script), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert "Confirmée" in out.read_text(encoding="utf-8")


def test_cli_factcheck_without_llm_shows_message(monkeypatch):
    monkeypatch.setattr(
        cli_stats, "load_profile", lambda name: SimpleNamespace(llm=SimpleNamespace(enabled=False))
    )
    result = CliRunner().invoke(stats_app, ["factcheck", "bytetricks", "--text", "Un fait."])
    assert result.exit_code == 1
    assert "sans LLM" in result.output


def test_cli_factcheck_requires_one_input(monkeypatch):
    monkeypatch.setattr(
        cli_stats, "load_profile", lambda name: SimpleNamespace(llm=SimpleNamespace(enabled=False))
    )
    result = CliRunner().invoke(stats_app, ["factcheck", "bytetricks"])
    assert result.exit_code == 1

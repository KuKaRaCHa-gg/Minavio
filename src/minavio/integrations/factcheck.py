"""Fact-check des affirmations d'un script (Byte Tricks).

Le LLM local extrait les affirmations ; chacune est cherchée sur Wikipédia (puis Wikidata) ;
le LLM juge ensuite chaque affirmation à partir du seul extrait trouvé. Aucune image n'est envoyée.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from minavio.errors import LLMUnavailableError, MinavioError

if TYPE_CHECKING:
    from minavio.llm.client import BaseLLM

Verdict = Literal["supported", "contradicted", "unverifiable"]
VERDICT_LABELS: dict[str, str] = {
    "supported": "Confirmée",
    "contradicted": "Contredite",
    "unverifiable": "Non vérifiable",
}
MAX_CLAIMS = 8
EXTRACT_CHARS = 1500
TIMEOUT_S = 10.0
USER_AGENT = "Minavio/0.1 (fact-check; https://github.com/)"
NO_LLM_MESSAGE = (
    "Fact-check impossible sans LLM : l'extraction des affirmations repose sur le LLM local. "
    "Démarre le serveur configuré dans le profil (ex. Ollama sur http://localhost:11434) "
    "ou corrige MINAVIO_LLM_BASE_URL / MINAVIO_LLM_MODEL, puis relance."
)
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "llm" / "prompts" / "factcheck.md"

# Injecté par les tests (httpx.MockTransport) ; None = réseau réel.
_TRANSPORT: httpx.BaseTransport | None = None


class Claim(BaseModel):
    claim: str
    entities: list[str] = Field(default_factory=list)
    verdict: Verdict = "unverifiable"
    note: str = ""
    source: str | None = None
    url: str | None = None


class _RawClaim(BaseModel):
    claim: str
    entities: list[str] = Field(default_factory=list)


class _Extraction(BaseModel):
    claims: list[_RawClaim] = Field(default_factory=list)


class _Judgement(BaseModel):
    verdict: Verdict
    note: str = ""


def _prompt(section: str) -> str:
    try:
        text = _PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise MinavioError(f"Prompt fact-check illisible ({_PROMPT_FILE}) : {exc}") from exc
    match = re.search(rf"<!-- section: {section} -->\n(.*?)(?=<!-- section: |\Z)", text, re.S)
    if match is None:
        raise MinavioError(f"Section « {section} » absente du prompt {_PROMPT_FILE}.")
    return match.group(1).strip()


def _api_get(url: str, params: dict) -> dict:
    host = url.split("/")[2]
    try:
        with httpx.Client(
            transport=_TRANSPORT, timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT}
        ) as client:
            resp = client.get(url, params={**params, "format": "json"})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MinavioError(f"Source indisponible ({host}) : {type(exc).__name__}.") from exc
    return data if isinstance(data, dict) else {}


def _wiki_extract(query: str, lang: str) -> tuple[str, str, str] | None:
    """(titre, extrait, url) de l'article Wikipédia le plus pertinent, ou None."""
    base = f"https://{lang}.wikipedia.org/w/api.php"
    hits = (
        _api_get(base, {"action": "query", "list": "search", "srsearch": query, "srlimit": 1})
        .get("query", {})
        .get("search", [])
    )
    if not hits or not isinstance(hits[0], dict) or not hits[0].get("title"):
        return None
    title = hits[0]["title"]
    pages = (
        _api_get(
            base,
            {
                "action": "query",
                "prop": "extracts",
                "exintro": 1,
                "explaintext": 1,
                "titles": title,
            },
        )
        .get("query", {})
        .get("pages", {})
    )
    extract = ""
    if isinstance(pages, dict):
        for page in pages.values():
            if isinstance(page, dict) and page.get("extract"):
                extract = str(page["extract"]).strip()
                break
    if not extract:
        return None
    url = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'), safe='()_,')}"
    return title, extract, url


def _wikidata_desc(entity: str, lang: str) -> tuple[str, str, str] | None:
    """(libellé, description, url) de l'entité Wikidata la plus proche, ou None."""
    hits = _api_get(
        "https://www.wikidata.org/w/api.php",
        {"action": "wbsearchentities", "search": entity, "language": lang, "limit": 1},
    ).get("search", [])
    if not hits or not isinstance(hits[0], dict):
        return None
    hit = hits[0]
    desc = str(hit.get("description") or "").strip()
    if not desc:
        return None
    label = str(hit.get("label") or entity)
    url = str(hit.get("concepturi") or f"https://www.wikidata.org/wiki/{hit.get('id', '')}")
    return f"{label} : {desc}", desc, url


def _evidence(claim: _RawClaim, lang: str) -> tuple[str, str, str] | None:
    """(source, extrait, url) pour une affirmation, ou None si rien n'est trouvé."""
    queries = [*claim.entities[:1], claim.claim]
    for query in dict.fromkeys(q for q in queries if q.strip()):
        if (found := _wiki_extract(query, lang)) is not None:
            return f"Wikipédia ({lang}) : {found[0]}", found[1], found[2]
    if claim.entities and (found := _wikidata_desc(claim.entities[0], lang)) is not None:
        return f"Wikidata : {found[0]}", found[1], found[2]
    return None


def _extract(text: str, llm: BaseLLM) -> list[_RawClaim]:
    messages = [
        {"role": "system", "content": _prompt("extract")},
        {"role": "user", "content": text},
    ]
    try:
        result = llm.chat_json(messages, _Extraction)
    except ValueError as exc:
        raise MinavioError(
            f"Extraction des affirmations impossible : réponse LLM invalide ({exc}). "
            "Réessaie ou change de modèle."
        ) from exc
    return [c for c in result.claims if c.claim.strip()][:MAX_CLAIMS]


def _judge(llm: BaseLLM, claim: str, source: str, extract: str) -> tuple[Verdict, str]:
    user = json.dumps(
        {"affirmation": claim, "source": source, "extrait": extract[:EXTRACT_CHARS]},
        ensure_ascii=False,
    )
    messages = [
        {"role": "system", "content": _prompt("judge")},
        {"role": "user", "content": user},
    ]
    try:
        result = llm.chat_json(messages, _Judgement)
    except ValueError as exc:
        return "unverifiable", f"Réponse du juge invalide ({exc})."
    return result.verdict, result.note.strip()


def check_claims(text: str, llm: BaseLLM | None, *, lang: str = "fr") -> list[Claim]:
    """Extrait les affirmations de `text` et les juge contre Wikipédia/Wikidata.

    Sans LLM, l'extraction est impossible : lève `LLMUnavailableError` avec un message actionnable.
    """
    if llm is None:
        raise LLMUnavailableError(NO_LLM_MESSAGE)
    if not text.strip():
        raise MinavioError("Texte vide : rien à vérifier.")

    claims: list[Claim] = []
    for raw in _extract(text, llm):
        base = {"claim": raw.claim, "entities": raw.entities}
        try:
            found = _evidence(raw, lang)
        except MinavioError as exc:
            claims.append(Claim(**base, note=str(exc)))
            continue
        if found is None:
            claims.append(Claim(**base, note="Aucune source trouvée sur Wikipédia ni Wikidata."))
            continue
        source, extract, url = found
        verdict, note = _judge(llm, raw.claim, source, extract)
        claims.append(Claim(**base, verdict=verdict, note=note, source=source, url=url))
    return claims


def factcheck_report(claims: list[Claim]) -> str:
    """Rapport Markdown des verdicts."""
    if not claims:
        return "# Fact-check\n\nAucune affirmation vérifiable détectée.\n"
    counts = Counter(c.verdict for c in claims)
    summary = ", ".join(f"{VERDICT_LABELS[v]} : {counts[v]}" for v in VERDICT_LABELS)
    lines = ["# Fact-check", "", f"Synthèse : {summary}", ""]
    for i, c in enumerate(claims, 1):
        lines.append(f"## {i}. {c.claim}")
        lines.append(f"- **Verdict** : {VERDICT_LABELS[c.verdict]}")
        if c.note:
            lines.append(f"- **Justification** : {c.note}")
        if c.source:
            link = f" ({c.url})" if c.url else ""
            lines.append(f"- **Source** : {c.source}{link}")
        lines.append("")
    return "\n".join(lines)

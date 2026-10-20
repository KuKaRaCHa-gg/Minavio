"""Choix des miniatures : juge visuel (LLM avec images) ou repli heuristique sans LLM."""

from __future__ import annotations

import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from minavio.config.schema import ChannelProfile
from minavio.core.models import ThumbnailCandidate, ThumbnailChoice
from minavio.errors import LLMUnavailableError
from minavio.llm.prompts import load_prompt

log = logging.getLogger(__name__)

LAYOUT_SIZE = {"hero": 1, "split": 2, "collage": 3}
_SIZE_LAYOUT = {v: k for k, v in LAYOUT_SIZE.items()}
MAX_VISION_CANDIDATES = 16
THUMB_WIDTH = 512

_EMOJI_RE = re.compile("[\U0001f000-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff‍️]+")
# Accroches de secours (sans juge visuel). Jamais de fait chiffré inventé (« 1V3 », « 5 KILLS ») :
# seul le LLM, qui voit l'image, ou l'utilisateur peut l'affirmer. Plusieurs par label = variété.
_LABEL_TEXT: dict[str, list[str]] = {
    "clutch": ["QUEL CLUTCH", "IL L'A FAIT", "PERSONNE N'Y CROYAIT"],
    "multi_kill": ["SQUAD WIPED", "ILS SONT TOUS TOMBÉS", "CARNAGE"],
    "wipe": ["SQUAD WIPED", "CARNAGE"],
    "squad_wipe": ["SQUAD WIPED", "CARNAGE"],
    "squad_wiped": ["SQUAD WIPED", "CARNAGE"],
    "kill": ["INSANE KILL", "IL A PAS VU VENIR", "PROPRE"],
    "knock": ["DOWNED", "IL TOMBE"],
    "fail": ["EPIC FAIL", "C'ÉTAIT PAS PRÉVU"],
    "chaos": ["TOTAL CHAOS", "N'IMPORTE QUOI"],
    "scream": ["IL HURLE", "LA PANIQUE"],
    "funny": ["TROP DRÔLE", "J'EN PEUX PLUS"],
    "laugh": ["FOU RIRE", "J'EN PEUX PLUS"],
    "rage": ["RAGE", "IL PÈTE UN CÂBLE"],
    "fact": ["FUN FACT", "TU SAVAIS ?"],
}
_GENERIC_TEXT = ["À VOIR", "INCROYABLE", "REGARDE ÇA"]


class _Item(BaseModel):
    layout: str | None = None
    images: list[int] = Field(default_factory=list)
    text: str = ""
    reason: str | None = None


class _Response(BaseModel):
    items: list[_Item]


def _clean_text(text: str, max_words: int) -> str:
    text = _EMOJI_RE.sub("", text or "")
    words = text.replace("\n", " ").strip().strip("\"'«»“”`").split()
    return " ".join(words[:max_words]).upper()


def _label_texts(label: str | None, max_words: int) -> list[str]:
    key = (label or "").strip().lower().replace(" ", "_")
    phrases = _LABEL_TEXT.get(key) or next((v for k, v in _LABEL_TEXT.items() if k in key), None)
    if phrases is None:
        phrases = [key.replace("_", " ")] if key else []
    return [t for t in (_clean_text(x, max_words) for x in phrases) if t]


def _fallback_text(
    i: int, cand: ThumbnailCandidate, copy: dict | None, max_words: int, used: set[str]
) -> str:
    """1re variante : le hook du copy ; ensuite des accroches du label, jamais deux fois la même."""
    hook = _clean_text(str((copy or {}).get("hook") or ""), max_words)
    pool = ([hook] if i == 0 and hook else []) + _label_texts(cand.label, max_words)
    pool += ([hook] if hook else []) + [_clean_text(t, max_words) for t in _GENERIC_TEXT]
    text = next((t for t in pool if t and t not in used), pool[0] if pool else "À VOIR")
    used.add(text)
    return text


def _layouts(profile: ChannelProfile, count: int) -> list[str]:
    layouts = list(profile.thumbnails.layouts) or ["hero"]
    return [layouts[i % len(layouts)] for i in range(count)]


def _take(pool: list[int], used: set[int], n: int, all_ids: list[int]) -> list[int]:
    """n ids parmi `pool`, en préférant les images non encore utilisées."""
    chosen: list[int] = []
    for ids in (
        [i for i in pool if i not in used],
        [i for i in all_ids if i not in used],
        pool,
        all_ids,
    ):
        for i in ids:
            if i not in chosen:
                chosen.append(i)
            if len(chosen) == n:
                return chosen
    return chosen


def _compose(
    layout: str, ids: list[int], cands: list[ThumbnailCandidate], text: str, reason: str | None
) -> ThumbnailChoice:
    ids = ids[: LAYOUT_SIZE.get(layout, 1)]
    layout = _SIZE_LAYOUT[len(ids)]  # le layout suit le nombre d'images réellement disponible
    return ThumbnailChoice(layout=layout, images=[cands[i] for i in ids], text=text, reason=reason)


def _heuristic(
    cands: list[ThumbnailCandidate], profile: ChannelProfile, copy: dict | None, count: int
) -> list[ThumbnailChoice]:
    cfg = profile.thumbnails
    order = sorted(range(len(cands)), key=lambda i: cands[i].score, reverse=True)
    used: set[int] = set()
    used_texts: set[str] = set()
    out = []
    for i, layout in enumerate(_layouts(profile, count)):
        n = min(LAYOUT_SIZE.get(layout, 1), len(cands))
        ids = _take(order, used, n, order)
        if n == 2 and len(ids) == 2 and cands[ids[0]].label == cands[ids[1]].label:
            # split : préférer une image de label différent (contraste « VS »)
            alt = [j for j in order if j not in used and j != ids[0]]
            alt = [j for j in alt if cands[j].label != cands[ids[0]].label]
            if alt:
                ids[1] = alt[0]
        used.update(ids)
        out.append(
            _compose(
                layout,
                ids,
                cands,
                _fallback_text(i, cands[ids[0]], copy, cfg.max_words, used_texts),
                "Meilleures notes (sans juge visuel)",
            )
        )
    return out


def _resize_for_llm(cands: list[ThumbnailCandidate], tmp: Path) -> list[Path]:
    """Copies JPEG 512 px de large (Pillow) ; originales si Pillow est absent."""
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow absent : images envoyées sans redimensionnement.")
        return [c.image for c in cands]
    paths = []
    for i, c in enumerate(cands):
        try:
            with Image.open(c.image) as im:
                im = im.convert("RGB")
                if im.width > THUMB_WIDTH:
                    im = im.resize((THUMB_WIDTH, max(1, round(im.height * THUMB_WIDTH / im.width))))
                dest = tmp / f"cand_{i}.jpg"
                im.save(dest, "JPEG", quality=82)
                paths.append(dest)
        except OSError as exc:
            raise ValueError(f"Image candidate illisible : {c.image} ({exc})") from exc
    return paths


def _vision_pick(
    cands: list[ThumbnailCandidate],
    profile: ChannelProfile,
    copy: dict | None,
    llm: Any,
    count: int,
) -> list[ThumbnailChoice]:
    cfg = profile.thumbnails
    layouts = _layouts(profile, count)
    infos = [
        {"id": i, "label": c.label, "score": round(c.score, 2), "t": round(c.t, 1)}
        for i, c in enumerate(cands)
    ]
    user = (
        f"Chaîne : {profile.display_name}. {profile.description}\n"
        f"Images ci-jointes dans l'ordre (Image 0 à Image {len(cands) - 1}) :\n"
        f"{json.dumps(infos, ensure_ascii=False)}\n"
        f"Compositions à produire (layout imposé, dans l'ordre) : "
        f"{json.dumps(layouts)}\nmax_words = {cfg.max_words}\n"
    )
    if copy:
        user += "Titre / hook de la vidéo : " + json.dumps(
            {"title": copy.get("title"), "hook": copy.get("hook")}, ensure_ascii=False
        )
    messages = [
        {"role": "system", "content": load_prompt(profile.llm.prompt, "thumbnails")},
        {"role": "user", "content": user},
    ]
    with tempfile.TemporaryDirectory(prefix="minavio_thumbs_") as td:
        images = _resize_for_llm(cands, Path(td))
        resp = llm.chat_json(messages, _Response, images=images)

    all_ids = sorted(range(len(cands)), key=lambda i: cands[i].score, reverse=True)
    valid = set(range(len(cands)))
    used: set[int] = set()
    used_texts: set[str] = set()
    out = []
    for i, layout in enumerate(layouts):
        item = resp.items[i] if i < len(resp.items) else _Item()
        n = min(LAYOUT_SIZE.get(layout, 1), len(cands))
        ids: list[int] = []
        for j in item.images:
            if j in valid and j not in ids:
                ids.append(j)
            elif j not in valid:
                log.warning("Id d'image inventé par le LLM ignoré : %s", j)
        ids = ids[:n]
        # Doublon entre compositions : remplacer si une image libre existe.
        ids = [j for j in ids if j not in used or len(used) >= len(cands)]
        if len(ids) < n:
            ids = ids + [j for j in _take(all_ids, used | set(ids), n, all_ids) if j not in ids]
            ids = ids[:n]
        used.update(ids)
        text = _clean_text(item.text, cfg.max_words) or _fallback_text(
            i, cands[ids[0]], copy, cfg.max_words, used_texts
        )
        used_texts.add(text)
        out.append(_compose(layout, ids, cands, text, item.reason))
    return out


def pick_thumbnails(
    candidates: list[ThumbnailCandidate],
    profile: ChannelProfile,
    copy: dict | None = None,
    llm: Any = None,
    use_llm: bool = True,
) -> list[ThumbnailChoice]:
    """`profile.thumbnails.count` compositions variées (hero / split / collage).

    `use_llm=False` (`--no-llm`, case décochée dans l'UI) : aucune image n'est envoyée à un LLM,
    même si un client est disponible — choix heuristique uniquement.
    """
    if not candidates:
        return []
    cfg = profile.thumbnails
    top = sorted(candidates, key=lambda c: c.score, reverse=True)[:MAX_VISION_CANDIDATES]
    if use_llm and cfg.use_vision_llm and profile.llm.enabled:
        try:
            if llm is None:
                from minavio.llm.client import LocalLLM

                llm = LocalLLM.from_settings(profile.llm)
            return _vision_pick(top, profile, copy, llm, cfg.count)
        except LLMUnavailableError as exc:
            log.warning("%s Miniatures choisies par heuristique (sans juge visuel).", exc)
        except ValueError as exc:
            log.warning("Juge visuel inexploitable (%s) : heuristique.", exc)
    return _heuristic(top, profile, copy, cfg.count)

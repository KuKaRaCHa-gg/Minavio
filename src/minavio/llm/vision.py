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
_LABEL_TEXT = {
    "clutch": "CLUTCH 1V3",
    "wipe": "SQUAD WIPED",
    "squad_wipe": "SQUAD WIPED",
    "squad_wiped": "SQUAD WIPED",
    "kill": "INSANE KILL",
    "knock": "DOWNED",
    "fail": "EPIC FAIL",
    "chaos": "TOTAL CHAOS",
    "funny": "TROP DRÔLE",
    "laugh": "IMPOSSIBLE DE SE RETENIR",
    "rage": "RAGE",
    "fact": "FUN FACT",
}


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


def _label_text(label: str | None, max_words: int) -> str:
    key = (label or "").strip().lower().replace(" ", "_")
    if key in _LABEL_TEXT:
        return _clean_text(_LABEL_TEXT[key], max_words)
    for k, v in _LABEL_TEXT.items():
        if k in key:
            return _clean_text(v, max_words)
    return _clean_text(key.replace("_", " "), max_words)


def _fallback_text(i: int, cand: ThumbnailCandidate, copy: dict | None, max_words: int) -> str:
    hook = _clean_text(str((copy or {}).get("hook") or ""), max_words)
    label = _label_text(cand.label, max_words)
    # Variété : la 1re variante reprend le hook, les suivantes les labels.
    text = (hook or label) if i == 0 else (label or hook)
    return text or "À VOIR"


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
                _fallback_text(i, cands[ids[0]], copy, cfg.max_words),
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
            i, cands[ids[0]], copy, cfg.max_words
        )
        out.append(_compose(layout, ids, cands, text, item.reason))
    return out


def pick_thumbnails(
    candidates: list[ThumbnailCandidate],
    profile: ChannelProfile,
    copy: dict | None = None,
    llm: Any = None,
) -> list[ThumbnailChoice]:
    """`profile.thumbnails.count` compositions variées (hero / split / collage)."""
    if not candidates:
        return []
    cfg = profile.thumbnails
    top = sorted(candidates, key=lambda c: c.score, reverse=True)[:MAX_VISION_CANDIDATES]
    if cfg.use_vision_llm and profile.llm.enabled:
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

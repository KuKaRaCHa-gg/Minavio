"""Détecteur par template matching OpenCV (éléments d'UI de jeu : kills, bannières...)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from minavio import config
from minavio.config.schema import ChannelProfile, GameProfile, UiTemplate
from minavio.core import ffmpeg
from minavio.core.detect.registry import DetectorUnavailable, register_detector
from minavio.core.models import Highlight
from minavio.errors import ConfigError

log = logging.getLogger("minavio.detect")


@dataclass
class _Hit:
    t: float
    score: float
    label: str
    name: str


def _load_templates(game: GameProfile, cv2) -> list[tuple[UiTemplate, object]]:
    loaded = []
    for ui in game.ui_templates:
        path = config.CONFIG_DIR / "games" / game.id / ui.image
        if not path.exists():
            log.warning("Template '%s' (%s) introuvable : ignoré", ui.name, path)
            continue
        img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            log.warning("Template '%s' illisible (%s) : ignoré", ui.name, path)
            continue
        loaded.append((ui, img))
    return loaded


def group_combos(hits: list[_Hit], window_s: float) -> list[list[_Hit]]:
    """Regroupe les hits 'kill' proches (<= window_s entre deux) ; les autres restent seuls."""
    groups: list[list[_Hit]] = []
    for h in sorted(hits, key=lambda x: x.t):
        if (
            h.label == "kill"
            and groups
            and groups[-1][-1].label == "kill"
            and h.t - groups[-1][-1].t <= window_s
        ):
            groups[-1].append(h)
        else:
            groups.append([h])
    return groups


def _scan(src: Path, game: GameProfile, templates, cv2, sample_fps: float) -> list[_Hit]:
    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        raise DetectorUnavailable(f"OpenCV ne peut pas ouvrir {src}")
    fps = cap.get(cv2.CAP_PROP_FPS) or ffmpeg.probe(src).fps or 30.0
    step = max(1, round(fps / sample_fps))
    hits: list[_Hit] = []
    last: dict[str, float] = {}
    idx = 0
    try:
        while cap.grab():
            if idx % step == 0:
                ok, frame = cap.retrieve()
                if ok:
                    t = idx / fps
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    fh, fw = gray.shape
                    for ui, tpl in templates:
                        if t - last.get(ui.name, -1e9) < ui.cooldown_s:
                            continue
                        rx, ry, rw, rh = ui.roi
                        x0, y0 = int(rx * fw), int(ry * fh)
                        roi = gray[y0 : y0 + max(1, int(rh * fh)), x0 : x0 + max(1, int(rw * fw))]
                        if roi.shape[0] < tpl.shape[0] or roi.shape[1] < tpl.shape[1]:
                            continue
                        res = cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED)
                        val = float(res.max())
                        if val >= ui.threshold:
                            last[ui.name] = t
                            hits.append(_Hit(t, min(1.0, val), ui.label, ui.name))
            idx += 1
    finally:
        cap.release()
    return hits


@register_detector("template_match")
def template_match(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    try:
        import cv2
    except ImportError as e:
        raise DetectorUnavailable("opencv absent (extra 'vision')") from e

    sample_fps = float(params.get("sample_fps", 4))
    pre = float(params.get("pre_s", 1.0))
    post = float(params.get("post_s", 1.0))
    out: list[Highlight] = []
    for game_id in profile.games:
        try:
            game = config.load_game(game_id, config.CONFIG_DIR)
        except ConfigError as e:
            log.warning("Jeu '%s' ignoré : %s", game_id, e)
            continue
        templates = _load_templates(game, cv2)
        if not templates:
            log.warning("Aucun template utilisable pour '%s' : jeu ignoré", game_id)
            continue
        for src in sources:
            hits = _scan(src, game, templates, cv2, sample_fps)
            for grp in group_combos(hits, game.combo_window_s):
                kills = sum(1 for h in grp if h.label == "kill")
                best = max(grp, key=lambda h: h.score)
                multi = kills >= 2
                out.append(
                    Highlight(
                        source=src,
                        start=max(0.0, grp[0].t - pre),
                        end=grp[-1].t + post,
                        score=min(1.0, best.score + (0.1 * (len(grp) - 1) if multi else 0)),
                        label="multi_kill" if multi else best.label,
                        detector="template_match",
                        reason=(
                            f"{kills} kills en {grp[-1].t - grp[0].t:.1f}s"
                            if multi
                            else f"{best.name} ({best.score:.2f}) à {best.t:.1f}s"
                        ),
                        # peak_t = dernier kill du combo (le moment décisif), temps source.
                        signals={
                            "kills": float(kills),
                            "peak_t": round(grp[-1].t if multi else best.t, 3),
                        },
                    )
                )
    return out

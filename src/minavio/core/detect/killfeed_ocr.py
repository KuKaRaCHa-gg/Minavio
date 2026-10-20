"""Détecteur « killfeed » par OCR : aucun template ni capture à fournir.

Pour chaque jeu du profil, lit le killfeed (RapidOCR, CPU/ONNX) à ~2 fps et repère les mots-clés
de `GameProfile.killfeed` (tolérance OCR par distance d'édition). Si `killfeed.roi` est None, la
zone est trouvée seule : OCR plein écran sur ~30 frames réparties, ROI = enveloppe robuste
(quantiles 10/90 % entre frames) des lignes contenant un mot-clé, élargie de 20 % (taille minimale
25 % x 20 % : le killfeed empile des lignes de longueurs variables). Elle est loguée (info) pour
être recopiée dans le YAML du jeu, et gardée en mémoire par (source, jeu).

Params : `sample_fps` (2), `locate_frames` (30), `locate_max_frames` (300, repli 1 fps si les 30
frames n'ont aucun kill), `roi_margin` (0.2), `min_conf` (0.5), `dedupe_s` (4), `max_width` (1280),
`pre_s` (1.5), `post_s` (1.5), `foreign_factor` (0.5, malus si le pseudo est absent de la ligne).
Dépendance : extra `ocr` (rapidocr + onnxruntime) ; absent -> DetectorUnavailable.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from minavio import config
from minavio.config.schema import ChannelProfile, GameProfile
from minavio.core import ffmpeg
from minavio.core.detect.registry import DetectorUnavailable, register_detector
from minavio.core.models import Highlight
from minavio.errors import ConfigError, FFmpegError

log = logging.getLogger("minavio.detect.killfeed")

Box = tuple[float, float, float, float]  # x0, y0, x1, y1 normalisés dans l'image OCRisée

# Priorité en cas d'égalité de distance entre deux labels.
_PRIORITY = {"win": 0, "kill": 1, "knock": 2}
_BASE_SCORE = {"win": 0.95, "kill": 0.7, "knock": 0.45}

# ROI trouvées seules, par (source résolue, mtime, jeu) -> (x, y, w, h)
_ROI_CACHE: dict[tuple[str, float, str], tuple[float, float, float, float]] = {}


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: Box
    conf: float


@dataclass
class Event:
    t: float  # première apparition
    last_seen: float
    label: str
    text: str  # texte normalisé
    mine: bool | None  # None = pseudo non configuré
    conf: float


# --------------------------------------------------------------------------- texte


def normalize(text: str) -> str:
    """Minuscules, sans accents, ponctuation -> espaces, espaces compactés."""
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def edit_distance(a: str, b: str, limit: int = 3) -> int:
    """Levenshtein borné (renvoie limit+1 si la distance dépasse limit)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[-1]


def tolerance(word: str) -> int:
    """Fautes d'OCR tolérées selon la longueur (mots courts : exact, anti faux positifs)."""
    n = len(word)
    return 0 if n < 5 else (1 if n < 10 else 2)


def fuzzy_find(norm_text: str, phrase: str) -> int | None:
    """Distance d'édition minimale de `phrase` (normalisée) dans `norm_text`, ou None si trop loin.

    Compare à des fenêtres de k-1 à k+1 mots (l'OCR coupe ou colle les mots), espaces ignorés.
    """
    target = phrase.replace(" ", "")
    if not target:
        return None
    tol = tolerance(target)
    tokens = norm_text.split()
    k = len(phrase.split())
    best: int | None = None
    for size in range(max(1, k - 1), k + 2):
        for i in range(0, len(tokens) - size + 1):
            d = edit_distance("".join(tokens[i : i + size]), target, tol)
            if d <= tol and (best is None or d < best):
                best = d
    return best


def classify(norm_text: str, keywords: dict[str, list[str]]) -> str | None:
    """Label du meilleur mot-clé (distance minimale, puis priorité win > kill > knock)."""
    best: tuple[int, int, str] | None = None
    for label, words in keywords.items():
        for w in words:
            d = fuzzy_find(norm_text, normalize(w))
            if d is None:
                continue
            cand = (d, _PRIORITY.get(label, 9), label)
            if best is None or cand < best:
                best = cand
    return best[2] if best else None


def mentions_player(norm_text: str, names: list[str]) -> bool:
    return any(fuzzy_find(norm_text, normalize(n)) is not None for n in names if normalize(n))


# --------------------------------------------------------------------------- logique pure


def _same_line(a: str, b: str) -> bool:
    """Même ligne de killfeed lue deux fois : <= 2 fautes d'OCR (ou 1/12 de la longueur)."""
    limit = max(2, max(len(a), len(b)) // 12)
    return edit_distance(a, b, limit) <= limit


def dedupe_events(obs: list[Event], window_s: float) -> list[Event]:
    """Fusionne les observations d'une même ligne (même label, texte similaire) vue en continu.

    Une ligne reste affichée plusieurs secondes : tant qu'elle réapparaît à moins de `window_s`
    de sa dernière observation, c'est le même événement (daté de sa première apparition).
    """
    events: list[Event] = []
    for o in sorted(obs, key=lambda e: e.t):
        for ev in reversed(events):
            if o.t - ev.last_seen > window_s:
                continue
            if ev.label == o.label and _same_line(ev.text, o.text):
                ev.last_seen = max(ev.last_seen, o.t)
                ev.conf = max(ev.conf, o.conf)
                ev.mine = ev.mine or o.mine if ev.mine is not None else o.mine
                if len(o.text) > len(ev.text):  # lecture la plus complète
                    ev.text = o.text
                break
        else:
            events.append(Event(o.t, o.t, o.label, o.text, o.mine, o.conf))
    return events


def group_kills(events: list[Event], window_s: float) -> list[list[Event]]:
    """Regroupe les kills « à nous » (ou sans pseudo) espacés de <= window_s ; le reste est seul."""
    groups: list[list[Event]] = []
    for ev in sorted(events, key=lambda e: e.t):
        comboable = ev.label == "kill" and ev.mine is not False
        prev = groups[-1][-1] if groups else None
        if (
            comboable
            and prev is not None
            and prev.label == "kill"
            and prev.mine is not False
            and ev.t - prev.t <= window_s
        ):
            groups[-1].append(ev)
        else:
            groups.append([ev])
    return groups


def to_highlights(
    src: Path, events: list[Event], combo_window_s: float, params: dict
) -> list[Highlight]:
    pre = float(params.get("pre_s", 1.5))
    post = float(params.get("post_s", 1.5))
    foreign = float(params.get("foreign_factor", 0.5))
    out: list[Highlight] = []
    for grp in group_kills(events, combo_window_s):
        scored = []
        for ev in grp:
            s = _BASE_SCORE.get(ev.label, 0.5) * (0.7 + 0.3 * min(1.0, ev.conf))
            scored.append(s * foreign if ev.mine is False else s)
        n = len(grp)
        first, last = grp[0], grp[-1]
        multi = n >= 2
        score = min(1.0, max(scored) + (0.1 * (n - 1) if multi else 0.0))
        best = grp[scored.index(max(scored))]
        out.append(
            Highlight(
                source=src,
                start=max(0.0, first.t - pre),
                end=last.t + post,
                score=score,
                label="multi_kill" if multi else best.label,
                detector="killfeed_ocr",
                reason=(
                    f"{n} kills en {last.t - first.t:.1f}s (killfeed)"
                    if multi
                    else f"{best.label} « {best.text} » à {best.t:.1f}s"
                    + (" (pseudo absent)" if best.mine is False else "")
                ),
                signals={
                    "kills": float(n),
                    "peak_t": round(last.t if multi else best.t, 3),
                },
            )
        )
    return out


def enlarge_roi(
    box: Box, margin: float, min_w: float = 0.25, min_h: float = 0.2
) -> tuple[float, float, float, float]:
    """(x0,y0,x1,y1) -> (x,y,w,h) élargi de `margin` au total, avec une taille minimale.

    Le killfeed empile des lignes de longueurs différentes qui apparaissent et disparaissent :
    la ROI ne doit pas être ajustée au plus juste sur ce qui a été vu pendant la localisation.
    """
    x0, y0, x1, y1 = (float(v) for v in box)
    dx, dy = (x1 - x0) * margin / 2, (y1 - y0) * margin / 2
    x0, y0, x1, y1 = x0 - dx, y0 - dy, x1 + dx, y1 + dy
    out = []
    for lo, hi, size in ((x0, x1, min_w), (y0, y1, min_h)):
        if hi - lo < size:
            mid = (lo + hi) / 2
            lo, hi = mid - size / 2, mid + size / 2
        if lo < 0:
            lo, hi = 0.0, min(1.0, hi - lo)
        if hi > 1:
            lo, hi = max(0.0, lo - (hi - 1)), 1.0
        out.append((lo, hi))
    (x0, x1), (y0, y1) = out
    return (round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4))


def _quantile(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = q * (len(s) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def envelope_box(frames_boxes: list[list[Box]], q: float = 0.1) -> Box:
    """Boîte englobante des lignes de chaque frame, puis enveloppe robuste entre frames.

    Gauche/haut = quantile q des minima, droite/bas = quantile 1-q des maxima : la zone couvre les
    lignes longues ou empilées vues sur quelques frames, sans suivre un faux positif isolé.
    """
    unions = [
        (min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs))
        for bs in frames_boxes
        if bs
    ]
    return (
        _quantile([u[0] for u in unions], q),
        _quantile([u[1] for u in unions], q),
        _quantile([u[2] for u in unions], 1 - q),
        _quantile([u[3] for u in unions], 1 - q),
    )


# --------------------------------------------------------------------------- E/S


def _make_ocr() -> Callable[[object], list[OcrLine]]:
    """Moteur RapidOCR (modèles ONNX fournis par le paquet, aucun téléchargement)."""
    try:
        import onnxruntime  # noqa: F401
        from rapidocr import RapidOCR
    except ImportError as e:
        raise DetectorUnavailable("RapidOCR absent : `pip install minavio[ocr]`") from e
    # limit_type=max : ne pas agrandir une ROI fine jusqu'à 736 px (10x plus lent, sans gain).
    engine = RapidOCR(params={"Det.limit_type": "max", "Det.limit_side_len": 1280})
    # « détection vide » = frame sans texte : pas un warning (réglé après init, qui le réécrit).
    logging.getLogger("RapidOCR").setLevel(logging.ERROR)

    def run(frame) -> list[OcrLine]:
        h, w = frame.shape[:2]
        res = engine(frame)
        if res is None or res.txts is None or res.boxes is None:
            return []
        lines = []
        for txt, box, conf in zip(res.txts, res.boxes, res.scores, strict=False):
            xs, ys = [p[0] for p in box], [p[1] for p in box]
            lines.append(
                OcrLine(
                    str(txt),
                    (min(xs) / w, min(ys) / h, max(xs) / w, max(ys) / h),
                    float(conf),
                )
            )
        return lines

    return run


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def _frames(
    src: Path,
    info: ffmpeg.MediaInfo,
    fps: float,
    crop: tuple[float, float, float, float] | None,
    max_width: int,
) -> Iterator[tuple[float, object]]:
    """Frames BGR (numpy) à `fps`, rognées sur `crop` (x,y,w,h normalisés), en flux (pas de RAM)."""
    import numpy as np

    cw, ch, cx, cy = info.width, info.height, 0, 0
    vf = [f"fps={fps}"]
    if crop is not None:
        cw, ch = _even(crop[2] * info.width), _even(crop[3] * info.height)
        cx = min(int(crop[0] * info.width), info.width - cw)
        cy = min(int(crop[1] * info.height), info.height - ch)
        vf.append(f"crop={cw}:{ch}:{cx}:{cy}")
    ow = min(cw, max_width)
    oh = _even(ch * ow / cw)
    ow = _even(ow)
    vf.append(f"scale={ow}:{oh}")
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(src),
        "-an", "-vf", ",".join(vf), "-f", "rawvideo", "-pix_fmt", "bgr24", "-",
    ]  # fmt: skip
    size = ow * oh * 3
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        except FileNotFoundError as e:
            raise FFmpegError("'ffmpeg' est introuvable : installez FFmpeg.") from e
        try:
            k = 0
            while True:
                buf = proc.stdout.read(size)  # type: ignore[union-attr]
                if len(buf) < size:
                    break
                yield k / fps, np.frombuffer(buf, np.uint8).reshape(oh, ow, 3)
                k += 1
        finally:
            proc.stdout.close()  # type: ignore[union-attr]
            if proc.poll() is None:
                proc.kill()
            code = proc.wait()
        if code not in (0, -9):
            err.seek(0)
            msg = err.read().decode("utf-8", errors="replace").strip()[-1500:]
            raise FFmpegError(f"Lecture des frames échouée ({src}) : {' '.join(cmd)}\n{msg}")


def locate_roi(
    src: Path,
    info: ffmpeg.MediaInfo,
    keywords: dict[str, list[str]],
    ocr: Callable[[object], list[OcrLine]],
    params: dict,
) -> tuple[float, float, float, float] | None:
    """Auto-localisation du killfeed : (x, y, w, h) normalisés, ou None si aucun mot-clé vu."""
    n = int(params.get("locate_frames", 30))
    max_n = int(params.get("locate_max_frames", 300))
    max_w = int(params.get("max_width", 1280))
    margin = float(params.get("roi_margin", 0.2))
    min_conf = float(params.get("min_conf", 0.5))
    duration = max(info.duration, 1.0)

    def scan(fps: float, limit: int) -> list[list[Box]]:
        hits: list[list[Box]] = []
        frames = _frames(src, info, fps, None, max_w)
        try:
            for i, (_, frame) in enumerate(frames):
                if i >= limit:
                    break
                boxes = [
                    ln.box
                    for ln in ocr(frame)
                    if ln.conf >= min_conf and classify(normalize(ln.text), keywords)
                ]
                if boxes:
                    hits.append(boxes)
        finally:
            frames.close()
        return hits

    hits = scan(min(n / duration, 2.0), n)
    if (
        not hits and duration > n
    ):  # kills rares : balayage plein écran à 1 fps, jusqu'au premier kill
        log.info("killfeed : aucun mot-clé sur %d frames, balayage plein écran à 1 fps", n)
        hits = scan(1.0, max_n)
    if not hits:
        return None
    return enlarge_roi(envelope_box(hits), margin)


def _scan_roi(
    src: Path,
    info: ffmpeg.MediaInfo,
    game: GameProfile,
    roi: tuple[float, float, float, float],
    ocr: Callable[[object], list[OcrLine]],
    params: dict,
) -> list[Event]:
    fps = float(params.get("sample_fps", 2))
    min_conf = float(params.get("min_conf", 0.5))
    kf = game.killfeed
    obs: list[Event] = []
    for t, frame in _frames(src, info, fps, roi, int(params.get("max_width", 1280))):
        for ln in ocr(frame):
            if ln.conf < min_conf:
                continue
            norm = normalize(ln.text)
            label = classify(norm, kf.keywords)
            if label is None:
                continue
            mine = mentions_player(norm, kf.player_names) if kf.player_names else None
            obs.append(Event(t, t, label, norm, mine, ln.conf))
    return obs


@register_detector("killfeed_ocr")
def killfeed_ocr(sources: list[Path], params: dict, profile: ChannelProfile) -> list[Highlight]:
    games: list[GameProfile] = []
    for game_id in profile.games:
        try:
            games.append(config.load_game(game_id, config.CONFIG_DIR))
        except ConfigError as e:
            log.warning("Jeu '%s' ignoré : %s", game_id, e)
    if not games:
        return []
    ocr = _make_ocr()  # DetectorUnavailable si RapidOCR absent

    dedupe_s = float(params.get("dedupe_s", 4.0))
    out: list[Highlight] = []
    for game in games:
        for src in sources:
            info = ffmpeg.probe(src)
            roi = game.killfeed.roi
            if roi is None:
                key = (str(Path(src).resolve()), Path(src).stat().st_mtime, game.id)
                roi = _ROI_CACHE.get(key)
                if roi is None:
                    roi = locate_roi(src, info, game.killfeed.keywords, ocr, params)
                    if roi is None:
                        log.warning(
                            "killfeed '%s' introuvable dans %s (aucun mot-clé lu) : ignoré. "
                            "Renseignez killfeed.roi dans configs/games/%s/game.yaml.",
                            game.id, src, game.id,
                        )  # fmt: skip
                        continue
                    _ROI_CACHE[key] = roi
                    log.info(
                        "killfeed '%s' localisé dans %s : roi: [%.3f, %.3f, %.3f, %.3f] "
                        "(à recopier dans killfeed.roi du YAML pour sauter la localisation)",
                        game.id, src.name, *roi,
                    )  # fmt: skip
            events = dedupe_events(_scan_roi(src, info, game, roi, ocr, params), dedupe_s)
            out.extend(to_highlights(src, events, game.combo_window_s, params))
    return out

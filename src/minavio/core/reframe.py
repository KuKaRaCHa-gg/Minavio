"""Reframe : filtres FFmpeg pour passer d'une source 16:9 à un format de sortie (ex. 9:16).

- `none`        : mise à l'échelle + bandes noires si le ratio diffère.
- `blur_pad`    : fond = même vidéo agrandie + boxblur, premier plan centré.
- `face_track`  : crop suivant le visage (YuNet, sinon MediaPipe/Haar, sinon centre).
- `action_track`: crop suivant le centre de mouvement (OpenCV), sinon centre.
- `split_facecam`: haut = gameplay (crop horizontal suivi), bas = facecam recadrée (cover).

Caméra virtuelle : la trajectoire (face/action) passe par un One Euro filter, une zone morte, une
vitesse de pan bornée et une remise à zéro (saut franc) aux changements de scène.

Punch-in (`Clip.zoom > 1`) : crop centré + scale avant le reframe (modes sans suivi) ou fenêtre de
crop réduite (modes avec suivi / split, où la facecam n'est jamais zoomée).

Heuristique facecam (`detect_facecam`) :
  1. ~8 frames réparties sur le clip ; YuNet sur chacun des 4 coins (zones 42 % x 50 %).
  2. Par coin, on garde le meilleur visage de chaque frame, puis ceux qui sont « stables » (même
     position à 0,75 largeur de visage près, même taille à +-30 %). Confiance = fraction de frames
     stables x score moyen. `auto` : meilleur coin avec >= 60 % de frames stables.
  3. Boîte de la facecam : a priori visage x 2,5 en largeur (ratio 4:3), puis chaque bord est
     « aimanté » sur le pic de gradient (Sobel 1D de l'image moyenne) le plus net de sa zone de
     recherche s'il domine nettement le bruit ; un bord à < 2,5 % du bord de l'image y est collé.
     Sans bord net, l'a priori est conservé (et signalé dans le log).
"""

from __future__ import annotations

import itertools
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from minavio.config.schema import FramingSettings, SmoothingSettings

log = logging.getLogger("minavio.reframe")

CropPath = list[tuple[float, float]]  # (t relatif au début du clip en s source, centre x 0..1)

SAMPLE_FPS = 4.0
MAX_PATH_POINTS = 60
CUT_EPS = 0.002  # durée du « saut » (s) émis au changement de scène
HOLD_S = 1.5  # maintien de la dernière position quand la détection disparaît
CENTER_RETURN_PER_S = 0.08  # puis retour au centre (fraction de largeur / s)
CUT_THRESHOLD = 0.5  # distance de Bhattacharyya entre histogrammes de frames consécutives


FACECAM_MIN_FRACTION = 0.6  # auto : part minimale de frames avec un visage stable
FACECAM_FORCED_MIN_FRACTION = 0.3  # coin imposé : on tolère plus d'absences
CORNER_W, CORNER_H = 0.42, 0.5  # zones de recherche (fraction de l'image)
FACE_TO_BOX_W = 2.5  # a priori : largeur de la facecam = visage x 2,5
BOX_ASPECT = 0.75  # a priori : facecam 4:3
SEPARATOR_PX_PER_480 = 1  # épaisseur du séparateur : out_h / 480 px (min 2)


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


# --------------------------------------------------------------------------- filtres


def scale_pad_filter(in_label: str, out_label: str, out_w: int, out_h: int) -> list[str]:
    return [
        f"[{in_label}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[{out_label}]"
    ]


def blur_pad_filter(
    in_label: str, out_label: str, out_w: int, out_h: int, tag: str = ""
) -> list[str]:
    radius = max(2, min(out_w, out_h) // 30)
    bg, fg, bgb, fgs = (f"{tag}{n}" for n in ("bg", "fg", "bgb", "fgs"))
    return [
        f"[{in_label}]split=2[{bg}][{fg}]",
        f"[{bg}]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h},boxblur={radius}:2[{bgb}]",
        f"[{fg}]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[{fgs}]",
        f"[{bgb}][{fgs}]overlay=(W-w)/2:(H-h)/2,setsar=1[{out_label}]",
    ]


def zoom_filter(in_label: str, out_label: str, src_w: int, src_h: int, zoom: float) -> list[str]:
    """Punch-in centré : crop de 1/zoom puis retour à la résolution d'origine."""
    cw, ch = _even(src_w / zoom), _even(src_h / zoom)
    return [
        f"[{in_label}]crop={cw}:{ch}:{(src_w - cw) // 2}:{(src_h - ch) // 2},"
        f"scale={src_w}:{src_h}:flags=bicubic[{out_label}]"
    ]


def crop_x_expression(
    path: CropPath,
    src_w: int,
    crop_w: int,
    x_lo: float = 0.0,
    x_hi: float | None = None,
) -> str:
    """Expression FFmpeg (variable `t`) : x du crop, interpolation linéaire entre les points."""
    hi = src_w - crop_w if x_hi is None else x_hi
    pts = [(t, min(max(cx * src_w - crop_w / 2, x_lo), hi)) for t, cx in path]
    if len(pts) == 1:
        return f"{pts[0][1]:.1f}"
    expr = f"{pts[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(itertools.pairwise(pts))):
        span = max(t1 - t0, 1e-3)
        expr = f"if(lt(t,{t1:.3f}),{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{span:.3f},{expr})"
    return expr


def window_geometry(
    src_w: int,
    src_h: int,
    aspect: float,
    zoom: float = 1.0,
    exclude: tuple[str, float] | None = None,
) -> tuple[int, int, float, float, int]:
    """Fenêtre de crop d'aspect `aspect` dans la zone visible (source / zoom), centrée.

    Renvoie (w, h, x_min, x_max, y). `exclude=("left"|"right", bord_px)` écarte une facecam si la
    fenêtre peut s'en passer (sinon ignoré).
    """
    rw, rh = src_w / zoom, src_h / zoom
    if rw / rh >= aspect:
        wh, ww = rh, rh * aspect
    else:
        ww, wh = rw, rw / aspect
    ww, wh = _even(ww), _even(wh)
    x_lo = (src_w - rw) / 2
    x_hi = x_lo + rw - ww
    if exclude is not None:
        side, edge = exclude
        lo, hi = (max(x_lo, edge), x_hi) if side == "left" else (x_lo, min(x_hi, edge - ww))
        if hi >= lo:
            x_lo, x_hi = lo, hi
    return ww, wh, x_lo, x_hi, int((src_h - wh) // 2)


def track_filter(
    in_label: str,
    out_label: str,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    path: CropPath | None,
    zoom: float = 1.0,
) -> list[str]:
    """Crop au ratio de sortie (centré si path est None) puis scale ; le zoom réduit la fenêtre."""
    cw, ch, x_lo, x_hi, cy = window_geometry(src_w, src_h, out_w / out_h, zoom)
    cx = crop_x_expression(path, src_w, cw, x_lo, x_hi) if path else f"{(x_lo + x_hi) / 2:.0f}"
    return [
        f"[{in_label}]crop=w={cw}:h={ch}:x='{cx}':y={cy},scale={out_w}:{out_h},setsar=1[{out_label}]"
    ]


def _facecam_cover(fc: FacecamDetection, src_w: int, src_h: int, aspect: float):
    """Rectangle (x, y, w, h) px de la facecam, au ratio `aspect` (cover), centré sur le visage."""
    bx, by, bw, bh = (fc.box[0] * src_w, fc.box[1] * src_h, fc.box[2] * src_w, fc.box[3] * src_h)
    if bw / bh >= aspect:
        ch, cw = bh, bh * aspect
    else:
        cw, ch = bw, bw / aspect
    cw, ch = _even(cw), _even(ch)
    fcx, fcy = fc.face[0] * src_w, fc.face[1] * src_h
    x = min(max(fcx - cw / 2, bx), max(bx, bx + bw - cw))
    y = min(max(fcy - ch / 2, by), max(by, by + bh - ch))
    x, y = min(int(x), src_w - cw), min(int(y), src_h - ch)
    return x, y, cw, ch


def split_facecam_filter(
    in_label: str,
    out_label: str,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    path: CropPath | None,
    facecam: FacecamDetection,
    split_ratio: float,
    zoom: float = 1.0,
    tag: str = "",
    separator: bool = True,
) -> list[str]:
    """Haut : gameplay (suivi, facecam exclue si possible, zoom). Bas : facecam (jamais zoomée)."""
    fh = _even(out_h * split_ratio)
    gh = out_h - fh
    bx0, bx1 = facecam.box[0] * src_w, (facecam.box[0] + facecam.box[2]) * src_w
    exclude = ("left", bx1) if facecam.corner.endswith("left") else ("right", bx0)
    gw, gwh, x_lo, x_hi, gy = window_geometry(src_w, src_h, out_w / gh, zoom, exclude)
    gx = crop_x_expression(path, src_w, gw, x_lo, x_hi) if path else f"{(x_lo + x_hi) / 2:.0f}"
    fx, fy, fw, fch = _facecam_cover(facecam, src_w, src_h, out_w / fh)
    sg, sf, gt, fc = (f"{tag}{n}" for n in ("sg", "sf", "gt", "fc"))
    sep = ""
    if separator:
        th = max(2, round(out_h * SEPARATOR_PX_PER_480 / 480))
        sep = f",drawbox=x=0:y={gh - th // 2}:w=iw:h={th}:color=white@0.9:t=fill"
    return [
        f"[{in_label}]split=2[{sg}][{sf}]",
        f"[{sg}]crop=w={gw}:h={gwh}:x='{gx}':y={gy},scale={out_w}:{gh},setsar=1[{gt}]",
        f"[{sf}]crop=w={fw}:h={fch}:x={fx}:y={fy},scale={out_w}:{fh},setsar=1[{fc}]",
        f"[{gt}][{fc}]vstack=inputs=2{sep},setsar=1[{out_label}]",
    ]


def build_reframe(
    mode: str,
    in_label: str,
    out_label: str,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    path: CropPath | None = None,
    tag: str = "",
    *,
    zoom: float = 1.0,
    facecam: FacecamDetection | None = None,
    split_ratio: float = 0.35,
) -> list[str]:
    """Filtres FFmpeg du reframe ; `zoom` > 1 = punch-in appliqué avant (ou intégré au crop)."""
    if mode == "split_facecam" and facecam is not None:
        return split_facecam_filter(
            in_label, out_label, src_w, src_h, out_w, out_h, path, facecam, split_ratio, zoom, tag
        )
    if mode in {"face_track", "action_track"}:
        return track_filter(in_label, out_label, src_w, src_h, out_w, out_h, path, zoom)
    pre: list[str] = []
    if zoom > 1.0001:
        pre = zoom_filter(in_label, f"{tag}z", src_w, src_h, zoom)
        in_label = f"{tag}z"
    if mode in {"blur_pad", "split_facecam"}:  # split sans facecam : repli de sécurité
        return pre + blur_pad_filter(in_label, out_label, out_w, out_h, tag)
    return pre + scale_pad_filter(in_label, out_label, out_w, out_h)


# --------------------------------------------------------------------------- lissage caméra


class OneEuroFilter:
    """One Euro filter (Casiez et al. 2012) : lissage fort à l'arrêt, réactif en mouvement.

    cutoff = min_cutoff + beta * |vitesse filtrée| ; `speed_scale` convertit la vitesse (unités/s)
    avant ce calcul (ex. 100 pour des coordonnées normalisées : vitesse en % de largeur / s).
    """

    def __init__(
        self,
        min_cutoff: float = 1.0,
        beta: float = 0.0,
        d_cutoff: float = 1.0,
        speed_scale: float = 1.0,
    ):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.speed_scale = speed_scale
        self.reset()

    def reset(self, x: float | None = None, t: float | None = None) -> None:
        """Remise à zéro : le prochain échantillon (ou `x` fourni) devient la valeur filtrée."""
        self._x = x
        self._t = t
        self._dx = 0.0

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, t: float, x: float) -> float:
        if self._x is None or self._t is None:
            self._x, self._t, self._dx = x, t, 0.0
            return x
        dt = t - self._t
        if dt <= 0:
            return self._x
        raw_dx = (x - self._x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        self._dx += a_d * (raw_dx - self._dx)
        cutoff = self.min_cutoff + self.beta * abs(self._dx) * self.speed_scale
        self._x += self._alpha(cutoff, dt) * (x - self._x)
        self._t = t
        return self._x


def smooth_camera(
    raw: list[tuple[float, float | None]],
    settings: SmoothingSettings | None = None,
    cuts: set[int] | frozenset[int] = frozenset(),
) -> CropPath:
    """Trajectoire de caméra virtuelle à partir de détections brutes (t, x | None).

    - One Euro filter sur la cible ; zone morte (la caméra ne bouge que si la cible sort de la zone,
      et seulement jusqu'à son bord) ; vitesse de pan <= max_pan_per_s ;
    - `cuts` : indices d'échantillons où commence une nouvelle scène -> saut franc (filtre remis à
      zéro) si reset_on_scene_cut ;
    - trou de détection : dernière cible maintenue HOLD_S, puis retour lent vers 0,5.
    """
    if settings is None:
        from minavio.config.schema import SmoothingSettings

        settings = SmoothingSettings()
    if not raw:
        return [(0.0, 0.5)]
    flt = OneEuroFilter(settings.min_cutoff, settings.beta, speed_scale=100.0)
    out: CropPath = []
    cam = 0.5
    target = 0.5
    last_seen: float | None = None
    last_t: float | None = None
    for i, (t, x) in enumerate(raw):
        dt = 0.0 if last_t is None else max(t - last_t, 0.0)
        cut = settings.reset_on_scene_cut and i in cuts and last_t is not None
        if x is not None:
            target, last_seen = x, t
        elif cut:
            target = 0.5
        elif last_seen is not None and t - last_seen > HOLD_S:
            target += max(-CENTER_RETURN_PER_S * dt, min(CENTER_RETURN_PER_S * dt, 0.5 - target))
        if last_t is None or cut:
            if cut:
                out.append((max(t - CUT_EPS, out[-1][0]), cam))
            flt.reset()
            cam = flt(t, target)
        else:
            filt = flt(t, target)
            gap = filt - cam
            desired = cam
            if abs(gap) > settings.dead_zone:
                desired = filt - math.copysign(settings.dead_zone, gap)
            limit = settings.max_pan_per_s * dt
            cam += max(-limit, min(limit, desired - cam))
        cam = min(max(cam, 0.0), 1.0)
        out.append((t, cam))
        last_t = t
    if out[0][0] > 0:
        out.insert(0, (0.0, out[0][1]))
    return simplify_path(out)


def simplify_path(path: CropPath, max_points: int = MAX_PATH_POINTS) -> CropPath:
    """Ramer-Douglas-Peucker sur x(t) ; tolérance augmentée jusqu'à tenir dans max_points."""
    if len(path) <= max_points:
        return path
    eps = 0.002
    while True:
        keep = {0, len(path) - 1}
        stack = [(0, len(path) - 1)]
        while stack:
            a, b = stack.pop()
            (ta, xa), (tb, xb) = path[a], path[b]
            worst, wi = 0.0, -1
            for k in range(a + 1, b):
                tk, xk = path[k]
                interp = xa + (xb - xa) * ((tk - ta) / (tb - ta) if tb > ta else 0)
                if (e := abs(xk - interp)) > worst:
                    worst, wi = e, k
            if worst > eps:
                keep.add(wi)
                stack += [(a, wi), (wi, b)]
        if len(keep) <= max_points:
            return [path[k] for k in sorted(keep)]
        eps *= 1.6


def smooth_path(raw: list[tuple[float, float | None]], alpha: float = 0.25) -> CropPath:
    """(Ancien lissage, conservé) comble les trous, médian (3), exponentiel, décime."""
    if not raw:
        return [(0.0, 0.5)]
    xs: list[float] = []
    last = 0.5
    for _, x in raw:
        last = x if x is not None else last
        xs.append(last)
    med = [
        sorted(xs[max(0, i - 1) : i + 2])[len(xs[max(0, i - 1) : i + 2]) // 2]
        for i in range(len(xs))
    ]
    out, cur = [], med[0]
    for (t, _), x in zip(raw, med, strict=True):
        cur += alpha * (x - cur)
        out.append((t, cur))
    if len(out) > MAX_PATH_POINTS:
        step = len(out) / MAX_PATH_POINTS
        out = [out[int(i * step)] for i in range(MAX_PATH_POINTS)] + [out[-1]]
    if out[0][0] > 0:
        out.insert(0, (0.0, out[0][1]))
    return out


# --------------------------------------------------------------------------- échantillonnage


def _frames(source: Path, start: float, end: float):
    """Itère (t_relatif, frame BGR) à SAMPLE_FPS sur [start, end]."""
    import cv2

    cap = cv2.VideoCapture(str(source))
    try:
        t = 0.0
        while start + t <= end:
            cap.set(cv2.CAP_PROP_POS_MSEC, (start + t) * 1000)
            ok, frame = cap.read()
            if not ok:
                break
            yield t, frame
            t += 1 / SAMPLE_FPS
    finally:
        cap.release()


def _hist(frame):
    import cv2

    small = cv2.resize(frame, (64, 36), interpolation=cv2.INTER_AREA)
    h = cv2.calcHist([small], [0, 1, 2], None, [8, 8, 8], [0, 256] * 3)
    return cv2.normalize(h, h, 1.0, 0.0, cv2.NORM_L1).flatten()


class CutDetector:
    """Changement de scène léger : distance entre histogrammes de frames échantillonnées."""

    def __init__(self, threshold: float = CUT_THRESHOLD):
        self.threshold = threshold
        self._prev = None

    def update(self, frame) -> bool:
        import cv2

        h = _hist(frame)
        cut = (
            self._prev is not None
            and cv2.compareHist(self._prev, h, cv2.HISTCMP_BHATTACHARYYA) > self.threshold
        )
        self._prev = h
        return bool(cut)


YUNET_MODEL = Path(__file__).parent / "assets" / "face_detection_yunet_2023mar.onnx"


def _yunet_faces():
    """frame -> [(x, y, w, h, score)] normalisés (YuNet embarqué), ou None si indisponible."""
    import cv2

    if not hasattr(cv2, "FaceDetectorYN") or not YUNET_MODEL.is_file():
        return None
    det = cv2.FaceDetectorYN.create(str(YUNET_MODEL), "", (320, 320), 0.7)

    def faces(frame, min_side: int = 0):
        h, w = frame.shape[:2]
        scale = max(640, min_side) / max(h, w)
        small = cv2.resize(frame, (round(w * scale), round(h * scale)))
        det.setInputSize((small.shape[1], small.shape[0]))
        _, found = det.detect(small)
        if found is None:
            return []
        sw, sh = small.shape[1], small.shape[0]
        return [
            (f[0] / sw, f[1] / sh, f[2] / sw, f[3] / sh, float(f[14]))
            for f in found
            if f[2] > 0 and f[3] > 0
        ]

    return faces


def _yunet_detector():
    """YuNet (OpenCV, Apache-2.0) : modèle embarqué dans le paquet, aucun téléchargement."""
    faces = _yunet_faces()
    if faces is None:
        return None

    def detect_yunet(frame):
        found = faces(frame)
        if not found:
            return None
        x, _y, fw, _fh, _s = max(found, key=lambda f: f[4])
        return float(x + fw / 2)

    return detect_yunet


def _face_detector():
    """Renvoie une fonction frame -> centre x normalisé | None, ou None si rien de dispo."""
    try:
        if (detect := _yunet_detector()) is not None:
            return detect
    except Exception as e:
        log.info("YuNet indisponible (%s) : essai de MediaPipe", e)
    try:
        import mediapipe as mp

        solutions = getattr(mp, "solutions", None)
        if solutions is not None:
            det = solutions.face_detection.FaceDetection(model_selection=1)

            def detect_mp(frame):
                import cv2

                res = det.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                if not res.detections:
                    return None
                best = max(res.detections, key=lambda d: d.score[0])
                box = best.location_data.relative_bounding_box
                return box.xmin + box.width / 2

            return detect_mp
    except Exception as e:  # mediapipe absent ou API différente
        log.info("MediaPipe indisponible (%s) : essai du détecteur Haar OpenCV", e)
    try:
        import cv2

        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        if cascade.empty():
            return None

        def detect_haar(frame):
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = cascade.detectMultiScale(gray, 1.2, 5, minSize=(40, 40))
            if len(faces) == 0:
                return None
            x, _y, w, _h = max(faces, key=lambda f: f[2] * f[3])
            return (x + w / 2) / frame.shape[1]

        return detect_haar
    except Exception:
        return None


# --------------------------------------------------------------------------- facecam


@dataclass(frozen=True)
class FacecamDetection:
    corner: str  # bottom_left | bottom_right | top_left | top_right
    box: tuple[float, float, float, float]  # x, y, w, h normalisés (image source)
    face: tuple[float, float]  # centre du visage médian (x, y) normalisé
    confidence: float  # fraction de frames stables x score moyen
    box_source: str = "prior"  # "edges" si au moins un bord a été aimanté

    def __str__(self) -> str:
        x, y, w, h = self.box
        return (
            f"coin={self.corner} confiance={self.confidence:.2f} "
            f"boîte=({x:.2f},{y:.2f},{w:.2f},{h:.2f}) [{self.box_source}]"
        )


CORNERS = ("bottom_left", "bottom_right", "top_left", "top_right")


def _corner_region(corner: str) -> tuple[float, float, float, float]:
    x0 = 0.0 if corner.endswith("left") else 1 - CORNER_W
    y0 = 0.0 if corner.startswith("top") else 1 - CORNER_H
    return x0, y0, CORNER_W, CORNER_H


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2] if len(s) % 2 else (s[len(s) // 2 - 1] + s[len(s) // 2]) / 2


def _snap_edge(profile, lo: int, hi: int, outer_first: bool):
    """Pic de gradient dominant de profile[lo:hi] ; None s'il ne se détache pas du bruit."""
    import numpy as np

    if hi - lo < 4:
        return None
    seg = profile[lo:hi]
    k = int(np.argmax(seg))
    if seg[k] < 10.0 or seg[k] < 2.5 * float(np.median(seg)) + 1e-6:
        return None
    strong = np.flatnonzero(seg >= 0.6 * seg[k])  # bord extérieur : le plus éloigné du visage
    k = int(strong[0] if outer_first else strong[-1])
    return float(lo + k)


def estimate_facecam_box(gray_mean, face_px: tuple[float, float, float, float]):
    """Boîte (x0, y0, x1, y1) px de la facecam autour d'un visage + True si un bord a été aimanté.

    A priori visage x FACE_TO_BOX_W puis aimantation de chaque bord sur le gradient net le plus
    fort de sa zone (image moyenne : la bordure statique ressort, le gameplay se floute).
    """
    import numpy as np

    h_img, w_img = gray_mean.shape
    fx, fy, fw, fh = face_px
    cx, cy = fx + fw / 2, fy + fh / 2
    bw = fw * FACE_TO_BOX_W
    bh = max(bw * BOX_ASPECT, fh * 1.4)
    x0, x1 = max(0.0, cx - bw / 2), min(float(w_img), cx + bw / 2)
    y0, y1 = max(0.0, cy - bh / 2), min(float(h_img), cy + bh / 2)
    prior = (x0, y0, x1, y1)

    gx = np.abs(gray_mean[:, 2:] - gray_mean[:, :-2])  # gradient horizontal (colonne x+1)
    gy = np.abs(gray_mean[2:, :] - gray_mean[:-2, :])
    r0, r1 = int(y0 + 0.2 * (y1 - y0)), int(y1 - 0.2 * (y1 - y0))
    c0, c1 = int(x0 + 0.2 * (x1 - x0)), int(x1 - 0.2 * (x1 - x0))
    col_prof = gx[r0:r1].mean(axis=0) if r1 > r0 else np.zeros(w_img - 2)
    row_prof = gy[:, c0:c1].mean(axis=1) if c1 > c0 else np.zeros(h_img - 2)
    snapped = False
    hit = [False] * 4  # bords aimantés (x0, x1, y0, y1)

    def band(a: float, b: float, n: int) -> tuple[int, int]:
        return max(0, int(a)), min(n - 3, int(b))

    ext_w, ext_h = (x1 - x0), (y1 - y0)
    lo, hi = band(x0 - 0.5 * ext_w, min(x0 + 0.3 * ext_w, fx - 0.1 * fw), w_img)
    if (e := _snap_edge(col_prof, lo, hi, True)) is not None:
        x0, snapped, hit[0] = e + 1, True, True
    lo, hi = band(max(x1 - 0.3 * ext_w, fx + 1.1 * fw), x1 + 0.5 * ext_w, w_img)
    if (e := _snap_edge(col_prof, lo, hi, False)) is not None:
        x1, snapped, hit[1] = e + 1, True, True
    lo, hi = band(y0 - 0.5 * ext_h, min(y0 + 0.3 * ext_h, fy - 0.05 * fh), h_img)
    if (e := _snap_edge(row_prof, lo, hi, True)) is not None:
        y0, snapped, hit[2] = e + 1, True, True
    lo, hi = band(max(y1 - 0.3 * ext_h, fy + 1.05 * fh), y1 + 0.5 * ext_h, h_img)
    if (e := _snap_edge(row_prof, lo, hi, False)) is not None:
        y1, snapped, hit[3] = e + 1, True, True

    # cohérence : sinon retour à l'a priori sur l'axe fautif
    if not (1.4 * fw <= x1 - x0 <= 6 * fw):
        x0, x1 = prior[0], prior[2]
    if not (1.2 * fh <= y1 - y0 <= 6 * fh):
        y0, y1 = prior[1], prior[3]
    # collé au bord de l'image si proche
    if x0 < 0.025 * w_img and not hit[0]:
        x0 = 0.0
    if x1 > 0.975 * w_img and not hit[1]:
        x1 = float(w_img)
    if y0 < 0.025 * h_img and not hit[2]:
        y0 = 0.0
    if y1 > 0.975 * h_img and not hit[3]:
        y1 = float(h_img)
    return (x0, y0, x1, y1), snapped


def detect_facecam_in_frames(
    frames: list, corner: str = "auto", faces_fn=None
) -> FacecamDetection | None:
    """Cœur de la détection (frames BGR en mémoire) ; voir l'en-tête du module."""
    import cv2
    import numpy as np

    if not frames:
        return None
    faces_fn = faces_fn or _yunet_faces()
    if faces_fn is None:
        log.warning("facecam : YuNet indisponible")
        return None
    h_img, w_img = frames[0].shape[:2]
    candidates = CORNERS if corner == "auto" else (corner,)
    best: tuple[float, str, list] | None = None
    for c in candidates:
        rx, ry, rw, rh = _corner_region(c)
        px0, py0 = int(rx * w_img), int(ry * h_img)
        px1, py1 = int((rx + rw) * w_img), int((ry + rh) * h_img)
        dets = []
        for frame in frames:
            crop = frame[py0:py1, px0:px1]
            found = []
            for fx, fy, fw, fh, sc in faces_fn(crop, 480):
                x = px0 + fx * (px1 - px0)
                y = py0 + fy * (py1 - py0)
                w = fw * (px1 - px0)
                h = fh * (py1 - py0)
                cxm, cym = x + w / 2, y + h / 2
                if px0 <= cxm <= px1 and py0 <= cym <= py1 and 0.03 * w_img <= w <= 0.3 * w_img:
                    found.append((x, y, w, h, sc))
            dets.append(max(found, key=lambda f: f[4]) if found else None)
        present = [d for d in dets if d is not None]
        if not present:
            continue
        mx = _median([d[0] + d[2] / 2 for d in present])
        my = _median([d[1] + d[3] / 2 for d in present])
        mw = _median([d[2] for d in present])
        stable = [
            d
            for d in present
            if abs(d[0] + d[2] / 2 - mx) <= 0.75 * mw
            and abs(d[1] + d[3] / 2 - my) <= 0.75 * mw
            and 0.7 <= d[2] / mw <= 1.4
        ]
        fraction = len(stable) / len(frames)
        conf = fraction * sum(d[4] for d in stable) / max(len(stable), 1)
        need = FACECAM_MIN_FRACTION if corner == "auto" else FACECAM_FORCED_MIN_FRACTION
        log.debug("facecam %s : %d/%d frames stables", c, len(stable), len(frames))
        if fraction >= need and (best is None or conf > best[0]):
            best = (conf, c, stable)
    if best is None:
        return None
    conf, c, stable = best
    face = (
        _median([d[0] for d in stable]),
        _median([d[1] for d in stable]),
        _median([d[2] for d in stable]),
        _median([d[3] for d in stable]),
    )
    gray = np.mean([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames], axis=0)
    (x0, y0, x1, y1), snapped = estimate_facecam_box(gray, face)
    return FacecamDetection(
        corner=c,
        box=(x0 / w_img, y0 / h_img, (x1 - x0) / w_img, (y1 - y0) / h_img),
        face=((face[0] + face[2] / 2) / w_img, (face[1] + face[3] / 2) / h_img),
        confidence=round(conf, 3),
        box_source="edges" if snapped else "prior",
    )


def detect_facecam(
    source: Path, start: float, end: float, corner: str = "auto", n_frames: int = 8
) -> FacecamDetection | None:
    """Détecte la facecam d'un clip (None si absente) ; le résultat est loggé."""
    import cv2

    cap = cv2.VideoCapture(str(source))
    frames = []
    try:
        for k in range(n_frames):
            t = start + (end - start) * (k + 0.5) / n_frames
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if ok:
                frames.append(frame)
    finally:
        cap.release()
    result = detect_facecam_in_frames(frames, corner)
    if result is None:
        log.info(
            "facecam : aucune dans %s [%.1f-%.1f] (corner=%s)", source.name, start, end, corner
        )
    else:
        log.info("facecam %s [%.1f-%.1f] : %s", source.name, start, end, result)
    return result


# --------------------------------------------------------------------------- trajectoire


def compute_crop_path(
    source: Path,
    start: float,
    end: float,
    mode: str,
    smoothing: SmoothingSettings | None = None,
    mask_box: tuple[float, float, float, float] | None = None,
) -> CropPath | None:
    """Trajectoire lissée du centre de crop ; None (=> crop centré) si indisponible.

    `mask_box` (x, y, w, h normalisés) : zone ignorée par action_track (facecam).
    """
    try:
        if mode == "face_track":
            detect = _face_detector()
            if detect is None:
                log.warning("face_track : aucun détecteur de visage disponible, crop centré")
                return None
            raw, cuts = [], set()
            cd = CutDetector()
            for i, (t, f) in enumerate(_frames(source, start, end)):
                if cd.update(f):
                    cuts.add(i)
                raw.append((t, detect(f)))
        elif mode == "action_track":
            raw, cuts = _motion_centroids(source, start, end, mask_box)
        else:
            return None
    except Exception as e:
        log.warning("%s : échec du suivi (%s), crop centré", mode, e)
        return None
    if not raw or all(x is None for _, x in raw):
        log.warning("%s : rien à suivre dans %s, crop centré", mode, source)
        return None
    return smooth_camera(raw, smoothing, cuts)


def _motion_centroids(
    source: Path, start: float, end: float, mask_box=None
) -> tuple[list[tuple[float, float | None]], set[int]]:
    import cv2
    import numpy as np

    out: list[tuple[float, float | None]] = []
    cuts: set[int] = set()
    cd = CutDetector()
    prev = None
    for i, (t, frame) in enumerate(_frames(source, start, end)):
        is_cut = cd.update(frame)
        if is_cut:
            cuts.add(i)
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (9, 9), 0)
        if prev is not None and not is_cut:
            diff = cv2.absdiff(gray, prev).astype(np.float32)
            diff[diff < 20] = 0
            if mask_box is not None:
                h, w = diff.shape
                bx, by, bw, bh = mask_box
                diff[int(by * h) : int((by + bh) * h) + 1, int(bx * w) : int((bx + bw) * w) + 1] = 0
            col = diff.sum(axis=0)
            total = float(col.sum())
            out.append(
                (t, float((col * np.arange(len(col))).sum() / total / len(col)) if total else None)
            )
        else:
            out.append((t, None))
        prev = gray
    return out, cuts


# --------------------------------------------------------------------------- plan par clip


_FACECAM_CACHE: dict[tuple[str, str], FacecamDetection | None] = {}
FACECAM_SOURCE_FRAMES = 16  # frames échantillonnés sur toute la durée de la source


def clear_facecam_cache() -> None:
    _FACECAM_CACHE.clear()


def facecam_for_source(source: Path, corner: str = "auto") -> FacecamDetection | None:
    """Facecam d'une source entière, détectée une seule fois par `(source, corner)`.

    Tous les fragments d'une même source (autocut) partagent donc la même décision split/repli
    et la même boîte. Une exception n'est pas mise en cache.
    """
    key = (str(Path(source).resolve()), corner)
    if key not in _FACECAM_CACHE:
        from minavio.core import ffmpeg

        duration = ffmpeg.probe(Path(source)).duration
        _FACECAM_CACHE[key] = detect_facecam(
            Path(source), 0.0, duration, corner, n_frames=FACECAM_SOURCE_FRAMES
        )
    return _FACECAM_CACHE[key]


@dataclass
class ClipReframe:
    mode: str  # mode effectif (après repli éventuel de split_facecam)
    path: CropPath | None = None
    facecam: FacecamDetection | None = None
    zoom: float = 1.0
    split_ratio: float = 0.35


def plan_clip(
    source: Path,
    start: float,
    end: float,
    mode: str,
    zoom: float = 1.0,
    framing: FramingSettings | None = None,
) -> ClipReframe:
    """Analyse un clip : trajectoire de caméra, facecam (split_facecam) ou repli loggé."""
    if framing is None:
        from minavio.config.schema import FramingSettings

        framing = FramingSettings()
    cfg = framing.facecam
    plan = ClipReframe(mode=mode, zoom=zoom, split_ratio=cfg.split_ratio)
    if mode == "split_facecam":
        try:
            plan.facecam = facecam_for_source(source, cfg.corner)
        except Exception as e:
            log.warning("split_facecam : échec de la détection (%s)", e)
        if plan.facecam is None:
            log.warning(
                "split_facecam : aucune facecam dans %s [%.1f-%.1f], repli sur %s",
                source.name,
                start,
                end,
                cfg.fallback,
            )
            plan.mode = cfg.fallback
        else:
            plan.path = compute_crop_path(
                source, start, end, "action_track", framing.smoothing, plan.facecam.box
            )
            return plan
    if plan.mode in {"face_track", "action_track"}:
        plan.path = compute_crop_path(source, start, end, plan.mode, framing.smoothing)
    return plan

"""Reframe : filtres FFmpeg pour passer d'une source 16:9 à un format de sortie (ex. 9:16).

- `none`        : mise à l'échelle + bandes noires si le ratio diffère.
- `blur_pad`    : fond = même vidéo agrandie + boxblur, premier plan centré.
- `face_track`  : crop suivant le visage (MediaPipe, sinon Haar OpenCV, sinon centre).
- `action_track`: crop suivant le centre de mouvement (OpenCV), sinon centre.
"""

from __future__ import annotations

import itertools
import logging
from pathlib import Path

log = logging.getLogger("minavio.reframe")

CropPath = list[tuple[float, float]]  # (t relatif au début du clip en s source, centre x 0..1)

SAMPLE_FPS = 4.0
MAX_PATH_POINTS = 40


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


def crop_x_expression(path: CropPath, src_w: int, crop_w: int) -> str:
    """Expression FFmpeg (variable `t`) : x du crop, interpolation linéaire entre les points."""
    pts = [(t, min(max(cx * src_w - crop_w / 2, 0), src_w - crop_w)) for t, cx in path]
    if len(pts) == 1:
        return f"{pts[0][1]:.1f}"
    expr = f"{pts[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(itertools.pairwise(pts))):
        span = max(t1 - t0, 1e-3)
        expr = f"if(lt(t,{t1:.3f}),{x0:.1f}+({x1 - x0:.1f})*(t-{t0:.3f})/{span:.3f},{expr})"
    return expr


def track_filter(
    in_label: str,
    out_label: str,
    src_w: int,
    src_h: int,
    out_w: int,
    out_h: int,
    path: CropPath | None,
) -> list[str]:
    """Crop au ratio de sortie (centré si path est None) puis scale."""
    target = out_w / out_h
    if src_w / src_h <= target:  # source plus étroite : crop vertical centré
        cw, ch = src_w, _even(src_w / target)
        cx, cy = "0", f"{(src_h - ch) / 2:.0f}"
    else:
        cw, ch = _even(src_h * target), src_h
        cx = crop_x_expression(path, src_w, cw) if path else f"{(src_w - cw) / 2:.0f}"
        cy = "0"
    return [
        f"[{in_label}]crop=w={cw}:h={ch}:x='{cx}':y={cy},scale={out_w}:{out_h},setsar=1[{out_label}]"
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
) -> list[str]:
    if mode == "blur_pad":
        return blur_pad_filter(in_label, out_label, out_w, out_h, tag)
    if mode in {"face_track", "action_track"}:
        return track_filter(in_label, out_label, src_w, src_h, out_w, out_h, path)
    return scale_pad_filter(in_label, out_label, out_w, out_h)


# --------------------------------------------------------------------------- trajectoire


def smooth_path(raw: list[tuple[float, float | None]], alpha: float = 0.25) -> CropPath:
    """Comble les trous, filtre médian (3) puis lissage exponentiel, décime à MAX_PATH_POINTS."""
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


def _face_detector():
    """Renvoie une fonction frame -> centre x normalisé | None, ou None si rien de dispo."""
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


def compute_crop_path(source: Path, start: float, end: float, mode: str) -> CropPath | None:
    """Trajectoire lissée du centre de crop ; None (=> crop centré) si indisponible."""
    try:
        if mode == "face_track":
            detect = _face_detector()
            if detect is None:
                log.warning("face_track : aucun détecteur de visage disponible, crop centré")
                return None
            raw = [(t, detect(f)) for t, f in _frames(source, start, end)]
        elif mode == "action_track":
            raw = _motion_centroids(source, start, end)
        else:
            return None
    except Exception as e:
        log.warning("%s : échec du suivi (%s), crop centré", mode, e)
        return None
    if not raw or all(x is None for _, x in raw):
        log.warning("%s : rien à suivre dans %s, crop centré", mode, source)
        return None
    return smooth_path(raw)


def _motion_centroids(source: Path, start: float, end: float) -> list[tuple[float, float | None]]:
    import cv2
    import numpy as np

    out: list[tuple[float, float | None]] = []
    prev = None
    for t, frame in _frames(source, start, end):
        gray = cv2.GaussianBlur(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (9, 9), 0)
        if prev is not None:
            diff = cv2.absdiff(gray, prev).astype(np.float32)
            diff[diff < 20] = 0
            col = diff.sum(axis=0)
            total = float(col.sum())
            out.append(
                (t, float((col * np.arange(len(col))).sum() / total / len(col)) if total else None)
            )
        else:
            out.append((t, None))
        prev = gray
    return out

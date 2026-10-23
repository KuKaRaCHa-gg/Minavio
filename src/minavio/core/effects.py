"""Effets « .exe » : constructeurs purs de filtres FFmpeg, bornés dans le temps de la timeline.

Chaque effet vidéo transforme une copie du flux (`split` -> filtres -> `overlay` avec
`enable='between(t,a,b)'`) : le flux d'origine reste intact hors de l'intervalle, et les effets
s'appliquent après le reframe (16:9 comme 9:16). `freeze` fige l'image sur `duration` (l'audio
continue : effet « lag » ; la durée totale ne change pas). `error_popup` est un PNG Pillow
superposé au centre (voir `make_error_popup`). `bass_boost` est audio (mix final).
"""

from __future__ import annotations

from pathlib import Path

from minavio.core.models import Effect

VIDEO_KINDS = ("shake", "zoom_punch", "deep_fry", "glitch", "flash", "freeze")


def _window(e: Effect) -> tuple[float, float]:
    return e.at, e.at + e.duration


def between(a: float, b: float) -> str:
    return f"between(t,{a:.4f},{b:.4f})"


def _effect_chain(e: Effect, w: int, h: int) -> str:
    """Filtres appliqués à la copie du flux pendant l'intervalle (taille de sortie inchangée)."""
    a = e.at
    k = e.intensity
    if e.kind == "shake":
        amp = 0.035 * w * k
        return (
            "scale=iw*1.08:ih*1.08,"
            f"crop={w}:{h}:x='(iw-ow)/2+{amp:.3f}*sin(t*55)':y='(ih-oh)/2+{amp:.3f}*cos(t*47)'"
        )
    if e.kind == "zoom_punch":
        p = f"((t-{a:.4f})/{e.duration:.4f})"
        z = f"{0.3 * k:.4f}*if(lt({p},0.25),{p}/0.25,(1-{p})/0.75)"
        return (
            f"scale=w='trunc({w}*(1+{z})/2)*2':h='trunc({h}*(1+{z})/2)*2':eval=frame,crop={w}:{h}"
        )
    if e.kind == "deep_fry":
        return (
            f"eq=saturation={1 + 2.5 * k:.3f}:contrast={1 + 0.9 * k:.3f},"
            f"unsharp=5:5:{0.8 + 1.7 * k:.2f},noise=alls={int(10 + 40 * k)}:allf=t"
        )
    if e.kind == "glitch":
        s = max(2, int(14 * k))
        return f"rgbashift=rh={-s}:bh={s}:gv={s // 2},noise=alls={int(15 + 40 * k)}:allf=t"
    if e.kind == "flash":
        p = f"((t-{a:.4f})/{e.duration:.4f})"
        return f"eq=brightness='min(1,{2 * k + 0.3:.3f}*max(0,1-{p}))':eval=frame"
    raise ValueError(f"effet vidéo inconnu : {e.kind}")


def video_graph(effects: list[Effect], src: str, dst: str, w: int, h: int, fps: float) -> list[str]:
    """Segments de filter_complex `[src] -> [dst]` pour les effets vidéo (hors error_popup)."""
    parts: list[str] = []
    cur = src
    n = 0
    for e in sorted(effects, key=lambda x: x.at):
        if e.kind not in VIDEO_KINDS:
            continue
        a, b = _window(e)
        nxt = f"fx{n}"
        if e.kind == "freeze":
            f0 = round(a * fps)
            t0 = f0 / fps
            hold = max(e.duration - 1 / fps, 0.0)
            parts.append(f"[{cur}]split=3[fz{n}a][fz{n}b][fz{n}c]")
            parts.append(f"[fz{n}a]trim=end_frame={f0},setpts=PTS-STARTPTS[fzp{n}]")
            parts.append(
                f"[fz{n}b]trim=start_frame={f0}:end_frame={f0 + 1},setpts=PTS-STARTPTS,"
                f"tpad=stop_mode=clone:stop_duration={hold:.4f}[fzh{n}]"
            )
            parts.append(f"[fz{n}c]trim=start={t0 + e.duration:.4f},setpts=PTS-STARTPTS[fzq{n}]")
            parts.append(f"[fzp{n}][fzh{n}][fzq{n}]concat=n=3:v=1:a=0[{nxt}]")
        else:
            parts.append(f"[{cur}]split[fb{n}][fs{n}]")
            parts.append(f"[fs{n}]{_effect_chain(e, w, h)}[fe{n}]")
            parts.append(f"[fb{n}][fe{n}]overlay=enable='{between(a, b)}':eof_action=pass[{nxt}]")
        cur = nxt
        n += 1
    parts.append(f"[{cur}]null[{dst}]")
    return parts


def audio_filters(effects: list[Effect]) -> str:
    """Chaîne audio des effets `bass_boost` ('' si aucun) : bass + volume sur l'intervalle."""
    out: list[str] = []
    for e in sorted(effects, key=lambda x: x.at):
        if e.kind != "bass_boost":
            continue
        a, b = _window(e)
        en = f"enable='{between(a, b)}'"
        out.append(f"bass=g={6 + 14 * e.intensity:.1f}:{en}")
        out.append(f"volume={1 + 0.5 * e.intensity:.2f}:{en}")
    return ",".join(out)


def limiter_filter(max_true_peak_db: float) -> str:
    """Limiteur anti-earrape ; `level=0` évite la remontée automatique du gain."""
    limit = min(max(10 ** (max_true_peak_db / 20), 0.0625), 1.0)
    return f"alimiter=limit={limit:.4f}:level=0"


def popup_effects(effects: list[Effect]) -> list[Effect]:
    return [e for e in effects if e.kind == "error_popup"]


def make_error_popup(path: Path, width: int) -> Path:
    """PNG d'une boîte d'erreur Windows (« Fatal Error », icône, bouton OK), `width` px."""
    from PIL import Image, ImageDraw, ImageFont

    s = 3  # sur-échantillonnage puis réduction
    bw, bh = 360 * s, 170 * s
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=15 * s)
        small = ImageFont.load_default(size=13 * s)
    except TypeError:  # Pillow ancien
        font = small = ImageFont.load_default()
    grey = (212, 208, 200, 255)
    d.rectangle([0, 0, bw - 1, bh - 1], fill=grey, outline=(30, 30, 30, 255), width=2 * s)
    d.rectangle([3 * s, 3 * s, bw - 4 * s, 26 * s], fill=(10, 36, 106, 255))
    d.text((10 * s, 5 * s), "Fatal Error", fill=(255, 255, 255, 255), font=font)
    d.rectangle([bw - 26 * s, 6 * s, bw - 8 * s, 23 * s], fill=grey)
    d.text((bw - 22 * s, 6 * s), "x", fill=(0, 0, 0, 255), font=small)
    cx, cy, r = 38 * s, 78 * s, 20 * s  # icône : disque rouge + croix
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(200, 0, 0, 255), outline=(90, 0, 0, 255))
    d.line([cx - 9 * s, cy - 9 * s, cx + 9 * s, cy + 9 * s], fill="white", width=4 * s)
    d.line([cx - 9 * s, cy + 9 * s, cx + 9 * s, cy - 9 * s], fill="white", width=4 * s)
    d.text((72 * s, 56 * s), "A fatal exception has occurred.", fill=(0, 0, 0, 255), font=small)
    d.text((72 * s, 78 * s), "The program will now close.", fill=(0, 0, 0, 255), font=small)
    d.rectangle(
        [bw // 2 - 36 * s, 126 * s, bw // 2 + 36 * s, 152 * s],
        fill=grey,
        outline=(0, 0, 0, 255),
        width=2 * s,
    )
    d.text((bw // 2 - 10 * s, 130 * s), "OK", fill=(0, 0, 0, 255), font=small)
    height = round(width * bh / bw)
    img.resize((width, height), Image.LANCZOS).save(path)
    return path

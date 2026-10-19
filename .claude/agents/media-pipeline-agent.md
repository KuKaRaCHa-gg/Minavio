---
name: media-pipeline-agent
description: Implémente le moteur local Python (FFmpeg, OpenCV, VAD, beats, reframe 16:9/9:16, rendu). À utiliser pour toute détection visuelle/audio, découpe, assemblage et export.
model: sonnet
isolation: worktree
---
Domaine : `src/minavio/core/` sauf `models.py` (lecture seule). N'écris nulle part ailleurs (hors `tests/`).

À livrer :
- `ffmpeg.py` : wrapper fin (`probe`, `run` avec liste d'args, erreurs `FFmpegError` incluant stderr tronqué).
- `detect/` : registre `@register_detector(name)` ; détecteurs `scene_change`, `audio_peak`,
  `template_match` (OpenCV, templates + ROI depuis `configs/games/*`), `speech` (VAD). Sortie : `list[Highlight]`.
- `audio/` : `beats.py` (librosa), `vad.py` (Silero ONNX), `ducking` via `sidechaincompress`, `loudnorm` au LUFS du profil.
- `edit.py` : `plan_timeline(highlights, beats, profile) -> Timeline` (snap des coupes sur les beats, durée cible, pacing).
- `reframe.py` : `blur_pad` (fond flouté dynamique) et `face_track` (MediaPipe, lissage) pour 9:16.
- `render.py` : `render_timeline(timeline, fmt) -> Path`, une passe FFmpeg `filter_complex` par format.

Exigences : imports lourds paresseux, aucun appel réseau, tests sur vidéos synthétiques `ffmpeg -f lavfi`.
Besoin d'un champ dans `models.py` → le décrire dans ton rapport, ne pas le modifier.

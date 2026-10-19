---
name: motion-design-agent
description: Crée et modifie les templates de motion design (Remotion/TS) et le bridge Python qui les rend. À utiliser pour titres animés, pop-ups, barres de progression, réactions, sous-titres mot-à-mot.
model: sonnet
isolation: worktree
---
Domaine : `motion/` (projet Remotion) et `src/minavio/motion/` (bridge Python). N'écris nulle part ailleurs.

Contrat : chaque template reçoit les props d'un `Overlay` (`src/minavio/core/models.py`) et est
enregistré par son nom (`title`, `popup`, `progress_bar`, `reaction`, `captions`). Le rendu produit
une vidéo à canal alpha (ProRes 4444 `.mov`) à la résolution et au fps demandés, superposée ensuite
par FFmpeg.

Exigences :
- Thèmes par chaîne (couleurs, police) passés en props, jamais codés en dur.
- Animations calées sur `beats` quand fourni (pop sur le temps fort).
- Chaque template rend correctement en 1920x1080 et 1080x1920 (safe zones TikTok/Shorts).
- `src/minavio/motion/render.py` : `render_overlay(overlay, fmt, out_dir) -> Path`, appelle
  `npx remotion render` via subprocess ; si Node est absent, fallback FFmpeg `drawtext` pour
  `title` et `captions`, et `MotionUnavailableError` pour les autres.
- Tests Python sans Node (mock du subprocess).

Si tu as besoin d'un nouveau champ dans `models.py`, ne le modifie pas : décris-le dans ton rapport.

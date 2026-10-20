# Veille outils — octobre 2026 (proposition de lot 5)

Critères : local, gratuit, licence compatible usage commercial, gain concret pour **Minivitch**
(puis Byte Tricks / The Lost Chat). Les licences marquées « à vérifier » doivent être relues dans
le fichier LICENSE du dépôt avant intégration (poids de modèles parfois sous d'autres termes).

## Priorité A — fort impact, faible risque

| Outil | Licence | Ce que ça apporte à Minavio | Intégration |
| :--- | :--- | :--- | :--- |
| **RapidOCR** (modèles PaddleOCR en ONNX) | Apache-2.0 (à vérifier) | Lire le **killfeed** d'Apex (« KNOCKED », « ELIMINATED », pseudos) : détection des kills **sans captures PNG à fournir**, robuste aux changements d'interface ; meilleur qu'EasyOCR sur du texte vidéo dans un benchmark 2025 | nouveau détecteur `killfeed_ocr`, ROI du killfeed dans `game.yaml` |
| **Practical-RIFE** | MIT | **Vrais ralentis fluides** sur le kill décisif (interpolation d'images) au lieu d'images dupliquées ; repli sans GPU : filtre FFmpeg `minterpolate` | rendu : `Clip.speed < 1` → interpolation du segment |
| **Demucs** (htdemucs) | MIT | Séparer **voix / musique / effets du jeu** : retirer la musique du lobby (risque Content ID), ducking plus propre, VAD plus fiable | étape audio optionnelle avant le mix |
| **DeepFilterNet** | MIT ou Apache-2.0 | **Débruiter le micro** (ventilo, clavier) — surtout Byte Tricks ; repli intégré FFmpeg `arnndn` (RNNoise) | filtre audio par profil |
| **YouTube Data API v3** (`google-api-python-client`) | Apache-2.0 | **Publication automatique** : upload, titre/description/tags/chapitres générés, miniature, publication programmée (`status.publishAt`), Shorts ; quota 2026 : `videos.insert` dans son propre compartiment (≈ 100 uploads/jour), `thumbnails.set` ≈ 50 unités | commande `minavio publish` + onglet UI, OAuth local |

## Priorité B — utile, à valider sur de vrais rushes

| Outil | Licence | Apport | Remarque |
| :--- | :--- | :--- | :--- |
| **Real-ESRGAN** | BSD-3-Clause | Upscale ×2 des images recadrées pour les miniatures (moins floues) | GPU conseillé |
| **rembg / BiRefNet** | à vérifier | Détourer le personnage pour les miniatures | éviter RMBG-2.0 (non commercial) |
| **TwitchDownloaderCLI** | à vérifier | Récupérer automatiquement le chat d'une VOD (JSON) → détecteur `chat_spike` | The Lost Chat |
| **Revideo** | MIT | Alternative à Remotion si la structure dépasse 3 personnes (licence Remotion payante au-delà) | maintenance à surveiller |
| **GitHub Models** (via ton abonnement Copilot) | service | Fournisseur LLM `github` (texte + images), gratuit dans les limites de la preview (~8k tokens d'entrée/requête, quelques req/min) | intégré au lot en cours, plutôt en secours |

## Écartés

- **EasyOCR** : moins précis, dernière version 2024.
- **Spleeter** : abandonné (TensorFlow 1.x).
- **SeedVR2** (upscale) : poids non commerciaux.
- **Ultralytics YOLO** : AGPL-3.0.
- **Outils cloud commerciaux** (Eklipse, DOR, Krisp) : payants, données envoyées hors de ta machine.

## GitHub Copilot côté développement

En plus du fournisseur `github`, l'agent Copilot peut prendre des tâches du dépôt
via des issues GitHub (une issue = une PR proposée par Copilot, relue ensuite ici).
Idéal pour les tâches isolées : nouveau jeu (`configs/games/<jeu>/`), traductions, docs.

## Sources

- OCR : [comparatif 2026](https://imagetotable.ai/blog/best-open-source-ocr-tools-2026),
  [OCR sur vidéo (arXiv 2502.06445)](https://arxiv.org/html/2502.06445v1),
  [PaddleOCR vs EasyOCR](https://www.codesota.com/ocr/paddleocr-vs-easyocr)
- Interpolation : [Practical-RIFE](https://github.com/hzwer/practical-rife),
  [classements VFI](https://github.com/AIVFI/Video-Frame-Interpolation-Rankings-and-Video-Deblurring-Rankings)
- Séparation audio : [Demucs](https://github.com/facebookresearch/demucs)
- Débruitage : [DeepFilterNet](https://github.com/rikorose/deepfilternet),
  [comparatif RNNoise / DeepFilterNet](https://www.forasoft.com/learn/ai-for-video-engineering/articles-ai/real-time-noise-suppression-krisp-rnnoise-deepfilternet)
- YouTube : [videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert),
  [quotas 2026](https://www.postzen.dev/blog/youtube-api-quota-exceeded)
- Upscale : [Real-ESRGAN](https://awesome.ecosyste.ms/projects/github.com/xinntao/Real-ESRGAN)
- Motion : [Remotion vs Motion Canvas vs Revideo](https://www.pkgpulse.com/guides/remotion-vs-motion-canvas-vs-revideo-programmatic-video-2026)
- Chat Twitch : [TwitchDownloader](https://github.com/ScrubN/TwitchDownloader)
- GitHub Models : [limites (discussion)](https://github.com/orgs/community/discussions/137298),
  [revue 2026](https://toolchew.com/en/review-github-models-2026/)

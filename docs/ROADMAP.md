# Minavio — Phase 4 : plan d'amélioration (à valider)

Socle de départ : `main` @ `d3cffde` (pipeline complet, 100 tests verts).
Contraintes conservées : 100 % local, RTX 5070 Ti 16 Go, licences compatibles usage commercial,
dépendances lourdes derrière des extras, chaque nouveauté = un détecteur / une étape du pipeline
qui écrit son JSON dans `work/<job>/`.

## Vue d'ensemble

| # | Module | Outils retenus | Licence | GPU | Gain principal | Agent |
| :- | :--- | :--- | :--- | :--- | :--- | :--- |
| A | Autocut intelligent | Silero VAD (déjà là) + RMS numpy + mots Whisper | MIT | non | -20 à -40 % de durée sur Byte Tricks / Lost Chat sans couper de réplique | media-pipeline |
| B | Détection « hype » | PANNs CNN14 (AudioSet), prosodie librosa, emotion2vec+ | MIT / MIT / à vérifier | ~2 Go | Rires, cris, explosions, voix qui monte → meilleurs moments | media-pipeline + local-llm |
| C | ASR aligné | WhisperX (faster-whisper + wav2vec2) ; pyannote en option | BSD / MIT (pyannote : modèles « gated » HF) | ~4 Go | Timestamps mot-à-mot précis → sous-titres et coupes au cordeau | media-pipeline |
| D | Miniatures | Score OpenCV + YuNet, imagehash, Qwen3-VL 8B (Ollama), rembg/BiRefNet, Remotion `<Still>` | Apache / BSD / Apache / MIT | ~6 Go | 3 vignettes pixel-arcade prêtes (16:9 + 9:16) par vidéo | motion + local-llm + media-pipeline |
| E | Auto-framing v2 | Layout « split facecam », One Euro filter, RF-DETR (base/nano) + ByteTrack (`trackers`/`supervision`) | Apache / MIT | ~1 Go | Shorts qui gardent l'action **et** la tête du streamer | media-pipeline |

Écartés : **Ultralytics YOLO** (AGPL-3.0, contaminant pour un outil distribué), **RMBG-2.0**
(non commercial), **RF-DETR XL/2XL** (licence PML séparée), **auto-editor** comme dépendance
(excellent outil de référence, mais il produit un fichier monté ; on veut des coupes dans notre
`Timeline` pour garder beat-sync, captions et overlays alignés).

---

## A. Autocut / suppression intelligente des silences — `core/autocut.py`

Principe : on ne coupe **que** ce qui n'est ni de la voix ni de l'action.

1. Trame de 50 ms : énergie RMS (dBFS) + probabilité de voix Silero (déjà installé).
2. Seuil adaptatif par clip (percentile 20 de l'énergie + marge), pas un seuil fixe : les sons de jeu
   d'Apex ne sont pas du silence, un micro calme non plus.
3. Un creux devient coupable s'il dure ≥ `min_silence_s` (0,6 s calm → 0,35 s hyper).
4. Protection des répliques : marges `0,12 s` avant / `0,25 s` après la voix, et **jamais** de coupe
   à l'intérieur d'un mot (snap sur les timestamps de C).
5. Option *punch-in* : zoom alterné 100 % / 108 % à chaque jump-cut pour masquer la coupe (Byte Tricks).

Contrat : un `Clip` est éclaté en sous-clips ; rien à changer dans `models.py`.
Profil : `editing.autocut { enabled, min_silence_s, margin_before_s, margin_after_s, punch_in }` —
activé fort sur The Lost Chat, moyen sur Byte Tricks, léger sur Minivitch (le jeu fait du bruit).

## B. Détection d'émotions / moments « hype » — détecteur `hype`

Fenêtres de 0,5 s, chaque signal normalisé par rapport au clip (z-score), puis fusion pondérée :

| Signal | Outil | Pourquoi |
| :--- | :--- | :--- |
| Événements sonores : *Laughter, Screaming, Shout, Cheering, Gunshot, Explosion* | **PANNs CNN14** (`panns_inference`, 527 classes AudioSet) | Rires et cris = meilleur indicateur de moment fort, indépendant de la langue |
| Prosodie : volume, hauteur (pitch `librosa.pyin`), débit de parole | librosa + mots Whisper | Gratuit, capte la voix qui monte avant le clutch |
| Émotion vocale (surprise, joie, colère) | **emotion2vec+** (FunASR, 9 classes) | Distingue un cri de joie d'un cri de rage (labels plus riches) |
| *(Option Lost Chat)* débit du chat Twitch | export JSON de TwitchDownloader / chat-downloader | Le pic de messages/s est LE signal des best-of streamers |

Sorties : `Highlight` du détecteur `hype` + **transcript annoté** passé au Qwen
(`[RIRE] non mais attends [CRI]`), ce qui améliore fortement son classement et ses hooks.
Contrat : nouveau modèle `AudioEvent(_TimeSpan){label, confidence}` et `Highlight.signals: dict[str, float]`
(explicabilité : pourquoi ce moment a été retenu).

## C. ASR aligné — backend `whisperx` dans `core/asr.py`

WhisperX = faster-whisper (déjà là) + alignement forcé wav2vec2 → timestamps par mot nettement plus
fiables (sous-titres pixel qui « tombent » pile, autocut sans couper les mots).
Diarization pyannote **optionnelle** (jeton Hugging Face requis) : utile pour The Lost Chat
(plusieurs streamers → couleur de sous-titre par locuteur). `MINAVIO_ASR_BACKEND=faster-whisper|whisperx`.

## D. Miniatures automatiques — nouveau module `minavio/thumbnail/` + `minavio thumbnail <job>`

1. **Candidats** : frames à 2 fps dans les 5 meilleurs highlights.
2. **Pré-score OpenCV** (rapide, CPU) : netteté (variance du Laplacien), visage présent / taille /
   ouverture de bouche (YuNet déjà embarqué), mouvement, contraste ; doublons retirés par `imagehash`.
3. **Juge VLM local** : les 12 meilleures → **Qwen3-VL 8B** via Ollama (même API que le LLM texte)
   choisit 3 frames et propose un texte de 2–4 mots. Chargé *après* le Qwen 30B (`keep_alive: 0`).
4. **Détourage** du streamer / du personnage : `rembg` avec BiRefNet-general (ou u2net en repli).
5. **Composition** : template Remotion `thumbnail` (rendu `<Still>`), thème pixel-arcade de la chaîne,
   1280×720 (YouTube) + 1080×1920 (couverture Shorts/TikTok), 3 variantes pour le « Test & Compare » YouTube.

## E. Auto-framing v2 — `core/reframe.py`

1. **Layout « split facecam »** (le format standard des clips de streamers) : facecam repérée une fois par
   YuNet dans un coin → bas 35 % = facecam, haut 65 % = gameplay. Profil `reframe: split_facecam`.
   Gros gain immédiat pour Minivitch et The Lost Chat.
2. **Lissage de caméra** : One Euro filter + zone morte + vitesse de pan max, remise à zéro sur
   changement de scène (détecteur déjà là) → plus d'effet « caméra ivre ».
3. **Suivi du sujet** : RF-DETR (poids base/nano Apache) + ByteTrack (`trackers` Roboflow, Apache) pour
   garder le *même* personnage/joueur malgré les occlusions ; pour les FPS, combinaison avec les
   positions des événements HUD (killfeed) et le flux optique.
4. *(Plus tard)* détection du locuteur actif (Light-ASD) pour cadrer celui qui parle.

---

## Budget GPU (16 Go)

Les étapes GPU sont **séquentielles** et libèrent la mémoire entre elles (`torch.cuda.empty_cache`,
Ollama `keep_alive: 0`) : WhisperX ~4 Go → PANNs + emotion2vec ~2 Go → RF-DETR ~1 Go → Qwen 30B
(offload partiel) → Qwen3-VL 8B ~6 Go. Jamais deux gros modèles chargés en même temps.

## Ordre proposé (lots)

| Lot | Contenu | Pourquoi d'abord | Agents en parallèle |
| :--- | :--- | :--- | :--- |
| **4.1** | A (autocut) + E1/E2 (split facecam, lissage) + B sans emotion2vec (PANNs + prosodie) | Gains visibles immédiats, risque faible | media-pipeline ×2 (worktrees séparés : `autocut`/`hype` et `reframe`), cli-config (profils) |
| **4.2** | C (WhisperX) + emotion2vec + transcript annoté pour le Qwen | Améliore captions, coupes et classement | media-pipeline, local-llm |
| **4.3** | D (miniatures) | Nouveau livrable | motion, local-llm, media-pipeline |
| **4.4** | E3 (RF-DETR + ByteTrack), signal chat Twitch | Plus lourd, à mesurer sur vrais rushes | media-pipeline, cli-config |

Chaque lot : contrat figé par l'orchestrateur → agents en worktrees → revue `orchestrator` → merge →
test e2e étendu → push. `main` n'avance que sur un lot validé.

## Questions avant de lancer le lot 4.1

1. Tes clips Minivitch / Lost Chat ont-ils une **facecam** (et dans quel coin, en général) ?
2. As-tu accès aux **logs de chat** des VODs pour The Lost Chat (sinon on ignore ce signal) ?
3. OK pour un **jeton Hugging Face** (diarization pyannote) ou on s'en passe ?
4. Ordre des lots ci-dessus OK, ou miniatures en priorité ?

---

## État — lot 4.1 livré (sur `main`)

- Autocut (`core/autocut.py`) : plancher de silence absolu −45 dBFS, mots transcrits protégés, interjections
  courtes fusionnées ; activé fort sur The Lost Chat, moyen sur Byte Tricks, léger sur Minivitch.
- Détecteurs `hype` (PANNs + prosodie, `signals` conservés à la fusion) et `chat_spike` (log de chat
  optionnel à côté du clip).
- Cadrage : One Euro + zone morte + pan borné + reset aux cuts ; `split_facecam` pour The Lost Chat
  (coin auto, une détection par source, repli `face_track`) ; punch-in.
- Rendu : seek en entrée par clip (~22× plus rapide sur des fragments loin dans la source).
- Revue `orchestrator` : 7 bugs trouvés, tous corrigés avec tests de non-régression.

Reporté au lot 4.2 : `speech_rate_z` du détecteur hype (nécessite la transcription avant la détection).

## État — lot Minivitch livré (sur `main`)

Outils open source évalués : PySceneDetect (BSD-3, **intégré** : `scene_change` AdaptiveDetector),
Kenney (CC0, prévu ; banque embarquée procédurale CC0 faute d'accès réseau, remplaçable via
`audio.sfx.library_dir`), ClipsAI (MIT, écarté : cadrage podcast), vidcom (YOLO, licence non vérifiée,
écarté), Eklipse / DOR (commerciaux cloud, écartés).

- Cold open (`editing.cold_open`) centré sur l'instant décisif (`signals.peak_t`) + transition `rewind`.
- Ordre `countdown` + `rank_card` → profil `minivitch_top10`.
- SFX (`audio.sfx`) : whoosh entre moments, hit/boom à l'instant du kill, rewind synchronisé.
- `kill_counter` pixel calé sur le kill ; chapitres YouTube (Qwen, règles YouTube) ; Shorts par moment
  (`shorts.per_highlight`, `--shorts N`).
- Contrat : `Clip.moment` / `Clip.peak_t`, `Timeline.moments()` par identifiant, `SfxCue`, `Chapter`.
- Revue `orchestrator` : 7 points, tous corrigés (moments, popups dans le cold open, SFX au kill,
  rewind synchronisé, peak_t fusionné, 2e hit, scenedetect ≥ 0.7).

## État — miniatures, multi-LLM, base de connaissances et lot 5 livrés (sur `main`)

- Miniatures (`thumbnails`), chaîne LLM multi-fournisseurs (local par défaut ; Claude, Gemini,
  GitHub Models, OpenAI-compatible en option, clés hors dépôt), interface Gradio.
- Base de connaissances locale (BM25, ~85 fiches) injectée dans rank/copy/chapitres/miniatures/vision ;
  fiches par **genre** (battle royale, tactique, souls-like, horreur, coop PvE, roguelike, sandbox,
  MOBA, combat, sport/course, extraction) filtrées par `GameProfile.genre` / `modes`.
- Détection autonome : `killfeed_ocr` (RapidOCR, Apache-2.0, ROI trouvée seule), `motion_intensity`
  (flux optique, secousses, flashs), `vision_scan` (VLM local, cloud seulement si `allow_cloud` +
  `vision_model`).
- Audio : `audio_clean` (Demucs pour retirer la musique du jeu, DeepFilterNet / arnndn), rejoué à
  chaque run (cache par source et réglages).
- Ralenti au kill (`editing.slowmo`) : rife-ncnn-vulkan (MIT) ou `minterpolate`.
- Publication YouTube manuelle (`minavio publish`, onglet 📤), idempotente ; une vidéo programmée
  est forcément `public` à l'heure dite.
- Revue `orchestrator` du lot 5 : 9 points, 8 corrigés (`--no-llm` et vision_scan, Shorts vs
  compilation 9:16, programmation/confidentialité, reframe en mode RIFE, reprise d'audio_clean,
  cache vision_scan, ROI validées, arrêt anticipé ffmpeg) ; le 9e est informatif (poids Demucs
  téléchargés au premier usage, message d'erreur explicite).
- Profils de jeux supplémentaires (PvP, PvE, solo, coop) : PR Copilot en cours.

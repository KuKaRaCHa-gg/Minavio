<!-- kb: tasks= -->
# Bibliothèques et outils

## Comment lire ce catalogue
Ce catalogue complète `docs/RESEARCH.md` (veille d'octobre 2026, priorités A/B et outils écartés) sans le dupliquer. Statuts : **intégré** (dépendance ou extra du projet), **contrat posé** (prévu dans le lot 5, voir RESEARCH), **candidat** (à évaluer sur de vrais rushes), **écarté** (licence ou qualité). Les licences viennent des dépôts ou des fiches modèles ; les poids de modèles ont parfois d'autres termes que le code : relire le fichier LICENSE avant d'intégrer. Dernière revue : octobre 2026.

## OCR et lecture d'interface
- **RapidOCR** (modèles PaddleOCR en ONNX), Apache-2.0 : lecture du killfeed, contrat posé (détecteur `killfeed_ocr`).
- **OpenCV template matching**, Apache-2.0 : bannières fixes (champion, knock), intégré (extra `vision`).
- **EasyOCR**, Apache-2.0 : écarté (moins précis sur le texte vidéo, moins entretenu).
- **Tesseract**, Apache-2.0 : candidat de repli, mauvais sur le texte de jeu stylisé.
Astuce : binariser et agrandir la zone du killfeed, ne lire qu'un ROI, filtrer par pseudo connu.

## Détection d'événements et de scènes
- **PySceneDetect** (`scenedetect`), BSD-3-Clause : changements de plan, intégré (extra `vision`).
- **MediaPipe**, Apache-2.0 : visages et suivi de cadrage, intégré.
- **PANNs / panns-inference** et **YAMNet** (AudioSet), Apache-2.0 / MIT à vérifier : classification d'événements sonores (rire, cris, applaudissements, tirs). PANNs intégré (extra `hype`), YAMNet candidat.
- **librosa**, ISC : pics d'énergie, tempo, intégré (extra `audio`).
- **Silero VAD**, MIT : détection de parole, intégré.

## Modèles vision-langage (VLM)
- **Qwen2.5-VL / Qwen3-VL** : lecture d'images et de captures pour juger une miniature ou un moment. Licence variable selon la taille (certaines tailles Apache-2.0, d'autres licence Qwen avec clause d'usage) : vérifier la fiche du modèle choisi. Candidat via Ollama.
- **Florence-2**, MIT : légendes et OCR visuel légers, candidat.
- **SigLIP / CLIP**, Apache-2.0 / MIT : scorer des images contre un texte (« squad wipe »), candidat pour trier des miniatures.
- Les images sont redimensionnées avant envoi au LLM (budget de contexte).

## Transcription (ASR)
- **faster-whisper** (CTranslate2), MIT : transcription locale rapide, intégré (extra `asr`).
- **WhisperX**, BSD-2-Clause : horodatage mot à mot et diarisation, candidat (la diarisation pyannote demande des conditions d'accès).
- **whisper.cpp**, MIT : repli CPU, candidat.
Pour les sous-titres mot à mot, préférer un alignement forcé au niveau du mot.

## Audio : séparation, débruitage, mixage
- **Demucs** (htdemucs), MIT : séparer voix, musique et effets (retirer la musique du lobby avant Content ID), contrat posé.
- **DeepFilterNet**, MIT ou Apache-2.0 : débruitage micro, contrat posé. Repli FFmpeg `arnndn` (RNNoise).
- **Spleeter** : écarté (abandonné, TensorFlow 1.x).
- FFmpeg `loudnorm` et `sidechaincompress` : normalisation et ducking.

## Interpolation, upscale, détourage
- **Practical-RIFE**, MIT : vrais ralentis, contrat posé ; repli FFmpeg `minterpolate`.
- **Real-ESRGAN**, BSD-3-Clause : upscale des captures de miniature, candidat (GPU conseillé).
- **rembg / BiRefNet** : détourage du sujet, candidat. Éviter RMBG-2.0 (usage non commercial).
- **SeedVR2** : écarté (poids non commerciaux).

## Téléchargement et chat
- **yt-dlp**, Unlicense : récupérer une source dont on a les droits, candidat, respecter les conditions de la plateforme.
- **TwitchDownloader** (CLI), licence à vérifier : télécharger le chat d'une VOD (JSON) pour un détecteur de pics de chat, candidat pour The Lost Chat.
- **chat-downloader**, MIT : chat Twitch et YouTube, candidat.
Signal utile : vitesse de messages comparée à la médiane récente, vagues d'émotes.

## Publication et montage
- **YouTube Data API v3** (`google-api-python-client`), Apache-2.0 : upload, chapitres, miniature, programmation, contrat posé.
- **Remotion** (templates, licence payante au-delà de petites équipes) et **Revideo**, MIT : motion design, intégré / alternative.
- **auto-editor**, Unlicense : coupe des silences, candidat de comparaison avec l'autocut interne.
- **Ultralytics YOLO**, AGPL-3.0 : écarté (copyleft).
- Outils cloud commerciaux (Eklipse, Opus, Krisp) : écartés (payants, données hors de la machine).

## LLM locaux et serveurs
- **Qwen** (texte) via **Ollama** ou **vLLM**, licence Apache-2.0 pour la plupart des tailles : fournisseur local par défaut.
- **llama.cpp**, MIT : serveur compatible OpenAI, candidat de repli.
- Fournisseurs de secours : Claude, Gemini, GitHub Models, uniquement avec une clé et au prix d'envoyer du contenu hors machine.
- Un modèle plus petit se trompe plus : valider les JSON et les timestamps.

## Sources
- `docs/RESEARCH.md` du dépôt : licences et priorités déjà évaluées.
- Dépôts GitHub des outils cités ; Hugging Face (fiches Qwen2.5-VL, licences par taille).
- Projets de référence : Montage Maker (lecture de killfeed par OCR), Stream Clipper (audio et chat), Autohighlight (League of Legends).
- Plusieurs licences sont rappelées de mémoire : toutes à revérifier dans le dépôt avant intégration.

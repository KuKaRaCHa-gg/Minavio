# Minavio

Moteur de montage automatique pour **Minivitch**, **Byte Tricks** et **The Lost Chat** :
détection locale des moments forts (OpenCV, VAD, audio), classement par LLM local (Qwen 30B),
beat-sync, ducking, motion design pixel/rétro (Remotion), rendu simultané 16:9 et 9:16.
Aucun appel à une API cloud pendant un rendu.

## Installation (machine hôte)

```bash
uv sync --all-extras                 # OpenCV, librosa, Silero VAD, faster-whisper, publication YouTube
(cd motion && npm install)           # templates Remotion (Node ≥ 18)
uv run minavio profiles validate
```

LLM local : n'importe quel serveur OpenAI-compatible (Ollama, vLLM, LM Studio…).

```bash
export MINAVIO_LLM_BASE_URL=http://localhost:11434/v1   # défaut : Ollama
export MINAVIO_LLM_MODEL=qwen3:30b-a3b                  # le nom de TON modèle
uv run minavio llm ping minivitch
```

## Interface graphique

```bash
uv sync --all-extras          # inclut l'interface (extra `ui`, Gradio)
uv run minavio ui             # ouvre http://127.0.0.1:7860 dans le navigateur
```

Sous Windows : double-clic sur `scripts/minavio-ui.bat`. L'interface n'écoute que sur ta machine.

| Onglet | Rôle |
| :--- | :--- |
| 🎬 Montage | chaîne, dossier de clips, musique, formats, Shorts, Qwen/chapitres ; progression par étape, annulation, vidéos produites, titre/description/chapitres |
| 🖼️ Miniatures | choix d'un job récent (`work/`), texte personnalisé et disposition (hero / split / collage), génération et téléchargement des PNG |
| 🎯 Captures de jeu | upload d'une capture d'écran, recadrage de l'élément (marqueur de kill…), enregistrement au bon endroit |
| ⚙️ Réglages | édition des profils YAML, vérifiés avant enregistrement (`.bak` conservé) |
| 📤 Publication | connexion YouTube, aperçu éditable (titre, description, tags, chapitres, miniature), confidentialité, heure programmée, dry-run puis publication confirmée |
| 🩺 Système | FFmpeg, Node/Remotion, modules installés, GPU, test de chaque fournisseur LLM, gestion des clés d'API |

## Utilisation en ligne de commande

```bash
# Un dossier de clips Outplayed → out/minivitch/<job>_youtube.mp4 et <job>_shorts.mp4
uv run minavio run minivitch ~/Videos/Outplayed/Apex --music ~/music/track.mp3

uv run minavio run thelostchat clips/ --format 9:16      # un seul format
uv run minavio run bytetricks clips/ --dry-run           # s'arrête à work/<job>/timeline.json
uv run minavio run minivitch clips/ --resume             # reprend après une erreur / un réglage
uv run minavio run minivitch clips/ --no-llm             # sans LLM (classement par score)
```

Chaque étape écrit son JSON dans `work/<job_id>/` (detect, transcribe, rank, copy, beats,
timeline, overlays…) : on peut l'inspecter, le corriger à la main et relancer avec `--resume`.

| Variable | Rôle |
| :--- | :--- |
| `MINAVIO_LLM_BASE_URL`, `MINAVIO_LLM_MODEL` | serveur / modèle LLM local |
| `MINAVIO_WHISPER_MODEL` | modèle faster-whisper (défaut `large-v3` sur CUDA, `small` sur CPU) |
| `MINAVIO_BROWSER_EXECUTABLE` | Chromium à utiliser par Remotion (sinon il le télécharge) |
| `MINAVIO_MOTION_DIR`, `MINAVIO_FONT` | projet Remotion / fonte du repli FFmpeg |

## Ingestion automatique

`minavio watch` surveille un dossier de clips (Outplayed) et lance le montage par lots :

```bash
uv run minavio watch minivitch "%USERPROFILE%\Videos\Outplayed"   # Ctrl+C pour arrêter
uv run minavio watch minivitch ./clips --once                     # scanne, traite, quitte
```

- Un clip est traité une fois stable (taille et date inchangées pendant `--stable`, 30 s par défaut).
  Les clips stables sont groupés : le lot part après `--batch` secondes sans nouveau clip (600 s),
  ou à 20 clips.
- Un montage en échec est marqué `failed` dans `work/.watch_<profil>.json` et n'est pas relancé
  seul. Pour le relancer, supprime son entrée dans ce fichier (ou modifie le clip) puis relance.
- Aucune publication : la mise en ligne reste `minavio publish`.
- Installation : `uv sync --extra watch` (surveillance temps réel via watchdog ; sans lui, scrutation).
- Windows : `scripts/minavio-watch.bat` ; pour le lancer à la connexion, voir le commentaire en tête du script.

## Miniatures

Si `thumbnails.enabled` (profil), `minavio run` produit après les rendus des miniatures
`out/<profil>/<job>_thumb<i>_<format>.png` (désactivable : `--no-thumbnails`). Pour les refaire
depuis un job existant, avec un texte imposé si besoin :

```bash
uv run minavio thumbnails minivitch <job_id> --text "3 KILLS EN 5 SECONDES" --layout hero
```

## Détection autonome, ralenti et audio

Plus besoin de captures PNG du jeu : le profil `minivitch` combine des détecteurs autonomes
(`killfeed_ocr` lit le killfeed par OCR, `motion_intensity` repère les fusillades,
`vision_scan` fait juger des images candidates par le LLM vision, plus `hype`, `audio_peak`,
`scene_change`). `template_match` reste un bonus optionnel, ignoré sans PNG dans `configs/games/`.

- **Ralenti** : `editing.slowmo` ralentit l'instant du kill (facteur 0,5 pendant 1,2 s source, avec
  interpolation d'images).
- **Audio** : `audio.separation` (Demucs) retire la musique du jeu et garde voix + effets, ce qui
  limite le risque de **Content ID** ; `audio.denoise` nettoie la voix (ventilo, clavier). L'étape
  `audio_clean` tourne après `collect` et écrit `work/<job>/audio_tracks.json` ; un échec donne un
  warning et le rendu garde l'audio d'origine.

Détails des clés : [`configs/README.md`](configs/README.md).

## Publication YouTube

**Rien n'est jamais publié par `minavio run`.** La publication passe uniquement par
`minavio publish` ou le bouton de l'onglet 📤 Publication. Par défaut les vidéos sont envoyées en
**privé** ; relis-les dans YouTube Studio avant de les rendre publiques.

### Créer le client OAuth Google (une seule fois)

1. Ouvre <https://console.cloud.google.com/> et crée un projet (ex. « Minavio »).
2. *API et services > Bibliothèque* : active **YouTube Data API v3**.
3. *API et services > Écran de consentement OAuth* : type **Externe**, renseigne un nom d'app et
   ton adresse e-mail, ajoute ton compte Google dans **Utilisateurs test**. Tant que l'app est en
   mode « Test », le jeton expire au bout de 7 jours (relance `minavio publish auth`) ; passe en
   « Production » pour un jeton durable.
4. *Identifiants > Créer des identifiants > ID client OAuth*, type **Application de bureau**.
   Télécharge le JSON (`client_secret_….json`).
5. Installe l'extra et connecte-toi :

```bash
uv sync --extra publish
uv run minavio publish auth --client-secret ~/Downloads/client_secret_123.json
# le navigateur s'ouvre : choisis ta chaîne YouTube et accepte (scopes youtube.upload + youtube)
uv run minavio publish auth --status       # état, sans afficher de jeton
```

Le fichier est copié dans `~/.config/minavio/youtube_client_secret.json` et le jeton (rafraîchi
automatiquement) dans `youtube_token.json`, à côté de `secrets.env`, tous deux en permissions 600
(`%APPDATA%\minavio\` sous Windows). Ne les partage ni ne les versionne jamais ; aucun jeton n'est
écrit dans les logs ni dans l'interface. `minavio publish auth --logout` supprime le jeton.

### Publier

```bash
uv run minavio publish minivitch <job_id> --dry-run             # affiche ce qui serait envoyé, sans réseau
uv run minavio publish minivitch <job_id>                       # vidéo longue (privée, 18:00 si schedule_at)
uv run minavio publish minivitch <job_id> --video shorts        # tous les Shorts (ou short2, all)
uv run minavio publish minivitch <job_id> --privacy unlisted --at now
```

Titre, description, tags viennent de `work/<job>/copy.json` ; les chapitres sont ajoutés à la
description ; la miniature est la première variante 16:9 de `thumbnails.json` (convertie en JPEG si
le PNG dépasse 2 Mo). Shorts : `#Shorts` ajouté au titre, description courte, pas de miniature.
`publish.schedule_at: "18:00"` (heure locale, prochaine occurrence) envoie la vidéo en `private`
avec `publishAt` en UTC : YouTube la publie à l'heure dite. Le journal `work/<job>/publish.json`
rend la commande **idempotente** : une vidéo déjà envoyée n'est jamais republiée. Un upload
interrompu (5xx, réseau) reprend avec 3 essais et backoff.

**Quotas** : l'API YouTube accorde 10 000 unités/jour par projet ; `videos.insert` coûte environ
100 unités (≈ 100 envois/jour dans son compartiment) et `thumbnails.set` environ 50. Sur
`quotaExceeded` le message le dit ; le quota se réinitialise à minuit heure du Pacifique, et
relancer la même commande ne renvoie pas les vidéos déjà publiées. Une extension se demande dans la
console Google Cloud.

Dans l'interface : onglet **📤 Publication** (connexion YouTube, job récent, aperçu éditable,
confidentialité, heure, « Prévisualiser » puis « Publier » avec case de confirmation).

## Fournisseurs LLM, coût et confidentialité

Par défaut tout reste local (Qwen via Ollama). Un profil peut déclarer une chaîne `llm.providers`
(local, Claude, Gemini, GitHub Models, API compatible OpenAI) essayée dans l'ordre, par exemple
local d'abord et Claude en secours (voir `configs/README.md`).

- **Les API cloud sont payantes** (Claude, Gemini, OpenAI) et **envoient vos transcripts et images
  au fournisseur** : n'activez-les que si cela vous convient. GitHub Models (abonnement Copilot)
  est limité (~8k tokens d'entrée, quelques requêtes/min) : à réserver au secours.
- Les clés ne sont jamais dans les YAML : variables d'environnement ou fichier local
  `~/.config/minavio/secrets.env` (`%APPDATA%\minavio\secrets.env` sous Windows, permissions 600).

```bash
uv run minavio llm set-key ANTHROPIC_API_KEY   # saisie masquée (aussi GEMINI_API_KEY, OPENAI_API_KEY, GITHUB_TOKEN)
uv run minavio llm keys                        # état des clés, sans les valeurs
uv run minavio llm ping minivitch              # teste chaque fournisseur de la chaîne
```

## Pour aller plus loin

- Architecture, décisions et limites : [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- Règles Claude Code et routage des agents : [`CLAUDE.md`](CLAUDE.md)
- Nouveau jeu : [`configs/games/README.md`](configs/games/README.md)
- Templates motion : [`motion/README.md`](motion/README.md)

## Développement

```bash
uv run ruff check . && uv run ruff format --check . && uv run pytest -q
```

`tests/test_e2e.py` génère des clips synthétiques, simule le serveur Qwen sur localhost et
rend les deux formats avec le vrai FFmpeg/OpenCV (~1 min).

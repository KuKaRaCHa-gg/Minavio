# Configs : profils de chaîne et jeux

Référence courte. Le schéma complet et les valeurs par défaut s'obtiennent avec `minavio schema`
(JSON Schema des profils) ; la validation avec `minavio profiles validate`.

```
configs/
  channels/<id>.yaml        profils de chaîne (un par chaîne)
  games/<id>/game.yaml      jeux : templates d'UI pour le détecteur template_match
```

## `editing.autocut` : coupe des silences

Bloc optionnel de `editing`. Activé par `enabled: true`, il lance l'étape `autocut` du pipeline
(après `transcribe`, avant `timeline`) qui calcule, pour chaque source retenue, les plages à garder.
Le montage retire le reste. Sans `enabled: true`, l'étape n'existe pas.

| Clé | Rôle |
| :--- | :--- |
| `enabled` | active l'étape `autocut` |
| `min_silence_s` | durée minimale d'un silence pour qu'il soit coupé (plus élevé = coupes plus rares) |
| `energy_margin_db` | marge (dB) au-dessus du niveau de fond pour considérer qu'il y a du son |
| `punch_in` | facteur de zoom appliqué après une coupe (`1.0` = pas de zoom) |

Profils actuels : `thelostchat` (agressif : `min_silence_s: 0.35`, `punch_in: 1.06`),
`bytetricks` (`0.5`, `1.08`), `minivitch` (léger : `1.2`, `energy_margin_db: 4`, `punch_in: 1.0`,
le jeu fait du bruit en continu).

Si l'analyse échoue pour une source, un warning est affiché et le montage continue sans coupe.
Résultat intermédiaire : `work/<job_id>/autocut.json` au format `{source: [[début, fin], ...]}`.
Les seuils exacts et leur effet sont à confirmer dans `minavio schema`.

## `framing` : cadrage

Bloc optionnel au niveau du profil (hors `editing`).

### `framing.smoothing`

Lissage du suivi de cadrage utilisé par les reframes `face_track` et `action_track`.

| Clé | Rôle |
| :--- | :--- |
| `beta` | coefficient de lissage : plus grand = cadrage plus réactif |
| `max_pan_per_s` | vitesse maximale de déplacement du cadre, en fraction de l'image par seconde |

`minivitch` utilise `beta: 0.04`, `max_pan_per_s: 0.5` (plus réactif que le défaut).

### `framing.facecam`

Utilisé par le reframe `split_facecam` (sortie 9:16 : facecam en haut, jeu/contenu en bas).

| Clé | Rôle |
| :--- | :--- |
| `corner` | coin de la facecam ; `auto` laisse le détecteur la trouver |
| `split_ratio` | part de la hauteur de sortie occupée par la facecam (ex. `0.35`) |
| `fallback` | reframe utilisé si la facecam n'est pas détectée (ex. `face_track`) |

`thelostchat` : `corner: auto`, `split_ratio: 0.35`, `fallback: face_track`.

## Reframe des formats (`outputs[].reframe`)

`none`, `blur_pad`, `face_track`, `action_track`, `split_facecam`. Un format 16:9 utilise en
général `none`.

## Détecteurs (`detection`)

Chaque entrée : `name`, `weight` (poids du score du highlight, `>= 0`), `params` optionnels.
Les scores sont fusionnés ; un highlight détecté par plusieurs détecteurs gagne un bonus.

| Détecteur | Détecte | Params |
| :--- | :--- | :--- |
| `template_match` | éléments d'UI d'un jeu (kill, knock, victoire) via OpenCV, depuis `games/<id>/` | aucun (templates du jeu) |
| `audio_peak` | pics sonores (rires, cris) au-dessus de la médiane | `min_db_above_median` |
| `speech` | parole (VAD) : réactions, fil narratif | aucun |
| `scene_change` | changements de plan visuels | `threshold` (0..1) |
| `hype` | pics combinés d'intensité audio et vocale | aucun |
| `chat_spike` | pics d'activité du chat de la VOD (bonus) | aucun ; ignoré sans log de chat |

`chat_spike` est un signal optionnel : sans log de chat à côté du clip, le détecteur n'émet rien
et le profil reste valable.

### Convention du log de chat

Placer le fichier à côté du clip, avec le même nom de base. Formats recherchés, par ordre de
priorité :

1. `<stem>.chat.json` (export TwitchDownloader)
2. `<stem>.json` (export chat-downloader)
3. `<stem>.chat.txt`
4. `<stem>.txt`

Pour les fichiers `.txt`, une ligne par message au format `[H:MM:SS] user: message`, par exemple
`[0:12:05] viewer42: LUL`.

Exemple : pour `/videos/vod_2026-10-01.mp4`, le log est `/videos/vod_2026-10-01.chat.json`.

## `audio.sfx` : effets sonores

Désactivé par défaut. Avec `enabled: true`, l'étape `sfx` (après `overlays`) pose des effets
sonores locaux (banque CC0 embarquée, ou `library_dir`) sur les événements du montage. Résultat :
`work/<job_id>/sfx.json`.

| Clé | Rôle |
| :--- | :--- |
| `enabled` | active l'étape `sfx` |
| `library_dir` | dossier de sons à la place de la banque embarquée |
| `gain_db` | gain des effets (dB, `<= 12`) |
| `on_cut` | son entre deux moments (`whoosh`), `null` = aucun |
| `on_kill` / `on_multi_kill` | son d'un kill (`hit`) / multi-kill (`boom`) |
| `on_cold_open` | son du rewind de l'ouverture (`rewind`) |

## `editing.cold_open` et `editing.order`

- `cold_open: { enabled, duration_s }` : la vidéo s'ouvre sur le pic du meilleur moment (durée
  `duration_s`, `0.5` à `8`), puis le montage normal reprend. Le titre (hook) se place alors au
  début du premier vrai moment, pas à t=0. Avec le template `rewind`, un effet rewind joue sur la
  dernière seconde de l'ouverture.
- `order` : `chronological` (ordre des sources, défaut) ou `countdown` (du moins bon au meilleur,
  pour un Top N ; le template `rank_card` affiche N…1 au début de chaque moment).

## Motion par moment

Ces templates ne s'affichent que s'ils sont listés dans `motion.templates` :

| Template | Props | Quand |
| :--- | :--- | :--- |
| `kill_counter` | `kills`, `label?` | 0,3 s après le début d'un moment `kill` (1) ou `multi_kill` (2, ou `signals.kills` du highlight) ; pas sur le cold open |
| `rank_card` | `rank`, `total`, `label?` | début de chaque vrai moment si `editing.order: countdown` |
| `rewind` | `text?` | dernière seconde du cold open |

## `shorts` : un short par meilleur moment

Bloc optionnel au niveau du profil. Pour les `per_highlight` meilleurs highlights (par score), le
pipeline monte une timeline dédiée (durée `max_duration_s`, sans cold open, ordre chronologique,
sous-titres et `kill_counter`, sans titre ni popups) et la rend dans le format `format`.

| Clé | Rôle |
| :--- | :--- |
| `per_highlight` | nombre de shorts (`0` = désactivé, `<= 10`) ; surchargeable par `minavio run --shorts N` |
| `max_duration_s` | durée cible d'un short (`> 5`, `<= 60`) |
| `format` | nom d'un `outputs[]` **9:16** du profil (erreur claire sinon, avant les étapes longues) |

Fichiers : `out/<profil>/<job_id>_short<i>.mp4`. Un short qui échoue donne un warning et le job continue.

## `chapters` : chapitres YouTube

`chapters: true` lance l'étape `chapters` (LLM local, après le montage). Elle écrit
`out/<profil>/<job_id>_chapters.txt` (format prêt à coller dans une description) et
`work/<job_id>/chapters.json`. Désactivable par `minavio run --no-chapters`. Ignorée avec `--no-llm`.
Les chemins de ces fichiers annexes sont affichés après les vidéos.

## `llm.providers` : chaîne de fournisseurs LLM

Liste optionnelle, essayée dans l'ordre : le premier fournisseur joignable sert. Vide = le LLM
local défini par `llm.base_url` / `llm.model` (comportement historique).

```yaml
llm:
  prompt: gaming
  providers:
    - { kind: local }                      # Ollama / llama.cpp (gratuit, privé)
    - { kind: anthropic, effort: low }     # secours, seulement si ANTHROPIC_API_KEY existe
```

| `kind` | Clé (variable) | Remarques |
| :--- | :--- | :--- |
| `local` | aucune | serveur compatible OpenAI sur la machine |
| `anthropic` | `ANTHROPIC_API_KEY` | `effort: low\|medium\|high` ; payant |
| `google` | `GEMINI_API_KEY` | Gemini ; payant au-delà du quota gratuit |
| `github` | `GITHUB_TOKEN` | GitHub Models (abonnement Copilot) |
| `openai_compat` | `OPENAI_API_KEY` | `base_url` et `model` obligatoires |

Autres champs : `model`, `vision_model`, `base_url`, `api_key_env` (autre nom de variable).
**Les clés ne sont jamais dans le YAML.** Claude, Gemini et les API payantes envoient les
transcripts et images au fournisseur : coût et confidentialité à votre charge.

`{kind: github}` (GitHub Models, base `https://models.github.ai/inference`) utilise votre
abonnement Copilot. Créez un jeton fine-grained sur
<https://github.com/settings/personal-access-tokens> avec la permission **Models: read**, puis
`minavio llm set-key GITHUB_TOKEN`. Limites de la preview : environ 8k tokens d'entrée par requête
et quelques requêtes par minute : à réserver au **secours** (en dernier de la chaîne), pas aux longs
transcripts.

## Secrets locaux

Les clés se lisent dans l'environnement, sinon dans `~/.config/minavio/secrets.env`
(`%APPDATA%\minavio\secrets.env` sous Windows ; `MINAVIO_SECRETS_FILE` pour un autre chemin),
lignes `CLE=valeur`, permissions 600. Une variable déjà définie n'est jamais écrasée.

```bash
minavio llm set-key ANTHROPIC_API_KEY    # saisie masquée ; --delete pour retirer
minavio llm keys                         # état env / fichier / absent, sans valeur
minavio llm ping minivitch               # une ligne par fournisseur de la chaîne
```

## `thumbnails` : miniatures

| Clé | Rôle |
| :--- | :--- |
| `enabled` | active l'étape `thumbnails` (désactivable par run : `--no-thumbnails`) |
| `count` | variantes produites (A/B test), 1 à 6 |
| `layouts` | compositions : `hero` (1 image), `split` (2), `collage` (3) |
| `formats` | tailles de sortie (défaut `youtube` 1280x720 et `short` 1080x1920) |
| `candidates` | images présélectionnées avant le choix final |
| `max_words` | longueur maximale du texte |
| `use_vision_llm` | juge visuel (local, Claude, Gemini) ; sinon heuristique |

Sortie : `out/<profil>/<job>_thumb<i>_<format>.png`, résumé dans `work/<job>/thumbnails.json`
(images candidates dans `work/<job>/thumbs/`). L'étape est optionnelle : un échec = un warning.
Pour refaire des miniatures d'un job existant :
`minavio thumbnails minivitch <job_id> [--text "TON TEXTE"] [--layout hero|split|collage]`.

## Profils livrés

| Profil | Particularités |
| :--- | :--- |
| `minivitch` | sfx, cold open 2,5 s, chapitres, 3 shorts par moment, `kill_counter` et `rewind`, miniatures, exemple `llm.providers` (local puis Claude) |
| `minivitch_top10` | « Minivitch — Top 10 » : copie de minivitch en `order: countdown`, `max_highlights: 10`, `rank_card`, durée cible 420 s, shorts désactivés |
| `bytetricks`, `thelostchat` | sfx désactivé, pas de chapitres ni de shorts ; miniatures activées (hero+split / hero+collage) |

## Environnement

| Variable | Surcharge |
| :--- | :--- |
| `MINAVIO_LLM_BASE_URL` | `llm.base_url` du profil |
| `MINAVIO_LLM_MODEL` | `llm.model` du profil |
| `MINAVIO_CONFIG_DIR` | dossier `configs/` à lire à la place du dépôt |

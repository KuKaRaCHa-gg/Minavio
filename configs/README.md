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

## Environnement

| Variable | Surcharge |
| :--- | :--- |
| `MINAVIO_LLM_BASE_URL` | `llm.base_url` du profil |
| `MINAVIO_LLM_MODEL` | `llm.model` du profil |
| `MINAVIO_CONFIG_DIR` | dossier `configs/` à lire à la place du dépôt |

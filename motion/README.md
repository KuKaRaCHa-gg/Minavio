# motion/ — templates Remotion (pixel / arcade)

Projet Remotion 4 (React/TS). Fonte *Press Start 2P* via `@remotion/google-fonts`, rendu net,
animations en paliers. Compositions : `captions`, `title`, `popup`, `progress-bar` (template Python
`progress_bar`), `reaction`, `kill-counter`, `rewind`, `rank-card` (Python : `kill_counter`, `rewind`, `rank_card`). Props communes : `width`, `height`, `fps`, `durationInFrames`,
`theme {primary, accent}`, `beats` (s, relatifs à l'overlay), `scanlines`, `style` (`pixel`|`clean`).

```
npm install
npx tsc --noEmit
npx remotion studio src/index.ts
npx remotion still src/index.ts captions out.png --props=props.json
```

Le rendu de production passe par `minavio.motion.render.render_overlays` (ProRes 4444 alpha).
Sans accès à fonts.gstatic.com, `loadFont` échoue : repli `monospace`.
Navigateur personnalisé : variable `MINAVIO_BROWSER_EXECUTABLE`.

## Templates lot Minivitch

| Python | Composition | Props propres | Durée typique |
| :--- | :--- | :--- | :--- |
| `kill_counter` | `kill-counter` | `kills` 1..5, `label?` (défaut : KILL / DOUBLE KILL / TRIPLE KILL / SQUAD WIPED pour 4 et 5) | 2 s |
| `rewind` | `rewind` | `text?` (défaut `REWIND ◀◀`) | 0,6 à 1 s |
| `rank_card` | `rank-card` | `rank`, `total`, `label?` | 1,8 s |

- `kill_counter` : haut-droite en 16:9, haut-centre en 9:16 ; un crâne s'allume toutes les 0,25 s, flash + shake à la dernière.
- `rewind` : plein cadre, fond semi-transparent ; les flèches ◀ / ▶ sont dessinées en CSS (la fonte n'a pas ces glyphes).
- `rank_card` : gauche en 16:9, haut-centre en 9:16 ; entrée en glissement par paliers.
- Repli FFmpeg sans Node : `kill_counter` (texte du label) et `rank_card` (`#N`) ; `rewind` lève `MotionUnavailableError` si strict, sinon est ignoré.

## Miniatures (`thumbnail`, Remotion Still)

`npx remotion still src/index.ts thumbnail out.png --props=props.json` (bridge :
`minavio.motion.thumbnail.render_thumbnail(choice, fmt, theme, out, badge=None, kills=None)`).

| Prop | Rôle |
| :--- | :--- |
| `layout` | `hero` (plein cadre, zoom 1,12, texte bas-gauche), `split` (diagonale crénelée + gros VS), `collage` (1 grande + 2 petites, bordures pixel) |
| `images` | 1 à 3 sources ; moins que nécessaire : réutilisées en boucle |
| `text` | 2-4 mots, mis en 1 à 3 lignes à la plus grande taille ; dernière ligne en `theme.accent` |
| `badge?` | cartouche pixel (ex. `MINIVITCH`) en haut à gauche |
| `kills?` | crânes pixel en haut à droite (> 5 : un crâne + `xN`) |
| `highlight?` | `{x, y}` normalisés : zoom centré dessus + anneau et flèche pixel (hero) |
| `theme`, `width`, `height` | 1280x720 ou 1080x1920 (images empilées, texte en haut, zones sûres Shorts) |

**Images** : une entrée est une data URI / URL http(s), ou un chemin relatif résolu par
`staticFile()` (`motion/public/`). Un chemin disque ou `file://` n'est pas lisible par Chromium
depuis la page de rendu : le bridge Python inline donc chaque image en data URI JPEG (réduite à
2560 px) dans le JSON de props. Aucun accès réseau requis.

Sortie PNG ; si elle dépasse 2 Mo (limite YouTube), un JPEG est écrit à côté (`upload_path()`).
Repli sans Node (ou si Remotion échoue) : composition Pillow simplifiée avec la même fonte
(`render_thumbnail_pillow`), puis FFmpeg `drawtext` si Pillow est absent. Pillow n'est pas encore
déclaré dans `pyproject.toml`.

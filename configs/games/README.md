# Ajouter un jeu

Aucun code à écrire : un jeu = un dossier + un YAML.

1. Copier `configs/games/apex_legends/` vers `configs/games/<id_du_jeu>/` (l'id doit
   être identique au champ `id` de `game.yaml`).
2. Capturer une image 1080p du jeu au moment où l'élément d'UI apparaît (marqueur de kill,
   bandeau de victoire...). Découper un PNG serré autour de l'élément (sans fond mouvant)
   et le placer dans `templates/` (ex. `templates/kill_marker.png`). Les templates sont
   comparés à l'échelle de la vidéo : cadrer sur du 1920x1080.
3. Déclarer chaque template dans `game.yaml` (`ui_templates`) :
   - `name`, `image` (chemin relatif au dossier du jeu), `label` (kill, knock, win...).
   - `roi: [x, y, w, h]` : zone de recherche, valeurs normalisées entre 0 et 1 (origine en haut
     à gauche). Plus la zone est petite, plus la détection est rapide et fiable.
   - `threshold` (0-1) : score minimal de correspondance. Départ à 0.80 ; si trop de faux
     positifs, monter par pas de 0.02 ; si des événements manquent, descendre.
   - `cooldown_s` : délai minimal entre deux détections du même template.
   - `combo_window_s` (niveau jeu) : fenêtre de regroupement des événements (multi-kills).
4. Ajouter l'id du jeu à `games: [...]` dans les profils de chaîne concernés.
5. Vérifier : `minavio games list` puis `minavio profiles validate`, et tester sur un clip
   avec `minavio run <profil> clip.mp4 --dry-run -v`.

Jeux disponibles : `apex_legends`, `valorant`, `fortnite`, `warzone`.

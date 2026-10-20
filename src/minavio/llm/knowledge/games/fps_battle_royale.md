<!-- kb: tasks=rank,copy,chapters,vision_scan,thumbnails; content_type=gaming; genre=battle_royale,tactical_fps,arena_shooter,extraction_shooter -->
# FPS et battle royale (notions communes)

## Killfeed et élimination
Presque tous les FPS affichent un fil d'éliminations : une ligne par kill avec le tueur, une icône d'arme (parfois un symbole de headshot ou de mur) et la victime. Il se trouve dans un coin de l'écran (souvent en haut à droite) et disparaît après quelques secondes. Un kill du joueur est mis en évidence (couleur, cadre ou son dédié). Les pseudos du joueur et de ses coéquipiers distinguent « kill pour nous » et « kill contre nous ». Valable pour Warzone, Fortnite, Valorant, CS2, PUBG, The Finals, Apex.

## Multi-kill, ace et séries
- **Multi-kill** : plusieurs kills rapprochés (double, triple, quad). Les jeux affichent un message ou un son.
- **Ace** : un joueur élimine toute l'équipe adverse dans un round (Valorant, CS2).
- **Killstreak** : série de kills sans mourir.
- **Squad wipe / team wipe** : toute une équipe éliminée (battle royale).
Plus l'intervalle est court et les kills nombreux, plus le moment est fort. Fenêtre de regroupement typique : 4 à 8 secondes.

## Clutch
**Clutch** : le dernier survivant de son équipe gagne seul contre plusieurs ennemis (1v2, 1v3, 1v5). Signaux : compteur d'équipe indiquant un seul joueur vivant, tension sonore, voix qui chuchote puis éclate. Dans les jeux par rounds (CS2, Valorant), le clutch se joue souvent avec la bombe posée ou le défuse en cours. Un clutch lisible a un enjeu clair (round décisif, fin de manche) et un payoff net (victoire, dernier kill).

## Round win et victoire
Jeux par rounds : écran « Round won / Victory », score qui change, ralenti de fin. Battle royale : écran de victoire (« Victory Royale » sur Fortnite, « Winner Winner Chicken Dinner » sur PUBG, « Victory » sur Warzone, « Champion » sur Apex). Ces bannières servent de marqueur de payoff : on cherche l'action dans les 20 à 60 secondes qui précèdent.

## Signaux visuels génériques
Hit markers (croix à l'impact, souvent rouge pour un kill), barre de santé et de bouclier qui chute, flash écran au dégât, indicateurs de direction de tir, minimap, compteur de joueurs ou d'escouades restantes, score d'équipe, timer de round, symbole de bombe. Un bouclier qui casse déclenche souvent une animation ou un son net, repère utile pour les détecteurs.

## Signaux sonores génériques
Tirs nourris, explosions, son de kill (un « ding » propre à chaque jeu), son d'élimination d'équipe, cri du joueur, rire, chuchotement de tension. Un pic de volume brutal puis une voix qui monte vaut généralement plus qu'un volume soutenu. Attention à la musique de fond : elle peut créer un faux pic ou une réclamation Content ID.

## Faux positifs communs
Menus et lobbies, écran de chargement, killcam ou replay, spectateur après la mort, écran de scores de fin de manche, cinématique d'intro, entraînement (training range), parties contre bots. Un killfeed lisible dans un replay n'est pas un événement en direct. Un pic audio de jingle d'interface n'est pas un combat.

## Ajouter un nouveau jeu
Créer `configs/games/<id>/game.yaml` (modèle : apex_legends), puis une fiche `configs/knowledge/games/<id>.md` avec : vocabulaire propre au jeu, interface à repérer (killfeed, bannières, compteurs), sons marquants, faux positifs. Ajouter l'id du jeu dans `games:` du profil de chaîne pour que ses passages soient retrouvés.

## Sources
- Pratiques générales des FPS (killfeed, ace, clutch) : documentation et wikis des jeux concernés (Valorant, CS2, Fortnite, PUBG, Warzone, The Finals).
- Recherche académique sur la détection de temps forts e-sport (fusion audio, texte, image).
- Libellés exacts à vérifier jeu par jeu avant d'ajouter des détecteurs.

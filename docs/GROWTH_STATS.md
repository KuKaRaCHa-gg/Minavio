# Stats de jeu et fact-check

Module : `src/minavio/integrations/` (`gamestats.py`, `factcheck.py`, `cli_stats.py`).
Sous-commandes exposées par `stats_app` (à monter dans la CLI principale).

## Clés d'API

Les clés se placent dans les variables d'environnement ou dans `~/.config/minavio/secrets.env`
(ou `MINAVIO_SECRETS_FILE`). Une variable d'environnement déjà définie n'est jamais écrasée.
Aucune valeur n'est journalisée.

| Variable | Usage | Obtenir la clé |
| :--- | :--- | :--- |
| `APEX_API_KEY` | Apex Legends Status API (profil, rang, rotation des cartes) | Gratuite sur https://apexlegendsapi.com (inscription, clé affichée dans le compte) |
| `RIOT_API_KEY` | Riot (compte, dernier match LoL, Valorant) | https://developer.riotgames.com (clé de développement, connexion Riot requise) |

Sans clé, la commande échoue avec un message qui nomme la variable manquante.

## Commandes

```bash
minavio stats apex <pseudo> [--platform PC|PS4|X1]   # niveau, rang, RP, légende
minavio stats apex-map                                # rotation des cartes
minavio stats riot <Pseudo#TAG> [--region europe]     # compte + dernier match
minavio stats factcheck <profil> --text "..." | --file script.txt [--out factcheck.md]
```

(Le préfixe `minavio stats` dépend du montage de `stats_app` dans la CLI principale.)

## Limites

- **Apex** : quota du plan gratuit limité ; les réponses sont mises en cache 1 h. Les champs
  de dégâts et de kills dépendent de ce que l'API expose pour le joueur : un champ absent est omis.
- **Riot account-v1** : routage `americas`, `asia` ou `europe` (ni `sea` ni `esports` pour ce service).
- **LoL match-v5** : dernier match uniquement (un seul appel de liste + un appel détail).
- **Valorant** : l'API match exige une clé de production. Avec une clé de développement, la
  commande l'indique et ne plante pas.
- **Cache** : `work/.cache/stats/`, TTL 1 h. Une erreur n'est jamais mise en cache.
- **Fact-check** : le LLM local extrait les affirmations ; le verdict ne repose que sur le premier
  extrait Wikipédia (ou la description Wikidata). Une affirmation sans source reste
  « Non vérifiable ». Le rapport est une aide à la relecture, pas une garantie : relis les sources.
  Sans LLM configuré, la commande explique comment le lancer et s'arrête.
- **Wikipédia / Wikidata** : pas de clé. Un User-Agent identifiable est envoyé.
- **Copy** : `stats_context(profile, pseudo, plateforme)` renvoie un bloc factuel à donner au LLM
  de copy. Les valeurs absentes ne sont jamais inventées.

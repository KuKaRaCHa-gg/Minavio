# Croissance : recherche de niche et analytics

Deux commandes en lecture seule, qui n'envoient rien sur YouTube :

```bash
uv run minavio growth niche <profil> "requête 1" "requête 2" [--days 30] [--shorts | --long] [--out niche.md]
uv run minavio growth analytics <profil> [--days 90] [--out analytics.md]
```

Les rapports Markdown sont écrits dans `out/<profil>/reports/`.

## Clé YouTube Data API (recherche de niche)

1. Ouvre la [console Google Cloud](https://console.cloud.google.com/) et crée (ou choisis) un projet.
2. Menu « APIs et services » > « Bibliothèque » : cherche **YouTube Data API v3** et clique sur **Activer**.
3. « Identifiants » > « Créer des identifiants » > **Clé d'API**. Copie la clé.
4. Ajoute-la dans `~/.config/minavio/secrets.env` (permissions 600) :

   ```
   YOUTUBE_API_KEY=ta_cle
   ```

   ou exporte la variable `YOUTUBE_API_KEY` dans ton shell. Restreindre la clé à « YouTube Data API v3 »
   est recommandé.

## Quotas

- Quota Google : **10 000 unités par jour** (reset à minuit, heure du Pacifique).
- Coût : `search.list` = 100 unités par page de 50 résultats ; `videos.list` et `channels.list` = 1 unité.
  Une requête de niche de 50 résultats coûte donc environ 102 unités.
- Garde-fou local : Minavio tient un compteur dans `~/.config/minavio/yt_quota.json` et refuse toute
  requête au-delà du budget (3 000 unités/jour par défaut). Pour le changer : `MINAVIO_YT_QUOTA_BUDGET=5000`.
- Cache : les réponses sont gardées 24 h dans `work/.cache/yt/` ; une relance le même jour ne consomme
  pas de quota.

## Analytics de mes chaînes (OAuth)

`growth analytics` réutilise le jeton de `publish auth` et demande le scope
`yt-analytics.readonly`. Si ton jeton a été créé avant cette version, relance l'authentification :

```bash
uv run minavio publish auth --logout
uv run minavio publish auth
```

Le CTR d'impression n'est pas garanti par l'API Analytics : le rapport indique « indisponible » si
l'API refuse la métrique, et il faut alors le lire dans YouTube Studio.

## LLM local (optionnel)

Si le profil active `llm` (Qwen local), les rapports ajoutent 5 titres, 5 sujets ou des
recommandations. Sans serveur local, le rapport est produit sans cette section, avec une note.

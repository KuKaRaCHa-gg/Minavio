# Croissance : veille de tendances et notifications

Commandes :

- `minavio trends <profil> [--out trends.md]` : rapport Markdown (sources publiques, cache 6 h
  dans `work/.cache/trends/`) ; si le LLM local répond, cinq idées de vidéos sont ajoutées.
- `minavio notify-test` : envoie un message de test sur chaque canal configuré.

Toutes les clés sont facultatives. Elles se mettent dans les variables d'environnement ou dans
`~/.config/minavio/secrets.env` (`CLE=valeur`, permissions 600). Une clé absente produit une note
dans le rapport ou un canal ignoré, jamais une erreur.

## Discord (webhook)

1. Ouvre le salon cible, puis Paramètres du salon > Intégrations > Webhooks > Nouveau webhook.
2. Copie l'URL du webhook.
3. `DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...`

Le webhook donne un accès d'écriture au salon : ne le partage pas et ne le commite jamais.

## Telegram (bot)

1. Dans Telegram, ouvre @BotFather et envoie `/newbot`. Copie le jeton fourni.
2. Envoie un message à ton bot depuis ton compte, puis ouvre
   `https://api.telegram.org/bot<JETON>/getUpdates` et repère `"chat":{"id":...}`.
3. `TELEGRAM_BOT_TOKEN=<jeton>` et `TELEGRAM_CHAT_ID=<id>` (un groupe a un id négatif).

## ntfy (notifications push, sans compte)

1. Installe l'application ntfy (Android, iOS) ou ouvre https://ntfy.sh dans le navigateur.
2. Choisis un nom de topic difficile à deviner (ex. `minavio-<chaîne>-<aléatoire>`) : sur le
   serveur public, quiconque connaît le nom peut lire et publier.
3. `NTFY_TOPIC=<topic>`. `NTFY_SERVER` est facultatif (défaut `https://ntfy.sh`, sinon ton
   serveur auto-hébergé). Les miniatures ne sont pas jointes sur ntfy : texte et lien seulement.

## Twitch (jeux populaires et clips)

1. Ouvre https://dev.twitch.tv/console, connecte-toi, puis « Register Your Application ».
2. Type « Confidential » (ou « Public » si tu n'as pas de secret, mais le jeton client_credentials
   exige le secret). URL de redirection : `http://localhost` (non utilisée ici).
3. Récupère le Client ID et génère un Client Secret.
4. `TWITCH_CLIENT_ID=...` et `TWITCH_CLIENT_SECRET=...` (jeton d'application, aucune connexion
   utilisateur).

Les clips appartiennent à leur streamer : demande sa permission avant toute republication.

## Reddit (limites)

- L'accès public à `reddit.com/r/<sub>/top.json` est limité en débit. Un 403 ou 429 signale
  une limite : le rapport l'indique au lieu d'échouer.
- Optionnel : crée une app sur https://www.reddit.com/prefs/apps (type « script », sans URL de
  redirection utile), puis `REDDIT_CLIENT_ID` et `REDDIT_CLIENT_SECRET`. Minavio utilise alors
  le jeton app-only (`client_credentials`), sans compte utilisateur.
- Respecte les règles de chaque subreddit et l'API de Reddit : pas de publication automatique de
  contenu récupéré.

## Steam

Aucune clé. Le classement « jeux les plus joués » est lu ; à défaut, le rapport affiche les
joueurs actuels de quelques jeux, sans tendance.

## Publication

Les notifications de fin de job rappellent `minavio publish <profil> <job>`. Minavio ne publie
jamais automatiquement : la publication reste une action manuelle.

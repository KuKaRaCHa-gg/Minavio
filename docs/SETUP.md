# Minavio — tout ce qu'il faut paramétrer

Coche au fur et à mesure. **Obligatoire** = sans ça l'app ne monte pas. Tout le reste est optionnel :
chaque fonction s'éteint proprement (avertissement) si sa clé manque.

Les clés ne vont **jamais** dans les YAML ni dans le dépôt. Pour en enregistrer une (saisie masquée) :

```bash
uv run minavio llm set-key NOM_DE_LA_CLE     # écrit dans %APPDATA%\minavio\secrets.env (Windows)
uv run minavio llm keys                       # état de toutes les clés, sans les valeurs
```

## 1. Obligatoire (installation)

- [ ] **Python 3.11+ et uv** : `pip install uv` puis, dans le dossier du projet, `uv sync --all-extras`.
- [ ] **FFmpeg** dans le PATH (`ffmpeg -version` doit répondre).
- [ ] **Node.js 18+** puis `cd motion && npm install` (motion design Remotion ; sinon repli FFmpeg).
- [ ] **Ollama** + modèles :
  ```bash
  ollama pull qwen3:30b-a3b     # texte : classement, titres, chapitres, memes, tendances
  ollama pull qwen3-vl:8b       # vision locale : juge visuel, miniatures
  ```
- [ ] Vérifier : `uv run minavio profiles validate` puis `uv run minavio llm ping minivitch`.

Variables utiles (facultatives) : `MINAVIO_LLM_BASE_URL` (défaut `http://localhost:11434/v1`),
`MINAVIO_LLM_MODEL`, `MINAVIO_WHISPER_MODEL` (défaut `large-v3` sur GPU).

## 2. Au premier lancement (automatique, mais à savoir)

- [ ] **Demucs** télécharge ses poids (~80 Mo) la première fois (retrait de la musique du jeu).
- [ ] **Whisper** télécharge son modèle la première fois.
- [ ] **RIFE** (ralenti fluide, optionnel) : installer `rife-ncnn-vulkan` et le mettre dans le PATH ou
      `MINAVIO_RIFE_BIN=C:\chemin\rife-ncnn-vulkan.exe`. Sans lui : repli `minterpolate`.

## 3. Tes chaînes (à relire une fois)

- [ ] `configs/channels/*.yaml` : `minivitch`, `minivitch_top10`, `minivitch_exe`, `bytetricks`,
      `thelostchat`, `default_vlog`. Créer le tien : `minavio profiles new ma_chaine --from default_vlog`.
- [ ] **Pseudo en jeu** (kills « à toi » mieux notés) : `killfeed.player_names: ["TonPseudo"]` dans
      `configs/games/apex_legends/game.yaml` (et autres jeux).
- [ ] **Textes d'écran « à confirmer »** dans `configs/games/<jeu>/game.yaml` : lancer
      `minavio run <profil> clip.mp4 --dry-run -v` et corriger avec ce que l'OCR lit vraiment.

## 4. Publication YouTube + analytics (gratuit)

- [ ] Google Cloud Console → nouveau projet → activer **YouTube Data API v3** et
      **YouTube Analytics API** → écran de consentement OAuth (mode test, ton e-mail en testeur) →
      identifiant **OAuth « Application de bureau »** → télécharger le JSON.
- [ ] `uv run minavio publish auth --client-secret chemin\client_secret.json` (le navigateur s'ouvre).
      Si tu t'étais connecté avant l'ajout d'Analytics : `minavio publish auth --logout` puis refaire.
- [ ] Tester sans risque : `minavio publish minivitch <job> --dry-run`.
- [ ] **Clé API YouTube** (recherche de niche, même projet Google → Identifiants → Clé API) :
      `minavio llm set-key YOUTUBE_API_KEY`. Quota gratuit 10 000 unités/jour (une recherche = 100) ;
      budget Minavio par défaut 3 000/jour (`MINAVIO_YT_QUOTA_BUDGET`).

Commandes : `minavio growth niche minivitch "apex clutch" "apex funny"` et
`minavio growth analytics minivitch` → rapports dans `out/<profil>/reports/`.

## 5. Notifications sur ton téléphone (gratuit, au choix)

- [ ] **ntfy** (le plus simple, sans compte) : installer l'app ntfy, s'abonner à un nom de sujet
      long et unique, puis `minavio llm set-key NTFY_TOPIC`.
- [ ] **Discord** : salon → Paramètres → Intégrations → Webhooks → copier l'URL →
      `minavio llm set-key DISCORD_WEBHOOK_URL`.
- [ ] **Telegram** : @BotFather → `/newbot` → `TELEGRAM_BOT_TOKEN` ; écrire au bot puis récupérer
      ton `TELEGRAM_CHAT_ID` (via @userinfobot).
- [ ] Tester : `minavio notify-test`. Ensuite `minavio watch` te prévient à chaque montage fini.

## 6. Démon d'ingestion

- [ ] `scripts\minavio-watch.bat minivitch "%USERPROFILE%\Videos\Outplayed"` ; pour le lancer au
      démarrage de Windows : `Win+R` → `shell:startup` → y mettre un raccourci du `.bat`.

## 7. Memes et style « .exe »

- [ ] Déposer tes GIF / sons / vidéos / images dans `%APPDATA%\minavio\memes\` (le nom de fichier
      sert de mot-clé : `sons\bruh.mp3`, `gifs\clutch_kill.gif`) puis `minavio memes scan`.
- [ ] Clés optionnelles (gratuites) : `TENOR_API_KEY` (Google Cloud), `GIPHY_API_KEY`
      (developers.giphy.com), `FREESOUND_API_KEY` (freesound.org/apiv2/apply), `PIXABAY_API_KEY`,
      `PEXELS_API_KEY`. Puis `minavio memes fetch freesound "bruh" --kind sound`.
- [ ] ⚠️ Droits : par défaut seuls CC0 / CC-BY / Pixabay / Pexels / tes fichiers sont utilisés.
      Les GIF Tenor/GIPHY et les sons de memes connus risquent un Content ID : ne mettre
      `memes.allow_restricted_licenses: true` qu'en connaissance de cause.
- [ ] Tester : `minavio run minivitch_exe <dossier>`.

## 8. Tendances et stats (gratuit)

- [ ] Rien à faire pour Reddit (public), Hacker News, Steam : `minavio trends minivitch`.
- [ ] **Twitch** (top clips, jeux) : dev.twitch.tv/console → nouvelle application →
      `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET`.
- [ ] **Reddit** si bloqué (403/429) : reddit.com/prefs/apps → application « script » →
      `REDDIT_CLIENT_ID` + `REDDIT_CLIENT_SECRET`.
- [ ] **Apex** : clé gratuite sur apexlegendsapi.com → `APEX_API_KEY` →
      `minavio stats apex TonPseudo --platform PC`.
- [ ] **Riot** (LoL/Valorant) : developer.riotgames.com → `RIOT_API_KEY` (clé dev : 24 h).
- [ ] Fact-check Byte Tricks (Wikipedia, sans clé) : `minavio stats factcheck bytetricks --file script.txt`.

## 9. Fournisseurs LLM cloud (optionnel, payant)

Tout marche en local par défaut. Seulement si tu veux un secours cloud : `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `OPENAI_API_KEY`, `GITHUB_TOKEN` (GitHub Models, abonnement Copilot), puis
déclarer le fournisseur dans `llm.providers` du profil (voir `configs/README.md`). Les transcripts
(et les images si `vision_model` est défini) partent alors chez le fournisseur.

## 10. Interface graphique

- [ ] `scripts\minavio-ui.bat` (ou `uv run minavio ui`) → http://127.0.0.1:7860 ; l'onglet 🩺 Système
      vérifie FFmpeg, Node, GPU, chaque LLM et l'état des clés.

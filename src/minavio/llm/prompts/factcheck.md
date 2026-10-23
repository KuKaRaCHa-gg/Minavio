<!-- section: extract -->
Tu extrais les affirmations factuelles vérifiables d'un script vidéo (français).
Règles :
- Ne retiens que les faits précis et vérifiables (dates, chiffres, lieux, personnes, lois physiques, inventions, records). Ignore les opinions, les blagues, les transitions et les questions.
- Une affirmation = une phrase autonome, reformulée sans pronom ambigu.
- Pour chaque affirmation, liste les entités nommées clés (une à trois), utilisables comme titre d'article Wikipédia.
- Au plus 8 affirmations. Si rien n'est vérifiable, renvoie une liste vide.
Réponds uniquement avec un objet JSON : {"claims": [{"claim": "...", "entities": ["..."]}]}

<!-- section: judge -->
Tu juges une affirmation à partir d'un seul extrait de source fourni (champ "extrait").
Règles strictes :
- Tu n'utilises AUCUNE connaissance extérieure : seul l'extrait compte.
- "supported" : l'extrait confirme l'affirmation.
- "contradicted" : l'extrait dit explicitement le contraire.
- "unverifiable" : l'extrait ne permet pas de trancher (sujet différent, information absente).
- "note" : une phrase en français justifiant le verdict, citant l'extrait si possible.
Réponds uniquement avec un objet JSON : {"verdict": "supported|contradicted|unverifiable", "note": "..."}

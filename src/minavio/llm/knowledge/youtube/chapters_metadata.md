<!-- kb: tasks=chapters,copy -->
# Chapitres et métadonnées YouTube

## Règles des chapitres
Les chapitres YouTube viennent des horodatages de la description. Conditions : le premier horodatage est 0:00, au moins trois horodatages dans l'ordre croissant, chaque chapitre dure au moins 10 secondes. Une ligne au mauvais format peut empêcher l'activation de toute la liste. Format `M:SS Titre` (ou `H:MM:SS` dès une heure), un par ligne, dans la description et non dans les commentaires. Minavio génère ces lignes dans `<job>_chapters.txt`.

## Titres de chapitres
Courts : 2 à 5 mots, sans numérotation (« Chapitre 3 »), sans émoji, sans guillemets. Décrire le contenu de la scène (« Clutch 1v3 », « Squad wipe », « Fin de ring ») plutôt qu'un label vague (« Moment chaud »). Éviter les titres identiques : numéroter les doublons. Le premier chapitre peut s'appeler « Intro ». Écrire dans la langue de la vidéo. Le titre doit refléter ce qui se passe réellement dans ce segment.

## Longueur et placement
Un chapitre trop court est ignoré : fusionner les segments de moins de 10 secondes avec le voisin. Si la vidéo ne permet pas trois chapitres valides, ne pas en produire plutôt que d'en forcer. Les chapitres suivent la chronologie du montage final, pas celle de la source. Un chapitre par moment fort est un bon compromis ; pas plus d'un chapitre toutes les 15 secondes.

## Limites de métadonnées
Titre : 100 caractères maximum, soigner les 60 premiers. Description : 5 000 caractères maximum (limite à revérifier dans YouTube Studio). Tags : 500 caractères au total, d'importance secondaire. Hashtags : au plus 60 sinon ignorés, 3 à 5 pertinents suffisent. Les mots-clés doivent correspondre au contenu : les métadonnées trompeuses sont interdites par la politique de spam de YouTube.

## Tags ou description
La description sert le spectateur et la recherche : résumé dans les 2 premières lignes, chapitres, crédits (créateur d'origine, musique, jeu), liens. Les tags aident à corriger une orthographe ou un nom ambigu mais pèsent peu. Ne pas empiler des mots-clés sans rapport. Pour un clip repris d'un créateur, citer son nom et le lien de la vidéo d'origine dans la description.

## Format du copy généré
Titre : une promesse concrète, ≤ 70 caractères. Hook : ≤ 8 mots. Description : 1 à 3 phrases, sans promesse non tenue. Tags : 5 à 12, sans doublons, sans #. Popups : textes très courts alignés sur un moment réel, jamais en dehors de la durée du montage. Ne pas inventer de nom, de chiffre ou de fait absent des moments fournis.

## Sources
- YouTube Help, chapitres de vidéos (règles 0:00, 3 horodatages, 10 secondes), confirmées via plusieurs guides tiers (Tella, Dickinson College, WP Manage Ninja).
- YouTube Help, « Spam, pratiques trompeuses et arnaques » : métadonnées trompeuses, plafond de 60 hashtags.
- Limites 100 / 5 000 / 500 caractères : documentation YouTube Studio, non revérifiées lors de cette recherche.

# Changelog

## 0.2.0 (2026-09-12)

Phase 0 réalisée sur le tenant de recette, et mise en conformité du code avec le Back Office réel.
**Changement de contrat HTTP incompatible avec 0.1.0.**

### Découvertes structurantes (`docs/DISCOVERY.md`)

- Le Back Office **n'accepte pas les identifiants de l'API Recruiting Customer**. Seul `applicantGuid`,
  identifiant interne indisponible côté Hippolyte.ai, adresse une fiche ; tous les autres paramètres d'URL
  sont ignorés. Une candidature est désormais désignée par **email du candidat + identifiant d'offre**.
- L'authentification est **fédérée sur trois domaines**, avec un écran de choix de compte.
- Les formulaires d'événement et de pièces jointes vivent dans des **iframes**.
- **Déposer une pièce jointe dans une catégorie occupée écrase** le document existant, sans avertissement.
- Le **commentaire d'un événement n'est pas relisible** dans l'historique, et Talentsoft ne dédoublonne pas.

### Contrat HTTP

- `application_id` / `application_url` remplacés par `candidate_email` + `offer_id` partout.
- Routes à identifiant supprimées : `/applications/events` et `/applications/documents` (plus de `{id}`).
- `/applications/documents` n'accepte plus qu'**un seul fichier** par appel (une catégorie = un champ).
- Nouveaux résultats d'action : `category_occupied`, `comment_too_long`, `multiple_documents_same_category`.
- Les événements vérifiés portent `"verification": "weak"` : le commentaire n'étant pas relisible, la
  relecture ne peut pas certifier que la ligne observée est celle du bot.
- L'email du candidat n'apparaît jamais en clair dans les réponses ni dans les logs (empreinte seulement).

### Garde-fous ajoutés

- Refus d'agir si la recherche par email ne désigne pas **un seul** candidat.
- Vérification que la candidature de la bonne offre est active **avant** toute mutation.
- Refus de déposer dans une catégorie de pièce jointe occupée, non configurable.
- Suppression du dédoublonnage d'événement par relecture : il produisait des faux positifs et aurait
  silencieusement perdu des synthèses.
- Commentaire au-delà de 2000 caractères refusé, au lieu d'être tronqué par le navigateur.
- `UPLOAD_ALLOWED_EXTENSIONS` aligné sur ce que Talentsoft accepte (`odt`, `txt`, `png`, `jpg` retirés).

### Configuration

- Nouvelles variables : `TS_AUTH_HOSTS` (obligatoire), `TS_ACCOUNT_CHOICE`, `TS_OFFER_URL_TEMPLATE`,
  `TS_SELFTEST_CANDIDATE_EMAIL`, `TS_SELFTEST_OFFER_ID`.
- `COMMENT_MAX_CHARS` plafonné à 2000 par le code.
- `TS_DEFAULT_EVENT_TYPE` n'a plus de valeur par défaut : `Commentaire` n'existe pas dans le référentiel du
  tenant, et certains types envoient un courrier au candidat — le choix revient au client.

### Tests

- Faux Back Office reconstruit à l'image du vrai : login fédéré, onglets Telerik, historique à deux niveaux,
  iframes, `confirm()` natif, écrasement des pièces jointes.
- 63 tests, dont 15 de bout en bout couvrant les garde-fous (mauvaise candidature, catégorie occupée,
  commentaire trop long, dépôt multiple).

## 0.1.0 (2026-09-12)

Première version : ossature complète du bot Talentsoft pour Hippolyte.ai.

- API FastAPI : `POST /update-application` (événement + pièces jointes), `POST /applications/{id}/events`,
  `POST /applications/{id}/documents`, `GET /applications/{id}/events`, référentiels, `POST /selftest`,
  `POST /admin/reset-session`, `GET /jobs/{id}`, healthcheck sans mutex.
- Navigateur Playwright : session partagée, `storage_state` persisté (0600), recyclage idle / max age,
  navigation principale limitée au tenant, traces uniquement en échec et purgées.
- Sécurité : Bearer token à temps constant avec rotation, allowlist d'URL, uploads renommés + octets magiques,
  commentaire borné, aucun secret ni donnée candidat dans les logs, conteneur non root en lecture seule.
- Résilience : dédoublonnage des mutations en sync et en async, aucun rejeu après début de mutation,
  résultat `unverified` explicite, deadline par job, état dégradé après échecs de login répétés.
- Tests : 47 tests unitaires sans navigateur et une suite de bout en bout sur un faux Back Office au vrai Chromium.
- Sélecteurs Talentsoft : gabarits à valider sur le tenant de recette (phase 0, `docs/DISCOVERY.md`).

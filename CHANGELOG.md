# Changelog

## 0.3.0 (2026-09-13)

Un seul navigateur pour tout le déploiement, piloté par une file. **Un `202` peut désormais répondre à un
appel synchrone** : c'est le seul changement de contrat pour l'appelant.

### Le problème corrigé

L'api et le worker ouvraient **chacun leur Chromium**, avec le même compte technique. Le tenant n'admettant
qu'une session active par compte, la seconde connexion invalidait la première ; le processus lésé relançait
un login, qui invalidait celle de l'autre, jusqu'à épuiser `LOGIN_MAX_FAILURES` et basculer en `degraded`.

`browser_lock.py` repose sur un `threading.Lock()`, qui ne franchit pas la frontière de processus : il ne
sérialisait donc rien entre les deux. Et `?async=1` n'existant que sur `/update-application`, toutes les
autres routes — `/selftest` en tête, que la documentation recommande de planifier en cron — s'exécutaient
dans l'api pendant que le worker tenait son propre navigateur.

### Architecture

- **Un déploiement = un propriétaire de navigateur** (`config.browser_owner()`) : le `worker` dès que
  `TS_ASYNC_JOBS_ENABLED=true`, l'`api` sinon (mono-processus : dev, tests, petites installations).
- L'api ne construit plus jamais de `TalentsoftBot` en mode worker. Un test paramétré sur **toutes** les
  routes navigateur le vérifie, pour qu'une régression soit un test rouge et non une déconnexion en recette.
- La file `ts:jobs:read`, jusqu'ici consommée mais jamais alimentée, porte les lectures et les référentiels.
  `BRPOP` la sert **avant** `ts:jobs:push` : une lecture ne patiente pas derrière vingt pushs.
- Nouveau `app/browser_runner.py` : le worker n'importe plus `app.main`, et donc ne construit plus une
  application FastAPI pour exécuter un job.
- Attente d'un résultat par `BLPOP` sur une clé de complétion, sans scrutation.
- Le worker publie un battement de cœur depuis un **thread dédié** — pas depuis sa boucle de jobs, qu'un
  push d'une minute ne traverse pas.

### Contrat HTTP

- **`202` sur dépassement d'attente** (`SYNC_WAIT_TIMEOUT_SECONDS`, 120 s) avec `job_id`, `Location` et
  `Retry-After`, là où un `500` survenait auparavant. Volontairement pas un `5xx` : à cet instant l'écriture
  est peut-être en cours, et un `5xx` inviterait à rejouer.
- Rejeu d'une clé d'idempotence dont le job tourne encore : `202` avec le **même** `job_id`, au lieu du `409`
  inexploitable.
- **Un seul corps de réponse `202`** — `{job_id, status, poll}` plus l'en-tête `Location` — quel que soit le
  chemin : `?async=1`, attente dépassée, ou rejeu. L'appelant n'a qu'un cas à coder.
- `503` + `Retry-After` quand le worker est absent ou sa session `degraded`, sans rien empiler.
- `GET /jobs/{id}` expose `error_code` (stable, analysable) et `error_detail` (diagnostic humain) au lieu du
  seul `"HTTPException"`, ainsi que `mutation_may_have_happened`.
- `GET /` expose `browser_owner`, un bloc `worker` et la profondeur des files ; il répond `200` même quand
  Redis est injoignable, avec `worker.alive = false`.
- `POST /admin/reset-session` en mode worker : `202`, la demande étant enfilée en **lecture** pour rester
  possible quand la file de pushs est saturée.

### Fiabilité

- `mutation_started` n'est plus posé au lancement du job mais à la **première écriture réelle**, via un
  callback `on_mutation_started` appelé par le scraper juste avant la première soumission. Un job qui
  échouait au login devenait auparavant définitivement non rejouable, et laissait croire à une écriture.
- La clé d'idempotence n'est libérée que si aucune écriture n'a été engagée.
- Le chemin `?async=1` réserve désormais la clé d'idempotence, comme le chemin synchrone.
- `LoginError` ne transporte plus le texte de la page du fournisseur d'identité : il reste dans le log. Ce
  texte remontait jusqu'à la réponse HTTP depuis que les détails d'erreur sont exposés.

### Infrastructure

- `api` et `worker` n'écrivent plus dans le même `state/` : deux `storage_state.json` partagés
  s'écrasaient mutuellement les cookies.
- Healthcheck du worker fondé sur la fraîcheur de son battement ; `depends_on: api` retiré.
- Avertissement `config_suspecte` au démarrage de l'api quand `REDIS_URL` est défini sans
  `TS_ASYNC_JOBS_ENABLED` — configuration typique de la panne corrigée ici.
- `make worker-status`.

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
- Mapping HTTP des échecs d'identification de candidature : candidat introuvable et candidat sans
  candidature sur l'offre donnent `404` ; plusieurs candidats pour un email donnent `409`. Sans ce mapping,
  ces cas remontaient en `500` et le client ne pouvait pas les distinguer d'une panne.
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

# Architecture technique de Talentsoft-bot

Service HTTP qui pilote le Back Office recruteur Cegid Talentsoft dans un navigateur, pour le compte
d'Hippolyte.ai. Il comble deux limites de l'API Recruiting Customer constatées sur tenant réel :
aucun événement typé avec commentaire, aucune pièce jointe sur une candidature existante.

## Vue d'ensemble

```
Hippolyte.ai (API NestJS)  --HTTP Bearer-->  Talentsoft-bot (FastAPI)  --Playwright/Chromium-->  Back Office Talentsoft
                                                   |  1 thread navigateur, 1 session partagée
                                                   |  Redis (optionnel) : jobs async + idempotence
```

Une instance de bot = un tenant Talentsoft (compte technique dédié), comme pour le DR bot.

## Modules

| Fichier | Rôle |
| --- | --- |
| `app/main.py` | FastAPI : authentification Bearer (temps constant, rotation), uploads sûrs, executor mono-thread + admission, `run_with_session`, idempotence sync, routes |
| `app/config.py` | Lecture centralisée des variables d'environnement |
| `app/safety.py` | Allowlist d'URL (`TS_BASE_URL` + hôtes d'authentification), validation email/offre, appariement de référence d'offre, chemins d'upload, octets magiques, contrat `actions_succeeded` |
| `app/browser_lock.py` | Mutex navigateur + file d'admission bornée (503 + `Retry-After`) |
| `app/idempotency.py` | Réservation et mémorisation des résultats par clé (Redis ou mémoire) |
| `app/session_manager.py` | Un `TalentsoftBot` partagé : recyclage idle / max age / navigateur mort, état dégradé après échecs de login |
| `app/scraper.py` | `TalentsoftBot` : launch Playwright, `storage_state`, login, ouverture de fiche, événement, pièces jointes, référentiels, auto-test, traces |
| `app/ts_pages.py` | Page objects (`LoginPage` avec choix de compte, `GlobalSearch`, `ApplicationPage`, `EventDialog`, `AttachmentsDialog`, `CookieBanner`), `first_locator`, `Deadline`, conversion de date |
| `app/ts_selectors.py` | Tous les sélecteurs et gabarits d'URL du Back Office : le seul fichier à retoucher quand Cegid change l'interface |
| `app/jobs.py`, `app/worker.py` | File Redis et worker : `mutation_started` persisté avant exécution, jamais de rejeu |
| `tools/discover.py` | Phase 0 : capture HAR, trace, DOM et résumé des sélecteurs sur le tenant |

## Flux `POST /update-application`

1. Vérification du token, validation de `candidate_email` et `offer_id`, commentaire borné à 2000 caractères,
   fichiers : extension (celles que Talentsoft accepte réellement), taille, renommage, octets magiques, `0600`.
2. Clé d'idempotence : fournie par l'appelant ou dérivée de
   `(sha256(email), offer_id, event_type, sha256(comment), sha256(fichiers))`. L'email n'entre dans la clé que
   sous forme d'empreinte. Résultat déjà mémorisé : réponse immédiate avec `X-Idempotent-Replay: true`.
   Même requête en cours : 409.
3. Admission dans la file (503 + `Retry-After` si pleine) puis exécution dans l'unique thread navigateur.
4. `session_manager.get_bot()` : réutilise la session ou relance Chromium + login fédéré (bandeau de consentement
   refusé, écran de choix de compte franchi, formulaire de l'IdP). Un retry de bootstrap, aucun après.
5. `TalentsoftBot.update_application` :
   - recherche du candidat **par email** dans la recherche globale ; 0 résultat → 404, plusieurs → refus ;
   - onglet Historique, clic sur la candidature portant la **référence de l'offre** ;
   - **vérification que la bonne candidature est active** avant toute mutation ;
   - événement : ouverture du formulaire (iframe), choix du type, date convertie en `JJ/MM/AAAA`, commentaire
     contrôlé contre la troncature, validation, relecture **faible** de l'historique ;
   - document : lecture des catégories occupées, **refus si la catégorie l'est**, dépôt dans le champ de la
     ligne portant le libellé voulu, validation, relecture par nom de fichier.
6. Réponse HTTP 200 : `success` = toutes les actions `ok` (les `skipped` comptent), détail par action.
   Trace Playwright conservée uniquement en échec, purgée après `TRACES_RETENTION_DAYS`.

## Règles de résilience

- Aucun rejeu après début de mutation : navigateur perdu ou deadline dépassée en cours de job = invalidation de la
  session et 500, le client ne relance pas aveuglément.
- **L'idempotence ne peut pas s'appuyer sur la relecture** pour les événements : le Back Office n'affiche pas le
  commentaire, et Talentsoft ne dédoublonne pas. Un dédoublonnage sur `(type, date)` confondrait deux synthèses
  distinctes du même jour et en perdrait une : il est volontairement absent. Seule la clé d'idempotence protège.
- **Aucun écrasement de pièce jointe** : déposer dans une catégorie occupée détruit le document existant sans
  avertissement, et le formulaire ne signale pas l'occupation. Le bot lit l'état de la fiche avant d'agir et
  refuse (`category_occupied`). Ce contrôle n'est pas configurable.
- Résultat `{"ok": false, "error": "unverified", "mutation_may_have_happened": true}` quand le clic a eu lieu mais que
  la relecture n'a pas confirmé : côté Hippolyte.ai, statut `INDETERMINATE`, relire via `GET /applications/events`.
- Deadline globale par job (`JOB_TIMEOUT_SECONDS`) en plus des timeouts par action.
- État `degraded` après `LOGIN_MAX_FAILURES` échecs de login dans la fenêtre : plus aucune tentative automatique,
  503 explicite, sortie par `POST /admin/reset-session` ou redémarrage.
- `POST /selftest` (lecture seule) détecte un changement d'interface avant que les pushs échouent.

## Sécurité

- Token Bearer statique comparé à temps constant, `API_TOKEN_PREVIOUS` pour la rotation, API publiée sur `127.0.0.1` uniquement.
- Navigation principale bloquée hors de `TS_BASE_URL` **et des hôtes d'authentification déclarés** (`TS_AUTH_HOSTS`) :
  l'authentification étant fédérée sur trois domaines, l'allowlist est nécessaire au login, et reste fermée pour
  tout le reste. Les cookies de session ne sortent pas de ce périmètre.
- `state/storage_state.json` en `0600`, jamais dans les traces ni les logs. Aucune trace pendant le login.
- Logs sans mot de passe, sans cookie, sans commentaire, sans nom de fichier d'origine, **et sans email de
  candidat** : seules des empreintes. L'email est une donnée personnelle et sert d'identifiant de recherche.
- Conteneur non root (`pwuser`), système de fichiers en lecture seule, `cap_drop ALL`, `no-new-privileges`.

## Concurrence

Un seul Chromium, jobs sérialisés (Uvicorn `--workers 1`). Suffisant pour le rythme des synthèses ; pour du volume,
plusieurs instances (une par tenant) ou un mode rejeu HTTP (phase 2 si les XHR internes le permettent).

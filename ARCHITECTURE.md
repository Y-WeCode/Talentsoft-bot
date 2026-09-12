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
| `app/safety.py` | Allowlist d'URL sur `TS_BASE_URL`, validation d'identifiants, chemins d'upload, octets magiques, contrat `actions_succeeded` |
| `app/browser_lock.py` | Mutex navigateur + file d'admission bornée (503 + `Retry-After`) |
| `app/idempotency.py` | Réservation et mémorisation des résultats par clé (Redis ou mémoire) |
| `app/session_manager.py` | Un `TalentsoftBot` partagé : recyclage idle / max age / navigateur mort, état dégradé après échecs de login |
| `app/scraper.py` | `TalentsoftBot` : launch Playwright, `storage_state`, login, ouverture de fiche, événement, pièces jointes, référentiels, auto-test, traces |
| `app/ts_pages.py` | Page objects (`LoginPage`, `ApplicationPage`, `EventDialog`, `AttachmentsPanel`), `first_locator`, `Deadline` |
| `app/ts_selectors.py` | Tous les sélecteurs et gabarits d'URL du Back Office : le seul fichier à retoucher quand Cegid change l'interface |
| `app/jobs.py`, `app/worker.py` | File Redis et worker : `mutation_started` persisté avant exécution, jamais de rejeu |
| `tools/discover.py` | Phase 0 : capture HAR, trace, DOM et résumé des sélecteurs sur le tenant |

## Flux `POST /update-application`

1. Vérification du token, validation `application_id` (ou `application_url` sur le tenant uniquement),
   commentaire borné, fichiers : extension, taille, renommage, octets magiques, `0600`.
2. Clé d'idempotence : fournie par l'appelant ou dérivée de `(application_id, event_type, sha256(comment), sha256(fichiers))`.
   Résultat déjà mémorisé : réponse immédiate avec `X-Idempotent-Replay: true`. Même requête en cours : 409.
3. Admission dans la file (503 + `Retry-After` si pleine) puis exécution dans l'unique thread navigateur.
4. `session_manager.get_bot()` : réutilise la session ou relance Chromium + login (un retry de bootstrap, aucun après).
5. `TalentsoftBot.update_application` : ouverture de la fiche (re-login une fois si la session a expiré, avant toute mutation),
   onglet historique, dédoublonnage (`already_present`), ajout de l'événement, relecture de l'historique (`verified`),
   puis pour chaque document : dédoublonnage par nom, `set_input_files`, catégorie, validation, relecture de la liste.
6. Réponse HTTP 200 : `success` = toutes les actions `ok` (les `skipped` comptent), détail par action.
   Trace Playwright conservée uniquement en échec, purgée après `TRACES_RETENTION_DAYS`.

## Règles de résilience

- Aucun rejeu après début de mutation : navigateur perdu ou deadline dépassée en cours de job = invalidation de la
  session et 500, le client ne relance pas aveuglément.
- Résultat `{"ok": false, "error": "unverified", "mutation_may_have_happened": true}` quand le clic a eu lieu mais que
  la relecture n'a pas confirmé : côté Hippolyte.ai, statut `INDETERMINATE`, relire via `GET /applications/{id}/events`.
- Deadline globale par job (`JOB_TIMEOUT_SECONDS`) en plus des timeouts par action.
- État `degraded` après `LOGIN_MAX_FAILURES` échecs de login dans la fenêtre : plus aucune tentative automatique,
  503 explicite, sortie par `POST /admin/reset-session` ou redémarrage.
- `POST /selftest` (lecture seule) détecte un changement d'interface avant que les pushs échouent.

## Sécurité

- Token Bearer statique comparé à temps constant, `API_TOKEN_PREVIOUS` pour la rotation, API publiée sur `127.0.0.1` uniquement.
- Navigation principale bloquée hors de `TS_BASE_URL` (route Playwright) : les cookies de session ne sortent pas du tenant.
- `state/storage_state.json` en `0600`, jamais dans les traces ni les logs. Aucune trace pendant le login.
- Logs sans mot de passe, sans cookie, sans commentaire, sans nom de fichier d'origine (empreintes uniquement).
- Conteneur non root (`pwuser`), système de fichiers en lecture seule, `cap_drop ALL`, `no-new-privileges`.

## Concurrence

Un seul Chromium, jobs sérialisés (Uvicorn `--workers 1`). Suffisant pour le rythme des synthèses ; pour du volume,
plusieurs instances (une par tenant) ou un mode rejeu HTTP (phase 2 si les XHR internes le permettent).

# Architecture technique de Talentsoft-bot

Service HTTP qui pilote le Back Office recruteur Cegid Talentsoft dans un navigateur, pour le compte
d'Hippolyte.ai. Il comble deux limites de l'API Recruiting Customer constatées sur tenant réel :
aucun événement typé avec commentaire, aucune pièce jointe sur une candidature existante.

## Vue d'ensemble

```
Hippolyte.ai  --HTTP Bearer-->  api (FastAPI, AUCUN navigateur)
                                   |  empile un job, attend le résultat
                                   v
                                 Redis  ts:jobs:read  (lectures, prioritaire)
                                        ts:jobs:push  (mutations)
                                   |
                                   v
                                 worker  --Playwright/Chromium-->  Back Office Talentsoft
                                         UN navigateur, UNE session, réutilisée entre les jobs
```

Une instance de bot = un tenant Talentsoft (compte technique dédié), comme pour le DR bot.

## Modules

| Fichier | Rôle |
| --- | --- |
| `app/main.py` | FastAPI : authentification Bearer (temps constant, rotation), uploads sûrs, admission, idempotence, traduction des codes métier en statuts HTTP, routes |
| `app/browser_runner.py` | Exécution d'un travail navigateur **sans dépendance à FastAPI** : mutex, bootstrap de session, traduction des exceptions en codes métier stables |
| `app/config.py` | Lecture centralisée des variables d'environnement |
| `app/safety.py` | Allowlist d'URL (`TS_BASE_URL` + hôtes d'authentification), validation email/offre, appariement de référence d'offre, chemins d'upload, octets magiques, contrat `actions_succeeded` |
| `app/browser_lock.py` | Mutex navigateur chez le propriétaire ; file d'admission bornée côté api (503 + `Retry-After`) |
| `app/idempotency.py` | Réservation et mémorisation des résultats par clé (Redis ou mémoire) |
| `app/session_manager.py` | Un `TalentsoftBot` partagé : recyclage idle / max age / navigateur mort, état dégradé après échecs de login |
| `app/scraper.py` | `TalentsoftBot` : launch Playwright, `storage_state`, login, ouverture de fiche, événement, pièces jointes, référentiels, auto-test, traces |
| `app/ts_pages.py` | Page objects (`LoginPage` avec choix de compte, `GlobalSearch`, `ApplicationPage`, `EventDialog`, `AttachmentsDialog`, `CookieBanner`), `first_locator`, `Deadline`, conversion de date |
| `app/ts_selectors.py` | Tous les sélecteurs et gabarits d'URL du Back Office : le seul fichier à retoucher quand Cegid change l'interface |
| `app/jobs.py` | Les deux files, l'état des jobs, l'attente de résultat (`BLPOP`), le battement de cœur du worker |
| `app/worker.py` | Seul propriétaire du navigateur : routage par type de job, `mutation_started` persisté à la première écriture, jamais de rejeu |
| `tools/discover.py` | Phase 0 : capture HAR, trace, DOM et résumé des sélecteurs sur le tenant |

## Flux `POST /update-application`

1. Vérification du token, validation de `candidate_email` et `offer_id`, commentaire borné à 2000 caractères,
   fichiers : extension (celles que Talentsoft accepte réellement), taille, renommage, octets magiques, `0600`.
2. Clé d'idempotence : fournie par l'appelant ou dérivée de
   `(sha256(email), offer_id, event_type, sha256(comment), sha256(fichiers))`. L'email n'entre dans la clé que
   sous forme d'empreinte. Résultat déjà mémorisé : réponse immédiate avec `X-Idempotent-Replay: true`.
   Même requête déjà en cours : **202** avec son `job_id` si un job la porte, `409` sinon.
3. Admission (503 + `Retry-After` si pleine), puis empilage sur `ts:jobs:push` et attente du résultat dans
   la limite de `SYNC_WAIT_TIMEOUT_SECONDS`. Budget dépassé : **202** avec le `job_id` à suivre.
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

## Concurrence : un déploiement = un propriétaire de navigateur

**Le tenant n'admet qu'une session active par compte technique.** Une seconde connexion invalide la première ;
le processus lésé se reconnecte, ce qui invalide l'autre, jusqu'à épuiser `LOGIN_MAX_FAILURES` et basculer en
`degraded`. Ce n'est pas une optimisation : c'est une contrainte du fournisseur.

D'où l'invariant, tenu par `config.browser_owner()` :

| `TS_ASYNC_JOBS_ENABLED` | Propriétaire du navigateur | Rôle de l'api |
| --- | --- | --- |
| `true` (production) | le `worker`, seul | guichet : empile, attend, ne construit **jamais** de `TalentsoftBot` |
| `false` (dev, tests, petite installation) | l'`api`, mono-processus | exécute dans son executor mono-thread |

Le test paramétré `test_api_never_opens_a_browser_in_worker_mode` remplace `TalentsoftBot` par un constructeur
qui échoue, sur **toutes** les routes navigateur : une régression devient un test rouge, pas une déconnexion en
recette.

Piège opérationnel correspondant : **deux déploiements quelconques** visant le même `TS_USERNAME` reproduisent le
symptôme — deux conteneurs, deux hôtes, ou un poste de développement resté allumé. L'api journalise
`config_suspecte` au démarrage quand `REDIS_URL` est défini sans `TS_ASYNC_JOBS_ENABLED`, configuration typique
d'un worker qui tourne pendant que l'api s'ouvre son propre navigateur.

### Pourquoi pas un verrou distribué Redis

C'est la correction qui vient naturellement à l'esprit, et elle ne marche pas : **la session n'est pas dans Redis,
elle est dans un processus**. Avec un verrou partagé entre `api` et `worker` :

1. l'api prend le verrou, réutilise sa session, exécute, relâche ;
2. le worker prend le verrou ; sa propre session n'existe pas ou a été invalidée → il se connecte, ce qui
   invalide celle de l'api ;
3. l'api reprend le verrou, constate qu'elle n'est plus authentifiée, se reconnecte, invalide celle du worker.

Chaque changement de propriétaire coûte un login. Le verrou transforme une course aléatoire en ping-pong
déterministe, et produit précisément la tempête de logins qui déclenche l'état `degraded`. Partager
`storage_state.json` n'y change rien : une session invalidée côté serveur le reste, quel que soit le cookie relu.

Le seul emploi légitime d'un verrou Redis ici serait un verrou d'**unicité de propriétaire** au démarrage
(`SET ts:browser:owner NX EX`), pour qu'un second propriétaire échoue bruyamment. Il n'est pas implémenté.

### Débit

Une seule session, donc un job à la fois. Un push durant de l'ordre de la minute, le plafond est de 40 à 60 pushs
par heure. `ts:jobs:read` est servie **avant** `ts:jobs:push` (l'ordre des clés passé à `BRPOP` fait la priorité),
si bien qu'une consultation d'historique ou un `/selftest` ne patiente jamais derrière vingt pushs — mais elle
peut patienter derrière **un**, ce qui est le principal effet de bord de cette architecture. Au-delà de ce volume,
la réponse n'est pas la concurrence sur ce tenant mais une seconde instance avec **son propre compte technique**.

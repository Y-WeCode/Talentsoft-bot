# Déploiement Docker

Guide pour installer **une instance** du bot (un tenant Talentsoft) sur un hôte Docker.
Pour le contrat HTTP, voir [INTEGRATION.md](INTEGRATION.md) ; pour le pourquoi de l'architecture, [../ARCHITECTURE.md](../ARCHITECTURE.md).

## 1. Choisir le mode

| Mode | Services | Quand |
| --- | --- | --- |
| **Async (recommandé)** | `api` + `worker` + `redis` | Production. Le worker est le seul à piloter Chromium, l'api empile les jobs et attend |
| **Sync** | `api` seule | Recette, petite installation. L'api pilote elle-même le navigateur |

> **Règle d'or : un déploiement = un seul propriétaire de navigateur.** Le tenant n'admet qu'une session par compte
> technique. Deux conteneurs, deux hôtes, ou un poste de dev resté allumé avec le même `TS_USERNAME` se déconnectent
> mutuellement jusqu'à l'état `degraded`. Avant de déployer, vérifier qu'aucune autre instance n'utilise ce compte.

## 2. Prérequis

- Docker Engine ≥ 24 avec le plugin Compose v2 (`docker compose version`).
- `make` (optionnel mais pratique ; chaque cible se lit dans le [Makefile](../Makefile)).
- Environ **6 Go de RAM** disponibles par conteneur navigateur (limite `mem_limit: 6g`, `shm_size: 2gb`) et 2 CPU. En mode async, seul le worker consomme réellement cela, mais l'api porte les mêmes limites.
- Un accès sortant HTTPS vers le tenant et vers les hôtes d'authentification (voir `TS_AUTH_HOSTS`).
- Un **compte technique Talentsoft dédié** au bot (voir README, « Compte technique Talentsoft »).
- Un reverse proxy TLS devant l'api : le port n'est publié que sur `127.0.0.1`.
- Tous les services doivent être sur **le même hôte** : api et worker partagent `./uploads`.

## 3. Installation

```bash
git clone <url-du-depot> talentsoft-bot && cd talentsoft-bot
cp .env.example .env
chmod 600 .env
```

### 3.1 Renseigner `.env`

Variables **obligatoires** :

| Variable | Valeur |
| --- | --- |
| `TS_BASE_URL` | Back Office **recrutement** du tenant (`https://<tenant>-rh.talent-soft.com`), pas l'espace collaborateur |
| `TS_USERNAME` / `TS_PASSWORD` | Compte technique dédié |
| `TS_AUTH_HOSTS` | Les **trois** hôtes traversés au login : fédération, fournisseur d'identité, espace collaborateur. Un seul oublié fait échouer le login à la dernière étape |
| `API_TOKEN` | Token long et aléatoire, ex. `openssl rand -hex 32`. À communiquer à Hippolyte.ai |
| `TS_DEFAULT_EVENT_TYPE` | À valider avec le client. **Jamais** un type qui envoie un courrier au candidat |

Variables à regarder de près :

| Variable | Pourquoi |
| --- | --- |
| `TS_ACCOUNT_CHOICE` | Identifiant du compte sur l'écran « Sélectionnez le compte ». Vide + plusieurs options = le bot refuse de deviner |
| `TS_SELFTEST_CANDIDATE_EMAIL` / `TS_SELFTEST_OFFER_ID` | Candidature témoin (lecture seule), nécessaire à `/selftest` et aux référentiels |
| `API_HOST_PORT` | Port hôte (défaut `42201`). À changer pour une **seconde instance sur le même hôte** |
| `TRACES_ENABLED` / `SCREENSHOTS_ENABLED` | Laisser à `false` en production : les traces contiennent des données personnelles |
| `BROWSER_SESSION_IDLE_TTL_SECONDS` | À régler sur la cadence réelle des appels (voir commentaire dans `.env.example`) |

Pour le **mode async**, ajouter :

```dotenv
TS_ASYNC_JOBS_ENABLED=true
REDIS_URL=redis://talentsoft_bot_redis:6379/0
```

Viser Redis par le **nom du conteneur** (`talentsoft_bot_redis`) et non `redis` : sur un hôte partagé, `redis` peut désigner le Redis d'un autre projet. Pour un Redis externe protégé : `REDIS_URL=redis://:MOT_DE_PASSE@hote:6379/0`.

### 3.2 Préparer les dossiers de données

Les volumes sont des *bind mounts* (`./logs`, `./traces`, `./data`, `./uploads`, `./state`, `./state-api`) et les conteneurs tournent en **utilisateur non root** (`pwuser`) avec un système de fichiers en lecture seule. Si ces dossiers n'existent pas, Docker les crée en `root` et le bot ne peut pas y écrire.

```bash
PWUID=$(docker run --rm --entrypoint id mcr.microsoft.com/playwright/python:v1.62.0-noble -u pwuser)
mkdir -p logs traces data uploads state state-api
sudo chown -R "$PWUID:$PWUID" logs traces data uploads state state-api
```

> Le `chown` utilise l'uid réel de `pwuser` dans l'image, sans le supposer. Si vous changez l'image de base dans le `Dockerfile`, relancez la commande `id`.

### 3.3 Démarrer

**Mode async (recommandé)**

```bash
make deploy
```

`make deploy` refuse de démarrer si `.env` ne contient pas `TS_ASYNC_JOBS_ENABLED=true` et `REDIS_URL` : sans cela l'api ouvrirait son propre Chromium en plus de celui du worker. Équivalent manuel :

```bash
docker compose --profile async up -d --build api worker redis
```

**Mode sync**

```bash
make up          # docker compose up -d --build api
```

Sans `REDIS_URL`, l'api pilote elle-même le navigateur (`browser_owner: "api"`).

## 4. Vérifier le déploiement

```bash
make ps              # api, worker, redis : « healthy » (le worker met jusqu'à 60 s)
make health          # GET / : degraded=false
make worker-status   # browser_owner doit valoir « worker » en mode async
make verify-code     # api et worker : même version de code
make selftest        # login + candidature témoin + sélecteurs critiques
```

Critères de succès :

- `GET /` répond `200` avec `degraded: false`.
- Mode async : `browser_owner: "worker"` et `worker.alive: true`. L'api ne doit **pas** journaliser `config_suspecte`.
- `make selftest` répond `"ok": true`. Un `503` indique un sélecteur qui ne matche plus ([../app/ts_selectors.py](../app/ts_selectors.py)) ou un problème de login.
- Après plusieurs appels, `login_count` reste **stable** : la session est réutilisée.

Premier appel réel (avec un token jamais affiché) :

```bash
make event EMAIL=candidat@example.com OFFER=25152 COMMENT='Test Hippolyte.ai'
```

À réserver à une candidature de test : l'événement est réellement créé dans le Back Office.

## 5. Reverse proxy

L'api n'écoute que sur `127.0.0.1:${API_HOST_PORT}`. Exposer via un reverse proxy TLS, avec :

- `proxy_read_timeout` **supérieur** à `SYNC_WAIT_TIMEOUT_SECONDS` (120 s par défaut) ; sinon le client reçoit une coupure au lieu du `202` qui lui indique où suivre son job. Prendre par exemple 150 s.
- `client_max_body_size` ≥ 11m (upload limité à 10 Mo, `MAX_UPLOAD_SIZE_BYTES`).

Exemple nginx :

```nginx
location / {
    proxy_pass http://127.0.0.1:42201;
    proxy_read_timeout 150s;
    client_max_body_size 11m;
}
```

## 6. Exploitation

| Besoin | Commande |
| --- | --- |
| Logs api / worker | `make logs` / `make logs-worker` |
| Redémarrer | `make restart` |
| Arrêter | `make down` |
| Sortir de l'état `degraded` | `make reset-session` (après avoir corrigé la cause : mot de passe, session concurrente…) |
| Shell dans l'api | `make shell-api` |
| Supervision | Planifier `POST /selftest` (cron, Uptime Kuma) pour détecter un changement d'interface Cegid |

### Mise à jour

```bash
git pull
make deploy            # ou make deploy-nocache si une dépendance a changé
make verify-code       # api et worker doivent porter la même version
make selftest
```

Le piège classique est l'image *stale* : un conteneur reconstruit, pas l'autre. `make verify-code` le détecte.

### Rotation du token d'API

1. Mettre l'ancien token dans `API_TOKEN_PREVIOUS` et le nouveau dans `API_TOKEN`.
2. `docker compose restart api` (ajouter `worker` en mode async si la variable y est lue).
3. Mettre à jour Hippolyte.ai, puis vider `API_TOKEN_PREVIOUS` et redémarrer.

### Rotation du mot de passe Talentsoft

Modifier `TS_PASSWORD` dans `.env`, redémarrer **les deux** services (`make restart`), puis `make selftest`. Ne pas laisser l'ancien mot de passe provoquer 3 échecs de login : le bot passerait en `degraded`.

### Sauvegarde et rétention

- À sauvegarder : `.env` (secrets, à stocker dans un coffre).
- Jetables : `./state*` (session navigateur, reconstruite au login), `./uploads` (fichiers en transit), `./traces`.
- Redis du `docker-compose.yml` n'a **aucun volume** : file de jobs et clés d'idempotence (24 h) sont perdues si le conteneur est recréé. Acceptable en général (un rejeu après recréation n'est plus dédupliqué : éviter de déployer pendant un push en cours) ; sinon, utiliser un Redis externe persistant via `REDIS_URL`.
- Sans `REDIS_URL` (mode sync), l'idempotence est en mémoire du process : perdue à chaque redémarrage de l'api.
- `./logs` : prévoir une rotation (logrotate) ou un driver de logs Docker.
- Les logs ne contiennent jamais l'email du candidat en clair, seulement son empreinte.

## 7. Plusieurs instances (un tenant chacune)

Une instance par tenant, chacune dans son propre dossier de projet :

1. Cloner le dépôt dans un dossier distinct.
2. Changer `API_HOST_PORT` dans son `.env`.
3. Les `container_name` du `docker-compose.yml` sont **fixes** (`talentsoft_bot_api`, etc.) : deux instances sur le même hôte entrent en collision. Les renommer dans la copie, et adapter `REDIS_URL`, l'alias Redis et les variables `API` / `WORKER` du Makefile en conséquence.
4. Un `TS_USERNAME` **différent** par tenant, évidemment.

## 8. Dépannage

| Symptôme | Cause probable |
| --- | --- |
| `PermissionError` à l'écriture de `logs/`, `state/`… | Dossiers créés par Docker en `root` : refaire l'étape 3.2 |
| Login qui échoue à la toute dernière étape | Hôte d'atterrissage (espace collaborateur) absent de `TS_AUTH_HOSTS` |
| Login accepté mais jamais authentifié | User-agent `HeadlessChrome` refusé par l'IdP : laisser `BROWSER_USER_AGENT` vide (défaut bureau) |
| Déconnexions répétées, puis `degraded` | Une autre instance utilise le même `TS_USERNAME`, ou l'api a son propre navigateur : vérifier `browser_owner` et `config_suspecte` |
| Toutes les routes navigateur répondent `503` en mode async | Worker arrêté ou Redis injoignable (volontaire : pas de second navigateur dans l'api) |
| `AuthenticationError` Redis / api en `503` | `REDIS_URL` vise le Redis d'un voisin : utiliser `redis://talentsoft_bot_redis:6379/0` |
| Chromium qui plante sans OOM | Vérifier `shm_size` (≥ 512 Mo) ; voir les commentaires du `docker-compose.yml` |
| `502`/coupure côté client sur appel long | `proxy_read_timeout` inférieur à `SYNC_WAIT_TIMEOUT_SECONDS` |
| `docker compose ps` n'affiche pas worker/redis | Profil absent : ajouter `--profile async` (ou `make ps`) |

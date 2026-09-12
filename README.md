# Talentsoft-bot

Bot Hippolyte.ai pour le Back Office **Cegid Talentsoft**. Il réalise, via un navigateur piloté par Playwright,
ce que l'API Recruiting Customer ne permet pas :

- créer un **événement typé avec commentaire** sur une candidature ;
- **ajouter des pièces jointes** à une candidature existante.

Même modèle que le DR bot (Digital Recruiters) : service indépendant, une instance par tenant, appelé en HTTP
par Hippolyte.ai avec un token Bearer.

## Comment une candidature est désignée

**Point structurant, issu de la phase 0** (voir [docs/DISCOVERY.md](docs/DISCOVERY.md)) : le Back Office
**n'accepte pas les identifiants de l'API Recruiting Customer**. Une fiche n'est adressable que par un
identifiant interne (`applicantGuid`) dont Hippolyte.ai ne dispose pas, et tous les autres paramètres d'URL
sont ignorés.

Une candidature est donc désignée par le couple **email du candidat + identifiant de l'offre**, les deux
seules données à la fois disponibles côté Hippolyte.ai et exploitables dans l'interface :

```
email → recherche globale du Back Office → le candidat
offre → ligne « réf. <année>-<offer_id> » de son historique → la candidature
```

Garde-fous associés, non négociables :

| Situation | Comportement |
| --- | --- |
| La recherche ne retourne **aucun** candidat | `404`, aucune mutation |
| La recherche retourne **plusieurs** candidats | Refus : écrire sur le dossier d'un autre candidat serait une divulgation de données personnelles |
| Le candidat n'a **pas postulé** à cette offre | `404`, aucune mutation |
| La bonne candidature n'est pas active après sélection | Abandon **avant** toute mutation |

## Démarrage rapide

```bash
cp .env.example .env        # renseigner TS_BASE_URL, TS_AUTH_HOSTS, TS_USERNAME, TS_PASSWORD, API_TOKEN
docker compose up -d --build api
curl http://127.0.0.1:42201/
```

Mode async (recommandé pour les pushs avec documents derrière un reverse proxy) :

```bash
# .env : REDIS_URL=redis://redis:6379/0 et TS_ASYNC_JOBS_ENABLED=true
make deploy
```

## Contrat HTTP

Tous les endpoints métier exigent `Authorization: Bearer $API_TOKEN`.

| Méthode | Route | Rôle |
| --- | --- | --- |
| `GET` | `/` | Healthcheck sans mutex : `browser_busy`, `browser_inflight`, `session_authenticated`, `degraded` |
| `POST` | `/update-application?async=0\|1` | Événement + pièce jointe en un appel (multipart) |
| `POST` | `/applications/events` | Événement seul (JSON) |
| `POST` | `/applications/documents` | Pièce jointe seule (multipart) |
| `GET` | `/applications/events?candidate_email=&offer_id=` | Historique de la candidature, lecture seule |
| `GET` | `/referentials/event-types` | Types d'événement lus dans le Back Office (cache 1 h, candidature témoin) |
| `GET` | `/referentials/document-categories` | Catégories de pièces jointes (idem) |
| `POST` | `/selftest` | Lecture seule : login, candidature témoin, sélecteurs critiques. 503 si un sélecteur ne matche plus |
| `POST` | `/admin/reset-session` | Sortie de l'état dégradé, fermeture de la session navigateur |
| `GET` | `/jobs/{job_id}` | Statut d'un job async |

### `POST /update-application`

Champs multipart :

| Champ | Description |
| --- | --- |
| `candidate_email` | Email du candidat. Sert à le retrouver dans la recherche du Back Office |
| `offer_id` | Identifiant de l'offre. Sert à choisir la bonne candidature du candidat |
| `event_type` | Libellé du type d'événement. Défaut `TS_DEFAULT_EVENT_TYPE` |
| `comment` | Commentaire de l'événement, **2000 caractères maximum**. Sans commentaire, aucun événement n'est créé |
| `event_date` | `YYYY-MM-DD`, défaut aujourd'hui (converti en `JJ/MM/AAAA` pour le Back Office) |
| `documents` | **0 ou 1** fichier (`pdf, doc, docx, rtf, tif, tiff, xlsx, zip`), 10 Mo max, contenu vérifié |
| `document_category` | Libellé de la catégorie. Défaut `TS_DEFAULT_DOCUMENT_CATEGORY` |
| `idempotency_key` | Clé fournie par l'appelant. Sinon dérivée du contenu |

```bash
curl -sS -X POST http://127.0.0.1:42201/update-application \
  -H "Authorization: Bearer $API_TOKEN" \
  -F candidate_email="candidat@example.com" \
  -F offer_id=25152 \
  -F event_type="Candidature à l'étude" \
  -F comment="Hippolyte.ai : profil retenu, synthèse jointe" \
  -F documents=@synthese.pdf \
  -F document_category="Autres documents" \
  -F idempotency_key=push-25152-synthese-v1
```

Réponse (HTTP 200 dès que la candidature a été atteinte) :

```json
{
  "success": true,
  "update_details": {
    "candidate_email_hash": "a1b2c3d4e5f60718",
    "offer_id": "25152",
    "application_label": "agent d'escale commercial f/h ( réf. 2026-25152)",
    "updated_at": "2026-09-12T14:03:00",
    "mutation_started": true,
    "actions": {
      "event": {"ok": true, "event_type": "Candidature à l'étude", "mutation_started": true,
                "verified": true, "verification": "weak"},
      "documents": [
        {"ok": true, "filename": "synthese.pdf", "category": "Autres documents",
         "mutation_started": true, "verified": true}
      ]
    }
  }
}
```

L'email n'est jamais renvoyé en clair : seule son empreinte figure dans la réponse et dans les logs.

`success` vaut `true` si toutes les actions ont `ok: true` (`skipped` compte comme succès).

### Résultats d'action

| Résultat | Sens | Conduite côté Hippolyte.ai |
| --- | --- | --- |
| `{"ok": true, "verified": true}` | Mutation relue dans le Back Office | Terminé |
| `{"ok": true, "skipped": true, "reason": "already_present"}` | Document déjà présent à l'identique | Terminé |
| `{"ok": false, "error": "category_occupied"}` | **La catégorie contient déjà un document : déposer l'aurait détruit** | Choisir une autre catégorie, ou traiter à la main |
| `{"ok": false, "error": "comment_too_long"}` | Commentaire au-delà de 2000 caractères | Raccourcir et rejouer, aucune mutation n'a eu lieu |
| `{"ok": false, "error": "multiple_documents_same_category"}` | Plusieurs fichiers pour une seule catégorie | Un appel par document |
| `{"ok": false, "error": "event_failed" \| "upload_failed"}` | Échec avant clic de validation | Rejeu possible |
| `{"ok": false, "error": "unverified", "mutation_may_have_happened": true}` | Clic effectué, relecture non confirmée | Statut indéterminé : relire `GET /applications/events` avant tout rejeu |

Codes HTTP :

| Code | Cause |
| --- | --- |
| `400` | Validation : email malformé, offre invalide, extension refusée, rien à faire |
| `401` | Token absent ou invalide |
| `404` | Candidat introuvable, ou candidat sans candidature sur cette offre |
| `409` | Requête identique déjà en cours, ou **plusieurs candidats pour cet email** (levée d'ambiguïté requise) |
| `413` | Fichier trop volumineux |
| `503` | Navigateur occupé ou session dégradée, avec `Retry-After` |
| `500` | Erreur générique (le détail reste dans les logs du bot) |

Sur `404` et `409`, aucune mutation n'a eu lieu et la clé d'idempotence est libérée : un rejeu est légitime
une fois la cause corrigée.

### Deux limites du Back Office à connaître

**1. Le commentaire d'un événement n'est pas relisible.** L'historique n'affiche que `type | date | auteur`.
La vérification d'un événement est donc **faible** (`"verification": "weak"`) : elle confirme qu'un événement
du bon type a été créé ce jour-là, pas que c'est exactement le nôtre. Conséquence : le bot **ne déduit jamais**
qu'un événement est « déjà présent », car deux synthèses différentes du même jour seraient confondues et l'une
serait silencieusement perdue. L'idempotence repose **entièrement** sur la clé d'idempotence.

**2. Déposer une pièce jointe dans une catégorie occupée écrase le document existant**, sans avertissement, et
le formulaire n'indique pas l'occupation. Le bot lit donc la liste des pièces jointes avant d'agir et **refuse**
de déposer dans une catégorie occupée (`category_occupied`). Ce contrôle n'est pas configurable : remplacer une
pièce d'un dossier candidat reste un geste de recruteur.

### Idempotence

Un second appel avec la même `idempotency_key` (ou le même contenu) dans les 24 h renvoie le résultat mémorisé avec
l'en-tête `X-Idempotent-Replay: true`, sans toucher au Back Office. La clé est libérée si la mutation n'a pas démarré
(404, 503, erreur de bootstrap) pour permettre un rejeu légitime.

### Mode async

`?async=1` renvoie `202 {"job_id", "status"}`. Interroger `GET /jobs/{job_id}` : `status` passe de `queued` à
`running` puis `completed` (`result` = même contrat que le mode sync) ou `failed` (`error`). Un job déjà marqué
`mutation_started` n'est jamais rejoué par le worker.

## Configuration

Voir `.env.example`. Points clés :

- `TS_BASE_URL` : l'hôte du **Back Office recrutement**, qui n'est pas celui sur lequel on atterrit après
  authentification (celui-là est l'espace collaborateur).
- `TS_AUTH_HOSTS` : **obligatoire**. L'authentification est fédérée et traverse deux autres domaines ; sans cette
  allowlist, la navigation est bloquée et le bot ne peut pas se connecter.
- `TS_ACCOUNT_CHOICE` : libellé du compte à sélectionner sur l'écran de fédération. Si plusieurs options existent
  et que la valeur est vide, le bot refuse de deviner.
- `TS_DEFAULT_EVENT_TYPE` : **à choisir avec le client**. Certains types déclenchent l'envoi d'un courrier au
  candidat ; le type par défaut d'un bot ne doit jamais en être un.
- `TRACES_ENABLED=false` en production par défaut (les traces contiennent des données personnelles).
- `LOGIN_MAX_FAILURES` / `LOGIN_FAILURE_WINDOW_SECONDS` : au-delà, état `degraded`, plus aucune tentative de login
  (protège le compte technique d'un verrouillage Talentsoft). Sortie : `make reset-session`.
- `API_TOKEN_PREVIOUS` : rotation du token sans coupure.

### Compte technique Talentsoft

Créer un compte dédié au bot avec un rôle limité : consultation des candidatures du périmètre concerné, ajout
d'événements et de pièces jointes. Mot de passe sans expiration ou procédure de rotation planifiée (mettre à jour
`.env`, `docker compose restart api`). Les événements apparaîtront au nom de ce compte : préfixer les commentaires
("Hippolyte.ai : ...") si les recruteurs doivent distinguer l'origine.

Vérifier que ce compte **voit les mêmes écrans** que celui utilisé pendant la phase 0 : les actions de workflow
disponibles dépendent du rôle.

## Phase 0 : découverte sur le tenant

**Réalisée le 12/09/2026** sur le tenant de recette. Résultats dans :

- [docs/DISCOVERY.md](docs/DISCOVERY.md) — parcours, sélecteurs, pièges et risques
- [docs/event-types-airfrance.md](docs/event-types-airfrance.md) — 105 types d'événement (codes et libellés)
- [docs/document-categories-airfrance.md](docs/document-categories-airfrance.md) — 46 catégories de pièces jointes

Rejouer la découverte sur un autre tenant :

```bash
python tools/discover.py --out discovery --headed --har --trace
# ou, sans écran : --login --dump --open "<url d'une fiche>"
```

Les sélecteurs vivent dans `app/ts_selectors.py`, seul fichier à retoucher quand Cegid change l'interface.
Après modification : `make selftest`.

## Développement

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium   # ou BROWSER_EXECUTABLE_PATH=/chemin/vers/chromium
make test                                # ruff + pytest (unitaires + bout en bout sur faux Back Office)
```

Le faux Back Office (`tests/fixtures/fake_backoffice`) reproduit la structure réelle : authentification fédérée
en deux écrans, onglets Telerik, historique à deux niveaux, formulaires servis dans des iframes, `confirm()`
natif, et le comportement d'écrasement des pièces jointes. Les tests bout en bout valident donc les sélecteurs
et les garde-fous, pas seulement la mécanique Playwright.

## Exploitation

- Reverse proxy : `proxy_read_timeout` supérieur à la durée d'un push avec documents (plusieurs minutes), ou mode async.
- Un seul replica par tenant (`--workers 1`, mutex process-local).
- `make verify-code` après un déploiement async : api et worker doivent porter la même version.
- Supervision : `POST /selftest` planifié (cron ou Uptime Kuma) pour détecter un changement d'interface Cegid.

## Périmètre suivant (hors de ce dépôt)

Client HTTP côté Hippolyte.ai (`providers/talentsoft-bot.service.ts`, sur le modèle de `digital-recruiters.service.ts`),
bascule du `TalentsoftAdapter` vers le bot pour les événements typés et les documents a posteriori, idempotence via
`TalentsoftOutboundOperation`.

Ce client devra fournir `candidate_email` et `offer_id`, et traiter explicitement `category_occupied` et
`unverified` : ce sont les deux cas où une intervention humaine peut être nécessaire.

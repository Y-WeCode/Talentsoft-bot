# Talentsoft-bot

Bot Hippolyte.ai pour le Back Office **Cegid Talentsoft**. Il réalise, via un navigateur piloté par Playwright,
ce que l'API Recruiting Customer ne permet pas :

- créer un **événement typé avec commentaire** sur une candidature ;
- **ajouter des pièces jointes** à une candidature existante.

Même modèle que le DR bot (Digital Recruiters) : service indépendant, une instance par tenant, appelé en HTTP
par Hippolyte.ai avec un token Bearer.

## Démarrage rapide

```bash
cp .env.example .env        # renseigner TS_BASE_URL, TS_USERNAME, TS_PASSWORD, API_TOKEN
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
| `POST` | `/update-application?async=0|1` | Événement + pièces jointes en un appel (multipart) |
| `POST` | `/applications/{id}/events` | Événement seul (JSON `{event_type, comment, event_date, idempotency_key}`) |
| `POST` | `/applications/{id}/documents` | Pièces jointes seules (multipart `documents[]`, `document_category`) |
| `GET` | `/applications/{id}/events` | Historique de la fiche, lecture seule (pour lever un `unverified`) |
| `GET` | `/referentials/event-types` | Types d'événement lus dans le Back Office (cache 1 h, candidature témoin) |
| `GET` | `/referentials/document-categories` | Catégories de pièces jointes (idem) |
| `POST` | `/selftest` | Lecture seule : login, fiche témoin, sélecteurs critiques. 503 si un sélecteur ne matche plus |
| `POST` | `/admin/reset-session` | Sortie de l'état dégradé, fermeture de la session navigateur |
| `GET` | `/jobs/{job_id}` | Statut d'un job async |

### `POST /update-application`

Champs multipart :

| Champ | Description |
| --- | --- |
| `application_id` | Identifiant Talentsoft de la candidature (celui de `TalentsoftApplicationLink` côté Hippolyte.ai) |
| `application_url` | Optionnel, prioritaire : URL de la fiche. Refusée si elle n'est pas sur `TS_BASE_URL` |
| `event_type` | Libellé ou code du type d'événement. Défaut `TS_DEFAULT_EVENT_TYPE` |
| `comment` | Commentaire de l'événement. Sans commentaire, aucun événement n'est créé |
| `event_date` | `YYYY-MM-DD`, défaut aujourd'hui |
| `documents` | 0..n fichiers (`pdf, doc, docx, odt, rtf, txt, png, jpg`), 10 Mo max chacun, contenu vérifié |
| `document_category` | Libellé ou code de la catégorie. Défaut `TS_DEFAULT_DOCUMENT_CATEGORY` |
| `idempotency_key` | Clé fournie par l'appelant. Sinon dérivée du contenu |

```bash
curl -sS -X POST http://127.0.0.1:42201/update-application \
  -H "Authorization: Bearer $API_TOKEN" \
  -F application_id=12345 \
  -F event_type="Commentaire" \
  -F comment="Hippolyte.ai : profil retenu, synthèse jointe" \
  -F documents=@synthese.pdf \
  -F document_category="Autre" \
  -F idempotency_key=push-12345-synthese-v1
```

Réponse (HTTP 200 dès que la fiche a été atteinte) :

```json
{
  "success": true,
  "update_details": {
    "application_id": "12345",
    "application_url": "https://tenant.talent-soft.com/...",
    "updated_at": "2026-09-12T14:03:00",
    "mutation_started": true,
    "actions": {
      "event": {"ok": true, "event_type": "Commentaire", "mutation_started": true, "verified": true},
      "documents": [
        {"ok": true, "filename": "synthese.pdf", "category": "Autre", "mutation_started": true, "verified": true},
        {"ok": true, "skipped": true, "reason": "already_present", "filename": "cv.pdf", "category": "CV"}
      ]
    }
  }
}
```

`success` vaut `true` si toutes les actions ont `ok: true` (`skipped` compte comme succès).

Résultats d'action possibles :

| Résultat | Sens | Conduite côté Hippolyte.ai |
| --- | --- | --- |
| `{"ok": true, "verified": true}` | Mutation relue dans le Back Office | Terminé |
| `{"ok": true, "skipped": true, "reason": "already_present"}` | Déjà présent, rien fait | Terminé |
| `{"ok": false, "error": "event_failed" \| "upload_failed"}` | Échec avant clic de validation | Rejouer possible |
| `{"ok": false, "error": "unverified", "mutation_may_have_happened": true}` | Clic effectué, relecture non confirmée | Statut indéterminé : relire `GET /applications/{id}/events` avant tout rejeu |

Codes HTTP : `400` validation, `401` token, `404` candidature introuvable, `409` requête identique en cours,
`413` fichier trop volumineux, `503` + `Retry-After` navigateur occupé ou session dégradée, `500` générique.

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

- `TS_APPLICATION_URL_TEMPLATE` : gabarit d'URL d'une fiche, à confirmer en phase 0 (`docs/DISCOVERY.md`).
- `TRACES_ENABLED=false` en production par défaut (les traces contiennent des données personnelles). En échec
  uniquement, purge après `TRACES_RETENTION_DAYS`.
- `LOGIN_MAX_FAILURES` / `LOGIN_FAILURE_WINDOW_SECONDS` : au-delà, état `degraded` visible sur `GET /`, plus aucune
  tentative de login (protège le compte technique d'un verrouillage Talentsoft). Sortie : `make reset-session`.
- `API_TOKEN_PREVIOUS` : rotation du token sans coupure.

### Compte technique Talentsoft

Créer un compte dédié au bot avec un rôle limité : consultation des candidatures du périmètre concerné, ajout
d'événements et de pièces jointes. Mot de passe sans expiration ou procédure de rotation planifiée (mettre à jour
`.env`, `docker compose restart api`). Les événements apparaîtront au nom de ce compte : préfixer les commentaires
("Hippolyte.ai : ...") si les recruteurs doivent distinguer l'origine.

## Phase 0 : découverte sur le tenant

Les sélecteurs de `app/ts_selectors.py` sont des gabarits validés sur un faux Back Office
(`tests/fixtures/fake_backoffice`). Ils doivent être confirmés sur le tenant de recette :

```bash
make discover URL="https://<tenant>.talent-soft.com/<chemin d'une fiche>"
# ou, avec écran : python tools/discover.py --headed --har --trace
```

Consigner les constats dans `docs/DISCOVERY.md`, ajuster `ts_selectors.py`, puis `make selftest`.

## Développement

```bash
pip install -r requirements-dev.txt
python -m playwright install chromium   # ou BROWSER_EXECUTABLE_PATH=/chemin/vers/chromium
make test                                # ruff + pytest (unitaires + bout en bout sur faux Back Office)
```

## Exploitation

- Reverse proxy : `proxy_read_timeout` supérieur à la durée d'un push avec documents (plusieurs minutes), ou mode async.
- Un seul replica par tenant (`--workers 1`, mutex process-local).
- `make verify-code` après un déploiement async : api et worker doivent porter la même version.
- Supervision : `POST /selftest` planifié (cron ou Uptime Kuma) pour détecter un changement d'interface Cegid.

## Périmètre suivant (hors de ce dépôt)

Client HTTP côté Hippolyte.ai (`providers/talentsoft-bot.service.ts`, sur le modèle de `digital-recruiters.service.ts`),
bascule du `TalentsoftAdapter` vers le bot pour les événements typés et les documents a posteriori, idempotence via
`TalentsoftOutboundOperation`.

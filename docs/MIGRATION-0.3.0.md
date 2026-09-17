# Migration 0.2.0 → 0.3.0 — ce qui change côté appelant

Destinataire : l'équipe qui intègre Talentsoft-bot dans Hippolyte.ai.

**Deux changements obligent à toucher au code** : un `202` peut désormais répondre à un appel
synchrone (§1), et une perte de navigateur rend un job `failed` sans `update_details` (§7). Tout le
reste est additif — le contrat d'une réponse `200` est strictement inchangé, aucun champ n'a
disparu, aucune route n'a bougé.

Si votre client traite déjà `?async=1`, l'essentiel du travail est fait : il s'agit d'appliquer le
même traitement aux appels synchrones.

---

## Pourquoi cette version existe

Le bot tournait avec **deux processus qui ouvraient chacun leur navigateur**, authentifiés par le
même compte technique. Le tenant Talentsoft n'admettant qu'une session active par compte, la
seconde connexion invalidait la première ; le processus lésé se reconnectait, ce qui invalidait
celle de l'autre, jusqu'à ce que le bot cesse toute tentative (`degraded`).

En 0.3.0, **un seul processus pilote le navigateur** et tout passe par une file d'attente. Un
appel synchrone n'est donc plus qu'une attente de courtoisie : il rend `200` si le job finit à
temps, et `202` sinon.

---

## 1. Traiter le `202` — premier changement obligatoire

### Avant

Un appel synchrone rendait `200`, ou une erreur. Un dépassement se manifestait par un `500` ou une
coupure du reverse proxy.

### Maintenant

Au-delà de `SYNC_WAIT_TIMEOUT_SECONDS` (120 s par défaut, réglable côté bot) :

```http
HTTP/1.1 202 Accepted
Location: /jobs/3f2a…
Retry-After: 60

{ "job_id": "3f2a…", "status": "running", "poll": "/jobs/3f2a…" }
```

Ce corps est **identique pour tous les `202`** — attente dépassée, `?async=1`, ou rejeu d'une clé
d'idempotence dont le job tourne encore. Un seul cas à coder.

### Les deux pièges

> **Ne jamais rejouer après un `202`.** À cet instant l'écriture est peut-être en cours dans le
> Back Office. Rejouer créerait un doublon visible par les recruteurs, et indétectable : le Back
> Office n'affiche pas le commentaire d'un événement, et Talentsoft ne dédoublonne pas.
>
> C'est précisément pour cela que le bot répond `202` et non `504` : un `5xx` inviterait à rejouer.

> **Vérifier que votre client HTTP ne traite pas `202` comme une erreur.** Axios avec la
> configuration par défaut accepte tout statut `2xx`, donc `res.status === 202` arrive dans le
> chemin nominal, pas dans le `catch`. Si vous avez posé un `validateStatus` personnalisé,
> relisez-le.

### Rejouer le même appel est en revanche sans danger

Avec la **même** `idempotency_key`, le bot reconnaît la clé et renvoie le `job_id` déjà en cours,
avec un nouveau `202`. C'est le cas « mon process a redémarré, j'ai perdu le `job_id` ».

Corollaire : là où vous receviez un `409` « requête identique en cours », vous recevez maintenant
un `202` portant le `job_id` — exploitable, là où le `409` ne l'était pas. Le `409` subsiste, mais
uniquement quand aucun job ne porte la clé.

### Le code

```ts
const res = await this.http.post('/update-application', form, {
  headers: { Authorization: `Bearer ${this.token}` },
  timeout: 300_000,
});

if (res.status === 202) {
  return this.followJob(operation, res.data.job_id);
}
// 200 : contrat inchangé
return this.interpretResult(res.data);
```

```ts
private async followJob(
  operation: TalentsoftOutboundOperation,
  jobId: string,
): Promise<OutboundStatus> {
  // Persister jobId AVANT de sortir : un redémarrage ne doit pas perdre la trace du job,
  // sans quoi on ne saurait plus si l'écriture a eu lieu.
  await this.operations.update(operation.id, { talentsoftJobId: jobId });

  const { data } = await this.http.get(`/jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${this.token}` },
  });

  if (data.status === 'queued' || data.status === 'running') {
    return this.scheduleRetry(operation, 30);   // on RE-interroge, on ne repousse pas
  }
  if (data.status === 'failed') {
    // Une écriture engagée puis perdue : jamais de rejeu automatique.
    if (data.mutation_started) return OutboundStatus.INDETERMINATE;
    return ['candidate_not_found', 'application_not_on_offer'].includes(data.error_code)
      ? OutboundStatus.TARGET_NOT_FOUND
      : OutboundStatus.FAILED;
  }
  return this.interpretResult(data.result);     // même lecture qu'en mode synchrone
}
```

---

## 2. `mutation_started` devient fiable — un rejeu de plus est désormais légitime

**C'est une amélioration silencieuse, et elle mérite qu'on la relise.**

Avant, le worker posait `mutation_started = true` **au lancement** du job. Un job qui échouait au
login — donc sans la moindre écriture — était pourtant marqué comme ayant potentiellement muté, et
devenait définitivement non rejouable. Vous conclusiez à un `INDETERMINATE` à tort, et
l'opération partait en revue humaine pour rien.

Maintenant le drapeau ne tombe qu'à la **première écriture réelle**, juste avant la soumission du
formulaire. Donc :

| `status` | `mutation_started` | Ce que ça veut dire | Action |
| --- | --- | --- | --- |
| `failed` | `false` | Échec **avant** toute écriture : login, recherche, sélection de candidature | Rejeu légitime, même clé |
| `failed` | `true` | Une écriture a été engagée, la suite est inconnue | **`INDETERMINATE`**, vérifier avant tout rejeu |
| `completed` | `true` | Job terminé, lire `result` pour le détail par action | — |

Si votre code traite tout `failed` comme indéterminé, vous pouvez maintenant distinguer les deux
et rejouer automatiquement le premier cas. C'est optionnel, mais c'est le gain pratique de cette
version pour vous.

---

## 3. Diagnostic d'erreur : `error_code` remplace `error`

Un job échoué ne portait que `"error": "HTTPException"` — inexploitable. Il porte maintenant :

| Champ | Usage |
| --- | --- |
| `error_code` | Code **stable**, prévu pour être testé par machine |
| `error_detail` | Phrase de diagnostic pour un humain. **Ne jamais l'analyser** — son libellé peut changer |
| `error` | Conservé pour compatibilité, vaut désormais `error_code` |

Chaque code correspond au statut HTTP qu'un appel synchrone aurait reçu :

| `error_code` | HTTP | Mutation ? | Rejeu |
| --- | --- | --- | --- |
| `candidate_not_found` | `404` | non | après correction |
| `application_not_on_offer` | `404` | non | après correction |
| `ambiguous_candidate` | `409` | non | après levée d'ambiguïté humaine |
| `browser_busy` | `503` | non | après `Retry-After` |
| `session_degraded` | `503` | non | après intervention sur le bot |
| `session_bootstrap_failed` | `500` | **non** | oui, aucune écriture n'a eu lieu |
| `browser_fatal` | `500` | peut-être | vérifier d'abord |
| `job_timeout` | `500` | peut-être | vérifier d'abord |
| `session_expired` | `500` | peut-être | vérifier d'abord |
| `internal_error` | `500` | peut-être | vérifier d'abord |
| `mutation_started_no_rejeu` | `500` | oui | **non** |

Le job porte aussi `mutation_may_have_happened` : une écriture a eu lieu mais la relecture ne l'a
pas confirmée. Même traitement que le `"error": "unverified"` déjà présent dans `result`.

---

## 4. Le `503` gagne une cause

Toujours le même traitement de votre côté — respecter `Retry-After`, rejeu légitime, aucune
mutation — mais une cause de plus :

| Cause | `Retry-After` | Commentaire |
| --- | --- | --- |
| File d'admission pleine | court | inchangé |
| **Worker navigateur indisponible** | court | **nouveau** : personne ne dépile. Le bot refuse plutôt que de vous faire attendre pour rien |
| Session `degraded` | `600` | inchangé |
| File de jobs (Redis) injoignable | court | inchangé |

---

## 5. `GET /` : champs ajoutés, rien retiré

Vos sondes existantes continuent de fonctionner : `degraded`, `session_open`,
`session_authenticated`, `login_count` restent **à la racine**. S'ajoutent :

```json
{
  "browser_owner": "worker",
  "worker": { "alive": true, "session_open": true, "login_count": 3, "degraded": false },
  "queues": { "read": 0, "push": 2 }
}
```

Deux points à superviser :

- `worker.alive: false` durable → **toutes les écritures sont bloquées**. C'est le nouveau signal
  d'alerte principal.
- `login_count` **stable** sur une série de pushs est la preuve que la session est réutilisée. Une
  croissance rapide signale un retour du problème corrigé par cette version.

`GET /` répond `200` même quand Redis est injoignable, avec `worker: {alive: false, reason:
"redis_unreachable"}`. Un healthcheck qui tombe avec son infrastructure ne sert à rien.

---

## 6. `POST /admin/reset-session` rend `202`

La demande est traitée par le worker, qui possède la session. La réponse porte un `job_id` si vous
voulez confirmer l'exécution. Route d'exploitation ; sans impact si vous ne l'appelez pas depuis
Hippolyte.ai.

---

## 7. Une perte de navigateur devient un job en échec

**C'est la seule autre modification qui vous impose du code.**

Jusqu'ici, un navigateur perdu en cours de traitement se présentait comme deux échecs d'action
distincts et le job restait `completed` :

```json
{ "status": "completed",
  "result": { "success": false,
              "update_details": { "actions": {
                  "event":     { "ok": false, "error": "event_failed" },
                  "documents": [ { "ok": false, "error": "upload_failed" } ] } } } }
```

C'était trompeur : rien là-dedans ne dit qu'il s'agit d'une panne d'infrastructure, et `event_failed`
promet un rejeu sûr — parfois à tort. Désormais :

```json
{ "status": "failed",
  "error_code": "browser_fatal",
  "result": null,
  "mutation_started": true }
```

> **Votre affichage d'erreur doit accepter un job sans `update_details`.** Sur un job `failed`,
> `result` vaut `null` : tout code qui lit `result.update_details.actions` sans le vérifier lèvera.
> C'est le seul vrai piège de ce changement.

En mode synchrone, cela se traduit par un `500` là où vous receviez un `200` avec
`success: false`. Votre `catch` traite déjà le `500` en `INDETERMINATE` — ce qui est la bonne
conclusion, puisque `mutation_started` peut être vrai.

---

## 8. `event_failed` et `upload_failed` tiennent enfin leur promesse

Le tableau des résultats par action affirme que ces deux codes signalent un échec **avant** le clic
de validation, donc un rejeu sûr. Le bot les rendait aussi **après** le clic : un rejeu pouvait
alors créer un doublon.

C'est corrigé, et vérifié par des tests. La règle est maintenant garantie :

| Ce que vous recevez | Ce que ça veut dire | Ce que vous pouvez faire |
| --- | --- | --- |
| `error: "event_failed"` / `"upload_failed"` | Rien n'a été écrit | **Rejouer directement**, sans vérification |
| `error: "unverified"` + `mutation_may_have_happened: true` | Le clic est parti, le résultat est inconnu | `INDETERMINATE`, vérifier avant tout rejeu |

Concrètement : vous pouvez cesser d'envoyer les `event_failed` en revue humaine.

---

## 9. Les deux `mutation_started` ne se contredisent plus

Un job pouvait rendre `update_details.mutation_started: false` — « rien n'a été écrit » — alors que
le champ `mutation_started` du job valait `true`. Les deux dérivent désormais de la même source.

**Fiez-vous au `mutation_started` de premier niveau** : c'est celui qui fait foi, et le seul présent
sur un job `failed`, où `update_details` n'existe pas.

---

## Ce qui n'a **pas** changé

Pour éviter une relecture inutile de votre côté :

- le corps d'une réponse `200`, à l'octet près — `success`, `update_details`, `actions`, et les
  résultats par action (`category_occupied`, `comment_too_long`, `unverified`,
  `multiple_documents_same_category`) ;
- les champs multipart de `/update-application`, et toutes les autres routes ;
- les règles d'idempotence : clé dérivée de l'objet métier, jamais d'un horodatage ni d'un aléa ;
- `400`, `401`, `404`, `409`, `413` et leurs causes ;
- le plafond de 2000 caractères du commentaire, les extensions acceptées, la limite de 10 Mo ;
- le refus d'écraser une pièce jointe dans une catégorie occupée ;
- l'impossibilité de relire un commentaire d'événement, et donc le fait que **la clé
  d'idempotence reste la seule protection contre les doublons**.

---

## Checklist de recette côté Hippolyte.ai

- [ ] Un `202` sur un appel **synchrone** est bien suivi par `GET /jobs/{id}`, et **jamais** rejoué.
- [ ] Le `job_id` est persisté avant de rendre la main, pour survivre à un redémarrage.
- [ ] `validateStatus` du client HTTP relu : `202` ne doit pas partir dans le `catch`.
- [ ] Un `failed` avec `mutation_started: false` est rejoué automatiquement, sous la même clé.
- [ ] Un `failed` avec `mutation_started: true` part en `INDETERMINATE`, jamais en rejeu.
- [ ] Le diagnostic lit `error_code`, jamais `error_detail`.
- [ ] Supervision de `worker.alive` et de `login_count` sur `GET /`.
- [ ] Timeout HTTP client **supérieur** à `SYNC_WAIT_TIMEOUT_SECONDS` côté bot (120 s par défaut),
      pour recevoir le `202` plutôt qu'une coupure.
- [ ] L'affichage d'erreur accepte un job `failed` **sans** `update_details` (`result: null`).
- [ ] `error_code: "browser_fatal"` est traité comme une panne d'infrastructure, pas comme un refus
      métier.
- [ ] Les `event_failed` / `upload_failed` ne partent plus en revue humaine : ils sont rejouables
      directement.
- [ ] Le `mutation_started` lu est celui de **premier niveau**, pas celui de `update_details`.

---

Détail complet du contrat : [`docs/INTEGRATION.md`](INTEGRATION.md).
Raison d'être de l'architecture : [`ARCHITECTURE.md`](../ARCHITECTURE.md), section « Concurrence ».

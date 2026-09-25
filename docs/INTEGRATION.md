# Intégrer Talentsoft-bot dans Hippolyte.ai

Référence complète pour écrire le client HTTP côté Hippolyte.ai
(`providers/talentsoft-bot.service.ts`, sur le modèle de `digital-recruiters.service.ts`).

Version de l'API : **0.2.0**.

- [1. Ce que fait le bot](#1-ce-que-fait-le-bot)
- [2. Comment une candidature est désignée](#2-comment-une-candidature-est-désignée)
- [3. Authentification](#3-authentification)
- [4. Liste des endpoints](#4-liste-des-endpoints)
- [5. `POST /update-application`](#5-post-update-application)
- [6. Lire le résultat](#6-lire-le-résultat)
- [7. Codes HTTP](#7-codes-http)
- [8. Idempotence](#8-idempotence)
- [9. Mode asynchrone](#9-mode-asynchrone)
- [10. Lectures et référentiels](#10-lectures-et-référentiels)
- [11. Deux limites du Back Office](#11-deux-limites-du-back-office)
- [12. Squelette d'intégration](#12-squelette-dintégration)
- [13. Checklist avant mise en production](#13-checklist-avant-mise-en-production)

---

> **Vous migrez depuis la 0.2.0 ?** Lire d'abord [MIGRATION-0.3.0.md](MIGRATION-0.3.0.md) : un seul
> changement oblige à toucher au code, tout le reste est additif.

## 1. Ce que fait le bot

Le bot pilote le Back Office recruteur Cegid Talentsoft dans un navigateur, pour réaliser les deux
opérations que l'API Recruiting Customer ne permet pas :

- créer un **événement typé avec commentaire** sur une candidature ;
- **déposer une pièce jointe** sur une candidature existante.

Une instance de bot = un tenant Talentsoft, avec un compte technique dédié. Le service est publié sur
`127.0.0.1` uniquement et n'est appelé que par Hippolyte.ai.

**Ce n'est pas une API Talentsoft.** C'est un automate qui clique dans une interface web. Deux conséquences
qui structurent toute l'intégration :

- **c'est lent** — plusieurs dizaines de secondes par push, et **un seul job à la fois** ;
- **c'est fragile par nature** — une mise à jour d'interface Cegid peut casser un sélecteur. D'où
  `POST /selftest`, à planifier en supervision.

---

## 2. Comment une candidature est désignée

> C'est le point le plus important de ce document. Il explique une bizarrerie du contrat qui n'a rien
> d'arbitraire.

Le Back Office **n'accepte pas les identifiants de l'API Recruiting Customer**. Vérifié sur le tenant
(voir [DISCOVERY.md](DISCOVERY.md)) : une fiche n'est adressable que par un identifiant interne
(`applicantGuid`), et **tous les autres paramètres d'URL sont ignorés** — un identifiant de candidature
volontairement invalide ouvre exactement la même fiche.

Or Hippolyte.ai ne dispose pas de cet identifiant interne. Les GUIDs `_TS_…` de l'API appartiennent à un
autre référentiel, que le Back Office ne reconnaît pas.

Une candidature est donc désignée par un **couple** :

| Donnée | Rôle |
| --- | --- |
| `candidate_email` | Retrouver le candidat via la recherche globale du Back Office |
| `offer_id` | Choisir la bonne candidature parmi celles du candidat |

Le bot déroule :

```
1. candidate_email → recherche globale → le candidat
2. offer_id        → ligne « réf. 2026-25152 » de son historique → la candidature
3.                 → vérification que cette candidature est bien celle qui est active
4.                 → mutation
```

### Le bot refuse plutôt que de deviner

| Situation | Réponse | Pourquoi |
| --- | --- | --- |
| Aucun candidat pour cet email | `404` | Rien à faire |
| **Plusieurs candidats** pour cet email | `409` | Écrire dans le dossier d'un autre candidat serait une divulgation de données personnelles, pas un bug bénin |
| Candidat sans candidature sur cette offre | `404` | Le couple ne désigne rien |
| La bonne candidature n'est pas active après sélection | échec avant mutation | Un candidat peut avoir plusieurs candidatures |

Ces refus sont délibérés et ne sont pas configurables.

### Ce que le client doit fournir

L'email doit être celui **connu de Talentsoft**. Si Hippolyte.ai stocke un email normalisé ou un alias, il
faut s'assurer que c'est bien celui du dossier candidat — sinon la recherche ne retourne rien et le push
échoue en `404`.

---

## 3. Authentification

```http
Authorization: Bearer <API_TOKEN>
```

Token statique, comparé à temps constant. `API_TOKEN_PREVIOUS` permet une rotation sans coupure :
l'ancien token reste accepté le temps de mettre à jour Hippolyte.ai, puis se retire.

---

## 4. Liste des endpoints

### Écriture

| Méthode | Route | Usage |
| --- | --- | --- |
| `POST` | `/update-application` | **À privilégier.** Événement et/ou document en un appel (multipart) |
| `POST` | `/applications/events` | Événement seul (JSON) |
| `POST` | `/applications/documents` | Document seul (multipart, **un seul fichier**) |

`/update-application` n'ouvre la session navigateur qu'une fois pour les deux actions : c'est plus rapide et
moins exposé aux échecs partiels que deux appels séparés.

### Lecture

| Méthode | Route | Usage |
| --- | --- | --- |
| `GET` | `/applications/events?candidate_email=&offer_id=` | Historique de la candidature |
| `GET` | `/referentials/event-types` | Types d'événement du tenant (cache 1 h) |
| `GET` | `/referentials/document-categories` | Catégories de pièces jointes (cache 1 h) |
| `GET` | `/` | Healthcheck, sans prise du mutex navigateur |

### Exploitation

| Méthode | Route | Usage |
| --- | --- | --- |
| `POST` | `/selftest` | Auto-test lecture seule. `503` si un sélecteur ne matche plus |
| `POST` | `/admin/reset-session` | Sortir de l'état dégradé, fermer la session navigateur. `202` : la demande est traitée par le worker |
| `GET` | `/jobs/{job_id}` | Statut d'un job |
| `GET` | `/` | Santé : `browser_owner`, bloc `worker`, profondeur des files. Sans authentification |

---

## 5. `POST /update-application`

Champs multipart :

| Champ | Requis | Description |
| --- | --- | --- |
| `candidate_email` | **oui** | Email du candidat, tel que connu de Talentsoft |
| `offer_id` | **oui** | Identifiant de l'offre, ex. `25152`. Alphanumérique, 32 caractères max |
| `event_type` | non | Libellé du type, tel que retourné par le référentiel. Défaut : `TS_DEFAULT_EVENT_TYPE` |
| `comment` | non | Commentaire, **2000 caractères maximum**. Sans commentaire, aucun événement n'est créé |
| `event_date` | non | `YYYY-MM-DD`, défaut aujourd'hui |
| `documents` | non | **0 ou 1** fichier |
| `document_category` | non | Libellé de la catégorie. Défaut : `TS_DEFAULT_DOCUMENT_CATEGORY`. Première catégorie essayée |
| `document_categories` | non | **Champ répété** (0.4.0) : catégories de repli, dans l'ordre. Le bot dépose dans la **première catégorie libre** ; `document_category` reste en tête. Nettoyées, dédoublonnées, 10 max. Un bot 0.3.0 ignore ce champ |
| `idempotency_key` | non mais **fortement recommandé** | Voir §8 |

### Contraintes de fichier

Extensions acceptées : `pdf`, `doc`, `docx`, `rtf`, `tif`, `tiff`, `xlsx`, `zip`.
Taille : **10 Mo** maximum. Le contenu est vérifié contre l'extension (octets magiques).

> **`odt`, `txt`, `png`, `jpg` sont refusés** : Talentsoft ne les accepte pas. Le bot rejette en `400`
> plutôt que de laisser le dépôt échouer après le début de la mutation.

### Catégories de repli (0.4.0)

Le Back Office n'accepte qu'un fichier par catégorie. Plutôt que d'échouer quand « Compte rendu » est
occupé, envoyez une liste ordonnée : `-F document_category="Compte rendu" -F document_categories="Compte rendu" -F document_categories="Compte rendu 2" -F document_categories="Compte rendu 3"`.
Le bot, qui voit la fiche et le formulaire, procède en **deux passes** sur toute la liste :

1. le fichier est déjà présent dans l'une des catégories ⇒ `ok: true, skipped: true, reason: "already_present"`,
   `category` = celle qui le contient (aucun doublon, même si la première s'est libérée entre-temps) ;
2. sinon, dépôt dans la **première catégorie sans occupant** ; `category` = catégorie utilisée,
   `categories_tried` = catégories parcourues dans l'ordre.

Toutes occupées ⇒ `category_occupied` avec `occupied_by_category` (occupants par catégorie). Une catégorie
absente du formulaire n'est **jamais** approchée par sous-chaîne : si aucune catégorie libre n'existe
exactement dans le formulaire, `category_not_found`, rien n'est écrit.

### Un seul document par appel

Le formulaire Talentsoft n'offre **qu'un champ par catégorie**. Deux fichiers dans la même catégorie
signifierait que le second écrase le premier. Le bot refuse
(`multiple_documents_same_category`). Pour plusieurs documents : un appel par document, avec des catégories
différentes.

### Format de date

Le contrat attend l'**ISO** (`2026-09-14`) ; le Back Office veut `JJ/MM/AAAA`. La conversion est faite par
le bot. Envoyez toujours l'ISO.

### Exemple

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

---

## 6. Lire le résultat

Le **code HTTP** dit si la candidature a été atteinte. C'est le **détail par action** qui dit ce qui a
réellement été écrit. Les deux doivent être lus.

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
      "event": {
        "ok": true,
        "event_type": "Candidature à l'étude",
        "mutation_started": true,
        "verified": true,
        "verification": "weak"
      },
      "documents": [
        {
          "ok": true,
          "filename": "synthese.pdf",
          "category": "Autres documents",
          "mutation_started": true,
          "verified": true
        }
      ]
    }
  }
}
```

`success` vaut `true` si **toutes** les actions ont `ok: true` — un `skipped` compte comme un succès.

L'email n'est **jamais** renvoyé en clair : seule son empreinte (`candidate_email_hash`) figure dans la
réponse, et il n'apparaît pas non plus dans les logs du bot.

### Résultats possibles par action

| Résultat | Sens | Conduite |
| --- | --- | --- |
| `ok: true, verified: true` | Mutation relue dans le Back Office | Terminé |
| `ok: true, skipped: true, reason: "already_present"` | Document déjà présent à l'identique dans l'une des catégories demandées (`category` la désigne) | Terminé |
| `ok: false, error: "category_occupied"` | Toutes les catégories demandées contiennent déjà un document ; y déposer l'aurait **détruit**. `occupied_by` (première catégorie) et `occupied_by_category` listent ce qui s'y trouve, `categories_tried` ce qui a été essayé | **Cas métier**, pas technique : arbitrage humain ou autres catégories de repli. Pas de rejeu automatique ; un rejeu **avec d'autres catégories** sous la même clé crée un nouveau job (§8) |
| `ok: false, error: "category_not_found"` | Aucune catégorie libre demandée n'existe **exactement** dans le formulaire (libellé erroné ou absent du paramétrage). Rien n'a été écrit | Corriger les libellés (référentiel §10) et rejouer |
| `ok: false, error: "comment_too_long"` | Le champ a reçu la saisie mais l'a tronquée (> 2000 caractères). Rien n'a été écrit | **Raccourcir** puis rejouer |
| `ok: false, error: "comment_not_retained"` | Le champ n'a rien retenu du tout, après trois tentatives. Rien n'a été écrit | **Rejouer tel quel** : la longueur n'est pas en cause |
| `ok: false, error: "multiple_documents_same_category"` | Plusieurs fichiers pour une catégorie | Un appel par document |
| `ok: false, error: "event_type_required"` | Aucun type fourni et aucun défaut configuré | Corriger l'appel ou la configuration du bot |
| `ok: false, error: "event_failed"` / `"upload_failed"` | Échec **avant** le clic de validation. Rien n'a été écrit | Rejeu possible |
| `ok: false, error: "unverified", mutation_may_have_happened: true` | Clic effectué, relecture non confirmée | **Statut indéterminé** : relire l'historique avant tout rejeu |

> **Cette distinction est garantie par le code**, et vérifiée par des tests. Une interruption
> survenue *après* le clic ne rend jamais `event_failed` ni `upload_failed` — elle rend
> `unverified` avec `mutation_may_have_happened: true`. Vous pouvez donc rejouer un
> `event_failed` sans le vérifier au préalable.
>
> Une **perte du navigateur** en cours de job ne se présente plus comme un échec d'action : le
> job entier passe `failed` avec `error_code: browser_fatal`. C'est une panne d'infrastructure,
> pas un refus métier — et `mutation_started` vous dira s'il faut vérifier avant de rejouer.

### `verification: "weak"`

Un événement vérifié porte ce marqueur. L'historique du Back Office n'affiche que *type · date · auteur* —
jamais le commentaire. La relecture confirme donc qu'un événement du bon type a été créé ce jour-là, **pas
que c'est exactement le nôtre**. Voir §11.

---

## 7. Codes HTTP

| Code | Cause | Mutation ? | Rejeu |
| --- | --- | --- | --- |
| `200` | Candidature atteinte — lire le détail par action | selon les actions | — |
| `202` | Job accepté : `?async=1`, **ou** attente synchrone dépassée | peut-être en cours | interroger `/jobs/{id}`, jamais rejouer |
| `400` | Validation : email malformé, offre invalide, extension refusée, ni commentaire ni document | non | après correction |
| `401` | Token absent ou invalide | non | non |
| `404` | Candidat introuvable, ou sans candidature sur cette offre | non | oui, après correction |
| `409` | Requête identique en cours, ou **plusieurs candidats** pour cet email | non | après levée d'ambiguïté |
| `413` | Fichier trop volumineux | non | non |
| `503` | Navigateur occupé, worker absent, ou session dégradée. En-tête `Retry-After` | non | oui, après le délai |
| `500` | Erreur générique (détail dans les logs du bot) | **peut-être** | prudence |

Sur `400`, `404`, `409` et `503`, aucune mutation n'a eu lieu et la clé d'idempotence est libérée : un rejeu
est légitime une fois la cause corrigée.

Sur `500`, la mutation a pu démarrer. Ne pas rejouer à l'aveugle.

### `503` : un seul job à la fois

Le bot sérialise tout : **un seul navigateur pour tout le déploiement**, parce que le tenant Talentsoft
n'admet qu'une session active par compte technique. Au-delà de la file d'admission, il répond `503` avec
`Retry-After`. Le client doit respecter ce délai plutôt que de réessayer immédiatement — une file d'attente
côté Hippolyte.ai est préférable à des retries serrés.

Trois causes distinctes, toutes rejouables, toutes sans mutation :

- file d'attente pleine (`Retry-After` court) ;
- **worker indisponible** : personne ne dépile ; le bot refuse plutôt que de faire attendre pour rien ;
- session `degraded` (`Retry-After: 600`) : trop d'échecs de login, intervention humaine requise.

### `202` : l'attente a expiré, le job continue

**C'est le point de contrat qui a changé.** Un appel synchrone attend le résultat pendant
`SYNC_WAIT_TIMEOUT_SECONDS` (120 s par défaut). Au-delà il rend :

```http
202 { "job_id": "…", "status": "queued" | "running", "poll": "/jobs/…" }
Location: /jobs/…
```

Ce corps est **identique pour tous les `202`** : attente dépassée, `?async=1`, ou rejeu d'une clé dont le job
tourne encore. Un seul cas à coder.

À traiter **comme un job asynchrone** : interroger `GET /jobs/{job_id}` jusqu'à `completed` ou `failed`.

> Ne **jamais** rejouer après un `202`, ni sous la même clé, ni sous une nouvelle.
>
> À cet instant l'écriture est peut-être en cours dans le Back Office. C'est exactement pour cela que le
> bot répond `202` et non `504` : un `5xx` inviterait à rejouer, et créerait un doublon dans le dossier du
> candidat.

Rejouer le **même** appel avec la **même** `idempotency_key` est en revanche sans danger : le bot reconnaît
la clé et renvoie le `job_id` déjà en cours, avec un nouveau `202`.

---

## 8. Idempotence

Un second appel portant la même `idempotency_key` — ou le même contenu — dans les 24 h renvoie le résultat
mémorisé avec l'en-tête `X-Idempotent-Replay: true`, sans toucher au Back Office.

> **Seul est mémorisé ce qui n'est pas rejouable.** Un appel qui échoue **sans avoir rien écrit**
> libère sa clé : le rejeu sous la même clé refait réellement le travail, au lieu de vous rendre
> l'échec précédent. C'est ce qui rend tenable la promesse du tableau des résultats par action —
> un `event_failed` ou un `upload_failed` se rejoue directement.
>
> Dès qu'une écriture a été engagée, la clé reste prise, y compris quand le succès global est
> faux : un événement écrit et un document en échec ne se rejouent pas, sous peine de créer un
> doublon de l'événement.

> **Fournissez systématiquement une `idempotency_key`.**
>
> Le bot **ne peut pas** détecter qu'un événement est déjà présent : le Back Office n'affiche pas le
> commentaire, et Talentsoft ne dédoublonne pas — deux appels identiques créent deux événements.
> Cette clé est la **seule** protection contre les doublons.

Dérivez-la d'un objet métier stable côté Hippolyte.ai — l'identifiant de `TalentsoftOutboundOperation`
convient bien — et **jamais** d'un horodatage ou d'un aléa, qui la rendraient inopérante au rejeu.

> **Rejeu après un échec rejouable (0.4.0).** Quand la clé est libérée (`category_occupied`,
> `event_failed`, `upload_failed`…), le bot oublie aussi le job qui la portait : une re-soumission sous la
> même clé, éventuellement avec un autre payload (catégories de repli ajoutées), crée un **nouveau job**.
> En 0.3.0, la clé libérée pointait encore vers le job terminé et la re-soumission rendait l'ancien
> résultat pendant 24 h sans rien réexécuter.

---

## 9. Jobs et suivi

En production, **tout passe par une file** : lectures, écritures et `/selftest`. Le processus worker est le
seul à piloter un navigateur, parce que le tenant n'admet qu'une session par compte technique. Un appel
synchrone se contente donc d'attendre le résultat de son job, et bascule en `202` au-delà du budget.

`?async=1` sur `/update-application` court-circuite l'attente et rend le `202` immédiatement — utile pour un
push volumineux derrière un reverse proxy au `read_timeout` serré. Contrairement aux appels synchrones, ce
chemin **empile même si le worker est arrêté** : c'est voulu (un redémarrage de worker ne doit pas rejeter les
jobs), mais cela suppose de surveiller `GET /` — un `worker.alive: false` durable signifie que les jobs
s'accumulent sans être traités.

```http
POST /update-application?async=1
→ 202 { "job_id": "…", "status": "queued", "poll": "/jobs/…" }

GET /jobs/{job_id}
→ {
    "status": "queued" | "running" | "completed" | "failed",
    "result": …,             // contrat du mode synchrone
    "error_code": "…",       // code stable, analysable par machine
    "error_detail": "…",     // diagnostic destiné à un humain, à ne PAS analyser
    "mutation_started": false,
    "mutation_may_have_happened": false
  }
```

`error_code` correspond au statut HTTP qu'un appel synchrone aurait reçu :

| `error_code` | HTTP équivalent | Rejeu |
| --- | --- | --- |
| `candidate_not_found` | `404` | après correction |
| `application_not_on_offer` | `404` | après correction |
| `ambiguous_candidate` | `409` | après levée d'ambiguïté |
| `session_degraded` | `503` | après intervention |
| `browser_busy` | `503` | après `Retry-After` |
| `session_bootstrap_failed` | `500` | oui : aucune écriture n'a eu lieu |
| `browser_fatal`, `job_timeout`, `session_expired`, `internal_error` | `500` | **prudence**, vérifier d'abord |
| `mutation_started_no_rejeu` | `500` | non |

`mutation_started` dit qu'une écriture a été **engagée** : c'est ce drapeau qui interdit le rejeu. Il ne tombe
qu'à la première écriture réelle — un échec au login, à la recherche ou à la sélection laisse donc le job
pleinement rejouable.

Un job dont `mutation_started` est déjà vrai n'est **jamais rejoué** par le worker : il passe en `failed`
avec `error: "mutation_started_no_rejeu"`. C'est volontaire — mieux vaut un job en échec explicite qu'un
doublon silencieux dans le dossier d'un candidat.

---

## 10. Lectures et référentiels

### Historique d'une candidature

```bash
curl -sS -G http://127.0.0.1:42201/applications/events \
  -H "Authorization: Bearer $API_TOKEN" \
  --data-urlencode "candidate_email=candidat@example.com" \
  --data-urlencode "offer_id=25152"
```

```json
{ "offer_id": "25152", "events": ["candidature à l'étude 12/09/2026 recruteur test", "…"] }
```

Les lignes sont du texte normalisé (minuscules, espaces réduits) reprenant *type · date · auteur*.
**Le commentaire n'y figure pas.** C'est l'endpoint à appeler pour lever un `unverified`.

### Référentiels

Les types d'événement et les catégories sont **propres au tenant** et évoluent avec son paramétrage.
Lisez-les plutôt que de les coder en dur.

```http
GET /referentials/event-types
→ { "values": [{ "code": "1735", "label": "Candidature à l'étude" }, …], "cached": false }

GET /referentials/document-categories
→ { "values": ["CV", "Autres documents", "Compte rendu", …], "cached": false }
```

Les deux acceptent `candidate_email` et `offer_id` en query ; à défaut ils utilisent la candidature témoin
configurée sur le bot. Cache d'une heure.

Un instantané du tenant de recette est versionné à titre indicatif :
[event-types-airfrance.md](event-types-airfrance.md) (105 types) et
[document-categories-airfrance.md](document-categories-airfrance.md) (46 catégories).

---

## 11. Deux limites du Back Office

Ces deux comportements ont été constatés sur le tenant réel. Ils expliquent des choix du contrat qui
paraîtraient sinon excessivement prudents.

### 11.1 Le commentaire d'un événement n'est pas relisible

L'historique n'affiche que *type · date · auteur*. Le texte du commentaire n'existe nulle part dans la page.

Conséquences :

- la vérification est **faible** (`verification: "weak"`) ;
- le bot **ne conclut jamais** qu'un événement est « déjà présent ». Un dédoublonnage sur *(type, date)*
  confondrait deux synthèses différentes du même jour et **perdrait silencieusement la seconde**. Un
  doublon se voit ; une perte, non ;
- l'idempotence repose **entièrement** sur la clé fournie par le client.

### 11.2 Un dépôt en catégorie occupée détruit l'existant

Le formulaire n'offre qu'un champ par catégorie et **n'indique pas** qu'une catégorie contient déjà un
document. Y déposer un fichier **écrase l'ancien, sans avertissement ni confirmation**.

Le bot lit donc l'état de la fiche avant d'agir et refuse (`category_occupied`). **Ce contrôle n'est pas
configurable** : remplacer une pièce d'un dossier candidat reste un geste de recruteur.

> **Choix de catégorie côté Hippolyte.ai**
>
> Visez des catégories destinées aux dépôts automatiques : `Autres documents`, `Compte rendu`.
> **Jamais** `CV`, `Pièce d'identité`, ni une catégorie marquée « (obligatoire) » — elles participent à la
> complétude du dossier candidat, et une seule copie du document peut exister.
>
> Prévoyez des **catégories de repli** (`document_categories`, §5) : « Compte rendu », « Compte rendu 2 »,
> « Compte rendu 3 »… Le bot dépose dans la première libre et vous rend celle utilisée.

---

## 12. Squelette d'intégration

```ts
// providers/talentsoft-bot.service.ts

async pushSynthesis(
  application: TalentsoftApplicationLink,
  synthesis: Synthesis,
  operation: TalentsoftOutboundOperation,
): Promise<OutboundStatus> {
  const form = new FormData();
  form.append('candidate_email', application.candidateEmail);
  form.append('offer_id', application.offerId);
  form.append('event_type', this.config.defaultEventType);
  form.append('comment', truncate(synthesis.text, 2000));
  form.append('document_category', 'Autres documents');
  // Clé stable, dérivée de l'objet métier : c'est la seule protection contre les doublons.
  form.append('idempotency_key', operation.id);
  if (synthesis.file) {
    form.append('documents', synthesis.file, 'synthese.pdf');
  }

  let res: AxiosResponse;
  try {
    res = await this.http.post('/update-application', form, {
      headers: { Authorization: `Bearer ${this.token}` },
      timeout: 300_000, // un push avec document prend des dizaines de secondes
    });
  } catch (err) {
    const status = err.response?.status;

    // Aucune mutation : rejeu légitime plus tard.
    if (status === 404) return OutboundStatus.TARGET_NOT_FOUND;
    if (status === 409) return OutboundStatus.NEEDS_REVIEW;   // email ambigu
    if (status === 503) {
      const retryAfter = Number(err.response.headers['retry-after'] ?? 60);
      return this.scheduleRetry(operation, retryAfter);
    }
    // 500 : la mutation a pu démarrer. Ne pas rejouer à l'aveugle.
    if (status === 500) return OutboundStatus.INDETERMINATE;
    throw err;
  }

  // 202 : l'attente a expiré, le job continue. On le suit, on ne le rejoue JAMAIS.
  if (res.status === 202) {
    return this.followJob(operation, res.data.job_id);
  }

  const { actions } = res.data.update_details;
  const results = [actions.event, ...(actions.documents ?? [])].filter(Boolean);

  // Priorité 1 : l'indéterminé. Un rejeu créerait un doublon indétectable.
  if (results.some(r => r.error === 'unverified')) {
    return OutboundStatus.INDETERMINATE;
  }

  // Priorité 2 : rien n'a été détruit, mais rien n'a été déposé non plus.
  if (results.some(r => r.error === 'category_occupied')) {
    this.logger.warn(`Catégorie occupée sur l'offre ${application.offerId}`);
    return OutboundStatus.NEEDS_REVIEW;
  }

  return res.data.success ? OutboundStatus.DONE : OutboundStatus.FAILED;
}
```

### Suivre un job après un `202`

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
    return this.scheduleRetry(operation, 30); // on REinterroge, on ne repousse pas
  }
  if (data.status === 'failed') {
    // Une écriture engagée puis perdue : jamais de rejeu automatique.
    if (data.mutation_started) return OutboundStatus.INDETERMINATE;
    return ['candidate_not_found', 'application_not_on_offer'].includes(data.error_code)
      ? OutboundStatus.TARGET_NOT_FOUND
      : OutboundStatus.FAILED;
  }
  return this.interpretResult(data.result); // même lecture qu'en mode synchrone
}
```

### Lever un `INDETERMINATE`

```ts
const { data } = await this.http.get('/applications/events', {
  params: { candidate_email: email, offer_id: offerId },
  headers: { Authorization: `Bearer ${this.token}` },
});

// Attention : le commentaire n'apparaît pas dans l'historique.
// On ne peut chercher que le type et la date — la confirmation reste faible.
const found = data.events.some(
  e => e.includes(eventTypeLabel.toLowerCase()) && e.includes(frenchDate),
);
```

Si l'ambiguïté persiste, préférez une revue humaine à un rejeu : un doublon dans un dossier candidat est
visible par les recruteurs.

### Les trois réflexes

0. Un **`202` se suit**, il ne se rejoue pas : `GET /jobs/{id}` jusqu'à un état final.
1. **Toujours** fournir une `idempotency_key` stable, dérivée de l'objet métier.
2. **Ne jamais** rejouer sur `unverified` sans avoir relu l'historique.
3. Traiter `category_occupied` comme un **cas métier** demandant un arbitrage, pas comme une erreur
   technique à réessayer.

---

## 13. Checklist avant mise en production

### Côté tenant Talentsoft

- [ ] Compte technique dédié créé, avec un rôle limité au périmètre concerné.
- [ ] Vérifier que ce compte **voit les mêmes écrans** que celui utilisé en phase 0 : les actions de
      workflow disponibles dépendent du rôle.
- [ ] Mot de passe sans expiration, ou procédure de rotation planifiée.

### Côté configuration du bot

- [ ] `TS_BASE_URL` : l'hôte du Back Office **recrutement**, pas celui d'atterrissage après login.
- [ ] `TS_AUTH_HOSTS` renseigné — **obligatoire**, sans quoi le login SSO est bloqué.
- [ ] `TS_ACCOUNT_CHOICE` si l'écran de choix de compte propose plusieurs options.
- [ ] **`TS_DEFAULT_EVENT_TYPE` arbitré avec le client.** Certains types déclenchent l'envoi d'un
      **courrier au candidat** (d'où l'existence de variantes « sans envoi de courrier »). Le type par
      défaut d'un automate ne doit jamais en être un.
- [ ] `TS_DEFAULT_DOCUMENT_CATEGORY` pointant vers une catégorie non critique.
- [ ] `TS_SELFTEST_CANDIDATE_EMAIL` et `TS_SELFTEST_OFFER_ID` pour la supervision.
- [ ] `TS_ASYNC_JOBS_ENABLED=true` et `REDIS_URL` renseignés, **pour l'api comme pour le worker**.
      Sans cela l'api ouvre son propre navigateur en plus de celui du worker, et les deux sessions se
      déconnectent mutuellement sur le même compte technique. `make deploy` refuse ce cas de figure.
- [ ] `SYNC_WAIT_TIMEOUT_SECONDS` **inférieur** au `proxy_read_timeout` du reverse proxy.
- [ ] Aucun autre déploiement (autre hôte, poste de développement) n'utilise le même `TS_USERNAME`.

### Validation

- [ ] `POST /selftest` vert contre le tenant.
- [ ] Un push de bout en bout sur une candidature de test, avec vérification visuelle dans le Back Office.
- [ ] Comportement vérifié sur un email correspondant à **plusieurs** candidats (attendu : `409`).
- [ ] Comportement vérifié sur une catégorie déjà occupée (attendu : `category_occupied`, et le document
      d'origine **intact**).
- [ ] `GET /` renvoie `browser_owner: "worker"` et `worker.alive: true`.
- [ ] Un push **et** un `/selftest` lancés en parallèle : le selftest attend son tour, et `login_count`
      ne bouge pas. C'est la preuve qu'une seule session existe.
- [ ] Dix pushs enchaînés : `login_count` **stable**. C'est la preuve que la session est réutilisée, et la
      condition de tenue des dizaines de pushs par heure.
- [ ] Worker arrêté : `/update-application` répond `503`, et **aucun** navigateur n'apparaît côté api
      (`docker compose logs api | grep -c Chromium` doit rester à zéro).
- [ ] Redis coupé : toutes les routes navigateur rendent un `503` propre, et `GET /` reste en `200` avec
      `worker.alive: false`.
- [ ] Côté Hippolyte.ai : un `202` est bien suivi par `GET /jobs/{id}` et **jamais** rejoué.

### Exploitation

- [ ] `POST /selftest` planifié (cron ou Uptime Kuma) pour détecter un changement d'interface Cegid avant
      que les pushs échouent.
- [ ] Supervision de `degraded` sur `GET /` : au-delà de `LOGIN_MAX_FAILURES` échecs de login, le bot cesse
      toute tentative pour protéger le compte technique d'un verrouillage. Sortie par
      `POST /admin/reset-session`.
- [ ] `proxy_read_timeout` du reverse proxy supérieur à `SYNC_WAIT_TIMEOUT_SECONDS`.
- [ ] Un seul replica par tenant, `api` et `worker` sur **le même hôte** : ils partagent le volume
      `./uploads`, par lequel l'api transmet les fichiers au worker.
- [ ] Supervision de `worker.alive` sur `GET /` : un worker absent bloque toutes les écritures.

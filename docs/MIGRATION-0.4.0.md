# Migration 0.3.0 → 0.4.0 — ce qui change côté appelant

Destinataire : l'équipe qui intègre Talentsoft-bot dans Hippolyte.ai.

**Rien n'oblige à toucher au code.** Le contrat est additif : un appelant 0.3.0 continue de fonctionner à
l'identique. Deux nouveautés méritent d'être exploitées, et un comportement par défaut change côté bot.

---

## 1. Catégories de repli pour le dépôt (`document_categories`)

Le Back Office n'accepte qu'un fichier par catégorie ; « Compte rendu » occupé faisait échouer le dépôt de
la synthèse (`category_occupied`) sans alternative automatique.

Envoyez désormais une liste ordonnée, en **champ multipart répété** :

```bash
-F document_category="Compte rendu" \
-F document_categories="Compte rendu" \
-F document_categories="Compte rendu 2" \
-F document_categories="Compte rendu 3"
```

- `document_category` reste la première catégorie essayée (et le seul champ compris par un bot 0.3.0 :
  l'envoi des deux champs est compatible dans les deux sens de déploiement).
- Le bot dépose dans la **première catégorie libre**. Le résultat porte `category` (catégorie utilisée) et
  `categories_tried` (parcourues dans l'ordre) : enregistrez `category`, c'est là que la pièce se trouve.
- Déjà présent dans l'une des catégories ⇒ `ok: true, skipped: true, reason: "already_present"`, `category` =
  celle qui le contient. Toutes occupées ⇒ `category_occupied` avec `occupied_by_category`.

Côté Node (multipart), ajoutez le champ **une fois par libellé** : `form.append('document_categories', label)`.
Un tableau passé en une fois serait sérialisé en une seule chaîne.

## 2. Rejeu après `category_occupied` (correction)

En 0.3.0, un rejeu sous la même `idempotency_key` après un échec rejouable rendait l'**ancien** job et son
résultat pendant 24 h, sans rien réexécuter — même avec un nouveau payload. Le job est désormais oublié en
même temps que la clé : la re-soumission crée un nouveau job.

Conséquence pour Hippolyte.ai : une opération `FAILED needs_review` peut être rejouée sous la même clé une
fois les catégories de repli configurées ; l'ancien `job_id` ne sera plus renvoyé.

## 3. Correspondance exacte des libellés (changement de comportement)

Le bot ne fait plus de correspondance par sous-chaîne entre la catégorie demandée et les lignes du
formulaire. Un libellé absent du paramétrage du tenant donne `category_not_found` (rien n'est écrit) au lieu
de viser une ligne voisine. Vérifiez vos libellés avec le référentiel (`GET /referentials/document-categories`).

## 4. Nouveau résultat d'action

| Résultat | Sens | Conduite |
| --- | --- | --- |
| `ok: false, error: "category_not_found"` | Aucune catégorie libre demandée n'existe exactement dans le formulaire | Corriger les libellés et rejouer |

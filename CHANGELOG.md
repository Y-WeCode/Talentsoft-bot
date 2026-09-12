# Changelog

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

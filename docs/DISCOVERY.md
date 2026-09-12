# Phase 0 : découverte du Back Office Talentsoft

Tenant de recette : `https://testairfrance-rh.talent-soft.com/`

Ce document consigne ce qui a été observé sur le tenant réel. Tant qu'une ligne est marquée
**À VALIDER**, le sélecteur correspondant dans `app/ts_selectors.py` est un gabarit.

## Comment capturer

```bash
# Poste avec écran : navigateur visible, HAR + trace, actions à la main puis Entrée
python tools/discover.py --out discovery --headed --har --trace

# Sans écran : login automatique (TS_USERNAME / TS_PASSWORD dans .env), ouverture d'une fiche, export DOM
python tools/discover.py --out discovery --login --dump --open "https://testairfrance-rh.talent-soft.com/<chemin fiche>"
```

Le dossier `discovery/` (ignoré par git) contient des données personnelles de candidats :
le supprimer après analyse. Le HAR est enregistré sans contenu de requête.

## Résultats

| Élément | Constat | Statut |
| --- | --- | --- |
| URL de login | | À VALIDER |
| Champs login (identifiant, mot de passe, bouton) | | À VALIDER |
| Marqueur "session ouverte" (lien déconnexion) | | À VALIDER |
| Gabarit d'URL d'une candidature (`TS_APPLICATION_URL_TEMPLATE`) | | À VALIDER |
| Onglet historique / événements | | À VALIDER |
| Bouton "Ajouter un événement" et boîte de dialogue | | À VALIDER |
| Liste des types d'événement (référentiel) | | À VALIDER |
| Champ commentaire, champ date | | À VALIDER |
| Lignes de l'historique après ajout (vérification) | | À VALIDER |
| Onglet pièces jointes | | À VALIDER |
| Champ fichier, catégorie, nom | | À VALIDER |
| Lignes de la liste des pièces jointes (vérification) | | À VALIDER |
| Appels XHR internes observés (candidats au mode rejeu HTTP) | | À VALIDER |
| Anti-CSRF (jeton, en-tête) | | À VALIDER |
| Durée de session, comportement à l'expiration | | À VALIDER |
| MFA ou SSO forcé sur le compte technique | | À VALIDER |

## Journal

- 12/09/2026 : dépôt créé, ossature livrée avec sélecteurs gabarits validés sur un faux Back Office
  (`tests/fixtures/fake_backoffice`). Accès réseau au tenant encore bloqué depuis l'environnement de développement.

# Catégories de pièces jointes — tenant Air France (recette)

Relevé le 12/09/2026 dans `Pages/Utils/AttachedFileEdit.aspx`. **46 catégories.**

Chaque catégorie est une **ligne du formulaire avec son propre `input[type=file]`** : il n'y a pas de liste
déroulante. Déposer un document « en catégorie X » = remplir le champ fichier de la ligne intitulée X.

L'indice `ctlNN` est donné à titre indicatif : c'est un numéro de répéteur ASP.NET qui se décale dès que le
paramétrage client change. **Toujours cibler par le libellé de la ligne.**

| ctl | Catégorie |
| --- | --- |
| ctl01 | Profil(s) individuel(s) |
| ctl02 | EAP(s) |
| ctl03 | Lettre de motivation |
| ctl04 | CV |
| ctl05 | Autres documents |
| ctl06 | Profil en ligne |
| ctl07 | Compte rendu |
| ctl08 | CVThèque |
| ctl09 | Diplôme(s) |
| ctl10 | Attestation d'anglais |
| ctl11 | Brevet de natation |
| ctl12 | Compte rendu 2 |
| ctl13 | Compte rendu 3 |
| ctl14 | Compte rendu 4 |
| ctl15 | Hello Talent |
| ctl16 | Copie du passeport (obligatoire) |
| ctl17 | Diplôme BAC (obligatoire) |
| ctl18 | Certificat de scolarité ou carte étudiante en cours de validité |
| ctl19 | Certificat d'anglais B2 de moins de 24 mois (obligatoire) |
| ctl20 | CCA |
| ctl21 | Attestation d'aptitude médicale |
| ctl22 | Extrait de casier judificiaire |
| ctl23 | Pièce d'identité |
| ctl24 | FCL.055 anglais |
| ctl25 | ATPL théorique |
| ctl26 | CPL IRME |
| ctl27 | MCC |
| ctl28 | QT EN COURS DE VALIDITE |
| ctl29 | CLASSE 1 VALIDE |
| ctl30 | FCL.055 français si langue non native |
| ctl31 | TOEIC |
| ctl32 | Attestation du nombre d'heures de vol |
| ctl33 | 2 dernières pages de votre carnet de vol certifiée sur l'honneur |
| ctl34 | LICENCE PART 66 |
| ctl35 | ACCORD RH écrit |
| ctl36 | CASIER JUDICIAIRE de moins de 3 mois |
| ctl37 | CARTE IDENTITE R/V ou PASSEPORT en cours de validité |
| ctl38 | DIPLÔME LE PLUS ELEVE |
| ctl39 | DIPLÔME CECRL Français si langue non native |
| ctl40 | Lettre de motivation pilote en français |
| ctl41 | CLASSE1/CLASSE1 avec Dérog/CLASSE2 |
| ctl42 | CV pilote en français |
| ctl43 | CV (Obligatoire) |
| ctl44 | Calendrier apprentissage/formation |
| ctl45 | Justificatif classes préparatoires |
| ctl46 | Justificatif de bourse |

## Contraintes de dépôt

- Taille maximale : **10240 Ko** (10 Mo)
- Extensions autorisées : **`.doc .rtf .docx .pdf .tif .tiff .xlsx .zip`**
  → ni `.odt`, ni `.txt`, ni `.png`, ni `.jpg`/`.jpeg`

## Choix de la catégorie par défaut

`.env.example` propose `TS_DEFAULT_DOCUMENT_CATEGORY=Autre` : ce libellé **n'existe pas**.
La valeur correcte est **`Autres documents`**.

Pour une synthèse produite par Hippolyte.ai, deux candidates : `Autres documents` ou `Compte rendu`.
À arbitrer avec le client — plusieurs catégories sont marquées « (obligatoire) » et participent à la
complétude du dossier candidat : n'y déposer un document automatique qu'en connaissance de cause.

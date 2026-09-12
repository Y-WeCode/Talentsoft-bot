# Référentiel des types d'événement de candidature — tenant Air France (recette)

Relevé le 12/09/2026 dans `select[id$='ddlJobApplicationChildEventType']` du formulaire
`Applicants.Events/JobApplicationChildEventEdit.aspx`. **105 types.**

`code` = attribut `value` de l'option (identifiant interne Talentsoft), `libellé` = texte affiché.
L'API du bot accepte les deux (`event_type` peut être un libellé ou un code).

> Ce référentiel est **propre au tenant** et évolue avec le paramétrage client.
> `GET /referentials/event-types` doit le relire dans le Back Office plutôt que s'appuyer sur ce fichier.

| Code | Libellé |
| --- | --- |
| 1329 | A traiter |
| 1735 | Candidature à l'étude |
| 1752 | Candidature non retenue |
| 1751 | Candidature retenue |
| 837 | Convocation à un entretien individuel |
| 1754 | Debriefing réalisé |
| 832 | Désistement |
| 3355 | Dossier Pilote non recevable |
| 3354 | Dossier Pilote recevable |
| 1743 | Entretien avis défavorable |
| 1742 | Entretien avis favorable |
| 1758 | Entretien avis réservé vers défavorable |
| 1757 | Entretien avis réservé vers favorable |
| 1816 | Vivier talents |
| 3356 | Profil Pilote ajourné |
| 3361 | Profil Pilote éliminé |
| 3353 | Profil Pilote retenu |
| 860 | Proposition refusée |
| 3745 | Retour tests pilote négatif |
| 3746 | Retour tests pilote positif |
| 3748 | Candidature Pilote Cadet non retenue-Absence Pré-sélection |
| 4069 | Candidature Pilote non retenue Pré-sélection |
| 4070 | Candidature Pilote en attente Pré-sélection |
| 4071 | Candidature Pilote retenue Pré-sélection |
| 4072 | Candidature Pilote non retenue-Absence Pré-sélection |
| 4130 | Candidature en attente de préselection |
| 4443 | Candidature à valider Manager |
| 4444 | Candidature à valider RH |
| 4445 | Candidature en cours de traitement |
| 4446 | Candidature non recevable |
| 4447 | Candidature recevable |
| 4448 | Interruption de Période d'essai |
| 4449 | Invitation entretien à distance |
| 4450 | Invitation entretien présentiel |
| 4451 | Invitation entretien partenaire |
| 4452 | Invitation sessions de test à distance |
| 4453 | Invitation sessions de test en présentiel |
| 4454 | Mise en situation réussie |
| 4455 | Offre clôturée |
| 4456 | Préqualification négative |
| 4457 | Préqualification positive |
| 4458 | Test réussi |
| 4518 | Ajouter à la grille d'évaluation |
| 4556 | Invitation session (sans mail) |
| 4575 | KD Non payé |
| 4577 | Remboursé |
| 4579 | Boursier |
| 4580 | Paiement effectué |
| 4594 | KD PSY0 Admis |
| 4595 | KD PSY0 Liste d'attente |
| 4596 | KD PSY0 Absent |
| 4597 | KD PSY0 Non admis |
| 4598 | KD PSY0 Exclus |
| 4612 | Invitation entretien présentiel JOB DATING |
| 4625 | En attente |
| 4640 | KD PSY1 Admis |
| 4642 | KD PSY1 Non admis |
| 4643 | KD PSY1 Non admis non recevable |
| 4644 | KD PSY1 Non admis éliminé |
| 4724 | KD PSY2 Admis |
| 4725 | KD PSY2 ajourné |
| 4726 | KD PSY2 éliminé |
| 4748 | PRO Candidature recevable A320 |
| 4749 | PRO Candidature recevable B737 |
| 4750 | PRO Candidature recevable NQ |
| 4751 | PRO Candidature pilote non recevable |
| 4752 | PRO Résultats PSY1 Admis |
| 4753 | PRO Résultats PSY1 liste d'attente |
| 4754 | PRO Résultats PSY1 non admis |
| 4755 | PRO Avis commission : retenu |
| 4756 | PRO Candidat non retenu ajourné |
| 4757 | PRO Candidat non retenu éliminé |
| 4781 | PRO Candidature non prioritaire |
| 4784 | PRO Résultats PSY1 absent non admis |
| 4802 | Clôturé |
| 4846 | En attente de paiement |
| 4847 | Paiement CB |
| 4848 | KD Demande de remboursement |
| 4998 | KD Relance paiement |
| 4862 | KD PSY0 Non recevable |
| 4865 | KD PSY0 Non admis éliminé |
| 4927 | Session1 |
| 4928 | Session2 |
| 4929 | Session3 |
| 4930 | Session4 |
| 4931 | Session5 |
| 4932 | Session6 |
| 4933 | Session7 |
| 4934 | Session8 |
| 4955 | PRO Résultat PSY1 Non admis ajourné |
| 4975 | Candidature non retenue-Offre volumique - Statut |
| 5038 | Session9 |
| 5146 | Candidature retenue sans envoi de courrier |
| 5147 | Candidature non retenue sans envoi de courrier |
| 5148 | KDPaiementDésactivé |
| 5150 | KD Relance paiement J-10 |
| 5151 | Candidature non recevable |
| 5404 | Invitation test BRIGHT |
| 5405 | BRIGHT rattrapage |
| 5418 | A/R DEMANDE DE DEBRIEFING |
| 5419 | DEMANDE DE DEBRIEFING RECUTEMENT DE MASSE |
| 5421 | Candidature non retenue - Manager/RH |
| 5422 | Candidature non retenue suite tests |
| 5423 | Candidature non retenue suite évaluation DPGS |
| 5435 | Nouvelle demande de paiement |

## Aucun type « Commentaire »

`.env.example` propose `TS_DEFAULT_EVENT_TYPE=Commentaire` : **ce type n'existe pas sur ce tenant**.

Un type par défaut doit être choisi avec le client. Pistes neutres, qui ne déclenchent pas de courrier
candidat ni de changement de statut fort : `A traiter` (1329), `Candidature à l'étude` (1735),
`En attente` (4625).

**Attention** : plusieurs types déclenchent l'envoi d'un **courrier au candidat** (leurs homologues
« sans envoi de courrier » existent : 5146, 5147). Le type par défaut du bot ne doit **jamais** être un type
qui écrit au candidat. À valider avec le client avant mise en production.

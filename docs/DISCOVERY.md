# Phase 0 : découverte du Back Office Talentsoft

Deux hôtes distincts, à ne pas confondre :

| Hôte | Rôle |
| --- | --- |
| `https://testairfrance.talent-soft.com` | MyTalentsoft, espace collaborateur. Atterrissage après login. **Pas** le périmètre du bot. |
| `https://testairfrance-rh.talent-soft.com` | **Back Office recrutement** (« TS recrutement »), ASP.NET WebForms. C'est la valeur de `TS_BASE_URL`. |

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

## Parcours d'authentification (constaté le 12/09/2026)

Le tenant n'a **pas** de formulaire de login local : il fédère via WS-Federation sur deux domaines distincts.

```
1. https://testairfrance-rh.talent-soft.com/            (TS_BASE_URL, Back Office)
      redirige vers
2. https://testfedauthsbg1.talent-soft.com/airfrance-test/wsfederation
      écran "Sélectionnez le compte avec lequel vous souhaitez vous authentifier"
      radios : "Accès @rtémis pour Air France" | "Accès @rtémis pour les filiales"
      bouton : "Continuer"
      redirige vers
3. https://testidpsbg1.talent-soft.com/airfrance-test/wsfed/issue?wa=wsignin1.0&wtrealm=...&wreply=...&wctx=...
      formulaire "Saisissez vos informations de connexion pour accéder à Talentsoft"
      POST sur la même URL, retour vers le Back Office
```

Formulaire de l'IdP (étape 3), relevé dans le DOM :

| Champ | Sélecteur |
| --- | --- |
| Identifiant | `input[name='Username']` (`#Username.form-control`) |
| Mot de passe | `input[name='Password']` (`#Password.form-control`) |
| Rester connecté | `input[name='RememberMe']` (checkbox, non coché par le bot) |
| Validation | `button[type='submit'].btn-primary`, libellé « Connexion » |
| Anti-CSRF | `input[name='__RequestVerificationToken']` + `input[name='RequestVerificationToken']` (ASP.NET MVC), posés par le serveur : transparents pour un pilotage navigateur |
| Mot de passe oublié | lien vers `https://testairfrance.talent-soft.com/ForgotPassword` |

### Après authentification

- Atterrissage sur `https://testairfrance.talent-soft.com/MyTalentsoft#/Me` (« Mon Espace ») : application
  Angular à routes en hash, espace collaborateur. Le Back Office recruteur est sur l'**autre** hôte
  (`testairfrance-rh`) et reste accessible directement par URL : le bot n'a pas besoin de passer par MyTalentsoft.
- **Bandeau de consentement Didomi** (`#didomi-notice`, conteneur `#didomi-host`).
  Mesuré le 12/09/2026 : 793 × 298 ancré en bas d'une fenêtre de 808 × 910, `z-index` maximal.
  Il ne recouvre donc **pas** toute la page, contrairement à ce qui avait été noté d'abord, mais
  masque la bande basse : un contrôle situé là devient incliquable. Le bot le refuse au premier
  chargement (finalités non essentielles).
- Aucun lien `logout`/`deconnexion` en clair dans le DOM à ce stade : les marqueurs `AUTHENTICATED_MARKERS`
  actuels (basés sur un lien de déconnexion) ne matchent pas. À remplacer par un marqueur fiable du Back Office.

### Conséquences sur le code

1. **`_guard_route` bloque le login.** `app/scraper.py:106` abandonne toute navigation principale hors de
   `TS_BASE_URL` ; les étapes 2 et 3 sont sur d'autres hôtes et seraient coupées. Il faut une allowlist
   d'hôtes d'authentification (nouvelle variable `TS_AUTH_HOSTS`), sans quoi le bot ne peut pas se connecter.
2. **Écran de sélection de compte à franchir** avant le formulaire : nouveaux sélecteurs
   `ACCOUNT_CHOICE_*`, et une variable de configuration pour désigner le compte à choisir
   (`TS_ACCOUNT_CHOICE`), le libellé étant propre à chaque tenant.
3. `LOGIN_USERNAME` / `LOGIN_SUBMIT` : les gabarits actuels matchent (`input[name='UserName']` est
   insensible à la casse côté HTML mais pas côté sélecteur CSS — ajouter `input[name='Username']`).
4. `LOGIN_URL_FRAGMENTS` doit couvrir `wsfed`, `wsfederation`, `issue`.

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

## Back Office recrutement (constaté le 12/09/2026)

### Technologie

ASP.NET **WebForms** (`*.aspx`), navigation interne par **postback** (`WebForm_DoPostBackWithOptions`).
Les onglets (« Offre », « Candidatures », « Historique », « Publication », « Rendement ») portent `href="#"`
et déclenchent un postback : **l'URL ne change pas quand on change d'onglet**.

Conséquence : impossible d'atteindre un onglet par `page.goto()`. Il faut cliquer, puis attendre la fin du
postback (et non un `load` de navigation). Les sélecteurs d'onglet par rôle/texte restent valides.

Iframes présents sur la page (la page principale est bien au top-level, pas dans un iframe) :

| Iframe | Rôle supposé |
| --- | --- |
| `fileIFrame` | Cible de l'upload de fichiers (technique WebForms classique) — **à surveiller pour les pièces jointes** |
| `applicationsdocumentreaderiframe` | Lecteur de documents de la candidature |
| `BackHeight`, `__tcfapiLocator` | Utilitaires (`Pages/Utils/Dummy.htm`) |
| `iframe-cookies-group` | Bandeau cookies, cross-origin (`cookies.talent-soft.com`) |

### URL d'une fiche candidature

Relevée depuis la liste de suivi des candidatures d'une offre :

```
https://testairfrance-rh.talent-soft.com/Pages/Applicants/MainPage.aspx
  ?applicantGuid=d052ac2a-2693-4f64-9af1-d72666377c2f
  &jobApplicationEventId=1111954
  &offerId=25152
  &lastColumnSorted=&orderMode=null&employeesOnly=false&filterByLocationIds=
  &action=jobapplicationmode
```

**Une candidature n'est pas identifiée par un seul entier.** Il faut au minimum :

| Paramètre | Nature |
| --- | --- |
| `applicantGuid` | GUID du **candidat** |
| `jobApplicationEventId` | Identifiant de la **candidature** (candidat × offre) |
| `offerId` | Identifiant de l'**offre** |
| `action=jobapplicationmode` | Ouvre la fiche en mode candidature |

Les autres paramètres (`lastColumnSorted`, `orderMode`, `employeesOnly`, `filterByLocationIds`) semblent
n'être que le contexte de tri de la liste d'origine : **à confirmer** qu'ils sont facultatifs.

Conséquences :

1. `TS_APPLICATION_URL_TEMPLATE` avec un unique `{application_id}` **ne suffit pas**. Le gabarit doit accepter
   plusieurs paramètres, ou `application_id` doit devenir une clé composite.
2. `safety.validate_application_id` (`^[A-Za-z0-9_-]{1,64}$`) accepte bien un GUID, mais pas une composition
   de trois valeurs. À revoir en même temps que le gabarit.
3. **Côté Hippolyte.ai (confirmé le 12/09/2026) : seuls `offerId` et `jobApplicationEventId` seront
   disponibles. `applicantGuid` ne le sera pas.** D'où la question bloquante suivante.

### QUESTION BLOQUANTE : la fiche s'ouvre-t-elle sans `applicantGuid` ?

`TS_APPLICATION_URL_TEMPLATE` ne pourra utiliser que `{offer_id}` et `{job_application_event_id}`.

- **Si oui** : gabarit direct, rien d'autre à faire.
  `{base}/Pages/Applicants/MainPage.aspx?jobApplicationEventId={job_application_event_id}&offerId={offer_id}&action=jobapplicationmode`
- **Si non** : le bot devra d'abord ouvrir la liste des candidatures de l'offre
  (`{base}/Pages/Offers/MainPage.aspx?FromContext=VacancyDashboard&id={offer_id}`), y retrouver la ligne dont
  le lien porte le bon `jobApplicationEventId`, et cliquer dessus. Coût : une navigation supplémentaire par
  push, et une pagination à gérer si l'offre compte beaucoup de candidatures.

**Statut : À TESTER.**

## Fiche candidature : structure réelle (12/09/2026)

Candidature témoin : Georges PAOLI, `Id : 584408` (identifiant candidat **affiché**, entier),
offre `2026-25152`, `jobApplicationEventId=1111954`.

### Onglets

RadTabStrip Telerik : `ul.rtsUL > li.rtsLI > a.rtsLink`, l'onglet actif porte `a.rtsLink.rtsSelected`.
Chaque onglet a une **vraie URL** (donc atteignable sans postback) :

| Onglet | URL |
| --- | --- |
| Fiche | `/Pages/Applicants/MainPage.aspx?tab=CardTab` |
| **Documents** | `/Pages/Applicants/MainPage.aspx?tab=ResumeTab` |
| **Historique** | `/Pages/Applicants/MainPage.aspx?tab=HistoryTab` |
| Appréciations | `/Pages/Applicants/MainPage.aspx?tab=AppreciationTab` |
| Entretiens | `/Pages/Applicants/MainPage.aspx?tab=InterviewsTab` |

### DEUX historiques distincts — ne pas confondre

L'onglet Historique contient deux tableaux séparés, avec deux boutons différents :

**1. « Historique des candidatures »** — `#...ApplicantNavigation_ctl01_lblSubTitleJobApplication_SubformTable`

- Tableau : `table.result-grid-view.events-history-table.ts-table-layout-auto`
- Colonnes : `Événement | Date Évén | Score | Note | Statut | Suivi par | Document Reader | Écrire | Action | Détails`
- Contenu témoin : « Réponse à offre Agent d'Escale… / 12/09/2026 / Réactivée / Gauthier BAILLEUL »
- Bouton : **« Ajouter une candidature »** `input#...ctl01_lblSubTitleJobApplication_subformContent_ctl00_fieldContent_btCreateJobApplicationEvent.add-button`
  → crée une **nouvelle candidature**, ce n'est PAS « ajouter un événement à cette candidature ».

**2. « Historique des événements »** — `#...ApplicantNavigation_ctl01_SubTitleApplicant_SubformTable`

- Contenu témoin : « Aucun événement pour ce candidat »
- Bouton : **« Ajouter un événement »** `input#...ctl01_SubTitleApplicant_subformContent_ctl00_fieldContent_btCreateApplicantEvent.add-button`
  → événement au niveau **candidat** (la personne), pas au niveau candidature.

**Le gabarit `ADD_EVENT_BUTTON` actuel matcherait `btCreateApplicantEvent`** (libellé « Ajouter un événement »)
et écrirait donc au mauvais endroit si la cible métier est la candidature. **Point à trancher.**

### Actions de workflow (panneau « Outils » à droite)

Liens de postback `#...RecruiterTools_frmJobApplicationAction_...repJobAppAction_ctl<N>_lblJobAppActionName`,
dont beaucoup sont explicitement suffixés `EVENEMENT` :

`Offre clôturée EVENEMENT`, `A l'étude EVENEMENT`, `En attente EVENEMENT`, `KD Non payé EVENEMENT`,
`KD Remboursé EVENEMENT`, `Entretien avis favorable`, `Entretien avis défavorable`, …

Un tableau de ~89 lignes (`table.table-layout-auto.subformWithoutBorder.ts-table`) liste les actions
disponibles : « Candidature en attente de présentation », « Candidature à valider Manager »,
« Candidature non recevable », … C'est vraisemblablement **le référentiel des types d'événement**.

**Piège : ces actions déclenchent un `confirm()` JavaScript natif** :
`onclick="return confirm('Êtes-vous sûr de vouloir exécuter l\'action \"…\" ?')"`.
Playwright **rejette automatiquement** les dialogues par défaut : sans `page.on("dialog", d => d.accept())`,
le clic ne produirait rien, silencieusement. À câbler impérativement.

### Pièces jointes

- Documents du candidat : `table.subformWithoutBorder.ts-table.ts-content-table`,
  libellés au format `NOM_DU_FICHIER.PDF (Catégorie)` — ex. `CV GAUTHIER (CV)`,
  `DIPLOME_INGENIEUR_SYSTEMES_NUMERIQUES.PDF (DIPLÔME LE PLUS ELEVE)`, `GAUTHIER_BAILLEUL_CI.PDF (Pièce d'identité)`.
  Une colonne indique l'origine : `FO` (Front Office, déposé par le candidat) ou `BO` (Back Office).
- Bouton d'ajout : **« Ajouter/Modifier »**
  `input#...RecruiterTools_AttachedFile_frmAttachedFiles_subformContent_btnAddFile.search-button`
- L'upload passe probablement par l'iframe `fileIFrame` repéré sur la page.

### Barre d'outils (11 actions)

`select#ctl00_ctl00_RootToolBarContent_ToolBarContent_ToolBar_Toolbar.toolbox` :
Action, Document Reader, Supprimer, Pousser une offre, Envoyer la fiche par email, Écrire un courrier au
candidat, Imprimer, Marquer comme non vu, Mettre en veille, Transférer, Envoyer un mot de passe provisoire.

### Identifiants : synthèse

| Référentiel | Candidat | Candidature |
| --- | --- | --- |
| API Recruiting Customer (Hippolyte.ai) | `_TS_913e6aa1-676c-43b5-b54c-ec0c52506875` | `_TS_8107b4db-5f9e-447d-b7e9-dd80b85418e1` |
| URL du Back Office | `applicantGuid=d052ac2a-2693-4f64-9af1-d72666377c2f` | `jobApplicationEventId=1111954` |
| Affiché dans l'écran | `Id : 584408` | — |

Tests d'URL (12/09/2026), sur la même candidature :

| Test | URL | Résultat |
| --- | --- | --- |
| A | sans `applicantGuid` | échec |
| B | `applicantGuid` = GUID API candidat | échec |
| C | idem avec préfixe `_TS_` | échec |
| D | `applicantGuid` BO + `jobApplicationEventId` = GUID API candidature | **ouvre la fiche** |
| E | `applicantGuid` BO + `applicationGuid` = GUID API candidature | **ouvre la fiche** |

**Lecture prudente de D et E** : les deux contiennent le `applicantGuid` du Back Office. L'hypothèse la plus
probable n'est pas que le BO accepte les GUIDs de l'API, mais qu'il **ouvre la fiche sur le seul
`applicantGuid` et ignore les paramètres qu'il ne reconnaît pas**. Tests de contrôle à faire :

- **G** : `applicantGuid` BO + `jobApplicationEventId=00000000-0000-0000-0000-000000000000` →
  si la fiche s'ouvre quand même, les paramètres sont bien ignorés et D/E ne prouvent rien.
- **F** : `applicantGuid` BO seul.

Tant que G n'est pas fait, **considérer que le bot ne sait pas construire l'URL d'une fiche à partir des
seuls identifiants d'Hippolyte.ai**. C'est le risque numéro un du projet.

## Test G (12/09/2026) : les paramètres d'URL sont ignorés

`jobApplicationEventId=00000000-0000-0000-0000-000000000000` ouvre **la même fiche, avec la même candidature
sélectionnée** que `jobApplicationEventId=1111954`.

**Conclusion : seul `applicantGuid` détermine la page. Les autres paramètres sont ignorés.**
D et E ouvraient donc la bonne fiche par coïncidence — le Back Office n'accepte pas les GUIDs de l'API.

Conséquence : **`TS_APPLICATION_URL_TEMPLATE` ne peut pas fonctionner** tel qu'il est conçu. Hippolyte.ai ne
dispose ni de `applicantGuid` ni de `jobApplicationEventId`.

## Solution retenue : passer par l'offre (le seul identifiant partagé)

`offerId` est le **seul identifiant commun** aux deux référentiels (`25152` côté Hippolyte.ai, `offerId=25152`
côté BO, et référence affichée `2026-25152`).

### Structure du tableau des candidatures — `table.result-grid-view.events-history-table`

| Classe de ligne | Rôle |
| --- | --- |
| `tr.ch_title` | En-tête |
| `tr.ch_content_outerrep` | Une candidature du candidat, texte de la forme `Réponse à offre <intitulé> ( réf. <référence> )` |
| **`tr.selectedLine`** | La candidature **actuellement sélectionnée** |
| `tr.trChildrenEvent` | Les événements de la candidature sélectionnée (libellé, date, auteur) |

Deux acquis majeurs :

1. **La référence de l'offre figure en clair dans la ligne** (`réf. 2026-25152`) : appariement possible à partir
   du seul `offerId`, sans GUID.
2. La sélection d'une candidature se fait par **postback sur sa ligne** (`__doPostBack`), et `tr.selectedLine`
   fournit la **vérification** que la bonne candidature est active avant toute mutation.

### Parcours cible du bot

```
1. offerId → {base}/Pages/Offers/MainPage.aspx?FromContext=VacancyDashboard&id={offer_id}
2. repérer la ligne du candidat dans la liste des candidatures de l'offre   ← MAILLON À DÉFINIR
3. cliquer : la fiche s'ouvre, applicantGuid est obtenu au passage
4. onglet Historique, vérifier que tr.selectedLine contient « réf. <référence de l'offre> »
   (sinon, cliquer la bonne ligne ch_content_outerrep et re-vérifier)
5. mutation (action de workflow / pièce jointe)
```

Cette vérification de l'étape 4 est une **garantie de sécurité essentielle** : elle empêche d'écrire un
événement sur la mauvaise candidature d'un candidat qui en a plusieurs (cas réel : Georges PAOLI en a deux).

### Maillon manquant : reconnaître le candidat dans la liste de l'offre

Hippolyte.ai ne dispose pas des identifiants du BO. Il faut un critère d'appariement parmi les colonnes de la
liste des candidatures d'une offre. **À déterminer** : l'email est le seul critère réellement discriminant
(le nom + prénom expose aux homonymes). Vérifier s'il est affiché ou filtrable dans cette liste.

`EVENT_ROWS` peut désormais être fixé : `tr.trChildrenEvent` (événements de la candidature sélectionnée).

## Sélection d'une candidature : le mécanisme (12/09/2026)

Dans « Historique des candidatures », chaque candidature expose un lien de postback dont **le texte contient
la référence de l'offre** :

```
a#...JobApplicationEventHistory_RepJobApplicationEvent_ctl01_lnkOfferLabel
   → « Peintre aéronautique F/H ( réf. 2026-23770) »
a#...JobApplicationEventHistory_RepJobApplicationEvent_ctl03_lnkOfferLabel
   → « Agent d'Escale Commercial F/H ( réf. 2026-25152) »
```

**C'est le point d'ancrage du bot** : cliquer le lien `lnkOfferLabel` dont le texte contient la référence
dérivée de `offerId`, puis vérifier que `tr.selectedLine` porte bien cette référence.

Remarque : l'indice `ctlNN` (`ctl01`, `ctl03`) est un numéro de répéteur ASP.NET, **non stable** — ne jamais
s'en servir. Cibler par le suffixe d'id (`[id$='lnkOfferLabel']`) combiné au texte.

Format de la référence : `<année>-<offerId>` (ex. `offerId=25152` → `réf. 2026-25152`). **Attention : l'année
n'est pas dérivable de `offerId`.** Chercher sur `réf. ` + `-25152` (suffixe) plutôt que sur la chaîne complète.

## Onglets complets de la fiche

| Onglet | URL |
| --- | --- |
| Fiche | `?tab=CardTab` |
| Documents | `?tab=ResumeTab` |
| Historique | `?tab=HistoryTab` |
| Appréciations | `?tab=AppreciationTab` |
| Entretiens | `?tab=InterviewsTab` |
| Embauche | `?tab=HiringPropositionTab` |
| Préqualification | `?tab=ScreeningTab` |

## Back Office hybride : deux technologies, deux règles de sélecteurs

| Zone | Technologie | Règle |
| --- | --- | --- |
| Fiche, historique, documents, outils | ASP.NET WebForms + Telerik | Ids longs `ctl00_ctl00_...` : cibler par **suffixe** (`[id$='btnAddFile']`), jamais l'id complet ni les indices `ctlNN` |
| Barre de recherche globale, en-tête | React + MUI (design system `ds-hrecruiting`) | **Ids et classes générés dynamiquement** (`input#mui-31537`, `.ds-hrecruiting-ts290`) : changent à chaque rendu. Cibler **uniquement** par rôle et placeholder |

Exemple : la recherche globale se cible par
`input[placeholder*='Rechercher un candidat' i]`, **jamais** par `#mui-31537`.

Un champ « Commentaires » existe aussi dans le panneau Outils :
`input[id$='btnSaveComments']` (« Enregistrer ») — piste à explorer pour le commentaire libre.

## Appariement validé (12/09/2026)

La recherche globale par **email** retourne **un seul résultat, le bon candidat**.

Parcours d'ouverture d'une candidature retenu pour le bot, à partir des seules données d'Hippolyte.ai
(`email du candidat` + `offerId`) :

```
1. Ouvrir le Back Office (session authentifiée)
2. Saisir l'email dans input[placeholder*='Rechercher un candidat' i], valider
   → un seul résultat : la fiche du candidat s'ouvre
3. Onglet Historique (?tab=HistoryTab)
4. Cliquer le lien [id$='lnkOfferLabel'] dont le texte contient « -<offerId>) »
5. VÉRIFIER que tr.selectedLine contient cette même référence  ← garde-fou anti-mauvaise-candidature
6. Mutation
```

Garde-fous à implémenter obligatoirement :

- **0 résultat** de recherche → `404 application_not_found`, aucune mutation.
- **Plusieurs résultats** → refuser (`409` ou `400`), ne jamais deviner : écrire sur le mauvais dossier
  candidat est une fuite de données personnelles.
- **Aucun `lnkOfferLabel` ne correspond à l'offre** → `404`, le candidat n'a pas postulé à cette offre.
- **`tr.selectedLine` ne porte pas la bonne référence après clic** → abandon avant mutation.

## Formulaire « Création d'un événement » — CAPTURÉ (12/09/2026)

Ouvert par **Outils → Actions** sur la fiche. C'est bien un événement **de candidature** (la cible métier).

### Structure : RadWindow Telerik + iframe

```
div.RadWindow.RadWindow_TsTelerik.rwNormalWindow      ← fenêtre (id contient un nombre ALÉATOIRE : inutilisable)
 └─ td.rwWindowContent.rwExternalContent
     └─ iframe src="../Applicants.Events/JobApplicationChildEventEdit.aspx?rwndrnd=<aléatoire>"
         └─ LE FORMULAIRE
div.TelerikModalOverlay                                ← voile modal
```

**Le formulaire est dans un IFRAME.** `app/ts_pages.py` interroge `page.locator(...)` sur la page
principale : il ne trouverait jamais ces champs. Il faut `page.frame_locator("iframe[src*='JobApplicationChildEventEdit']")`.

Ni l'id de la RadWindow ni `rwndrnd` ne sont stables : cibler l'iframe **par son `src`**.

### Champs

| Champ | Sélecteur (dans l'iframe) | Remarques |
| --- | --- | --- |
| Type d'événement | `select[id$='ddlJobApplicationChildEventType']` | **105 types**, `value` = code numérique, texte = libellé. Première option vide. |
| Date | `input[id$='EventDate_EventDate_txt']` | **Format `JJ/MM/AAAA`** (ex. `12/09/2026`), pré-rempli à aujourd'hui, éditable |
| Commentaire | `textarea[id$='EventComment']` | **`maxlength=2000`**, 15 lignes |
| Suivi par | `select[id$='ddlSupervisor']` | Pré-rempli avec l'utilisateur connecté (donc le compte technique du bot) |
| Valider | `input[id$='btValidate']` (`.valid-button`, value « Valider ») | |
| Annuler | `input[id$='btCancel']` (`.cancel-button`, value « Annuler ») | |

Titre de la modale : « Création d'un événement — Candidat : <Nom> ».

### DEUX INCOHÉRENCES DE CONFIGURATION À CORRIGER

1. **`COMMENT_MAX_CHARS=4000` > `maxlength=2000` du champ réel.** Un commentaire de plus de 2000 caractères
   serait **silencieusement tronqué par le navigateur**, et la relecture de vérification ne le verrait pas
   forcément. Ramener la limite à **2000** et rejeter en `400` au-delà, plutôt que tronquer.

2. **`TS_DEFAULT_EVENT_TYPE=Commentaire` n'existe pas dans le référentiel.** Aucun des 105 types ne
   s'appelle « Commentaire ». Avec cette valeur par défaut, tout push sans `event_type` explicite échouerait.
   Il faut choisir un type réel du tenant (voir `docs/event-types-airfrance.md`).

### Format de date

Le contrat HTTP du bot attend `event_date` en `YYYY-MM-DD`, le champ Talentsoft veut `JJ/MM/AAAA` :
**conversion obligatoire** avant remplissage. Ne pas envoyer la date ISO telle quelle.

### Note de confidentialité

Le select « Suivi par » contient **378 noms de collaborateurs** : données personnelles, **non consignées ici**.

## Formulaire « Pièces jointes » — CAPTURÉ (12/09/2026)

Ouvert par **« Ajouter/Modifier »** (`input[id$='btnAddFile']`). Même structure : RadWindow + iframe
`../Utils/AttachedFileEdit.aspx?rwndrnd=<aléatoire>`.

### Le modèle est à l'opposé de ce que suppose le code

**Il n'y a pas de liste déroulante de catégorie.** Le formulaire présente **46 champs `input[type=file]`,
un par catégorie**. La catégorie n'est pas une valeur à choisir : elle détermine **quel champ on remplit**.

```
| Profil(s) individuel(s)   [Parcourir] |   ← input …Files_ctl01_File
| EAP(s)                    [Parcourir] |   ← input …Files_ctl02_File
| Lettre de motivation      [Parcourir] |   ← input …Files_ctl03_File
| CV                        [Parcourir] |   ← input …Files_ctl04_File
| Autres documents          [Parcourir] |   ← input …Files_ctl05_File
…46 lignes
```

`ATTACHMENT_CATEGORY_SELECT` et `ATTACHMENT_NAME_INPUT` n'ont **aucun équivalent** : à supprimer.
Il n'existe pas non plus de champ « nom affiché » : le nom du fichier déposé fait foi.

**Ne pas cibler par `ctlNN`** (indice de répéteur, décalé dès qu'une catégorie est ajoutée au paramétrage).
Cibler la **ligne par son libellé**, puis l'`input[type=file]` de cette ligne :
`tr:has-text("<catégorie>") input[type=file]`.

Le formulaire poste en `multipart/form-data` vers `AttachedFileEdit.aspx`.
Bouton de validation : `input[id$='btValidate']`, **value « Enregistrer »** (et non « Valider » comme
pour les événements). Annulation : `input[id$='btCancel']`.

### CONTRAINTES DE FICHIERS — divergence avec la configuration du bot

Message affiché dans le formulaire :

> « la taille des fichiers joints est limitée à **10240 Ko** »
> « les types de documents autorisés sont : **.doc .rtf .docx .pdf .tif .tiff .xlsx .zip** »

| | Bot (`UPLOAD_ALLOWED_EXTENSIONS`) | Talentsoft | Verdict |
| --- | --- | --- | --- |
| `pdf`, `doc`, `docx`, `rtf` | autorisé | autorisé | OK |
| **`odt`, `txt`, `png`, `jpg`, `jpeg`** | **autorisé** | **REFUSÉ** | **le bot accepterait un fichier que Talentsoft rejettera** |
| `tif`, `tiff`, `xlsx`, `zip` | refusé | autorisé | restriction volontaire, à confirmer |

Taille : 10240 Ko = 10 485 760 octets = `MAX_UPLOAD_SIZE_BYTES` par défaut. **Cohérent.**

À corriger : aligner `UPLOAD_ALLOWED_EXTENSIONS` sur `pdf,doc,docx,rtf` (+ éventuellement `tif,tiff,xlsx,zip`),
sans quoi un push d'image ou de `.txt` serait accepté en `200` par le bot puis silencieusement perdu.

### `TS_DEFAULT_DOCUMENT_CATEGORY=Autre` : valeur inexacte

La catégorie réelle est « **Autres documents** ». La correspondance exacte échouerait ; seule la
correspondance partielle de `choose_option` sauverait le cas, ce qui est fragile.
Valeur correcte à mettre dans `.env` : `Autres documents`.

Catégories pertinentes pour une synthèse Hippolyte.ai : « Autres documents » (ctl05), « Compte rendu » (ctl07).
Liste complète : `docs/document-categories-airfrance.md`.

## Test d'écriture réel (12/09/2026) — événement créé DEUX fois

Événement créé volontairement deux fois à l'identique sur la candidature témoin :
type « Convocation à un entretien individuel » (837), date 14/09/2026,
commentaire « Hippolyte.ai : test de decouverte phase 0 du 12/09/2026 - a supprimer ».

Résultat : **deux lignes identiques dans l'historique** (7 → 8 → 9 lignes).

### Constat 1 — Talentsoft ne dédoublonne pas

Rien n'empêche la création de deux événements strictement identiques. Toute protection contre les doublons
doit donc venir **du bot**.

### Constat 2 — le commentaire n'est PAS lisible dans l'historique

La ligne d'un événement (`tr.trChildrenEvent`) ne contient que :

| Cellule | Contenu |
| --- | --- |
| 1-2 | vides (bordure, icône) |
| 3 | **Libellé du type** — lien `a[id$='lnkEventTitle']` |
| 4 | **Date** (`JJ/MM/AAAA`) |
| 5 | Score (vide) |
| 6 | **Auteur** (« Suivi par ») |
| 7-8 | vides |

Le texte du commentaire **n'apparaît nulle part dans la page** (vérifié par recherche plein texte), ni en
`title`, ni en infobulle. Seul le lien `lnkEventTitle` permet de rouvrir l'événement pour le consulter.

### Conséquences sur la conception — IMPORTANT

1. **`_event_fingerprint(comment)` est inopérant.** Le dédoublonnage et la vérification prévus dans
   `app/scraper.py` cherchent l'empreinte du commentaire dans les lignes : ce texte n'existe pas dans le DOM.

2. **`already_present` n'est pas déterminable** pour un événement sans rouvrir chaque ligne candidate.
   Un dédoublonnage sur `(type, date, auteur)` seulement produirait des **faux positifs** : deux synthèses
   différentes le même jour avec le même type seraient considérées comme déjà présentes, et la seconde
   serait silencieusement ignorée. **À proscrire.**

3. **`verified` ne peut porter que sur `(type, date, auteur)`**, c'est-à-dire une vérification *faible* :
   elle confirme qu'un événement du bon type a été créé ce jour-là, pas que c'est le nôtre.
   Vérification forte possible mais coûteuse : cliquer `lnkEventTitle` sur la ligne la plus récente et
   comparer le commentaire — une navigation de plus par push.

4. **L'idempotence repose donc entièrement sur la clé d'idempotence du bot** (Redis / mémoire), jamais sur
   la relecture du Back Office. Le cas `unverified` + rejeu est le plus dangereux : il créerait un doublon
   sans aucun moyen de le détecter. Conserver la règle « ne jamais rejouer après `mutation_started` ».

### Constat 3 — `tr.selectedLine` : présent après sélection, absent après mutation

Après le postback de validation, **plus aucune ligne ne porte `selectedLine`**.

> **Rectification du 12/09/2026 (audit du parcours réel).** Ce constat avait été généralisé à tort en
> « `selectedLine` n'est pas fiable ». En réalité la classe est **présente après une sélection explicite**
> de candidature — ce que le bot fait systématiquement avant toute action. Elle est absente au chargement
> de la fiche et après un postback de mutation, pas en permanence.
> `selected_offer_matches()` s'en sert donc comme **preuve directe**, avec l'ordre du DOM en second recours.

**Critère de remplacement — l'ordre du DOM** : les `tr.trChildrenEvent` d'une candidature suivent
immédiatement sa ligne `tr.ch_content_outerrep`, jusqu'à la ligne `ch_content_outerrep` suivante.
Pour cibler la bonne candidature, il faut donc :

1. localiser la ligne `ch_content_outerrep` dont le texte contient la référence de l'offre ;
2. ne considérer que les `trChildrenEvent` situés entre cette ligne et la `ch_content_outerrep` suivante.

### Ménage à faire

Les deux événements de test « Convocation à un entretien individuel » du 14/09/2026 sur la candidature
de Georges PAOLI (offre 2026-25152) sont à supprimer du tenant de recette.

## Test d'écriture réel — pièces jointes (12/09/2026)

Deux PDF déposés en **une seule validation**, dans deux catégories vides.
Résultat : 3 → 5 documents, **aucune suppression**.

| Fichier | Catégorie | Origine |
| --- | --- | --- |
| `AF17.PDF` | Compte rendu 2 | BO |
| `AF 15.PDF` | CCA | BO |

### Acquis

1. **Le multi-dépôt fonctionne** : plusieurs fichiers, plusieurs catégories, un seul postback.
   Le bot peut donc grouper tous les documents d'un push en une seule validation, au lieu d'un
   aller-retour par fichier — moins de risque d'échec partiel.
2. **Colonne d'origine** : `BO` pour un dépôt Back Office, `FO` pour un dépôt candidat.
   Critère utile pour que le bot distingue ses propres dépôts de ceux du candidat.
3. **Le nom du fichier est conservé tel quel**, espaces inclus (`AF 15.PDF`). Il n'y a pas de champ
   « nom affiché » : c'est le nom du fichier envoyé qui s'affichera.
   → `safety.safe_display_filename` garde tout son sens (le nom transmis devient visible par les recruteurs),
   mais il doit préserver l'extension en majuscules/minuscules d'origine sans la réécrire.

### Vérification des documents : possible, contrairement aux événements

Liste des pièces jointes sur la fiche :

| Élément | Sélecteur |
| --- | --- |
| Libellé complet | `span[id$='lblAttachedFile']` → `« NOM_FICHIER.PDF (Catégorie) »` |
| Cellule | `td.attachedFileTitle` |
| Origine (`FO`/`BO`) | 4ᵉ cellule de la ligne |
| Visualiser | `a[title='Visualiser la pièce jointe']` |
| **Supprimer** | `a[id$='btnDeleteAttachedFileNew']` — **le bot ne doit JAMAIS cliquer ce lien** |

Le nom du fichier étant affiché, `verified` et `already_present` sont **réellement praticables** pour les
documents (au contraire des événements, dont le commentaire est invisible) :

- `verified` : après dépôt, le libellé `« <nom> (<catégorie>) »` apparaît dans la liste.
- `already_present` : un libellé identique existe déjà → dépôt à sauter.

### RISQUE NON LEVÉ : dépôt dans une catégorie déjà occupée

Les deux catégories testées étaient **vides**. Le comportement en catégorie occupée reste inconnu.

Le bouton s'intitule « Ajouter/**Modifier** » et il n'y a **qu'un seul champ fichier par catégorie** :
l'hypothèse d'un **remplacement** est sérieuse. Si elle se vérifie, un bot déposant en catégorie « CV »
**écraserait le CV du candidat** — perte de données irréversible.

**Tant que ce point n'est pas tranché, le bot doit refuser de déposer dans une catégorie déjà occupée**
(le libellé `span[id$='lblAttachedFile']` permet de le détecter avant d'agir), et se limiter aux catégories
vides ou dédiées (« Autres documents », « Compte rendu »).

## RISQUE LEVÉ — un dépôt en catégorie occupée ÉCRASE le document existant

Test A (12/09/2026) : dépôt de `Air_France - 7.pdf` dans « Compte rendu 2 », qui contenait `AF17.PDF`.

| | Contenu de « Compte rendu 2 » |
| --- | --- |
| Avant | `AF17.PDF` |
| Après | `AIR_FRANCE - 7.PDF` |

Nombre total de pièces jointes : **5 avant, 5 après**. `AF17.PDF` a été **détruit**, sans confirmation
ni avertissement d'aucune sorte.

### Conséquence : garde-fou obligatoire

**Déposer dans une catégorie occupée détruit irréversiblement le document du candidat.** En production, un
dépôt automatique en « CV » ou « Pièce d'identité » effacerait une pièce du dossier — potentiellement la
seule copie.

Aggravation : **la modale n'indique pas les catégories occupées.** Toutes les lignes s'affichent
« Parcourir / Aucun fichier sélectionné », y compris celles qui contiennent déjà un document
(relevé sur les 46 lignes : aucune information d'occupation).

**Le bot ne peut donc pas se fier au formulaire.** Séquence obligatoire :

```
1. Sur la fiche, lire la liste des pièces jointes : span[id$='lblAttachedFile']
   → libellés « NOM (Catégorie) » → ensemble des catégories DÉJÀ OCCUPÉES
2. Si la catégorie visée y figure :
      → NE PAS déposer. Retourner {"ok": false, "error": "category_occupied"}
        (ou sauter en "skipped" si le nom du fichier est identique → already_present)
3. Sinon seulement, ouvrir la modale et déposer
```

Ce contrôle doit être **non contournable**. Aucune option de configuration ne devrait permettre au bot
d'écraser une pièce jointe existante : le gain ne vaut pas le risque de détruire une pièce d'un dossier
candidat. Un remplacement délibéré reste un geste de recruteur, à faire à la main.

### Normalisation du nom de fichier par Talentsoft

Le nom déposé est **converti en majuscules** : `Air_France - 7.pdf` → `AIR_FRANCE - 7.PDF`.
Espaces et tirets sont préservés.

→ Toute comparaison de noms (vérification `verified`, détection `already_present`) doit être
**insensible à la casse**. Comparer `normalize_text()` des deux côtés.

## Écran de choix de compte : structure exacte (12/09/2026, après échec du selftest)

Le premier `POST /selftest` contre le tenant a échoué au choix du compte, avec 30 s de timeout par
tentative. Inspection du DOM réel :

| Attribut du bouton radio | Valeur |
| --- | --- |
| `visibility` | **`hidden`** |
| `position` | `absolute` |
| Dimensions | 13 × 13 (non nulles) |
| `opacity` | `1` |

**C'est `visibility: hidden` qui bloquait.** Playwright refuse de cocher un élément invisible et attend
jusqu'au timeout d'action. `check(force=True)` ne suffit pas davantage : l'élément reste hors interaction.

Chaque option expose en revanche un `<label for>` **visible, associé et non recouvert**
(`label.control` pointe bien le radio, `elementFromPoint` renvoie le label lui-même) : c'est l'élément que
clique un recruteur, et le seul geste qui coche réellement l'option.

### Les deux comptes du tenant de recette

| `value` / `id` du radio | Libellé affiché |
| --- | --- |
| `airfrance.fr` | Accès @rtémis pour Air France |
| `idp01test_airfrance` | Accès @rtémis pour les filiales |

Le champ posté est `IdentityProviderName`.

**Les identifiants sont en ASCII, les libellés sont accentués** (`@rtémis`). `TS_ACCOUNT_CHOICE` accepte
donc les deux, et l'identifiant est à privilégier : un libellé accentué dans un `.env` traversant
docker-compose est une source d'ennuis inutile.

### Correctifs apportés

1. `LoginPage._select_account_option` tente plusieurs gestes, du plus humain au plus direct, et
   vérifie `is_checked()` après chacun — jamais supposé.

   **Premier essai en production : tous les gestes ont échoué.** Deux raisons, corrigées depuis :

   - le libellé est enveloppé dans un `<a href="#">` qui **intercepte le clic** : cliquer le
     `label[for]` ne coche donc pas le radio ;
   - `dispatch_event("click")` ne coche pas non plus : un événement **synthétique** ne déclenche
     pas le comportement par défaut du navigateur.

   Ce qui fonctionne est `el.click()` **appelé dans la page** (`radio.evaluate`) : il coche et laisse
   s'exécuter les gestionnaires. Un dernier filet force `checked` puis émet `input`/`change`.

   Les gestes physiques (`container`, `check`, `check(force)`) ne sont tentés que si le radio est
   réellement visible : sur un élément masqué ils ne peuvent aboutir, et chacun consommerait son
   timeout — une vingtaine de secondes perdues à **chaque** connexion.
2. Le sélecteur `label[for=...]` sérialise la valeur en littéral quoté : l'identifiant `airfrance.fr`
   contient un point, qu'un sélecteur CSS non quoté interpréterait comme une classe.
3. `TS_ACCOUNT_CHOICE` accepte l'identifiant ou le libellé.
4. Les options disponibles sont journalisées (`account_choice_options`) : ce sont des noms de compte
   applicatif, pas des données personnelles, et sans eux un échec de choix est indiagnosticable.
   Ce journal a immédiatement prouvé son utilité en production :

   ```
   account_choice_options count=2 options=[('airfrance.fr', 'accès @rtémis pour air france'),
                                           ('idp01test_airfrance', 'accès @rtémis pour les filiales')]
   LoginError: account_choice_not_selectable: option non cochable (TimeoutError)
   ```

   Le compte était bien trouvé ; c'est le geste de sélection qui échouait. L'erreur énumère
   désormais chaque tentative et son issue (`label_for:sans_effet, js_click:ok`), pour que le
   prochain diagnostic ne reparte pas de zéro.

### Leçon d'observabilité

`session_manager` ne propageait que le **type** de l'exception : 62 secondes d'attente pour un
`RuntimeError` sans contexte. Le message est désormais conservé — mais **uniquement** pour les exceptions
que ce dépôt construit lui-même (`LoginError`, `SelectorNotFound`), dont les messages sont écrits sans
secret par contrat. Une exception Playwright, qui peut porter une URL complète ou du HTML, ne propage
toujours que son type.

## Audit des sélecteurs en conditions réelles (12/09/2026)

Parcours rejoué dans le navigateur, en testant à chaque écran les sélecteurs réels du bot.

### Écran de choix de compte — conforme

| Sélecteur | Constat |
| --- | --- |
| `ACCOUNT_CHOICE_RADIOS` | 2 trouvés, **0 visible** (`visibility: hidden`) |
| `ACCOUNT_CHOICE_SUBMIT` | 1 visible |

URL observée : `/hrd?wa=wsignin1.0&wtrealm=urn:oidc-relyingparty:...` — le tenant mêle WS-Fed et OIDC.
`LOGIN_URL_FRAGMENTS` y détecte `signin` : le bot ne se croit pas authentifié sur cet écran.

### Formulaire de l'IdP — conforme

`LOGIN_USERNAME` (`input[name='Username']`), `LOGIN_PASSWORD`, `LOGIN_SUBMIT`
(`button[type='submit'].btn-primary`) et `LOGIN_PAGE_MARKERS` matchent tous, visibles et actifs.

### Bandeau de consentement — un sélecteur était DANGEREUX

Le bandeau expose trois boutons de même facture :

| id | Libellé |
| --- | --- |
| `didomi-notice-learn-more-button` | EN SAVOIR PLUS → |
| **`didomi-notice-disagree-button`** | **REFUSER** |
| `didomi-notice-agree-button` | ACCEPTER & FERMER |

`#didomi-notice-disagree-button` est correct. Mais le second candidat de `COOKIE_REFUSE`,
`button.didomi-button-standard`, matche **« EN SAVOIR PLUS »** : en cas de repli, le bot aurait ouvert un
panneau en croyant refuser. Ce candidat a été retiré au profit d'un sélecteur par libellé.

### Atterrissage post-SSO — BLOQUANT, corrigé

Après authentification, le navigateur atterrit sur **`testairfrance.talent-soft.com/MyTalentsoft#/Me`**
(espace collaborateur), et **non** sur le Back Office. Constats :

- aucun marqueur de `AUTHENTICATED_MARKERS` n'y matche (`markers: []`, `search_input: 0`) ;
- toute la session s'y poursuit (68 requêtes vers cet hôte).

Deux conséquences, toutes deux fatales avant correction :

1. `login()` attendait les marqueurs du Back Office sur cette page : un login **réussi** aurait été
   déclaré en échec (`login_not_confirmed`) après 45 s.
2. Cet hôte n'étant ni `TS_BASE_URL` ni un hôte d'authentification déclaré, **`_guard_route` aurait
   bloqué la navigation d'atterrissage** et le parcours n'aurait jamais abouti.

Correctifs :

- `TS_AUTH_HOSTS` désigne désormais les hôtes **traversés pendant l'authentification, atterrissage
  compris**. Pour ce tenant, trois valeurs sont nécessaires :
  `testfedauthsbg1`, `testidpsbg1` **et `testairfrance`** (espace collaborateur).
- `login()` distingue deux jalons : la **fin du parcours SSO** (`POST_LOGIN_MARKERS`, valable quelle que
  soit l'application d'atterrissage), puis la **présence sur le Back Office**, obtenue en y naviguant
  explicitement avant de conclure au succès.
- Le faux Back Office reproduit cet atterrissage sur un hôte distinct, pour que les tests couvrent le cas.

### Recherche par email — VALIDÉE sur le tenant

Recherche de `bailleulg@gmail.com` depuis `/Home/Welcome` :

```
[role='listbox'] [role='option']  →  1 résultat
libellé : « PAOLI Georges(Ref: 584408)bailleulg@gmail.com »
actionable: true, non recouvert, aucune entrée de menu capturée
```

Le sélecteur prioritaire du bot est confirmé, et **l'unicité du résultat par email** l'est aussi.

Deux enseignements supplémentaires :

1. **Le libellé du résultat contient l'adresse.** `open_single_result()` la confronte désormais à
   l'email demandé : une suggestion portant une **autre** adresse est refusée
   (`AmbiguousCandidate`) au lieu d'être ouverte. L'absence d'adresse dans le libellé n'est pas
   un motif de refus — seule une adresse présente et différente l'est.
2. **`[role='menu'] [role='menuitem']` capturait le menu utilisateur** (« Changer de mot de passe »,
   « Centre d'aide », « **Déconnexion** »). Ce candidat a été retiré, et un filtre d'exclusion par
   libellé a été ajouté dans `GlobalSearch.result_items()` : cliquer « Déconnexion » en croyant ouvrir
   une fiche aurait fait perdre la session à chaque tentative.

### Viewport : la barre de recherche se replie en dessous de ~1000 px

Constaté à 793 px de large : le champ de recherche passe sous un ancêtre en `display: none`, donc
invisible et inutilisable. À 1280 px, il mesure 480 × 33 et redevient normal.

La barre étant le **seul** chemin vers une fiche candidat, le contexte navigateur fixe désormais un
viewport explicite de 1440 × 900, au lieu de s'en remettre au défaut de Playwright.

### Accès direct au Back Office

`{base}/Home/Welcome` ouvre directement l'accueil recrutement (titre « Accueil recrutement »), et les
marqueurs `AUTHENTICATED_MARKERS` y matchent (`/Home/Welcome`, `/VacancyDashboard` visibles).

### Sélection de candidature : un postback qui conditionne tout le reste

Mesuré sur la fiche témoin, **avant** toute sélection :

| Élément | État |
| --- | --- |
| `tr.ch_content_outerrep` | 3 candidatures, visibles |
| `tr.trChildrenEvent` | 6 lignes présentes, **0 visible** (`display: none`) |
| `a[id*='lblJobAppActionName']` | **0 — absentes du DOM** |
| `tr.selectedLine` | absent |

**Après** un clic sur le lien `lnkOfferLabel` de la candidature visée :

| Élément | État |
| --- | --- |
| `tr.trChildrenEvent` | 6 lignes, **toutes visibles** |
| `a[id*='lblJobAppActionName']` | **88 liens, tous visibles, tous avec `confirm()`** |
| `tr.selectedLine` | **présent**, sur la bonne candidature |

Trois conséquences pour le bot :

1. **Sélectionner avant d'agir** n'est pas une précaution mais une nécessité : les actions de workflow
   n'existent pas dans le DOM tant qu'aucune candidature n'est sélectionnée.
2. Attendre la **visibilité** des lignes d'événement, pas leur présence : elles sont déjà là, repliées.
3. Les **88 actions déclenchent toutes un `confirm()`** natif. Sans le handler `page.on("dialog")`,
   aucune ne produirait le moindre effet, silencieusement.

### Accès direct à une fiche : `RedirectionMenu.ashx`

Le lien porté par une suggestion de recherche est :

```
{base}/Pages/Utils/RedirectionMenu.ashx?key=ApplicantView&id=<applicantGuid>
```

Cette URL **ouvre directement la fiche**, onglet Historique actif. Deux enseignements :

- la recherche **expose le `applicantGuid`**, l'identifiant interne qu'on croyait hors de portée ;
- une fois ce GUID connu pour un candidat, la fiche est atteignable sans repasser par l'overlay React.
  Piste d'optimisation : mémoriser le GUID par email côté Hippolyte.ai après le premier accès.

Une URL de recherche directe existe également :
`{base}/Search/RedirectToApplicantSearchResults?searchTerm=<email>`.

### Structure d'une suggestion de recherche

```
[role='option']
  └─ <a href="/Pages/Utils/RedirectionMenu.ashx?key=ApplicantView&id=<guid>">
<a>Voir plus de candidats pour : <email></a>      ← HORS de [role='option']
```

Le lien « Voir plus de candidats » est un frère de l'option, pas un enfant : le sélecteur
`[role='listbox'] [role='option']` ne le capture pas. Un repli sur `[role='listbox'] li`, lui, le
prendrait — raison de plus pour garder le sélecteur le plus étroit en tête de liste.

## FAILLE CRITIQUE : une action de workflow peut envoyer un courrier au candidat

Constatée le 12/09/2026 en exécutant réellement le parcours du bot sur la fiche témoin.

### Ce qui s'est passé

Clic sur l'action **« Candidature à l'étude »**, choisie comme la plus anodine (elle figurait déjà
dans l'historique de la candidature). Elle n'a PAS ouvert le formulaire d'événement, mais :

```
../Correspondence/ActionMailLanguageChoicePage.aspx
    « Langue »  [English UK | Français]
    [Annuler]  [Valider]
```

Un écran de choix de langue pour un **courrier au candidat**. Le bouton de validation est
`btnSend` — *send*, envoyer.

Aucun courrier n'a été envoyé : la modale a été annulée, et l'historique est resté à 6 événements.

### Pourquoi c'était dangereux pour le bot

```
btnSend  id="...ButtonPlaceHolder1_ctl02_btnSend"  class="valid-button"  value="Valider"

EVENT_SUBMIT = ["input[id$='btValidate']", "input.valid-button"]
                 ↑ 0 match ici            ↑ MATCHE btnSend
```

`valid-button` est une classe **partagée par toutes les modales** du Back Office : elle dit qu'un
bouton valide quelque chose, jamais *quoi*. Le repli sur cette classe désignait donc le bouton
d'envoi d'un courrier réel à un candidat.

Le bot n'aurait probablement pas cliqué — `wait_open()` attend l'iframe `JobApplicationChildEventEdit`,
absente ici, et aurait fini en `SelectorNotFound`. Mais la protection était **accidentelle**, et la
modale serait restée ouverte, bloquant les actions suivantes.

### Correctifs

1. **`input.valid-button` retiré** de `EVENT_SUBMIT` et `ATTACHMENT_SUBMIT`. Ne valider que sur un
   suffixe d'identifiant, qui identifie le formulaire.
2. **Détection active** : `EventDialog.wait_open()` surveille `FORBIDDEN_DIALOG_FRAMES`
   (`ActionMailLanguageChoicePage`, `Correspondence/`). Si un tel parcours s'ouvre, le bot **annule
   la modale** et lève `MailDialogOpened`.
3. Côté API, cela devient un échec d'action explicite :
   `{"ok": false, "error": "event_type_sends_mail"}`, sans mutation.
4. Deux tests de non-régression, dont un qui vérifie qu'aucun courrier n'est parti et que la modale
   a bien été refermée.

### Conséquence pour le paramétrage client

**On ne peut pas deviner, depuis le libellé d'un type d'événement, s'il déclenche un courrier.**
« Candidature à l'étude » semblait inoffensif et ne l'était pas. La liste des types « à courrier »
est propre au paramétrage du tenant.

Avant toute mise en production, faire valider par le client la liste des types utilisables par un
automate. Le bot refuse désormais ceux qui ouvrent un parcours de courrier, mais il vaut mieux ne
pas les demander du tout : l'échec survient après l'ouverture de la fiche, donc après avoir consommé
un créneau navigateur.

## Expiration de session : le tenant renvoie vers l'espace collaborateur

Constatée en cours d'audit, après un long moment d'inactivité sur le Back Office.

Un clic sur une action de workflow n'a pas produit de modale : le navigateur s'est retrouvé sur
`testairfrance.talent-soft.com/MyTalentsoft#/Me`. Viser ensuite directement l'URL d'une fiche
(`RedirectionMenu.ashx?key=ApplicantView&id=…`) a **encore** redirigé vers l'espace collaborateur.

**Ce tenant ne montre donc pas de formulaire de login quand la session du Back Office expire.**

### Pourquoi c'était un angle mort

`open_application()` détectait l'expiration en cherchant un formulaire de login ou l'écran de choix
de compte. Sur l'espace collaborateur, ni l'un ni l'autre : le bot se serait cru connecté, puis aurait
échoué plus loin sur une barre de recherche introuvable — un symptôme qui ne désigne pas sa cause, et
qui aurait coûté un long détour de diagnostic en production.

### Correctif

`_on_back_office()` vérifie l'**origine** de la page courante, et non la présence d'un marqueur :
après navigation vers `TS_BASE_URL`, se retrouver sur un autre hôte signifie que la session est perdue.
Le bot relance alors un login complet, et abandonne en `SessionExpired` si la seconde tentative échoue.

Le contrôle par origine est ici plus sûr qu'un contrôle par marqueur : `#TSBody.ts-page`, par exemple,
est présent **aussi** sur l'espace collaborateur, et aurait laissé passer le cas.

## Actions du panneau Outils : un sous-ensemble, et des pièges

Relevé sur la fiche témoin : **88 actions** dans le panneau, pour **105 types** dans le référentiel du
formulaire. Deux conséquences vérifiées :

1. **Un type peut n'avoir aucune action.** « Convocation à un entretien individuel » (code 837) existe
   dans le select du formulaire et dans l'historique de la candidature, mais ne figure pas parmi les 88
   actions. L'action ne sert donc qu'à **ouvrir** le formulaire ; le type se choisit ensuite dans la
   liste déroulante, qui porte le référentiel complet. C'est ce que fait `open_from_workflow_action()`.

2. **Le suffixe `EVENEMENT` semble distinguer les actions sans courrier.** Le tenant expose des paires :

   | Action | Comportement |
   | --- | --- |
   | « Candidature à l'étude » | ouvre un **envoi de courrier** (vérifié) |
   | « A l'étude EVENEMENT » | créerait l'événement seul |

   Cinq actions portent ce suffixe : `Offre clôturée`, `A l'étude`, `En attente`, `KD Non payé`,
   `KD Remboursé`. Le client a vraisemblablement dupliqué ses actions pour séparer les deux usages.
   **À confirmer avec lui** : c'est la piste la plus sérieuse pour choisir `TS_DEFAULT_EVENT_TYPE`.

Une seule action annonce explicitement l'absence d'envoi : « Invitation session (sans mail) ».

## Création d'un événement : LE bon chemin (vérifié par écriture réelle, 12/09/2026)

> **Rectification.** Une analyse précédente concluait qu'un événement avec commentaire exigeait deux
> étapes (création par une action, puis modification). **C'est faux.** Un formulaire de création complet
> existe ; il n'avait simplement pas été trouvé.

### Le bouton est porté par la LIGNE de la candidature

Le tableau d'historique expose, sur chaque ligne de candidature, quatre boutons en fin de ligne :

| Bouton | Intitulé | Usage |
| --- | --- | --- |
| `btnDocumentReader` | Accès aux documents liés à la candidature | lecture |
| `btnSendMailNew` | **Correspondre avec le candidat** | **envoie un courrier — à ne jamais cliquer** |
| **`btnEventActionNew`** | **Effectuer une action sur la candidature** | **ouvre « Création d'un événement »** |
| `btnEventDetailsNew` | Détails de la candidature | lecture |

`btnEventActionNew` ouvre `JobApplicationChildEventEdit.aspx`, titre « **Création d'un événement** » :
106 types, date, commentaire (`maxlength=2000`), « Suivi par ». **Un seul passage suffit.**

Test réel : type « Convocation à un entretien individuel » (837), date 14/09/2026, commentaire saisi.
Résultat en base, relu dans la vue de l'événement :

```
Créé le 12/09/2026 par Gauthier BAILLEUL
Événement   Convocation à un entretien individuel
Date        14/09/2026
Suivi par   Gauthier BAILLEUL
Motif       Hippolyte.ai : creation directe avec commentaire, test du 12/09/2026. A supprimer.
```

Ce bouton appartenant à la ligne de la candidature, **la cible est sans ambiguïté** : aucun risque
d'écrire sur une autre candidature du même candidat.

### Pourquoi les actions du panneau « Outils » ne conviennent pas

Trois comportements distincts, tous constatés :

| Action | Effet réel |
| --- | --- |
| « Candidature à l'étude » | ouvre un **envoi de courrier** (`ActionMailLanguageChoicePage`) |
| « A l'étude EVENEMENT » | **crée l'événement immédiatement, sans proposer de commentaire** |
| *(un formulaire de saisie)* | **aucune action n'en ouvre** |

Le besoin métier étant « événement typé **avec commentaire** », aucune de ces actions ne convient.
`WORKFLOW_ACTION_LINKS` reste déclaré pour le diagnostic, mais **le bot ne l'utilise plus**.

### Le commentaire s'appelle « Motif », et il EST relisible

Il n'apparaît pas dans la liste de l'historique, mais bien dans la **vue** de l'événement
(`JobApplicationChildEventView.aspx`), ouverte en cliquant `lnkEventTitle`, sous le libellé « Motif ».

Cela nuance le constat précédent : une vérification **forte** du commentaire est possible, au prix d'une
ouverture de modale supplémentaire. `verification: "weak"` reste le comportement par défaut — la
vérification forte serait une évolution, à arbitrer selon le coût acceptable par push.

La vue expose aussi `btnDelete` (**Supprimer**) : le bot ne doit jamais le cliquer.

### Piège : l'iframe navigue en interne

En passant de la vue à l'édition (bouton « Modifier »), le document chargé devient `...Edit.aspx` alors
que l'attribut `src` de l'iframe continue d'indiquer `...View.aspx`. Un sélecteur
`iframe[src*='...Edit']` ne la trouve donc pas. `EVENT_DIALOG_FRAME` porte un second candidat plus large
pour couvrir ce cas.


## Choix du compte : pourquoi les gestes Playwright ne pouvaient pas marcher

Deux essais en production ont été nécessaires pour comprendre. Le second journal a tout dit :

```
account_choice_options count=2 options=[('airfrance.fr', ...), ('idp01test_airfrance', ...)]
account_choice: radio non visible, gestes physiques ignorés
LoginError: account_choice_not_selectable: aucun geste n'a coché l'option
  [label_for:is_checked_TimeoutError, js_click:TimeoutError, js_checked:TimeoutError]
```

Trois enseignements, chacun contredisant une hypothèse antérieure :

1. **`label_for` a réussi son clic** — l'échec porte sur `is_checked`, pas sur l'action. Le geste
   « humain » partait donc bien, mais ne cochait rien : le `<a href="#">` qui enveloppe le libellé
   **intercepte** l'événement.
2. **Ce clic fait bouger la page**, ce qui **périme le locator**. Les gestes suivants (`js_click`,
   `js_checked`) opéraient sur un élément détaché et expiraient tous.
3. **Le coût mesuré : 90 secondes** (20:35:56 → 20:37:26), soit 3 × 30 s. Les timeouts de 3 s ne
   s'appliquaient qu'aux *actions* ; `is_checked()` et `evaluate()` n'en recevaient aucun et
   retombaient sur le défaut du contexte.

### Ce qui a été retenu

La sélection se fait désormais **en une seule évaluation dans la page** : retrouver l'option par son
identifiant ou sa valeur, appeler `click()` dessus, se replier sur `checked = true` + `input`/`change`,
et rendre compte de l'état obtenu. Un seul aller-retour, **aucun locator susceptible de se périmer**.

Le relevé des options est lui aussi regroupé en une évaluation, pour la même raison.

### Règle générale pour ce Back Office

Dès qu'un élément est masqué (`visibility: hidden`, taille nulle) **ou** qu'un clic fait muter la page,
les locators Playwright deviennent peu fiables et coûteux : chaque appel sans timeout explicite peut
consommer 30 s pour rien. Dans ces cas, une évaluation unique dans la page est à la fois plus rapide,
plus lisible et plus sûre.

## Login qui n'aboutit pas : la piste du mode headless

Après correction du choix de compte, le selftest atteint le formulaire de l'IdP, soumet les
identifiants… et reste sur l'IdP :

```
account_choice_selected_via=checked
account_choice_submitted choice='accès @rtémis pour les filiales'   (le bon compte)
login_attempt
login_not_confirmed: parcours SSO non abouti (url=testidpsbg1.../wsfed/issue)
```

Ce qui a été écarté par vérification :

| Hypothèse | Vérification |
| --- | --- |
| Mauvais compte sélectionné | Le compte technique dépend bien des **filiales** : la sélection était correcte |
| Formulaire différent selon l'accès | Rejoué dans le navigateur : l'IdP des filiales est **identique** (`Username`, `Password`, `button.btn-primary`) |
| Identifiants erronés | Connexion **manuelle réussie** avec les identifiants du `.env` |
| `.env` altéré (troncature, quotes) | Longueurs et premiers/derniers caractères conformes |
| Redirection vers un 4ᵉ hôte bloqué | Aucun hôte inconnu observé lors du login manuel |

### Hypothèse retenue : le user-agent headless

En headless, Chromium annonce **`HeadlessChrome`**. Des fournisseurs d'identité refusent ces
navigateurs **sans message d'erreur** : le formulaire est accepté, l'authentification n'aboutit pas.
Le symptôme correspond exactement — soumission acceptée, pas d'erreur affichée, retour au formulaire.

Le contexte présente donc désormais un user-agent de bureau, et `BROWSER_USER_AGENT` permet de
l'imposer explicitement. Deux tests garantissent qu'aucun « Headless » ne subsiste.

Ce n'est pas un contournement de protection : le bot s'authentifie avec un compte applicatif
légitime, sur un tenant dont l'exploitant demande cette automatisation.

**Statut : à confirmer** par un selftest après redéploiement. Si l'échec persiste, l'erreur porte
maintenant ce que la page affiche (`page=formulaire_toujours_affiché texte='…'`), et une capture est
enregistrée quand `SCREENSHOTS_ENABLED=true`.

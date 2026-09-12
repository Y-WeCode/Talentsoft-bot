"""Sélecteurs et gabarits d'URL du Back Office Talentsoft : LE fichier à maintenir.

Chaque entrée est une liste de candidats essayés dans l'ordre (chaînes Playwright :
CSS, `text=/regex/i`, `role=button[name=/regex/i]`).

ÉTAT : relevé sur le tenant de recette `testairfrance-rh.talent-soft.com` le 12/09/2026
(voir `docs/DISCOVERY.md`). `POST /selftest` vérifie que les sélecteurs critiques matchent encore.

RÈGLES DE MAINTENANCE, tirées de la découverte
----------------------------------------------

Le Back Office est **hybride** et impose deux styles de sélecteurs :

1. Fiche, historique, documents, modales : **ASP.NET WebForms + Telerik**.
   Les identifiants sont de la forme `ctl00_ctl00_AspMainContent_MainContent_..._btnAddFile`.
   → Cibler par **suffixe** : `input[id$='btnAddFile']`.
   → **Ne jamais** utiliser un id complet, ni un indice de répéteur (`ctl01`, `ctl42`) :
     ces indices se décalent dès qu'une ligne est ajoutée au paramétrage client.

2. Barre de recherche et en-tête : **React + MUI** (design system `ds-hrecruiting`).
   Les id (`#mui-31537`) et les classes (`.ds-hrecruiting-ts290`) sont **générés à chaque rendu**.
   → Cibler **uniquement** par rôle, libellé ou placeholder.

Les deux formulaires de mutation vivent dans des **iframes** (RadWindow Telerik) : voir
`EVENT_DIALOG_FRAME` et `ATTACHMENT_DIALOG_FRAME`, et utiliser `frame_locator` côté page objects.
"""

from __future__ import annotations

# --- Authentification ----------------------------------------------------------------
#
# Le tenant fédère en WS-Federation sur trois hôtes :
#   1. <tenant>-rh.talent-soft.com   (TS_BASE_URL, le Back Office)
#   2. <fédération>.talent-soft.com  (écran de choix de compte)
#   3. <idp>.talent-soft.com         (formulaire identifiant / mot de passe)
# Les hôtes 2 et 3 doivent être autorisés par TS_AUTH_HOSTS, sinon la navigation est bloquée.

LOGIN_PATH = "/"

# Présence de l'un de ces éléments = page de login affichée.
LOGIN_PAGE_MARKERS = [
    "input[name='Username']",
    "input[type='password']",
    "form[action*='wsfed' i]",
    "form[action*='login' i]",
    "form[action*='logon' i]",
]

LOGIN_USERNAME = [
    "input[name='Username']",  # relevé sur le tenant (IdP Talentsoft)
    "input[name='UserName']",
    "input[name='username']",
    "input[name='login']",
    "input[type='email']",
    "input[autocomplete='username']",
    "role=textbox[name=/identifiant|utilisateur|login|e-?mail|nom d'utilisateur/i]",
]

LOGIN_PASSWORD = ["input[name='Password']", "input[type='password']"]

LOGIN_SUBMIT = [
    "button[type='submit'].btn-primary",  # relevé sur le tenant
    "role=button[name=/^\\s*connexion\\s*$/i]",
    "role=button[name=/se connecter|connexion|connecter|login|sign in|valider/i]",
    "button[type='submit']",
    "input[type='submit']",
]

# Case « Maintenir la connexion » : le bot ne doit PAS la cocher (session longue non souhaitée
# pour un compte technique). Listée pour mémoire, jamais cliquée.
LOGIN_REMEMBER_ME = ["input[name='RememberMe']"]

LOGIN_ERROR = [
    "[role='alert']",
    ".validation-summary-errors",
    ".field-validation-error",
    ".alert-danger",
    ".login-error, .error-message",
]

# --- Choix du compte (écran de fédération intermédiaire) -------------------------------
#
# « Sélectionnez le compte avec lequel vous souhaitez vous authentifier »
# Les libellés sont propres au tenant : le compte visé vient de TS_ACCOUNT_CHOICE.

ACCOUNT_CHOICE_MARKERS = [
    "text=/s.lectionnez le compte/i",
    "form:has(input[type='radio']):has-text('authentifier')",
]

ACCOUNT_CHOICE_RADIOS = ["input[type='radio']"]

# Zone réellement cliquable d'une option, exprimée RELATIVEMENT à son bouton radio.
# Sur le tenant, chaque radio est enveloppé dans un lien qui porte le libellé, et le radio
# lui-même est masqué en CSS : c'est ce conteneur qu'un recruteur clique, et le seul élément
# actionnable par Playwright.
# Remonter depuis le radio plutôt que lister les conteneurs : une page qui imbrique
# <a><label><input radio></label></a> produirait deux fois plus de conteneurs que de radios,
# et tout appariement par position serait faux.
ACCOUNT_CHOICE_OPTION_CLICKABLE_FROM_RADIO = "xpath=ancestor::*[self::a or self::label][1]"

ACCOUNT_CHOICE_SUBMIT = [
    "role=button[name=/^\\s*continuer\\s*$/i]",
    "button[type='submit']",
    "input[type='submit']",
]

# Fragments d'URL qui signalent que l'on est encore sur un parcours de connexion.
LOGIN_URL_FRAGMENTS = [
    "login",
    "logon",
    "connexion",
    "signin",
    "sso",
    "wsfed",
    "wsfederation",
    "adfs",
    "microsoftonline",
]

# Présence de l'un de ces éléments = session Back Office recruteur ouverte.
# NB : le Back Office n'expose pas de lien « déconnexion » en clair ; on s'appuie sur des
# marqueurs structurels de l'application recruteur.
AUTHENTICATED_MARKERS = [
    "a.rtsLink",  # barre d'onglets Telerik de la fiche
    "#TSBody.ts-page",  # conteneur racine du Back Office
    "input[placeholder*='Rechercher un candidat' i]",
    "[href*='/Home/Welcome' i]",
    "[href*='/VacancyDashboard' i]",
]

# --- Bandeau de consentement (Didomi) -------------------------------------------------
#
# Affiché au premier chargement, ancré en BAS de la fenêtre (environ un tiers de la hauteur
# sur le tenant), avec un z-index maximal. Il ne recouvre donc pas toute la page, mais masque
# ce qui se trouve dans cette bande : un contrôle en bas d'écran devient incliquable.
# Le bot refuse les finalités non essentielles dès le premier chargement.

COOKIE_BANNER = ["#didomi-notice", "#didomi-host", ".didomi-popup-container"]

# ATTENTION : ne lister ici que des sélecteurs qui désignent le REFUS.
# Le bandeau du tenant expose trois boutons de même facture :
#   #didomi-notice-learn-more-button  « EN SAVOIR PLUS »
#   #didomi-notice-disagree-button    « REFUSER »            <- le seul acceptable
#   #didomi-notice-agree-button       « ACCEPTER & FERMER »
# Un sélecteur de classe générique (`button.didomi-button-standard`) attrape « EN SAVOIR
# PLUS » : le bot croirait refuser tout en ouvrant un panneau. Cibler l'id, puis le libellé.
COOKIE_REFUSE = [
    "#didomi-notice-disagree-button",
    "role=button[name=/^\\s*refuser\\s*$|tout refuser|continuer sans accepter/i]",
]

# Signe que le parcours d'authentification fédérée est terminé, quelle que soit l'application
# d'atterrissage. Le tenant renvoie vers MyTalentsoft (espace collaborateur) et NON vers le
# Back Office : les marqueurs de celui-ci n'y matchent pas. Sans ce jalon intermédiaire, le
# bot conclurait que le login a échoué alors qu'il vient de réussir.
POST_LOGIN_MARKERS = [
    "#TSBody",
    "[href*='MyTalentsoft' i]",
    "[class*='ts-page' i]",
    "a.rtsLink",
    "input[placeholder*='Rechercher' i]",
]

# --- Recherche d'un candidat par email -------------------------------------------------
#
# Barre React/MUI : ids et classes générés, cibler par placeholder uniquement.
# Confirmé : une recherche par email retourne un seul résultat, le bon candidat.

GLOBAL_SEARCH_INPUT = [
    "input[placeholder*='Rechercher un candidat' i]",
    "input[placeholder*='Rechercher' i]",
    "role=searchbox",
    "role=combobox[name=/recherch/i]",
]

# Résultats de la recherche globale (overlay React monté en portal).
#
# DANGER, constaté le 12/09/2026 : `[role='menu'] [role='menuitem']` attrape le **menu
# utilisateur** de l'en-tête (« Changer de mot de passe », « Centre d'aide », « Déconnexion »).
# Un sélecteur de résultats doit être assez étroit pour ne jamais désigner ces entrées :
# cliquer « Déconnexion » en croyant ouvrir une fiche ferait perdre la session à chaque essai.
# On s'en tient donc aux rôles de liste de suggestions, jamais aux menus.
SEARCH_RESULT_ITEMS = [
    "[role='listbox'] [role='option']",
    "[role='option']",
    "[role='listbox'] li",
]

# Libellés du menu utilisateur : un « résultat » qui porte l'un d'eux n'en est pas un.
# Garde-fou de dernier recours, si le tenant montait ses suggestions dans un menu.
SEARCH_RESULT_EXCLUDED_LABELS = [
    "déconnexion",
    "deconnexion",
    "changer de mot de passe",
    "centre d'aide",
    "centre d’aide",
]

# --- Fiche candidature ---------------------------------------------------------------

# Une fiche est ouverte quand la barre d'onglets Telerik est présente.
APPLICATION_READY = [
    "ul.rtsUL a.rtsLink",
    "a.rtsLink",
    "#navigation.TabbackApplicant",
]

APPLICATION_NOT_FOUND = [
    "text=/introuvable|n'existe pas|n’existe pas|not found|acc.s refus.|unauthorized|non autoris./i",
]

# Onglets de la fiche. Chacun a une vraie URL (`?tab=...`), mais la navigation se fait au clic.
TAB_URL_SUFFIXES = {
    "card": "?tab=CardTab",
    "documents": "?tab=ResumeTab",
    "history": "?tab=HistoryTab",
    "appreciation": "?tab=AppreciationTab",
    "interviews": "?tab=InterviewsTab",
    "hiring": "?tab=HiringPropositionTab",
    "screening": "?tab=ScreeningTab",
}

EVENTS_TAB = [
    "a.rtsLink[href*='HistoryTab']",
    "role=link[name=/^\\s*historique\\s*$/i]",
    "a.rtsLink:has-text('Historique')",
]

DOCUMENTS_TAB = [
    "a.rtsLink[href*='ResumeTab']",
    "role=link[name=/^\\s*documents\\s*$/i]",
    "a.rtsLink:has-text('Documents')",
]

# --- Historique des candidatures ------------------------------------------------------
#
# Tableau `events-history-table`, structure en deux niveaux :
#   tr.ch_title             en-tête
#   tr.ch_content_outerrep  une candidature — texte « Réponse à offre <intitulé> ( réf. <référence> ) »
#   tr.trChildrenEvent      les événements de la candidature développée
#
# ATTENTION : `tr.selectedLine` n'est PAS fiable — la classe disparaît après un postback.
# Pour cibler une candidature, se fier à l'ordre du DOM : les `trChildrenEvent` d'une
# candidature suivent sa ligne `ch_content_outerrep`, jusqu'à la `ch_content_outerrep` suivante.

APPLICATIONS_HISTORY_TABLE = ["table.events-history-table", "table.result-grid-view"]

APPLICATION_ROWS = ["table.events-history-table tr.ch_content_outerrep"]

# Lien cliquable d'une candidature : son TEXTE porte la référence de l'offre.
APPLICATION_OFFER_LINK = ["a[id$='lnkOfferLabel']"]

# Lignes d'événement de la candidature développée.
EVENT_ROWS = ["table.events-history-table tr.trChildrenEvent"]

# Libellé du type, dans une ligne d'événement (cellule 3).
EVENT_ROW_TITLE_LINK = ["a[id$='lnkEventTitle']"]

# Ligne éventuellement marquée comme sélectionnée (non garanti, cf. ci-dessus).
SELECTED_APPLICATION_ROW = ["tr.selectedLine"]

# --- Ajout d'un événement de candidature ----------------------------------------------
#
# Déclenché depuis le panneau « Outils » à droite (section Actions). Ouvre un RadWindow
# dont le contenu est un IFRAME.

RECRUITER_TOOLS_ACTIONS = [
    "text=/^\\s*Actions\\s*$/i",
    "[id$='frmApplicantActions_SubformTable'] >> text=/actions/i",
]

# Actions de workflow listées dans le panneau Outils. Beaucoup déclenchent un confirm()
# JavaScript natif : Playwright le REJETTE par défaut, il faut un handler `page.on("dialog")`.
WORKFLOW_ACTION_LINKS = ["a[id*='lblJobAppActionName']"]

# L'iframe qui porte le formulaire d'événement. `rwndrnd` est un anti-cache aléatoire :
# cibler par le nom de la page, jamais par l'URL complète.
EVENT_DIALOG_FRAME = ["iframe[src*='JobApplicationChildEventEdit']"]

# Boutons « Ajouter » de l'onglet Historique. ATTENTION à ne pas les confondre :
#   btCreateApplicantEvent      → événement au niveau CANDIDAT (la personne)
#   btCreateJobApplicationEvent → crée une NOUVELLE CANDIDATURE
# Aucun des deux n'ajoute un événement à une candidature existante : cela passe par
# le panneau Outils → Actions.
ADD_APPLICANT_EVENT_BUTTON = ["input[id$='btCreateApplicantEvent']"]
ADD_JOB_APPLICATION_BUTTON = ["input[id$='btCreateJobApplicationEvent']"]

# --- Champs du formulaire d'événement (DANS l'iframe) ---------------------------------

EVENT_TYPE_SELECT = ["select[id$='ddlJobApplicationChildEventType']", "select[name*='EventType' i]"]

# Format attendu : JJ/MM/AAAA (et non ISO). Pré-rempli à la date du jour.
EVENT_DATE = ["input[id$='EventDate_EventDate_txt']", "input.ts-date-picker__control"]

# maxlength = 2000 caractères.
EVENT_COMMENT = ["textarea[id$='EventComment']", "textarea"]

# Pré-rempli avec l'utilisateur connecté (le compte technique du bot).
EVENT_SUPERVISOR_SELECT = ["select[id$='ddlSupervisor']"]

EVENT_SUBMIT = ["input[id$='btValidate']", "input.valid-button"]
EVENT_CANCEL = ["input[id$='btCancel']", "input.cancel-button"]

# Limite réelle du champ commentaire dans le Back Office.
EVENT_COMMENT_MAX_CHARS = 2000

# --- Pièces jointes -------------------------------------------------------------------

# Liste des pièces jointes de la fiche, au format « NOM_FICHIER.PDF (Catégorie) ».
# Talentsoft met le nom en MAJUSCULES : comparer sans tenir compte de la casse.
ATTACHMENT_ROWS = ["span[id$='lblAttachedFile']", "td.attachedFileTitle"]

# Colonne d'origine d'une pièce jointe : « FO » (déposée par le candidat) ou « BO » (recruteur).
ATTACHMENT_ORIGIN_CELL_INDEX = 3

# Lien de suppression d'une pièce jointe. LE BOT NE DOIT JAMAIS LE CLIQUER.
ATTACHMENT_DELETE_LINK = ["a[id$='btnDeleteAttachedFileNew']"]

# Ouvre le formulaire de dépôt (RadWindow + iframe).
ADD_ATTACHMENT_BUTTON = ["input[id$='btnAddFile']"]

ATTACHMENT_DIALOG_FRAME = ["iframe[src*='AttachedFileEdit']"]

# Dans l'iframe : 46 lignes, UNE PAR CATÉGORIE, chacune avec son propre champ fichier.
# Il n'y a ni liste déroulante de catégorie, ni champ « nom affiché ».
# La catégorie se choisit en remplissant le champ de la ligne portant son libellé.
ATTACHMENT_FILE_INPUTS = ["input[type='file']"]

# Bouton de validation du dépôt : value « Enregistrer » (et non « Valider »).
ATTACHMENT_SUBMIT = ["input[id$='btValidate']", "input.valid-button"]
ATTACHMENT_CANCEL = ["input[id$='btCancel']", "input.cancel-button"]

# Contraintes affichées par le formulaire de dépôt.
ATTACHMENT_MAX_SIZE_BYTES = 10240 * 1024
ATTACHMENT_ALLOWED_EXTENSIONS = {".doc", ".rtf", ".docx", ".pdf", ".tif", ".tiff", ".xlsx", ".zip"}

# --- Auto-test -------------------------------------------------------------------------

# Sélecteurs vérifiés par POST /selftest sur la candidature témoin (lecture seule).
CRITICAL_SELECTORS = {
    "authenticated_markers": AUTHENTICATED_MARKERS,
    "global_search_input": GLOBAL_SEARCH_INPUT,
    "application_ready": APPLICATION_READY,
    "events_tab": EVENTS_TAB,
    "applications_history_table": APPLICATIONS_HISTORY_TABLE,
    "application_offer_link": APPLICATION_OFFER_LINK,
    "add_attachment_button": ADD_ATTACHMENT_BUTTON,
    "attachment_rows": ATTACHMENT_ROWS,
}

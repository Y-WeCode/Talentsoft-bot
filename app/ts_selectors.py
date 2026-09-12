"""Sélecteurs et gabarits d'URL du Back Office Talentsoft : LE fichier à maintenir.

Chaque entrée est une liste de candidats essayés dans l'ordre (chaînes Playwright :
CSS, `text=/regex/i`, `role=button[name=/regex/i]`). Préférer les rôles et le texte
visible aux classes CSS, qui changent à chaque mise à jour Cegid.

ÉTAT : gabarit de phase 0. Chaque liste est à confirmer sur le tenant de recette et
consignée dans docs/DISCOVERY.md. `POST /selftest` vérifie que les sélecteurs critiques
matchent encore.
"""

from __future__ import annotations

# --- Authentification ----------------------------------------------------------------

LOGIN_PATH = "/"

# Présence de l'un de ces éléments = page de login affichée.
LOGIN_PAGE_MARKERS = [
    "input[type='password']",
    "form[action*='login' i]",
    "form[action*='logon' i]",
    "form[action*='connexion' i]",
]

LOGIN_USERNAME = [
    "input[name='username']",
    "input[name='UserName']",
    "input[name='login']",
    "input[name='Login']",
    "input[type='email']",
    "input[autocomplete='username']",
    "role=textbox[name=/identifiant|utilisateur|login|e-?mail|nom d'utilisateur/i]",
]

LOGIN_PASSWORD = ["input[type='password']"]

LOGIN_SUBMIT = [
    "role=button[name=/se connecter|connexion|connecter|login|sign in|valider/i]",
    "button[type='submit']",
    "input[type='submit']",
]

LOGIN_ERROR = [
    "[role='alert']",
    ".validation-summary-errors",
    ".field-validation-error",
    ".alert-danger",
    ".login-error, .error-message",
]

# Fragments d'URL qui signalent que l'on est encore sur un parcours de connexion.
LOGIN_URL_FRAGMENTS = ["login", "logon", "connexion", "signin", "sso", "microsoftonline", "adfs"]

# Présence de l'un de ces éléments = session recruteur ouverte.
AUTHENTICATED_MARKERS = [
    "role=link[name=/déconnexion|se déconnecter|deconnexion|logout|sign out/i]",
    "role=button[name=/déconnexion|se déconnecter|deconnexion|logout|sign out/i]",
    "[href*='logout' i]",
    "[href*='logoff' i]",
    "[href*='deconnexion' i]",
]

# --- Fiche candidature ---------------------------------------------------------------

APPLICATION_READY = [
    "role=heading[level=1]",
    "role=heading[level=2]",
    "[class*='candidat' i]",
    "[class*='application' i]",
]

APPLICATION_NOT_FOUND = [
    "text=/introuvable|n'existe pas|n’existe pas|not found|accès refusé|acces refuse|unauthorized|non autorisé/i",
]

# --- Événements (onglet Historique / Suivi) -------------------------------------------

EVENTS_TAB = [
    "role=tab[name=/historique|événements|evenements|suivi|actions/i]",
    "role=link[name=/historique|événements|evenements|suivi|actions/i]",
    "role=button[name=/historique|événements|evenements|suivi/i]",
]

ADD_EVENT_BUTTON = [
    "role=button[name=/ajouter un événement|ajouter un evenement|nouvel événement|nouvel evenement|ajouter une action|add event/i]",
    "role=link[name=/ajouter un événement|ajouter un evenement|nouvel événement|nouvel evenement|ajouter une action/i]",
]

EVENT_DIALOG = [
    "role=dialog",
    ".modal:visible",
    "[class*='popin' i]:visible",
    "[class*='popup' i]:visible",
]

EVENT_TYPE_SELECT = [
    "role=combobox[name=/type d'événement|type d’événement|type d'evenement|type|événement|evenement|action/i]",
    "select[name*='event' i]",
    "select[name*='type' i]",
    "select[name*='action' i]",
    "select",
]

EVENT_COMMENT = [
    "role=textbox[name=/commentaire|comment|description|observation/i]",
    "textarea",
]

EVENT_DATE = [
    "input[type='date']",
    "role=textbox[name=/date/i]",
    "input[name*='date' i]",
]

EVENT_SUBMIT = [
    "role=button[name=/enregistrer|valider|confirmer|save|ok/i]",
    "button[type='submit']",
    "role=button[name=/ajouter/i]",
]

# Lignes de l'historique après ajout (vérification de la mutation).
EVENT_ROWS = [
    "[class*='history' i] li, [class*='historique' i] li, [class*='timeline' i] li",
    "[class*='event' i] [class*='item' i], [class*='evenement' i] [class*='item' i]",
    "table tbody tr",
]

# --- Pièces jointes --------------------------------------------------------------------

ATTACHMENTS_TAB = [
    "role=tab[name=/pièces jointes|pieces jointes|documents|fichiers|attachments/i]",
    "role=link[name=/pièces jointes|pieces jointes|documents|fichiers/i]",
    "role=button[name=/pièces jointes|pieces jointes|documents|fichiers/i]",
]

ADD_ATTACHMENT_BUTTON = [
    "role=button[name=/ajouter (une |un )?(pièce|piece|document|fichier)|joindre|importer|upload/i]",
    "role=link[name=/ajouter (une |un )?(pièce|piece|document|fichier)|joindre|importer/i]",
]

ATTACHMENT_FILE_INPUT = ["input[type='file']"]

ATTACHMENT_CATEGORY_SELECT = [
    "role=combobox[name=/catégorie|categorie|type de (pièce|piece|document)|type/i]",
    "select[name*='categor' i]",
    "select[name*='type' i]",
    "select",
]

ATTACHMENT_NAME_INPUT = [
    "role=textbox[name=/nom|libellé|libelle|titre|description/i]",
    "input[name*='name' i]",
    "input[name*='label' i]",
]

ATTACHMENT_SUBMIT = [
    "role=button[name=/enregistrer|valider|envoyer|confirmer|save|upload/i]",
    "button[type='submit']",
    "role=button[name=/ajouter/i]",
]

ATTACHMENT_ROWS = [
    "[class*='attachment' i] li, [class*='piece' i] li, [class*='document' i] li, [class*='fichier' i] li",
    "table tbody tr",
    "a[href*='download' i], a[href*='telecharg' i]",
]

# --- Auto-test -------------------------------------------------------------------------

# Sélecteurs vérifiés par POST /selftest sur la candidature témoin (lecture seule).
CRITICAL_SELECTORS = {
    "authenticated_markers": AUTHENTICATED_MARKERS,
    "application_ready": APPLICATION_READY,
    "events_tab": EVENTS_TAB,
    "add_event_button": ADD_EVENT_BUTTON,
    "attachments_tab": ATTACHMENTS_TAB,
    "add_attachment_button": ADD_ATTACHMENT_BUTTON,
}

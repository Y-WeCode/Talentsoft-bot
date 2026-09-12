"""Schémas d'entrée de l'API.

Une candidature n'est PAS adressable par un identifiant unique côté Back Office : les
identifiants de l'API Recruiting Customer n'y sont pas reconnus (docs/DISCOVERY.md).
Elle est donc désignée par le couple **email du candidat + identifiant de l'offre**, les
deux seules données dont dispose Hippolyte.ai et qui soient exploitables dans l'interface.
"""

from pydantic import BaseModel, Field


class ApplicationRef(BaseModel):
    """Désignation d'une candidature."""

    candidate_email: str = Field(
        ...,
        description="Email du candidat : sert à le retrouver dans la recherche du Back Office",
        max_length=254,
    )
    offer_id: str = Field(
        ...,
        description="Identifiant de l'offre : sert à choisir la bonne candidature du candidat",
        max_length=32,
    )


class EventRequest(ApplicationRef):
    event_type: str | None = Field(
        None,
        description="Type d'événement Talentsoft (libellé du référentiel). Défaut : TS_DEFAULT_EVENT_TYPE",
        max_length=200,
    )
    comment: str = Field(..., description="Commentaire de l'événement", min_length=1)
    event_date: str | None = Field(None, description="Date de l'événement (YYYY-MM-DD). Défaut : aujourd'hui")
    idempotency_key: str | None = Field(None, max_length=200, description="Clé de dédoublonnage fournie par l'appelant")


class JobStatus(BaseModel):
    id: str
    type: str
    status: str

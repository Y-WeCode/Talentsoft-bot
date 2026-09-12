from pydantic import BaseModel, Field


class EventRequest(BaseModel):
    event_type: str | None = Field(
        None,
        description="Type d'événement Talentsoft (libellé ou code du référentiel). Défaut : TS_DEFAULT_EVENT_TYPE",
        max_length=200,
    )
    comment: str = Field(..., description="Commentaire de l'événement", min_length=1)
    event_date: str | None = Field(None, description="Date de l'événement (YYYY-MM-DD). Défaut : aujourd'hui")
    idempotency_key: str | None = Field(None, max_length=200, description="Clé de dédoublonnage fournie par l'appelant")


class JobStatus(BaseModel):
    id: str
    type: str
    status: str

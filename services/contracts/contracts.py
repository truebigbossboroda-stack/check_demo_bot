from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type

from pydantic import BaseModel, Field, ValidationError, field_validator
from uuid import UUID

class AggregateRef(BaseModel):
    type: str
    id: UUID

class EventEnvelope(BaseModel):
    schema_version: int = Field(ge=1)
    event_id: UUID
    type: str
    aggregate: AggregateRef
    idempotency_key: Optional[str] = None
    created_at: datetime
    payload: Dict[str, Any]

    @field_validator("created_at")
    @classmethod
    def must_be_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        # нормализуем в UTC
        return v.astimezone(timezone.utc)

# ---- Payloads (пример) ----

class GameSessionCreated(BaseModel):
    initiator: str
    seed: Optional[int] = None

class CrisisPresented(BaseModel):
    crisis_id: str
    phase: str
    turn: int = Field(ge=1)

class ChoiceMade(BaseModel):
    crisis_id: str
    option_key: str
    cost: int = Field(ge=0)
    effects: Dict[str, int] = Field(default_factory=dict)

class StateUpdated(BaseModel):
    turn: int = Field(ge=1)
    delta: Dict[str, int]
    state: Optional[Dict[str, Any]] = None

class SessionFinished(BaseModel):
    result: str
    final_state: Dict[str, Any]

EVENT_PAYLOADS: Dict[str, Type[BaseModel]] = {
    "game_session.created": GameSessionCreated,
    "game_session.crisis.presented": CrisisPresented,
    "game_session.choice.made": ChoiceMade,
    "game_session.state.updated": StateUpdated,
    "game_session.finished": SessionFinished,
}

class UnknownEventType(Exception):
    pass

class InvalidEvent(Exception):
    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.details = details

def parse_and_validate(raw: bytes) -> tuple[EventEnvelope, BaseModel]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        raise InvalidEvent("invalid_json", {"error": str(e)})

    try:
        env = EventEnvelope.model_validate(data)
    except ValidationError as e:
        raise InvalidEvent("invalid_envelope", e.errors())

    payload_model = EVENT_PAYLOADS.get(env.type)
    if not payload_model:
        raise UnknownEventType(env.type)

    try:
        payload = payload_model.model_validate(env.payload)
    except ValidationError as e:
        raise InvalidEvent("invalid_payload", e.errors())

    return env, payload
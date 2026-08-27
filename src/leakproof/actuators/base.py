from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.orm import Session

from leakproof.guardrails import GateVerdict
from leakproof.messaging import RenderedMessage


@dataclass(frozen=True)
class ActuatorRequest:
    action_id: str
    action_type: str
    case_id: str
    entity_id: str
    customer_id: str
    amount_paise: int
    currency: str
    idempotency_key: str
    channel: str | None = None
    message: RenderedMessage | None = None


@dataclass(frozen=True)
class ActuatorResult:
    provider: str
    provider_ref: str
    status: str
    response: dict[str, Any]
    replayed: bool = False


class Actuator(Protocol):
    def execute(
        self,
        session: Session,
        request: ActuatorRequest,
        verdict: GateVerdict,
    ) -> ActuatorResult: ...

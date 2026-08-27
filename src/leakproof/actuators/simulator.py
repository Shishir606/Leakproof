from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from leakproof.actuators.base import ActuatorRequest, ActuatorResult
from leakproof.guardrails import GateVerdict
from leakproof.messaging import RenderedMessage
from leakproof.models.db import ActuatorReceipt


class SimulatorActuator:
    """Deterministic external-provider stand-in with a durable dedupe ledger."""

    def __init__(self, provider: str, *, requires_message: bool = False) -> None:
        self.provider = provider
        self.requires_message = requires_message

    def execute(
        self,
        session: Session,
        request: ActuatorRequest,
        verdict: GateVerdict,
    ) -> ActuatorResult:
        if not isinstance(verdict, GateVerdict) or verdict.decision != "ALLOW":
            raise PermissionError("an ALLOW GateVerdict is required for actuator execution")
        if self.requires_message and not isinstance(request.message, RenderedMessage):
            raise TypeError("customer messaging requires a registered RenderedMessage")

        existing = session.get(ActuatorReceipt, request.idempotency_key)
        if existing is not None:
            return ActuatorResult(
                provider=existing.provider,
                provider_ref=existing.provider_ref,
                status=str(existing.response["status"]),
                response=dict(existing.response),
                replayed=True,
            )

        digest = hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:20]
        provider_ref = f"sim_{self.provider}_{digest}"
        request_payload: dict[str, Any] = {
            "action_type": request.action_type,
            "case_id": request.case_id,
            "entity_id": request.entity_id,
            "customer_id": request.customer_id,
            "amount_paise": request.amount_paise,
            "currency": request.currency,
            "channel": request.channel,
        }
        if request.message is not None:
            request_payload["message"] = {
                "template_id": request.message.template_id,
                "registration_ref": request.message.registration_ref,
                "language": request.message.language,
            }
        response = {"status": "succeeded", "simulated": True}
        session.add(
            ActuatorReceipt(
                idempotency_key=request.idempotency_key,
                action_id=request.action_id,
                provider=self.provider,
                provider_ref=provider_ref,
                request=request_payload,
                response=response,
            )
        )
        session.flush()
        return ActuatorResult(
            provider=self.provider,
            provider_ref=provider_ref,
            status="succeeded",
            response=response,
        )


class SimulatorActuatorRegistry:
    def __init__(self) -> None:
        self.payment = SimulatorActuator("razorpay")
        self.messaging = SimulatorActuator("messaging", requires_message=True)
        self.voice = SimulatorActuator("voice", requires_message=True)
        self.human = SimulatorActuator("human_queue")

    def for_action(self, action_type: str) -> SimulatorActuator:
        if action_type == "silent_retry":
            return self.payment
        if action_type == "voice_hinglish":
            return self.voice
        if action_type == "human_handoff":
            return self.human
        if action_type in {
            "alt_method_prompt",
            "email_link",
            "whatsapp_link",
            "sms_link",
        }:
            return self.messaging
        raise LookupError(f"no simulator actuator for {action_type}")

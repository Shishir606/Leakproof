from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from leakproof.audit.timeline import append_event
from leakproof.messaging import RenderedMessage, TemplateRegistry
from leakproof.models.db import Action, Customer, Promise, RecoveryCase, VoiceTurn
from leakproof.models.domain import LeakType

VoiceIntent = Literal[
    "IDENTITY_CONFIRMED",
    "WRONG_PERSON",
    "PROMISE_TO_PAY",
    "PAY_NOW",
    "OPT_OUT",
    "STOP",
    "UNKNOWN",
]

MAX_CUSTOMER_TURNS = 2
MAX_PROMISE_DAYS = 30

_OPT_OUT = (
    "do not call",
    "don't call",
    "dont call",
    "stop calling",
    "opt out",
    "call mat",
    "phone mat",
    "dobara call mat",
)
_STOP = ("stop", "not now", "busy", "baad mein", "baad me", "abhi nahi")
_YES = ("yes", "haan", "han", "ji haan", "speaking", "main hi")
_WRONG_PERSON = ("wrong person", "galat number", "nahin", "nahi", "no")
_PAY_NOW = ("pay now", "abhi pay", "payment link", "link bhej", "send link")

_MONTHS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}
_MONTH_PATTERN = "|".join(sorted(_MONTHS, key=len, reverse=True))


@dataclass(frozen=True)
class VoiceReply:
    action_id: str
    case_id: str
    provider_turn_id: str
    turn_number: int
    intent: VoiceIntent
    message: RenderedMessage
    ended: bool
    promise_id: int | None = None
    replayed: bool = False


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _contains(text: str, phrases: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text) for phrase in phrases)


def _parse_promise_date(transcript: str, *, today: date) -> date | None:
    text = transcript.casefold().strip()
    if "tomorrow" in text or re.search(r"\bkal\b", text):
        return today + timedelta(days=1)

    iso = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            return None

    day_first = re.search(
        rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_PATTERN})(?:\s+(20\d{{2}}))?\b",
        text,
    )
    month_first = re.search(
        rf"\b({_MONTH_PATTERN})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s+(20\d{{2}}))?\b",
        text,
    )
    if day_first:
        day, month_name, year_text = day_first.groups()
    elif month_first:
        month_name, day, year_text = month_first.groups()
    else:
        return None

    year = int(year_text) if year_text else today.year
    try:
        candidate = date(year, _MONTHS[month_name], int(day))
        if year_text is None and candidate < today:
            candidate = candidate.replace(year=year + 1)
        return candidate
    except ValueError:
        return None


def _intent(transcript: str, *, today: date) -> tuple[VoiceIntent, date | None]:
    text = " ".join(transcript.casefold().split())
    if _contains(text, _OPT_OUT):
        return "OPT_OUT", None
    if _contains(text, _STOP):
        return "STOP", None
    promised_on = _parse_promise_date(text, today=today)
    if promised_on is not None:
        return "PROMISE_TO_PAY", promised_on
    if _contains(text, _PAY_NOW):
        return "PAY_NOW", None
    if _contains(text, _YES):
        return "IDENTITY_CONFIRMED", None
    if _contains(text, _WRONG_PERSON):
        return "WRONG_PERSON", None
    return "UNKNOWN", None


def _message(
    template_id: str,
    *,
    case: RecoveryCase,
    promised_on: date | None = None,
) -> RenderedMessage:
    variables: dict[str, str] = {}
    if template_id == "util_voice_payment_options_v1":
        variables = {
            "amount": f"INR {case.amount_at_risk / 100:,.2f}",
            "link": f"https://pay.example/{case.entity_id}",
        }
    elif template_id == "util_voice_promise_confirm_v1":
        assert promised_on is not None
        variables = {"promised_on": promised_on.isoformat()}
    elif template_id == "util_voice_link_confirm_v1":
        variables = {"link": f"https://pay.example/{case.entity_id}"}
    return TemplateRegistry().render(template_id, variables, language="hinglish")


def _existing_reply(session: Session, turn: VoiceTurn) -> VoiceReply:
    case = session.get(RecoveryCase, turn.case_id)
    if case is None:
        raise ValueError(f"voice turn {turn.provider_turn_id} has no case")
    promise = session.scalar(
        select(Promise).where(
            Promise.case_id == turn.case_id,
            Promise.transcript_ref == turn.provider_turn_id,
        )
    )
    promised_on = promise.promised_on if promise is not None else None
    return VoiceReply(
        action_id=turn.action_id,
        case_id=turn.case_id,
        provider_turn_id=turn.provider_turn_id,
        turn_number=turn.turn_number,
        intent=turn.intent,  # type: ignore[arg-type]
        message=_message(turn.reply_template_id, case=case, promised_on=promised_on),
        ended=turn.ended,
        promise_id=promise.id if promise is not None else None,
        replayed=True,
    )


def handle_voice_turn(
    session: Session,
    action_id: str,
    *,
    provider_turn_id: str,
    transcript: str,
    occurred_at: datetime | None = None,
) -> VoiceReply:
    """Process one untrusted transcript through a fixed, maximum-two-turn dialogue."""
    if not provider_turn_id.strip():
        raise ValueError("provider_turn_id is required")
    if not transcript.strip():
        raise ValueError("transcript is required")
    if len(transcript) > 2_000:
        raise ValueError("transcript exceeds 2000 characters")

    existing = session.get(VoiceTurn, provider_turn_id)
    if existing is not None:
        if existing.action_id != action_id:
            raise ValueError("provider_turn_id already belongs to another action")
        return _existing_reply(session, existing)

    action = session.scalar(select(Action).where(Action.id == action_id).with_for_update())
    if action is None:
        raise LookupError(action_id)
    if action.action_type != "voice_hinglish" or action.status != "succeeded":
        raise ValueError("voice turns require a successfully gated voice_hinglish action")
    case = session.get(RecoveryCase, action.case_id)
    if case is None:
        raise ValueError(f"action {action_id} has no case")
    if case.leak_type != LeakType.INVOICE_OVERDUE.value:
        raise ValueError("bounded promise-to-pay voice is limited to overdue invoices")
    if case.outcome is not None:
        raise ValueError("voice turns cannot be added to a closed case")

    prior_turns = list(
        session.scalars(
            select(VoiceTurn)
            .where(VoiceTurn.action_id == action.id)
            .order_by(VoiceTurn.turn_number)
        )
    )
    if prior_turns and prior_turns[-1].ended:
        raise ValueError("voice conversation has already ended")
    if len(prior_turns) >= MAX_CUSTOMER_TURNS:
        raise ValueError("voice conversation reached its two-turn limit")

    at = _aware(occurred_at or datetime.now(UTC))
    if action.executed_at is not None and at < _aware(action.executed_at):
        raise ValueError("voice turn cannot predate the executed call")
    turn_number = len(prior_turns) + 1
    intent, promised_on = _intent(transcript, today=at.date())
    normalized = " ".join(transcript.casefold().split())
    identity_confirmed = any(
        item.intent == "IDENTITY_CONFIRMED" for item in prior_turns
    ) or (turn_number == 1 and _contains(normalized, _YES))
    if intent in {"PROMISE_TO_PAY", "PAY_NOW"} and not identity_confirmed:
        intent, promised_on = "UNKNOWN", None
    promise: Promise | None = None

    if intent == "OPT_OUT":
        customer = session.get(Customer, case.customer_id)
        if customer is None:
            raise ValueError(f"case {case.id} has no customer")
        customer.dnc = True
        customer.dnc_at = at
        for pending in session.scalars(
            select(Action).where(Action.case_id == case.id, Action.status == "pending")
        ):
            pending.status = "cancelled"
        template_id, ended = "util_voice_optout_v1", True
    elif intent in {"STOP", "WRONG_PERSON"}:
        template_id, ended = "util_voice_stop_v1", True
    elif intent == "PROMISE_TO_PAY":
        assert promised_on is not None
        if not at.date() <= promised_on <= at.date() + timedelta(days=MAX_PROMISE_DAYS):
            intent = "UNKNOWN"
            promised_on = None
            template_id = "util_voice_human_v1" if turn_number == 2 else "util_voice_date_retry_v1"
            ended = turn_number == 2
        else:
            promise = Promise(
                case_id=case.id,
                promised_on=promised_on,
                amount_paise=case.amount_at_risk,
                captured_via="voice",
                transcript_ref=provider_turn_id,
            )
            session.add(promise)
            template_id, ended = "util_voice_promise_confirm_v1", True
    elif intent == "PAY_NOW":
        template_id, ended = "util_voice_link_confirm_v1", True
    elif intent == "IDENTITY_CONFIRMED" and turn_number == 1:
        template_id, ended = "util_voice_payment_options_v1", False
    else:
        template_id = "util_voice_human_v1" if turn_number == 2 else "util_voice_identity_retry_v1"
        ended = turn_number == 2

    message = _message(template_id, case=case, promised_on=promised_on)
    turn = VoiceTurn(
        provider_turn_id=provider_turn_id,
        action_id=action.id,
        case_id=case.id,
        turn_number=turn_number,
        transcript=transcript,
        intent=intent,
        reply_template_id=template_id,
        ended=ended,
        occurred_at=at,
    )
    try:
        with session.begin_nested():
            session.add(turn)
            session.flush()
    except IntegrityError:
        duplicate = session.get(VoiceTurn, provider_turn_id)
        if duplicate is None:
            raise
        return _existing_reply(session, duplicate)

    append_event(
        session,
        case,
        kind="VOICE_TURN",
        payload={
            "action_id": action.id,
            "provider_turn_id": provider_turn_id,
            "turn_number": turn_number,
            "transcript": transcript,
            "intent": intent,
            "reply_template_id": template_id,
            "ended": ended,
        },
        actor="voice_simulator",
    )
    if intent == "OPT_OUT":
        append_event(
            session,
            case,
            kind="STOPPED",
            payload={
                "reason": "VOICE_OPT_OUT",
                "action_id": action.id,
                "provider_turn_id": provider_turn_id,
            },
            actor="voice_simulator",
        )
    if promise is not None:
        session.flush()
        append_event(
            session,
            case,
            kind="PROMISE_CAPTURED",
            payload={
                "promise_id": promise.id,
                "promised_on": promise.promised_on.isoformat(),
                "amount_paise": promise.amount_paise,
                "captured_via": promise.captured_via,
                "transcript_ref": provider_turn_id,
            },
            actor="voice_simulator",
        )
    session.commit()
    return VoiceReply(
        action_id=action.id,
        case_id=case.id,
        provider_turn_id=provider_turn_id,
        turn_number=turn_number,
        intent=intent,
        message=message,
        ended=ended,
        promise_id=promise.id if promise is not None else None,
    )

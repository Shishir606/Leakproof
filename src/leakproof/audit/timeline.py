from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from leakproof.models.db import Event, RecoveryCase
from leakproof.models.domain import CaseEvent, CaseSnapshot, CaseState, ReplayedCase

STATE_BY_EVENT = {
    "DETECTED": CaseState.DETECTED,
    "DIAGNOSED": CaseState.DIAGNOSED,
    "PLANNED": CaseState.PLANNED,
    "ACTED": CaseState.ACTING,
    "WAITING": CaseState.WAITING,
    "VERIFYING": CaseState.VERIFYING,
    "CLOSED": CaseState.CLOSED,
    "SUPPRESSED": CaseState.SUPPRESSED,
    "STOPPED": CaseState.STOPPED,
    "ESCALATED": CaseState.ESCALATED,
}


def append_event(
    session: Session,
    case: RecoveryCase,
    *,
    kind: str,
    payload: dict,
    actor: str,
) -> Event:
    """Append a case event while holding the case row lock on PostgreSQL."""
    locked_case = session.execute(
        select(RecoveryCase).where(RecoveryCase.id == case.id).with_for_update()
    ).scalar_one()
    last_seq = session.scalar(select(func.max(Event.seq)).where(Event.case_id == locked_case.id))
    event = Event(
        case_id=locked_case.id,
        seq=(last_seq or 0) + 1,
        kind=kind,
        payload=payload,
        actor=actor,
    )
    session.add(event)
    next_state = STATE_BY_EVENT.get(kind)
    if next_state is not None:
        locked_case.state = next_state.value
    session.flush()
    return event


def reduce_state(events: Iterable[Event]) -> CaseState:
    state: CaseState | None = None
    for event in events:
        state = STATE_BY_EVENT.get(event.kind, state)
    if state is None:
        raise ValueError("cannot replay a case with no lifecycle events")
    return state


def replay_case(session: Session, case_id: str) -> ReplayedCase:
    case = session.get(RecoveryCase, case_id)
    if case is None:
        raise LookupError(case_id)
    events = list(
        session.scalars(select(Event).where(Event.case_id == case_id).order_by(Event.seq))
    )
    projection = CaseSnapshot.model_validate(case)
    first = events[0] if events else None
    source_snapshot = first.payload.get("case") if first is not None else None
    snapshot = (
        CaseSnapshot.model_validate(source_snapshot) if source_snapshot is not None else projection
    )
    for event in events:
        if event.kind == "RECLASSIFIED":
            for field in (
                "to_leak_type",
                "entity_type",
                "entity_id",
                "amount_at_risk",
                "currency",
            ):
                if field not in event.payload:
                    continue
                target = "leak_type" if field == "to_leak_type" else field
                setattr(snapshot, target, event.payload[field])
    snapshot.state = reduce_state(events)
    return ReplayedCase(
        case=snapshot,
        events=[CaseEvent.model_validate(event) for event in events],
        replayed_state=snapshot.state,
        projection_matches=projection == snapshot,
    )

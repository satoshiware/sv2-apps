from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import PayoutEvent, Settlement, User, UserPayout


@dataclass(frozen=True)
class SenderStats:
    attempted: int
    sent: int
    failed: int
    created_events: int


def send_payout_event(payload: dict[str, Any], dry_run: bool = True) -> bool:
    """Default sender transport.

    In dry-run mode we mark events as sent without calling external systems.
    """
    _ = payload
    if dry_run:
        return True
    return False


def _to_btc_str(value: Any) -> str:
    return f"{Decimal(str(value)):.8f}"


def _build_payload(settlement: Settlement, user: User, payout: UserPayout) -> dict[str, Any]:
    return {
        "settlement_id": settlement.id,
        "period_start": settlement.period_start.isoformat(),
        "period_end": settlement.period_end.isoformat(),
        "user_id": user.id,
        "username": user.username,
        "payout_id": payout.id,
        "amount_btc": _to_btc_str(payout.amount_btc),
        "idempotency_key": payout.idempotency_key,
    }


def _get_or_create_payout_event(session: Session, payout_id: int, payload: dict[str, Any]) -> tuple[PayoutEvent, bool]:
    event = session.execute(select(PayoutEvent).where(PayoutEvent.payout_id == payout_id)).scalar_one_or_none()
    if event is not None:
        return event, False

    event = PayoutEvent(
        payout_id=payout_id,
        payload_json=json.dumps(payload, sort_keys=True),
        status="pending_sent",
    )
    session.add(event)
    session.flush()
    return event, True


def process_payout_events(
    session: Session,
    *,
    dry_run: bool = True,
    transport: Callable[[dict[str, Any], bool], bool] = send_payout_event,
) -> SenderStats:
    """Create missing events and send unsent payout events exactly once."""
    payouts = session.execute(
        select(UserPayout, User, Settlement)
        .join(User, User.id == UserPayout.user_id)
        .join(Settlement, Settlement.id == UserPayout.settlement_id)
        .where(UserPayout.status != "sent")
        .order_by(UserPayout.id.asc())
    ).all()

    attempted = 0
    sent = 0
    failed = 0
    created_events = 0

    for payout, user, settlement in payouts:
        payload = _build_payload(settlement, user, payout)
        event, created = _get_or_create_payout_event(session, payout.id, payload)
        if created:
            created_events += 1

        if event.status == "sent":
            payout.status = "sent"
            continue

        attempted += 1
        ok = transport(payload, dry_run)
        if ok:
            event.status = "sent"
            payout.status = "sent"
            sent += 1
        else:
            event.status = "pending_sent"
            payout.status = "pending_sent"
            failed += 1

    session.commit()
    return SenderStats(
        attempted=attempted,
        sent=sent,
        failed=failed,
        created_events=created_events,
    )

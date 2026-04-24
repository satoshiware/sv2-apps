from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from app.models import PayoutEvent, Settlement, User, UserPayout
from app.sender import process_payout_events


@pytest.fixture
def session(tmp_path: Path):
    db_file = tmp_path / "sender_test.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)
    with Session() as s:
        yield s


def _seed_payout(session) -> UserPayout:
    user = User(username="alice")
    session.add(user)
    session.flush()

    settlement = Settlement(
        status="completed",
        period_start=datetime(2026, 1, 1, 0, 0, 0),
        period_end=datetime(2026, 1, 1, 0, 10, 0),
        pool_reward_btc=Decimal("0.01000000"),
    )
    session.add(settlement)
    session.flush()

    payout = UserPayout(
        settlement_id=settlement.id,
        user_id=user.id,
        amount_btc=Decimal("0.00600000"),
        idempotency_key=f"settlement-{settlement.id}-user-{user.id}",
        status="pending",
    )
    session.add(payout)
    session.commit()
    return payout


def test_process_payout_events_send_once_idempotent(session) -> None:
    _seed_payout(session)
    calls = {"n": 0}

    def _transport(payload, dry_run):
        _ = (payload, dry_run)
        calls["n"] += 1
        return True

    first = process_payout_events(session, dry_run=True, transport=_transport)
    second = process_payout_events(session, dry_run=True, transport=_transport)

    assert first.attempted == 1
    assert first.sent == 1
    assert first.created_events == 1

    assert second.attempted == 0
    assert second.sent == 0
    assert second.created_events == 0

    assert calls["n"] == 1
    assert session.query(PayoutEvent).count() == 1
    assert session.query(UserPayout).filter(UserPayout.status == "sent").count() == 1


def test_process_payout_events_failure_then_retry_success(session) -> None:
    _seed_payout(session)
    calls = {"n": 0}

    def _transport(payload, dry_run):
        _ = (payload, dry_run)
        calls["n"] += 1
        return calls["n"] >= 2

    first = process_payout_events(session, dry_run=False, transport=_transport)
    payout = session.query(UserPayout).one()
    event = session.query(PayoutEvent).one()

    assert first.attempted == 1
    assert first.failed == 1
    assert payout.status == "pending_sent"
    assert event.status == "pending_sent"

    second = process_payout_events(session, dry_run=False, transport=_transport)
    payout = session.query(UserPayout).one()
    event = session.query(PayoutEvent).one()

    assert second.attempted == 1
    assert second.sent == 1
    assert second.created_events == 0
    assert payout.status == "sent"
    assert event.status == "sent"
    assert session.query(PayoutEvent).count() == 1

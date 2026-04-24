from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from app.models import CarryState, MetricSnapshot, Settlement, User, UserPayout
from app.pool_client import PoolApiTimeout
from app.settlement import run_settlement


@pytest.fixture
def session(tmp_path: Path):
    db_file = tmp_path / "settlement_test.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)
    with Session() as s:
        yield s


def _add_snapshot(
    session,
    identity: str,
    total: int,
    created_at: datetime,
    *,
    channel_id: int | None = None,
    work_total: Decimal | float | int = Decimal("0"),
    shares_rejected_total: int = 0,
) -> None:
    session.add(
        MetricSnapshot(
            channel_id=channel_id,
            identity=identity,
            accepted_shares_total=total,
            accepted_work_total=work_total,
            shares_rejected_total=shares_rejected_total,
            created_at=created_at,
        )
    )


def test_run_settlement_splits_rewards_by_share_ratio(session) -> None:
    now = datetime(2026, 1, 1, 0, 30, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 10, start - timedelta(minutes=1))
    _add_snapshot(session, "alice.m1", 16, start + timedelta(minutes=2))

    _add_snapshot(session, "bob.m1", 20, start - timedelta(minutes=1))
    _add_snapshot(session, "bob.m1", 24, start + timedelta(minutes=3))
    session.commit()

    def _reward_fetcher(period_start, period_end):
        _ = (period_start, period_end)
        return 0.01000000

    result = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_fetcher,
    )

    payouts = session.query(UserPayout).order_by(UserPayout.user_id.asc()).all()
    users = {u.id: u.username for u in session.query(User).all()}

    by_user = {users[p.user_id]: Decimal(str(p.amount_btc)) for p in payouts}

    assert result.status == "completed"
    assert result.total_shares == 10
    assert by_user == {
        "alice": Decimal("0.00600000"),
        "bob": Decimal("0.00400000"),
    }

    carry = session.query(CarryState).filter(CarryState.bucket == "default").one()
    assert Decimal(str(carry.carry_btc)) == Decimal("0")


def test_run_settlement_carries_rounding_remainder(session) -> None:
    now = datetime(2026, 1, 1, 1, 0, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 1, start - timedelta(minutes=1))
    _add_snapshot(session, "alice.m1", 2, start + timedelta(minutes=1))
    _add_snapshot(session, "bob.m1", 1, start - timedelta(minutes=1))
    _add_snapshot(session, "bob.m1", 2, start + timedelta(minutes=1))
    _add_snapshot(session, "carol.m1", 1, start - timedelta(minutes=1))
    _add_snapshot(session, "carol.m1", 2, start + timedelta(minutes=1))
    session.commit()

    def _reward_fetcher(period_start, period_end):
        _ = (period_start, period_end)
        return 0.00000002

    result = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_fetcher,
    )

    payouts = session.query(UserPayout).all()
    allocated = sum(Decimal(str(p.amount_btc)) for p in payouts)
    carry = session.query(CarryState).filter(CarryState.bucket == "default").one()
    carry_value = Decimal(str(carry.carry_btc))

    assert result.status == "completed"
    assert result.total_shares == 3
    assert allocated == Decimal("0.00000002")
    assert allocated + carry_value == Decimal("0.00000002")
    assert carry_value == Decimal("0")


def test_run_settlement_marks_blocked_on_pool_timeout(session) -> None:
    now = datetime(2026, 1, 1, 2, 0, 0)

    def _timeout_fetcher(period_start, period_end):
        _ = (period_start, period_end)
        raise PoolApiTimeout("timeout")

    result = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_timeout_fetcher,
    )

    settlement = session.query(Settlement).one()

    assert result.status == "blocked"
    assert settlement.status == "blocked"
    assert session.query(UserPayout).count() == 0


def test_run_settlement_is_idempotent_for_same_window(session) -> None:
    now = datetime(2026, 1, 1, 3, 0, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 10, start - timedelta(minutes=1))
    _add_snapshot(session, "alice.m1", 15, start + timedelta(minutes=2))
    _add_snapshot(session, "bob.m1", 20, start - timedelta(minutes=1))
    _add_snapshot(session, "bob.m1", 25, start + timedelta(minutes=3))
    session.commit()

    calls = {"n": 0}

    def _reward_fetcher(period_start, period_end):
        _ = (period_start, period_end)
        calls["n"] += 1
        return 0.01000000

    first = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_fetcher,
    )
    second = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_fetcher,
    )

    assert first.settlement_id == second.settlement_id
    assert calls["n"] == 1
    assert session.query(Settlement).count() == 1
    assert session.query(UserPayout).count() == 2


def test_run_settlement_prefers_work_deltas_and_fully_allocates_reward(session) -> None:
    now = datetime(2026, 1, 1, 4, 0, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 10, start - timedelta(minutes=1), channel_id=1, work_total=100)
    _add_snapshot(session, "alice.m1", 16, start + timedelta(minutes=2), channel_id=1, work_total=130)

    _add_snapshot(session, "alice.m2", 20, start - timedelta(minutes=1), channel_id=2, work_total=200)
    _add_snapshot(session, "alice.m2", 24, start + timedelta(minutes=3), channel_id=2, work_total=220)

    _add_snapshot(session, "bob.m1", 5, start - timedelta(minutes=1), channel_id=3, work_total=100)
    _add_snapshot(session, "bob.m1", 7, start + timedelta(minutes=4), channel_id=3, work_total=150)
    session.commit()

    def _reward_fetcher(period_start, period_end):
        _ = (period_start, period_end)
        return 0.01000000

    result = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_fetcher,
    )

    payouts = session.query(UserPayout).order_by(UserPayout.user_id.asc()).all()
    users = {u.id: u.username for u in session.query(User).all()}
    by_user = {users[p.user_id]: Decimal(str(p.amount_btc)) for p in payouts}
    allocated = sum(by_user.values(), Decimal("0"))

    assert result.status == "completed"
    assert result.total_shares == 12
    assert result.total_work == Decimal("100")
    assert by_user == {
        "alice": Decimal("0.00500000"),
        "bob": Decimal("0.00500000"),
    }
    assert allocated == Decimal("0.01000000")

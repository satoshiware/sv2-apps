from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from app.models import CarryState, MetricSnapshot, Settlement, User, UserPayout, WorkAccrualBucket
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


@pytest.mark.smoke
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


def test_run_settlement_uses_contiguous_non_overlapping_periods(session) -> None:
    first_now = datetime(2026, 1, 1, 5, 0, 0)
    second_now = first_now + timedelta(minutes=5)

    _add_snapshot(session, "alice.m1", 10, first_now - timedelta(minutes=6), work_total=100)
    _add_snapshot(session, "alice.m1", 20, first_now - timedelta(minutes=1), work_total=130)
    _add_snapshot(session, "alice.m1", 24, second_now - timedelta(minutes=1), work_total=150)
    session.commit()

    def _reward_fetcher(period_start, period_end):
        _ = (period_start, period_end)
        return 0.01000000

    first = run_settlement(
        session,
        first_now,
        interval_minutes=5,
        payout_decimals=8,
        reward_fetcher=_reward_fetcher,
    )
    second = run_settlement(
        session,
        second_now,
        interval_minutes=5,
        payout_decimals=8,
        reward_fetcher=_reward_fetcher,
    )

    assert first.period_start == datetime(2026, 1, 1, 4, 55, 0)
    assert first.period_end == first_now
    assert second.period_start == first.period_end
    assert second.period_end == second_now


# ---------------------------------------------------------------------------
# Golden tests: Phase 3 deferred accrual behavior
# ---------------------------------------------------------------------------


def _reward_btc(btc_str: str):
    """Return a reward fetcher that returns a fixed BTC amount."""
    btc = Decimal(btc_str)

    def fetcher(period_start, period_end):
        _ = (period_start, period_end)
        return btc

    return fetcher


# Golden test A: one matured reward, payout completes, normal status
@pytest.mark.smoke
def test_golden_a_rewarded_interval_completes_normally(session) -> None:
    now = datetime(2026, 1, 1, 0, 10, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 0, start - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 10, start + timedelta(minutes=2), work_total=100)
    _add_snapshot(session, "bob.m1", 0, start - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "bob.m1", 5, start + timedelta(minutes=2), work_total=50)
    session.commit()

    result = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("1.50000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )

    assert result.status == "completed"
    assert result.user_count == 2
    assert result.pool_reward_btc == Decimal("1.50000000")

    payouts = session.query(UserPayout).all()
    assert len(payouts) == 2
    total_paid = sum(p.amount_btc for p in payouts)
    assert total_paid == Decimal("1.50000000")

    # Accrual should be cleared for both users
    alice = session.query(User).filter_by(username="alice").one()
    bob = session.query(User).filter_by(username="bob").one()
    alice_bucket = session.query(WorkAccrualBucket).filter_by(user_id=alice.id).one_or_none()
    bob_bucket = session.query(WorkAccrualBucket).filter_by(user_id=bob.id).one_or_none()
    assert alice_bucket is None or Decimal(str(alice_bucket.accumulated_work)) == Decimal("0")
    assert bob_bucket is None or Decimal(str(bob_bucket.accumulated_work)) == Decimal("0")


# Golden test B: no matured blocks, reward=0, settlement deferred, accrual updated
@pytest.mark.smoke
def test_golden_b_zero_reward_defers_and_accrues(session) -> None:
    now = datetime(2026, 1, 1, 0, 10, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 0, start - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 10, start + timedelta(minutes=2), work_total=200)
    session.commit()

    result = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )

    assert result.status == "deferred"
    assert result.user_count == 0
    assert result.pool_reward_btc == Decimal("0")

    payouts = session.query(UserPayout).all()
    assert len(payouts) == 0

    # Work should be accrued
    alice = session.query(User).filter_by(username="alice").one()
    bucket = session.query(WorkAccrualBucket).filter_by(user_id=alice.id).one()
    assert Decimal(str(bucket.accumulated_work)) == Decimal("200.00000000")


# Golden test C: first interval deferred (accrued), next interval rewarded uses carry-forward
@pytest.mark.smoke
def test_golden_c_carry_forward_consumed_on_rewarded_interval(session) -> None:
    now1 = datetime(2026, 1, 1, 0, 10, 0)
    now2 = now1 + timedelta(minutes=10)

    # Interval 1 snapshots: alice work=200, bob work=100
    start1 = now1 - timedelta(minutes=10)
    _add_snapshot(session, "alice.m1", 0, start1 - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 10, start1 + timedelta(minutes=2), work_total=200)
    _add_snapshot(session, "bob.m1", 0, start1 - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "bob.m1", 5, start1 + timedelta(minutes=2), work_total=100)

    # Interval 2 snapshots: alice new work=50, bob new work=50
    _add_snapshot(session, "alice.m1", 15, now2 - timedelta(minutes=1), work_total=250)
    _add_snapshot(session, "bob.m1", 10, now2 - timedelta(minutes=1), work_total=150)
    session.commit()

    # Cycle 1: deferred (zero reward)
    r1 = run_settlement(
        session,
        now1,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )
    assert r1.status == "deferred"

    # Verify accrual after deferral: alice=200, bob=100
    alice = session.query(User).filter_by(username="alice").one()
    bob = session.query(User).filter_by(username="bob").one()
    alice_bucket = session.query(WorkAccrualBucket).filter_by(user_id=alice.id).one()
    bob_bucket = session.query(WorkAccrualBucket).filter_by(user_id=bob.id).one()
    assert Decimal(str(alice_bucket.accumulated_work)) == Decimal("200.00000000")
    assert Decimal(str(bob_bucket.accumulated_work)) == Decimal("100.00000000")

    # Cycle 2: rewarded — effective_work alice=200+50=250, bob=100+50=150
    r2 = run_settlement(
        session,
        now2,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("4.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )
    assert r2.status == "completed"
    assert r2.user_count == 2

    payouts = (
        session.query(UserPayout)
        .filter(UserPayout.settlement_id == r2.settlement_id)
        .all()
    )
    payout_by_user = {
        session.query(User).filter_by(id=p.user_id).one().username: p.amount_btc
        for p in payouts
    }
    # alice: 250/400 * 4 = 2.5, bob: 150/400 * 4 = 1.5
    assert payout_by_user["alice"] == Decimal("2.50000000")
    assert payout_by_user["bob"] == Decimal("1.50000000")

    # Accrual cleared after rewarded settlement
    session.refresh(alice_bucket)
    session.refresh(bob_bucket)
    assert Decimal(str(alice_bucket.accumulated_work)) == Decimal("0")
    assert Decimal(str(bob_bucket.accumulated_work)) == Decimal("0")


# Golden test D: zero reward without defer_on_zero_reward falls through to normal completed path
def test_golden_d_zero_reward_without_defer_flag_completes(session) -> None:
    now = datetime(2026, 1, 1, 0, 10, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 0, start - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 10, start + timedelta(minutes=2), work_total=100)
    session.commit()

    result = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=False,  # not deferring
        use_work_accrual=False,
    )

    # No payout rows (nothing to distribute), but status is completed not deferred
    assert result.status == "completed"
    assert result.user_count == 0
    assert session.query(UserPayout).count() == 0
    # No accrual should be created
    assert session.query(WorkAccrualBucket).count() == 0


# Golden test E: repeated deferral accrues work additively across two cycles
def test_golden_e_two_consecutive_deferred_cycles_accumulate_work(session) -> None:
    now1 = datetime(2026, 1, 1, 0, 10, 0)
    now2 = now1 + timedelta(minutes=10)

    start1 = now1 - timedelta(minutes=10)
    # Cycle 1: alice gains work=100
    _add_snapshot(session, "alice.m1", 0, start1 - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 5, start1 + timedelta(minutes=2), work_total=100)
    # Cycle 2: alice gains another work=80
    _add_snapshot(session, "alice.m1", 8, now2 - timedelta(minutes=1), work_total=180)
    session.commit()

    run_settlement(
        session,
        now1,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )
    run_settlement(
        session,
        now2,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )

    alice = session.query(User).filter_by(username="alice").one()
    bucket = session.query(WorkAccrualBucket).filter_by(user_id=alice.id).one()
    # 100 + 80 = 180 total accrued
    assert Decimal(str(bucket.accumulated_work)) == Decimal("180.00000000")


def test_golden_f_accrual_only_user_participates_without_new_interval_work(session) -> None:
    now1 = datetime(2026, 1, 1, 0, 10, 0)
    now2 = now1 + timedelta(minutes=10)

    start1 = now1 - timedelta(minutes=10)
    _add_snapshot(session, "alice.m1", 0, start1 - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 10, start1 + timedelta(minutes=2), work_total=100)
    _add_snapshot(session, "bob.m1", 0, start1 - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "bob.m1", 10, start1 + timedelta(minutes=2), work_total=100)

    # Only bob has new work in interval 2. Alice should still participate via accrued work.
    _add_snapshot(session, "alice.m1", 10, now2 - timedelta(minutes=1), work_total=100)
    _add_snapshot(session, "bob.m1", 20, now2 - timedelta(minutes=1), work_total=200)
    session.commit()

    first = run_settlement(
        session,
        now1,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )
    assert first.status == "deferred"

    second = run_settlement(
        session,
        now2,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("2.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )
    assert second.status == "completed"

    payouts = (
        session.query(UserPayout)
        .filter(UserPayout.settlement_id == second.settlement_id)
        .all()
    )
    payout_by_user = {
        session.query(User).filter_by(id=p.user_id).one().username: p.amount_btc
        for p in payouts
    }

    # Alice effective work = 100 accrued + 0 current. Bob effective work = 100 accrued + 100 current.
    assert payout_by_user["alice"] == Decimal("0.66666666")
    assert payout_by_user["bob"] == Decimal("1.33333334")


def test_deferred_settlement_idempotent_does_not_double_accrue(session) -> None:
    now = datetime(2026, 1, 1, 0, 10, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 0, start - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 10, start + timedelta(minutes=2), work_total=100)
    session.commit()

    first = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )
    second = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )

    assert first.settlement_id == second.settlement_id
    assert first.status == "deferred"
    assert second.status == "deferred"

    alice = session.query(User).filter_by(username="alice").one()
    bucket = session.query(WorkAccrualBucket).filter_by(user_id=alice.id).one()
    assert Decimal(str(bucket.accumulated_work)) == Decimal("100.00000000")


def test_phase_d_payout_fraction_sum_and_reward_reconciliation(session) -> None:
    now = datetime(2026, 1, 1, 1, 10, 0)
    start = now - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 0, start - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 10, start + timedelta(minutes=2), work_total=30)
    _add_snapshot(session, "bob.m1", 0, start - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "bob.m1", 10, start + timedelta(minutes=2), work_total=70)
    session.commit()

    result = run_settlement(
        session,
        now,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("1.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )

    payouts = (
        session.query(UserPayout)
        .filter(UserPayout.settlement_id == result.settlement_id)
        .order_by(UserPayout.id.asc())
        .all()
    )
    assert len(payouts) == 2

    sum_fraction = sum((Decimal(str(p.payout_fraction)) for p in payouts), Decimal("0"))
    sum_amount = sum((Decimal(str(p.amount_btc)) for p in payouts), Decimal("0"))
    carry = session.query(CarryState).filter_by(bucket="default").one()
    carry_value = Decimal(str(carry.carry_btc))

    assert result.status == "completed"
    assert sum_fraction == Decimal("1.000000000000")
    assert sum_amount + carry_value == Decimal("1.00000000")


def test_phase_d_accrual_consumption_with_existing_carry_reconciles(session) -> None:
    now1 = datetime(2026, 1, 1, 2, 10, 0)
    now2 = now1 + timedelta(minutes=10)
    start1 = now1 - timedelta(minutes=10)

    _add_snapshot(session, "alice.m1", 0, start1 - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "alice.m1", 10, start1 + timedelta(minutes=2), work_total=120)
    _add_snapshot(session, "bob.m1", 0, start1 - timedelta(minutes=1), work_total=0)
    _add_snapshot(session, "bob.m1", 10, start1 + timedelta(minutes=2), work_total=80)

    _add_snapshot(session, "alice.m1", 15, now2 - timedelta(minutes=1), work_total=150)
    _add_snapshot(session, "bob.m1", 15, now2 - timedelta(minutes=1), work_total=110)
    session.commit()

    deferred = run_settlement(
        session,
        now1,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("0.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )
    assert deferred.status == "deferred"

    carry = session.query(CarryState).filter_by(bucket="default").one()
    carry.carry_btc = Decimal("0.12345678")
    session.commit()

    rewarded = run_settlement(
        session,
        now2,
        interval_minutes=10,
        payout_decimals=8,
        reward_fetcher=_reward_btc("2.00000000"),
        defer_on_zero_reward=True,
        use_work_accrual=True,
    )
    assert rewarded.status == "completed"

    payouts = (
        session.query(UserPayout)
        .filter(UserPayout.settlement_id == rewarded.settlement_id)
        .all()
    )
    paid_total = sum((Decimal(str(p.amount_btc)) for p in payouts), Decimal("0"))
    carry_after = Decimal(str(session.query(CarryState).filter_by(bucket="default").one().carry_btc))

    # distributable = reward + previous carry
    assert paid_total + carry_after == Decimal("2.12345678")

    alice = session.query(User).filter_by(username="alice").one()
    bob = session.query(User).filter_by(username="bob").one()
    alice_bucket = session.query(WorkAccrualBucket).filter_by(user_id=alice.id).one()
    bob_bucket = session.query(WorkAccrualBucket).filter_by(user_id=bob.id).one()
    assert Decimal(str(alice_bucket.accumulated_work)) == Decimal("0")
    assert Decimal(str(bob_bucket.accumulated_work)) == Decimal("0")

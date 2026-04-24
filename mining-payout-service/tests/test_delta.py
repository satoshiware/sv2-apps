from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from decimal import Decimal

from app.delta import (
    aggregate_user_share_deltas,
    compute_counter_delta,
    compute_identity_share_deltas,
    compute_user_share_deltas,
    compute_user_work_deltas,
)
from app.models import MetricSnapshot


@pytest.fixture
def session(tmp_path: Path):
    db_file = tmp_path / "delta_test.db"
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


def test_compute_counter_delta_handles_resets() -> None:
    delta, resets = compute_counter_delta([100, 107, 109, 3, 7])
    assert delta == 13
    assert resets == 1


def test_compute_identity_share_deltas_with_window_baseline_and_reset(session) -> None:
    base = datetime(2026, 1, 1, 0, 0, 0)
    start = base + timedelta(minutes=10)
    end = base + timedelta(minutes=30)

    _add_snapshot(session, "alice.m1", 10, base + timedelta(minutes=1))
    _add_snapshot(session, "alice.m1", 13, base + timedelta(minutes=12))
    _add_snapshot(session, "alice.m1", 16, base + timedelta(minutes=20))

    _add_snapshot(session, "bob.m1", 8, base + timedelta(minutes=2))
    _add_snapshot(session, "bob.m1", 2, base + timedelta(minutes=14))
    _add_snapshot(session, "bob.m1", 5, base + timedelta(minutes=25))

    _add_snapshot(session, "carol.m1", 7, base + timedelta(minutes=11))
    session.commit()

    deltas = compute_identity_share_deltas(session, start, end)

    assert deltas["alice.m1"].share_delta == 6
    assert deltas["alice.m1"].reset_count == 0

    assert deltas["bob.m1"].share_delta == 3
    assert deltas["bob.m1"].reset_count == 1

    assert "carol.m1" not in deltas


def test_compute_user_share_deltas_aggregates_multiple_workers_and_skips_invalid(session) -> None:
    base = datetime(2026, 1, 1, 0, 0, 0)
    start = base + timedelta(minutes=10)
    end = base + timedelta(minutes=20)

    _add_snapshot(session, "baveet.miner1", 10, base + timedelta(minutes=1))
    _add_snapshot(session, "baveet.miner1", 14, base + timedelta(minutes=12))

    _add_snapshot(session, "baveet.miner2", 20, base + timedelta(minutes=1))
    _add_snapshot(session, "baveet.miner2", 22, base + timedelta(minutes=13))
    _add_snapshot(session, "baveet.miner2", 26, base + timedelta(minutes=18))

    _add_snapshot(session, "badidentity", 5, base + timedelta(minutes=1))
    _add_snapshot(session, "badidentity", 9, base + timedelta(minutes=15))
    session.commit()

    user_totals = compute_user_share_deltas(session, start, end)
    assert user_totals == {"baveet": 10}


def test_aggregate_user_share_deltas_helper() -> None:
    from app.delta import IdentityDelta

    identity_deltas = {
        "u1.m1": IdentityDelta("u1.m1", share_delta=2, reset_count=0, sample_count=3),
        "u1.m2": IdentityDelta("u1.m2", share_delta=5, reset_count=0, sample_count=2),
        "broken": IdentityDelta("broken", share_delta=9, reset_count=0, sample_count=2),
    }
    assert aggregate_user_share_deltas(identity_deltas) == {"u1": 7}


def test_compute_user_work_deltas_aggregates_multiple_channels(session) -> None:
    base = datetime(2026, 1, 1, 0, 0, 0)
    start = base + timedelta(minutes=10)
    end = base + timedelta(minutes=20)

    _add_snapshot(session, "john.rig1", 10, base + timedelta(minutes=1), channel_id=1, work_total=100)
    _add_snapshot(session, "john.rig1", 14, base + timedelta(minutes=12), channel_id=1, work_total=130)

    _add_snapshot(session, "john.rig2", 20, base + timedelta(minutes=2), channel_id=2, work_total=200)
    _add_snapshot(session, "john.rig2", 23, base + timedelta(minutes=15), channel_id=2, work_total=260)

    _add_snapshot(session, "john.rig2", 23, base + timedelta(minutes=18), channel_id=5, work_total=40)
    _add_snapshot(session, "john.rig2", 24, base + timedelta(minutes=19), channel_id=5, work_total=55)
    session.commit()

    user_work = compute_user_work_deltas(session, start, end)
    assert user_work == {"john": Decimal("105")}

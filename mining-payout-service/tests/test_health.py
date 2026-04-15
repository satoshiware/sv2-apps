from datetime import datetime

from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.main import app
from app.models import PayoutEvent, Settlement, User, UserPayout


def test_health() -> None:
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_service_metrics_empty(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "service_metrics_empty.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)

    monkeypatch.setenv("DB_PATH", str(db_file))
    client = TestClient(app)
    response = client.get("/service-metrics")

    assert response.status_code == 200
    assert response.json() == {
        "settlements_total": 0,
        "payouts_sent_total": 0,
        "payout_failures_total": 0,
        "last_settlement_timestamp": None,
    }


def test_service_metrics_counts_and_timestamp(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "service_metrics_counts.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    with Session() as session:
        user = User(username="alice")
        session.add(user)
        session.flush()

        settlement = Settlement(
            status="completed",
            period_start=datetime(2026, 1, 1, 0, 0, 0),
            period_end=datetime(2026, 1, 1, 0, 10, 0),
            pool_reward_btc=0.01000000,
        )
        session.add(settlement)
        session.flush()

        payout_sent = UserPayout(
            settlement_id=settlement.id,
            user_id=user.id,
            amount_btc=0.00600000,
            idempotency_key=f"settlement-{settlement.id}-user-{user.id}",
            status="sent",
        )
        session.add(payout_sent)
        session.flush()

        payout_pending = UserPayout(
            settlement_id=settlement.id,
            user_id=user.id,
            amount_btc=0.00400000,
            idempotency_key=f"settlement-{settlement.id}-user-{user.id}-2",
            status="pending_sent",
        )
        session.add(payout_pending)
        session.flush()

        event = PayoutEvent(
            payout_id=payout_pending.id,
            payload_json='{"test":true}',
            status="pending_sent",
        )
        session.add(event)
        session.commit()

    monkeypatch.setenv("DB_PATH", str(db_file))
    client = TestClient(app)
    response = client.get("/service-metrics")

    assert response.status_code == 200
    assert response.json() == {
        "settlements_total": 1,
        "payouts_sent_total": 1,
        "payout_failures_total": 1,
        "last_settlement_timestamp": "2026-01-01T00:10:00",
    }

from datetime import datetime
from decimal import Decimal
import json

from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.main import app
from app.models import PayoutEvent, Settlement, User, UserPayout
from app.sender import SenderStats
from app.settlement import SettlementResult


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
            total_shares=10,
            total_work=100,
            pool_reward_btc=0.01000000,
        )
        session.add(settlement)
        session.flush()

        payout_sent = UserPayout(
            settlement_id=settlement.id,
            user_id=user.id,
            contribution_value=60,
            payout_fraction=0.6,
            amount_btc=0.00600000,
            idempotency_key=f"settlement-{settlement.id}-user-{user.id}",
            status="sent",
        )
        session.add(payout_sent)
        session.flush()

        payout_pending = UserPayout(
            settlement_id=settlement.id,
            user_id=user.id,
            contribution_value=40,
            payout_fraction=0.4,
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


def test_latest_settlement_returns_user_payout_table(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "latest_settlement.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    with Session() as session:
        alice = User(username="alice")
        bob = User(username="bob")
        session.add_all([alice, bob])
        session.flush()

        settlement = Settlement(
            status="completed",
            period_start=datetime(2026, 1, 1, 1, 0, 0),
            period_end=datetime(2026, 1, 1, 1, 10, 0),
            total_shares=12,
            total_work=100,
            pool_reward_btc=0.01000000,
        )
        session.add(settlement)
        session.flush()

        session.add_all([
            UserPayout(
                settlement_id=settlement.id,
                user_id=alice.id,
                contribution_value=50,
                payout_fraction=0.5,
                amount_btc=0.00500000,
                idempotency_key=f"settlement-{settlement.id}-user-{alice.id}",
                status="pending",
            ),
            UserPayout(
                settlement_id=settlement.id,
                user_id=bob.id,
                contribution_value=50,
                payout_fraction=0.5,
                amount_btc=0.00500000,
                idempotency_key=f"settlement-{settlement.id}-user-{bob.id}",
                status="pending",
            ),
        ])
        session.commit()

    monkeypatch.setenv("DB_PATH", str(db_file))
    client = TestClient(app)
    response = client.get("/settlements/latest")

    assert response.status_code == 200
    assert response.json() == {
        "settlement": {
            "settlement_id": 1,
            "status": "completed",
            "period_start": "2026-01-01T01:00:00",
            "period_end": "2026-01-01T01:10:00",
            "pool_reward_btc": "0.01000000",
            "total_shares": 12,
            "total_work": "100.00000000",
        },
        "users": [
            {
                "username": "alice",
                "contribution_value": "50.00000000",
                "payout_fraction": "0.500000000000",
                "amount_btc": "0.00500000",
                "status": "pending",
            },
            {
                "username": "bob",
                "contribution_value": "50.00000000",
                "payout_fraction": "0.500000000000",
                "amount_btc": "0.00500000",
                "status": "pending",
            },
        ],
    }


def test_run_settlement_cycle_uses_channel_endpoint_when_configured(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "run_cycle.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("TRANSLATOR_CHANNELS_URL", "http://127.0.0.1:8080/v1/translator/upstream/channels")
    monkeypatch.setenv("REWARD_MODE", "blocks")
    monkeypatch.setenv("BLOCK_REWARD_BTC", "1.87500000")
    monkeypatch.setenv("DRY_RUN", "true")

    monkeypatch.setattr(
        "app.main.poll_channels_once_with_blocks",
        lambda session, api_url, downstream_url=None, bearer_token=None: (3, {2: 1, 3: 2}),
    )
    monkeypatch.setattr(
        "app.main.run_settlement",
        lambda session, now, interval_minutes, payout_decimals, reward_fetcher=None: SettlementResult(
            settlement_id=9,
            status="completed",
            user_count=2,
            total_shares=12,
            total_work=Decimal("100"),
            pool_reward_btc=Decimal("5.62500000"),
            carry_btc=Decimal("0"),
        ),
    )
    monkeypatch.setattr(
        "app.main.process_payout_events",
        lambda session, dry_run: SenderStats(attempted=2, sent=2, failed=0, created_events=2),
    )

    client = TestClient(app)
    response = client.post("/settlements/run")

    assert response.status_code == 200
    assert response.json() == {
        "snapshots_created": 3,
        "settlement": {
            "settlement_id": 9,
            "status": "completed",
            "user_count": 2,
            "total_shares": 12,
            "total_work": "100.00000000",
            "pool_reward_btc": "5.62500000",
            "carry_btc": "0.00000000",
        },
        "sender": {
            "attempted": 2,
            "sent": 2,
            "failed": 0,
            "created_events": 2,
        },
        "block_reward": {
            "reward_mode": "blocks",
            "block_reward_btc": "1.87500000",
            "interval_blocks": 3,
            "computed_reward_btc": "5.62500000",
            "channels": [
                {
                    "channel_id": 2,
                    "previous_blocks_found": 0,
                    "current_blocks_found": 1,
                    "delta_blocks": 1,
                    "reset_detected": False,
                },
                {
                    "channel_id": 3,
                    "previous_blocks_found": 0,
                    "current_blocks_found": 2,
                    "delta_blocks": 2,
                    "reset_detected": False,
                },
            ],
        },
    }


def test_audit_logs_endpoint_returns_recent_entries(monkeypatch, tmp_path) -> None:
    log_file = tmp_path / "payout_audit.jsonl"
    entries = [
        {"event_type": "scheduler_started", "timestamp": "2026-01-01T00:00:00", "payload": {}},
        {"attempt_id": "abc", "settlement": {"status": "completed"}},
    ]
    with log_file.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry))
            handle.write("\n")

    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_INTERVAL_SECONDS", "5")

    client = TestClient(app)
    response = client.get("/audit/logs?limit=2")

    assert response.status_code == 200
    payload = response.json()
    assert payload["exists"] is True
    assert payload["entry_count"] == 2
    assert payload["scheduler_enabled"] is True
    assert payload["scheduler_interval_seconds"] == 5
    assert payload["entries"][0]["event_type"] == "scheduler_started"
    assert payload["entries"][1]["attempt_id"] == "abc"

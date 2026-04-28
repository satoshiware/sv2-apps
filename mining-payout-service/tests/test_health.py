from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

from fastapi.testclient import TestClient

from app.db import Base, make_engine, make_session_factory
from app.hooks import (
    reset_hooks_to_noop,
    set_block_event_replay_hook,
    set_reward_refetch_hook,
    set_settlement_replay_hook,
    set_startup_reconciliation_hook,
)
from app.main import app
from app.models import MetricSnapshot, PayoutEvent, Settlement, SnapshotBlock, User, UserPayout, WorkAccrualBucket
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
    log_file = tmp_path / "run_cycle_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
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
            period_start=datetime(2026, 1, 1, 0, 0, 0),
            period_end=datetime(2026, 1, 1, 0, 5, 0),
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
        "settlement_skipped": False,
        "settlement": {
            "settlement_id": 9,
            "status": "completed",
            "period_start": "2026-01-01T00:00:00",
            "period_end": "2026-01-01T00:05:00",
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


def test_audit_settlements_endpoint_returns_normalized_rows(monkeypatch, tmp_path) -> None:
    log_file = tmp_path / "payout_audit.jsonl"
    entries = [
        {"event_type": "scheduler_started", "timestamp": "2026-01-01T00:00:00", "payload": {}},
        {
            "attempt_id": "attempt-1",
            "attempted_at": "2026-01-01T00:01:00",
            "period_start": "2026-01-01T00:00:00",
            "period_end": "2026-01-01T00:01:00",
            "settlement": {
                "settlement_id": 31,
                "status": "completed",
                "reward_mode": "blocks",
                "pool_reward_btc": "1.87500000",
                "total_work": "100.00000000",
                "total_shares": 10,
            },
            "payout_rows": [
                {"username": "alice", "amount_btc": "0.75000000", "status": "sent"},
                {"username": "bob", "amount_btc": "1.12500000", "status": "sent"},
            ],
            "checks": {"unrewarded_user_count": 0, "unrewarded_users": []},
            "block_reward": {"interval_blocks": 1, "computed_reward_btc": "1.87500000", "channels": []},
            "snapshot_alignment": {
                "miners": [
                    {
                        "identity": "alice.rig1",
                        "channel_id": 2,
                        "baseline_work": "40.00000000",
                        "current_work": "70.00000000",
                        "work_delta": "30.00000000",
                        "reset_detected": False,
                    },
                    {
                        "identity": "bob.rig1",
                        "channel_id": 3,
                        "baseline_work": "20.00000000",
                        "current_work": "90.00000000",
                        "work_delta": "70.00000000",
                        "reset_detected": False,
                    },
                ]
            },
        },
    ]
    with log_file.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry))
            handle.write("\n")

    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    monkeypatch.setenv("SCHEDULER_INTERVAL_SECONDS", "60")

    client = TestClient(app)
    response = client.get("/audit/settlements?limit=20")
    assert response.status_code == 200
    payload = response.json()
    assert payload["scheduler_enabled"] is True
    assert len(payload["settlements"]) == 1

    settlement_row = payload["settlements"][0]
    assert settlement_row["settlement_id"] == 31
    assert settlement_row["interval_blocks"] == 1
    assert settlement_row["payout_total_btc"] == "1.87500000"
    assert settlement_row["payout_count"] == 2
    assert settlement_row["work_delta_explanation"]["source_metric"] == "accepted_work_total"
    assert settlement_row["work_delta_explanation"]["per_user"] == [
        {
            "username": "alice",
            "identity_count": 1,
            "baseline_work_sum": "40.00000000",
            "current_work_sum": "70.00000000",
            "work_delta_sum": "30.00000000",
            "formula": "70.00000000 - 40.00000000 = 30.00000000",
        },
        {
            "username": "bob",
            "identity_count": 1,
            "baseline_work_sum": "20.00000000",
            "current_work_sum": "90.00000000",
            "work_delta_sum": "70.00000000",
            "formula": "90.00000000 - 20.00000000 = 70.00000000",
        },
    ]


def test_audit_dashboard_returns_html() -> None:
    client = TestClient(app)
    response = client.get("/audit/dashboard")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Payout Settlements Dashboard" in response.text
    assert "How Work Delta Is Calculated" in response.text


def test_run_settlement_cycle_uses_matured_block_event_rewards(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "run_cycle_block_events.db"
    log_file = tmp_path / "run_cycle_block_events_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
    monkeypatch.setenv("TRANSLATOR_CHANNELS_URL", "http://127.0.0.1:8080/v1/translator/upstream/channels")
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "true")
    monkeypatch.setenv("BLOCK_REWARD_BATCH_URL", "http://127.0.0.1:8080/v1/az/blocks/rewards")
    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "http://127.0.0.1:8080/v1/translator/blocks-found")
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")

    monkeypatch.setattr(
        "app.main.poll_channels_once_with_blocks",
        lambda session, api_url, downstream_url=None, bearer_token=None: (1, {2: 1}),
    )
    monkeypatch.setattr(
        "app.main.fetch_block_rewards_by_hashes",
        lambda hashes: {"matured-hash-1": 100000000} if "matured-hash-1" in hashes else {},
    )
    monkeypatch.setattr(
        "app.main.fetch_blocks_found_in_window",
        lambda start, end: [
            {
                "found_at": (start + timedelta(minutes=1)).isoformat(),
                "channel_id": 2,
                "worker_name": "Ben.Cust1",
                "blockhash": "matured-hash-1",
            }
        ],
    )

    def _fake_run_settlement(session, now, interval_minutes, payout_decimals, reward_fetcher=None, defer_on_zero_reward=False, use_work_accrual=False):
        _ = (session, now, interval_minutes, payout_decimals)
        current = datetime.now(UTC)
        reward = Decimal(str(reward_fetcher(current, current))) if reward_fetcher else Decimal("0")
        assert reward == Decimal("1")
        return SettlementResult(
            settlement_id=9,
            status="completed",
            user_count=0,
            period_start=datetime(2026, 1, 1, 0, 0, 0),
            period_end=datetime(2026, 1, 1, 0, 10, 0),
            total_shares=0,
            total_work=Decimal("0"),
            pool_reward_btc=reward,
            carry_btc=Decimal("0"),
        )

    monkeypatch.setattr("app.main.run_settlement", _fake_run_settlement)

    def _fake_process_payout_events(session, dry_run):
        _ = dry_run
        session.commit()
        return SenderStats(attempted=0, sent=0, failed=0, created_events=0)

    monkeypatch.setattr(
        "app.main.process_payout_events",
        _fake_process_payout_events,
    )

    client = TestClient(app)
    response = client.post("/settlements/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["block_reward"]["reward_mode"] == "block_events"
    assert payload["block_reward"]["fetched_block_count"] == 1
    assert payload["block_reward"]["inserted_block_count"] == 1
    assert payload["block_reward"]["matured_hash_count"] == 1
    assert payload["block_reward"]["computed_reward_btc"] == "1.00000000"

    with Session() as session:
        row = session.query(SnapshotBlock).filter(SnapshotBlock.blockhash == "matured-hash-1").one()
        assert row.reward_sats == 100000000
        assert row.settlement_id == 9


def test_run_settlement_cycle_defers_and_accrues_when_rewards_missing(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "run_cycle_block_events_deferred.db"
    log_file = tmp_path / "run_cycle_block_events_deferred_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    anchor = datetime.now(UTC).replace(tzinfo=None)
    with Session() as session:
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=0,
                accepted_work_total=0,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=12),
            )
        )
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=5,
                accepted_work_total=100,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=2),
            )
        )
        session.commit()

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "true")
    monkeypatch.setenv("DEFER_ON_ZERO_MATURED_REWARD", "true")
    monkeypatch.setenv("BLOCK_REWARD_BATCH_URL", "http://127.0.0.1:8080/v1/az/blocks/rewards")
    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "http://127.0.0.1:8080/v1/translator/blocks-found")
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)

    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)
    monkeypatch.setattr(
        "app.main.fetch_blocks_found_in_window",
        lambda start, end: [
            {
                "found_at": (start + timedelta(minutes=1)).isoformat(),
                "channel_id": 2,
                "worker_name": "alice.m1",
                "blockhash": "matured-hash-missing",
            }
        ],
    )
    monkeypatch.setattr("app.main.fetch_block_rewards_by_hashes", lambda hashes: {})

    def _fake_process_payout_events(session, dry_run):
        _ = dry_run
        session.commit()
        return SenderStats(attempted=0, sent=0, failed=0, created_events=0)

    monkeypatch.setattr("app.main.process_payout_events", _fake_process_payout_events)

    client = TestClient(app)
    response = client.post("/settlements/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settlement"]["status"] == "deferred"
    assert payload["settlement"]["pool_reward_btc"] == "0.00000000"
    assert payload["block_reward"]["reward_mode"] == "block_events"
    assert payload["block_reward"]["matured_hash_count"] == 1
    assert payload["block_reward"]["computed_reward_btc"] == "0.00000000"
    assert payload["block_reward"]["settlement_reward_btc"] == "0.00000000"
    assert payload["block_reward"]["reward_entries_complete"] is False
    assert payload["block_reward"]["missing_reward_hash_count"] == 1

    with Session() as session:
        settlement = session.query(Settlement).one()
        assert settlement.status == "deferred"
        assert session.query(UserPayout).count() == 0

        row = session.query(SnapshotBlock).filter(SnapshotBlock.blockhash == "matured-hash-missing").one()
        assert row.reward_sats == 0
        assert row.settlement_id == settlement.id

        user = session.query(User).filter(User.username == "alice").one()
        bucket = session.query(WorkAccrualBucket).filter(WorkAccrualBucket.user_id == user.id).one()
        assert Decimal(str(bucket.accumulated_work)) == Decimal("100.00000000")


def test_run_settlement_cycle_defers_on_partial_reward_subset(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "run_cycle_block_events_partial.db"
    log_file = tmp_path / "run_cycle_block_events_partial_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    anchor = datetime.now(UTC).replace(tzinfo=None)
    with Session() as session:
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=0,
                accepted_work_total=0,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=12),
            )
        )
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=8,
                accepted_work_total=160,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=2),
            )
        )
        session.commit()

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "true")
    monkeypatch.setenv("DEFER_ON_ZERO_MATURED_REWARD", "true")
    monkeypatch.setenv("BLOCK_REWARD_BATCH_URL", "http://127.0.0.1:8080/v1/az/blocks/rewards")
    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "http://127.0.0.1:8080/v1/translator/blocks-found")
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)

    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)
    monkeypatch.setattr(
        "app.main.fetch_blocks_found_in_window",
        lambda start, end: [
            {
                "found_at": (start + timedelta(minutes=1)).isoformat(),
                "channel_id": 2,
                "worker_name": "alice.m1",
                "blockhash": "matured-hash-1",
            },
            {
                "found_at": (start + timedelta(minutes=2)).isoformat(),
                "channel_id": 2,
                "worker_name": "alice.m1",
                "blockhash": "matured-hash-2",
            },
        ],
    )
    monkeypatch.setattr(
        "app.main.fetch_block_rewards_by_hashes",
        lambda hashes: {"matured-hash-1": 100000000},
    )

    def _fake_process_payout_events(session, dry_run):
        _ = dry_run
        session.commit()
        return SenderStats(attempted=0, sent=0, failed=0, created_events=0)

    monkeypatch.setattr("app.main.process_payout_events", _fake_process_payout_events)

    client = TestClient(app)
    response = client.post("/settlements/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settlement"]["status"] == "deferred"
    assert payload["settlement"]["pool_reward_btc"] == "0.00000000"
    assert payload["block_reward"]["matured_hash_count"] == 2
    assert payload["block_reward"]["computed_reward_btc"] == "1.00000000"
    assert payload["block_reward"]["settlement_reward_btc"] == "0.00000000"
    assert payload["block_reward"]["reward_entries_complete"] is False
    assert payload["block_reward"]["missing_reward_hash_count"] == 1

    with Session() as session:
        settlement = session.query(Settlement).one()
        assert settlement.status == "deferred"
        assert session.query(UserPayout).count() == 0

        rows = session.query(SnapshotBlock).order_by(SnapshotBlock.blockhash.asc()).all()
        assert [row.blockhash for row in rows] == ["matured-hash-1", "matured-hash-2"]
        assert [row.reward_sats for row in rows] == [100000000, 0]
        assert all(row.settlement_id == settlement.id for row in rows)

        user = session.query(User).filter(User.username == "alice").one()
        bucket = session.query(WorkAccrualBucket).filter(WorkAccrualBucket.user_id == user.id).one()
        assert Decimal(str(bucket.accumulated_work)) == Decimal("160.00000000")


def test_phase5_hooks_are_invoked_only_when_flags_enabled(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "phase5_hooks.db"
    log_file = tmp_path / "phase5_hooks_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    anchor = datetime.now(UTC).replace(tzinfo=None)
    with Session() as session:
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=0,
                accepted_work_total=0,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=12),
            )
        )
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=5,
                accepted_work_total=100,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=2),
            )
        )
        session.commit()

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "true")
    monkeypatch.setenv("DEFER_ON_ZERO_MATURED_REWARD", "true")
    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "http://127.0.0.1:8080/v1/translator/blocks-found")
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("ENABLE_STARTUP_RECONCILIATION_HOOK", "true")
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REPLAY_HOOK", "true")
    monkeypatch.setenv("ENABLE_REWARD_REFETCH_HOOK", "true")
    monkeypatch.setenv("ENABLE_SETTLEMENT_REPLAY_HOOK", "true")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)

    calls = {"startup": 0, "block_replay": 0, "reward_refetch": 0, "settlement_replay": 0}

    def _startup_hook(session, settings):
        _ = (session, settings)
        calls["startup"] += 1

    def _block_replay_hook(session, matured_start, matured_end):
        _ = (session, matured_end)
        calls["block_replay"] += 1
        return [
            {
                "found_at": (matured_start + timedelta(minutes=1)).isoformat(),
                "channel_id": 2,
                "worker_name": "alice.m1",
                "blockhash": "hook-replayed-hash",
            }
        ]

    def _reward_refetch_hook(selected_hashes, rewards_by_hash):
        calls["reward_refetch"] += 1
        assert "hook-replayed-hash" in selected_hashes
        assert rewards_by_hash == {}
        return {"hook-replayed-hash": 50000000}

    def _settlement_replay_hook(session, settlement_result):
        _ = session
        calls["settlement_replay"] += 1
        assert settlement_result.status in {"completed", "deferred"}

    set_startup_reconciliation_hook(_startup_hook)
    set_block_event_replay_hook(_block_replay_hook)
    set_reward_refetch_hook(_reward_refetch_hook)
    set_settlement_replay_hook(_settlement_replay_hook)

    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)
    monkeypatch.setattr("app.main.fetch_blocks_found_in_window", lambda start, end: [])
    monkeypatch.setattr("app.main.fetch_block_rewards_by_hashes", lambda hashes: {})

    def _fake_process_payout_events(session, dry_run):
        _ = dry_run
        session.commit()
        return SenderStats(attempted=0, sent=0, failed=0, created_events=0)

    monkeypatch.setattr("app.main.process_payout_events", _fake_process_payout_events)

    try:
        with TestClient(app) as client:
            response = client.post("/settlements/run")
            assert response.status_code == 200

        assert calls["startup"] >= 1
        assert calls["block_replay"] == 1
        assert calls["reward_refetch"] == 1
        assert calls["settlement_replay"] == 1

        with Session() as session:
            row = session.query(SnapshotBlock).filter(SnapshotBlock.blockhash == "hook-replayed-hash").one()
            assert row.reward_sats == 50000000
    finally:
        reset_hooks_to_noop()

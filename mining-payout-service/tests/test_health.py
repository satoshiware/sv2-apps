from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
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


@pytest.mark.smoke
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
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "false")
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


def test_execute_settlement_cycle_tolerates_slightly_early_due_tick(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "run_cycle_tolerance.db"
    log_file = tmp_path / "run_cycle_tolerance_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    previous_end = datetime(2026, 1, 1, 0, 34, 38, 323905)
    with Session() as session:
        session.add(
            Settlement(
                status="deferred",
                period_start=previous_end - timedelta(minutes=6),
                period_end=previous_end,
                total_shares=0,
                total_work=Decimal("0"),
                pool_reward_btc=Decimal("0"),
            )
        )
        session.commit()

    current = datetime(2026, 1, 1, 0, 40, 38, 316679)

    class _FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return current.replace(tzinfo=tz)
            return current

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "6")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)
    monkeypatch.setattr("app.main.datetime", _FixedDateTime)
    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)
    monkeypatch.setattr(
        "app.main.run_settlement",
        lambda session, now, interval_minutes, payout_decimals, reward_fetcher=None, **kwargs: SettlementResult(
            settlement_id=2,
            status="deferred",
            user_count=0,
            period_start=previous_end,
            period_end=current,
            total_shares=0,
            total_work=Decimal("0"),
            pool_reward_btc=Decimal("0"),
            carry_btc=Decimal("0"),
        ),
    )
    monkeypatch.setattr(
        "app.main.process_payout_events",
        lambda session, dry_run: SenderStats(attempted=0, sent=0, failed=0, created_events=0),
    )

    result = main_module._execute_settlement_cycle(force_settlement=False)

    assert result["settlement_skipped"] is False
    assert result["settlement"]["settlement_id"] == 2


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
    db_file = tmp_path / "audit_settlements.db"
    log_file = tmp_path / "payout_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    with Session() as session:
        session.add(
            SnapshotBlock(
                found_at=datetime(2026, 1, 1, 0, 0, 30),
                channel_id=2,
                worker_identity="alice.rig1",
                blockhash="hash-31",
                source="translator_blocks_api",
                reward_sats=187500000,
                settlement_id=31,
            )
        )
        session.commit()

    entries = [
        {"event_type": "scheduler_started", "timestamp": "2026-01-01T00:00:00", "payload": {}},
        {
            "attempt_id": "attempt-1",
            "attempted_at": "2026-01-01T00:01:00",
            "period_start": "2026-01-01T00:00:00",
            "period_end": "2026-01-01T00:01:00",
            "contribution_window_start": "2025-12-31T20:30:00",
            "contribution_window_end": "2025-12-31T20:40:00",
            "settlement": {
                "settlement_id": 31,
                "status": "completed",
                "reward_mode": "blocks",
                "pool_reward_btc": "1.87500000",
                "total_work": "100.00000000",
                "total_shares": 10,
            },
            "payout_rows": [
                {
                    "username": "alice",
                    "amount_btc": "0.75000000",
                    "status": "sent",
                    "payout_fraction": "0.400000000000",
                    "contribution_value": "40.00000000",
                },
                {
                    "username": "bob",
                    "amount_btc": "1.12500000",
                    "status": "sent",
                    "payout_fraction": "0.600000000000",
                    "contribution_value": "60.00000000",
                },
            ],
            "checks": {"unrewarded_user_count": 0, "unrewarded_users": []},
            "block_reward": {
                "interval_blocks": 1,
                "computed_reward_btc": "1.87500000",
                "settlement_reward_btc": "1.87500000",
                "matured_window_start": "2025-12-31T20:30:00",
                "matured_window_end": "2025-12-31T20:40:00",
                "channels": [],
            },
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
    monkeypatch.setenv("DB_PATH", str(db_file))
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
    assert settlement_row["contribution_window_start"] == "2025-12-31T20:30:00"
    assert settlement_row["contribution_window_end"] == "2025-12-31T20:40:00"
    assert settlement_row["settlement_reward_btc"] == "1.87500000"
    assert settlement_row["block_rows"] == [
        {
            "found_at": "2026-01-01T00:00:30",
            "channel_id": 2,
            "worker_identity": "alice.rig1",
            "blockhash": "hash-31",
            "source": "translator_blocks_api",
            "reward_sats": 187500000,
            "reward_btc": "1.87500000",
        }
    ]
    assert settlement_row["payout_user_breakdown"] == [
        {
            "username": "alice",
            "amount_btc": "0.75000000",
            "status": "sent",
            "payout_fraction": "0.400000000000",
            "contribution_value": "40.00000000",
            "share_delta": 0,
            "work_delta": "0.00000000",
        },
        {
            "username": "bob",
            "amount_btc": "1.12500000",
            "status": "sent",
            "payout_fraction": "0.600000000000",
            "contribution_value": "60.00000000",
            "share_delta": 0,
            "work_delta": "0.00000000",
        },
    ]
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
    assert "Contribution Window (MST)" in response.text
    assert "Payout Ratio By User" in response.text
    assert "Matured Blocks And Rewards" in response.text
    assert "Snapshot Rows Used For Delta (Baseline -> Current)" in response.text
    assert "Latest Snapshot State By Identity" in response.text
    assert "How Work Delta Is Calculated" in response.text


@pytest.mark.smoke
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

    def _fake_run_settlement(session, now, interval_minutes, payout_decimals, reward_fetcher=None, defer_on_zero_reward=False, use_work_accrual=False, work_window_start=None, work_window_end=None, **kwargs):
        _ = (session, now, interval_minutes, payout_decimals, work_window_start, work_window_end)
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


@pytest.mark.smoke
def test_run_settlement_cycle_defers_and_accrues_when_rewards_missing(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "run_cycle_block_events_deferred.db"
    log_file = tmp_path / "run_cycle_block_events_deferred_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    # Matured window with MATURITY_WINDOW_MINUTES=200, T=10: [now-210min, now-200min)
    # Place a baseline snapshot just before the window and a reading inside it.
    anchor = datetime.now(UTC).replace(tzinfo=None)
    with Session() as session:
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=0,
                accepted_work_total=0,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=212),
            )
        )
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=5,
                accepted_work_total=100,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=205),
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
    monkeypatch.setenv("MATURITY_WINDOW_MINUTES", "200")
    monkeypatch.setenv("MATURITY_WINDOW_MINUTES", "200")
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

    # Matured window with MATURITY_WINDOW_MINUTES=200, T=10: [now-210min, now-200min)
    # Place a baseline snapshot just before the window and a reading inside it.
    anchor = datetime.now(UTC).replace(tzinfo=None)
    with Session() as session:
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=0,
                accepted_work_total=0,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=212),
            )
        )
        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=8,
                accepted_work_total=160,
                shares_rejected_total=0,
                created_at=anchor - timedelta(minutes=205),
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
    monkeypatch.setenv("MATURITY_WINDOW_MINUTES", "200")
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


def test_run_settlement_cycle_links_only_matured_window_block_boundaries(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "run_cycle_matured_boundaries.db"
    log_file = tmp_path / "run_cycle_matured_boundaries_audit.jsonl"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    matured_start = datetime(2026, 4, 28, 8, 30, 0)
    matured_end = datetime(2026, 4, 28, 8, 40, 0)

    with Session() as session:
        session.add(
            SnapshotBlock(
                found_at=matured_start,
                channel_id=2,
                worker_identity="alice.m1",
                blockhash="hash-start-inclusive",
                source="seed",
            )
        )
        session.add(
            SnapshotBlock(
                found_at=matured_end,
                channel_id=2,
                worker_identity="alice.m1",
                blockhash="hash-end-exclusive",
                source="seed",
            )
        )
        session.add(
            SnapshotBlock(
                found_at=matured_start - timedelta(seconds=1),
                channel_id=2,
                worker_identity="alice.m1",
                blockhash="hash-before-start",
                source="seed",
            )
        )
        session.commit()

    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setenv("PAYOUT_AUDIT_LOG_PATH", str(log_file))
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "true")
    monkeypatch.setenv("DEFER_ON_ZERO_MATURED_REWARD", "true")
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)
    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "")

    monkeypatch.setattr("app.main.compute_matured_window", lambda now, interval_minutes, maturity_window_minutes: (matured_start, matured_end))
    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)
    monkeypatch.setattr("app.main.fetch_block_rewards_by_hashes", lambda hashes: {"hash-start-inclusive": 187500000})

    def _fake_process_payout_events(session, dry_run):
        _ = dry_run
        session.commit()
        return SenderStats(attempted=0, sent=0, failed=0, created_events=0)

    monkeypatch.setattr("app.main.process_payout_events", _fake_process_payout_events)

    client = TestClient(app)
    response = client.post("/settlements/run")

    assert response.status_code == 200
    payload = response.json()
    assert payload["block_reward"]["matured_hash_count"] == 1
    assert payload["block_reward"]["computed_reward_btc"] == "1.87500000"

    with Session() as session:
        settlement = session.query(Settlement).one()

        in_window = session.query(SnapshotBlock).filter(SnapshotBlock.blockhash == "hash-start-inclusive").one()
        assert in_window.settlement_id == settlement.id
        assert in_window.reward_sats == 187500000

        end_boundary = session.query(SnapshotBlock).filter(SnapshotBlock.blockhash == "hash-end-exclusive").one()
        assert end_boundary.settlement_id is None
        assert end_boundary.reward_sats is None

        before_start = session.query(SnapshotBlock).filter(SnapshotBlock.blockhash == "hash-before-start").one()
        assert before_start.settlement_id is None
        assert before_start.reward_sats is None


@pytest.mark.smoke
def test_phase_e_deferred_then_rewarded_recovery_via_settlements_run(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "phase_e_recovery.db"
    log_file = tmp_path / "phase_e_recovery_audit.jsonl"
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
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)

    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)

    matured_windows = [
        (anchor - timedelta(minutes=2), anchor),
        (anchor, anchor + timedelta(minutes=3)),
    ]
    matured_call_state = {"n": 0}

    def _matured_window(now, interval_minutes, maturity_window_minutes):
        _ = (now, interval_minutes, maturity_window_minutes)
        idx = matured_call_state["n"]
        matured_call_state["n"] += 1
        return matured_windows[min(idx, len(matured_windows) - 1)]

    monkeypatch.setattr("app.main.compute_matured_window", _matured_window)

    block_call_state = {"n": 0}

    def _blocks_found(start, end):
        _ = (start, end)
        block_call_state["n"] += 1
        if block_call_state["n"] == 1:
            return [
                {
                    "found_at": (anchor - timedelta(minutes=1)).isoformat(),
                    "channel_id": 2,
                    "worker_name": "alice.m1",
                    "blockhash": "phase-e-hash-1",
                }
            ]
        return [
            {
                "found_at": (anchor + timedelta(minutes=1)).isoformat(),
                "channel_id": 2,
                "worker_name": "alice.m1",
                "blockhash": "phase-e-hash-2",
            }
        ]

    monkeypatch.setattr("app.main.fetch_blocks_found_in_window", _blocks_found)

    def _fetch_rewards(hashes):
        if "phase-e-hash-2" in hashes:
            return {"phase-e-hash-2": 100000000}
        return {}

    monkeypatch.setattr("app.main.fetch_block_rewards_by_hashes", _fetch_rewards)

    def _fake_process_payout_events(session, dry_run):
        _ = dry_run
        session.commit()
        return SenderStats(attempted=0, sent=0, failed=0, created_events=0)

    monkeypatch.setattr("app.main.process_payout_events", _fake_process_payout_events)

    client = TestClient(app)

    first = client.post("/settlements/run")
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["settlement"]["status"] == "deferred"
    assert first_payload["settlement"]["pool_reward_btc"] == "0.00000000"
    assert first_payload["block_reward"]["matured_hash_count"] == 1

    with Session() as session:
        first_settlement = session.query(Settlement).order_by(Settlement.id.asc()).one()
        assert first_settlement.status == "deferred"

        user = session.query(User).filter(User.username == "alice").one()
        bucket = session.query(WorkAccrualBucket).filter(WorkAccrualBucket.user_id == user.id).one()
        assert Decimal(str(bucket.accumulated_work)) == Decimal("100.00000000")

        session.add(
            MetricSnapshot(
                channel_id=2,
                identity="alice.m1",
                accepted_shares_total=7,
                accepted_work_total=160,
                shares_rejected_total=0,
                created_at=first_settlement.period_end + timedelta(minutes=1),
            )
        )
        session.commit()

    class _FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            _ = tz
            return (anchor + timedelta(minutes=2)).replace(tzinfo=UTC)

    monkeypatch.setattr("app.main.datetime", _FixedDateTime)

    second = client.post("/settlements/run")
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["settlement"]["status"] == "completed"
    assert second_payload["settlement"]["pool_reward_btc"] == "1.00000000"
    assert second_payload["block_reward"]["reward_entries_complete"] is True
    assert second_payload["block_reward"]["matured_hash_count"] == 1

    with Session() as session:
        settlements = session.query(Settlement).order_by(Settlement.id.asc()).all()
        assert [s.status for s in settlements] == ["deferred", "completed"]

        second_settlement = settlements[-1]
        payouts = session.query(UserPayout).filter(UserPayout.settlement_id == second_settlement.id).all()
        assert len(payouts) == 1
        assert Decimal(str(payouts[0].amount_btc)) == Decimal("1.00000000")

        user = session.query(User).filter(User.username == "alice").one()
        bucket = session.query(WorkAccrualBucket).filter(WorkAccrualBucket.user_id == user.id).one()
        assert Decimal(str(bucket.accumulated_work)) == Decimal("0")

        rows = session.query(SnapshotBlock).order_by(SnapshotBlock.blockhash.asc()).all()
        assert [r.blockhash for r in rows] == ["phase-e-hash-1", "phase-e-hash-2"]
        assert rows[0].reward_sats == 0
        assert rows[1].reward_sats == 100000000
        assert rows[0].settlement_id == settlements[0].id
        assert rows[1].settlement_id == settlements[1].id


# ---------------------------------------------------------------------------
# Phase F: idempotency and hook-boundary safety
# ---------------------------------------------------------------------------


@pytest.mark.smoke
def test_phase_f_rewarded_interval_api_rerun_is_idempotent(monkeypatch, tmp_path) -> None:
    """Calling /settlements/run twice for the same time window must return the
    same settlement (idempotency guard fires) and must not create duplicate
    Settlement or UserPayout rows in the database."""
    db_file = tmp_path / "phase_f_idem_rewarded.db"
    log_file = tmp_path / "phase_f_idem_rewarded_audit.jsonl"
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
    monkeypatch.setenv("DEFER_ON_ZERO_MATURED_REWARD", "false")
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)

    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)
    monkeypatch.setattr(
        "app.main.compute_matured_window",
        lambda now, interval_minutes, maturity_window_minutes: (
            anchor - timedelta(minutes=5),
            anchor,
        ),
    )
    monkeypatch.setattr(
        "app.main.fetch_blocks_found_in_window",
        lambda start, end: [
            {
                "found_at": (anchor - timedelta(minutes=3)).isoformat(),
                "channel_id": 2,
                "worker_name": "alice.m1",
                "blockhash": "phase-f1-hash",
            }
        ],
    )
    monkeypatch.setattr(
        "app.main.fetch_block_rewards_by_hashes",
        lambda hashes: {"phase-f1-hash": 100_000_000} if "phase-f1-hash" in hashes else {},
    )
    monkeypatch.setattr(
        "app.main.process_payout_events",
        lambda session, dry_run: (
            session.commit() or SenderStats(attempted=0, sent=0, failed=0, created_events=0)
        ),
    )

    # Pin datetime.now to the same value for both requests so period_end is
    # identical on both calls, triggering the idempotency guard on the second.
    # Use anchor itself (not anchor+10m) so the settlement period [anchor-10m, anchor)
    # covers the seeded metric snapshots.
    fixed_now = anchor.replace(tzinfo=UTC)

    class _FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            _ = tz
            return fixed_now

    monkeypatch.setattr("app.main.datetime", _FixedDateTime)

    client = TestClient(app)

    first = client.post("/settlements/run")
    assert first.status_code == 200
    first_payload = first.json()
    assert first_payload["settlement"]["status"] == "completed"
    first_sid = first_payload["settlement"]["settlement_id"]

    second = client.post("/settlements/run")
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["settlement"]["status"] == "completed"
    second_sid = second_payload["settlement"]["settlement_id"]

    # Same settlement returned — no new rows created
    assert first_sid == second_sid

    with Session() as session:
        assert session.query(Settlement).count() == 1
        assert session.query(UserPayout).count() == 1


def test_phase_f_deferred_api_rerun_does_not_double_accrue_work(monkeypatch, tmp_path) -> None:
    """Re-triggering a deferred settlement for the same window must not
    increment the accrual bucket a second time."""
    db_file = tmp_path / "phase_f_idem_deferred.db"
    log_file = tmp_path / "phase_f_idem_deferred_audit.jsonl"
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
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)

    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)
    monkeypatch.setattr(
        "app.main.compute_matured_window",
        lambda now, interval_minutes, maturity_window_minutes: (
            anchor - timedelta(minutes=5),
            anchor,
        ),
    )
    # No blocks → no reward → deferred
    monkeypatch.setattr("app.main.fetch_blocks_found_in_window", lambda start, end: [])
    monkeypatch.setattr("app.main.fetch_block_rewards_by_hashes", lambda hashes: {})
    monkeypatch.setattr(
        "app.main.process_payout_events",
        lambda session, dry_run: (
            session.commit() or SenderStats(attempted=0, sent=0, failed=0, created_events=0)
        ),
    )

    # Use anchor itself so the settlement period [anchor-10m, anchor) covers
    # the seeded metric snapshots.
    fixed_now = anchor.replace(tzinfo=UTC)

    class _FixedDateTime:
        @classmethod
        def now(cls, tz=None):
            _ = tz
            return fixed_now

    monkeypatch.setattr("app.main.datetime", _FixedDateTime)

    client = TestClient(app)

    first = client.post("/settlements/run")
    assert first.status_code == 200
    assert first.json()["settlement"]["status"] == "deferred"
    first_sid = first.json()["settlement"]["settlement_id"]

    second = client.post("/settlements/run")
    assert second.status_code == 200
    assert second.json()["settlement"]["status"] == "deferred"
    second_sid = second.json()["settlement"]["settlement_id"]

    # Idempotent — same deferred settlement row
    assert first_sid == second_sid

    with Session() as session:
        assert session.query(Settlement).count() == 1
        user = session.query(User).filter(User.username == "alice").one()
        bucket = session.query(WorkAccrualBucket).filter(WorkAccrualBucket.user_id == user.id).one()
        # Must be exactly 100, not doubled to 200
        assert Decimal(str(bucket.accumulated_work)) == Decimal("100.00000000")


@pytest.mark.smoke
def test_phase_f_block_replay_hook_exception_does_not_corrupt_settlement(monkeypatch, tmp_path) -> None:
    """An exception raised inside the block-replay hook must be swallowed
    (the try/except in _execute_settlement_cycle catches it) so the settlement
    cycle completes without a 500 and persists a valid Settlement row."""
    db_file = tmp_path / "phase_f_hook_exc.db"
    log_file = tmp_path / "phase_f_hook_exc_audit.jsonl"
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
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REPLAY_HOOK", "true")
    monkeypatch.setenv("DEFER_ON_ZERO_MATURED_REWARD", "true")
    monkeypatch.setenv("PAYOUT_INTERVAL_MINUTES", "10")
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.delenv("TRANSLATOR_CHANNELS_URL", raising=False)

    monkeypatch.setattr("app.main.poll_metrics_once", lambda session, api_url: 0)
    monkeypatch.setattr(
        "app.main.compute_matured_window",
        lambda now, interval_minutes, maturity_window_minutes: (
            anchor - timedelta(minutes=5),
            anchor,
        ),
    )
    monkeypatch.setattr("app.main.fetch_blocks_found_in_window", lambda start, end: [])
    monkeypatch.setattr("app.main.fetch_block_rewards_by_hashes", lambda hashes: {})
    monkeypatch.setattr(
        "app.main.process_payout_events",
        lambda session, dry_run: (
            session.commit() or SenderStats(attempted=0, sent=0, failed=0, created_events=0)
        ),
    )

    reset_hooks_to_noop()

    def _exploding_replay_hook(session, matured_start, matured_end):
        _ = (session, matured_start, matured_end)
        raise RuntimeError("simulated hook failure")

    set_block_event_replay_hook(_exploding_replay_hook)

    try:
        client = TestClient(app)
        response = client.post("/settlements/run")

        # Hook exception must not cause a 500 — the try/except swallows it
        assert response.status_code == 200, (
            f"Expected 200 but got {response.status_code}: {response.text}"
        )
        payload = response.json()
        # Settlement must be persisted in a valid terminal state
        assert payload["settlement"]["status"] in ("deferred", "completed")

        with Session() as session:
            assert session.query(Settlement).count() == 1
    finally:
        reset_hooks_to_noop()

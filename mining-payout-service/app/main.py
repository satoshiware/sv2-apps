from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
import traceback
import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from fastapi import FastAPI

from app.audit import build_payout_audit_event, read_recent_audit_entries, write_payout_audit_log
from app.config import load_settings
from app.db import make_engine, make_session_factory
from app.init_db import init_db
from app.models import BlockCounterState, PayoutEvent, Settlement, User, UserPayout
from app.poller import poll_channels_once, poll_channels_once_with_blocks, poll_metrics_once
from app.scheduler import start_scheduler, stop_scheduler
from app.sender import process_payout_events
from app.settlement import run_settlement

app = FastAPI(title="Mining Payout Service", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
def on_startup() -> None:
    settings = load_settings()
    if not settings.scheduler_enabled:
        _write_scheduler_event(
            "scheduler_disabled",
            {
                "reason": "SCHEDULER_ENABLED is false",
                "raw_env_value": os.getenv("SCHEDULER_ENABLED"),
            },
        )
        return

    scheduler = start_scheduler()
    scheduler.add_job(
        _run_scheduled_cycle,
        "interval",
        seconds=max(1, int(settings.scheduler_interval_seconds)),
        id="settlement-cycle",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=30,
    )
    _write_scheduler_event(
        "scheduler_started",
        {
            "interval_seconds": int(max(1, int(settings.scheduler_interval_seconds))),
            "reward_mode": _normalize_reward_mode(settings.reward_mode),
            "channels_url_configured": bool(settings.translator_channels_url),
            "audit_log_path": settings.payout_audit_log_path,
        },
    )


@app.on_event("shutdown")
def on_shutdown() -> None:
    settings = load_settings()
    stop_scheduler()
    if settings.scheduler_enabled:
        _write_scheduler_event("scheduler_stopped", {})


def _write_scheduler_event(event_type: str, payload: dict[str, object]) -> None:
    settings = load_settings()
    event = {
        "event_type": event_type,
        "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat(),
        "payload": payload,
    }
    try:
        write_payout_audit_log(settings.payout_audit_log_path, event)
    except OSError:
        pass


def _run_scheduled_cycle() -> None:
    started_at = datetime.now(UTC).replace(tzinfo=None)
    _write_scheduler_event("scheduler_cycle_started", {"started_at": started_at.isoformat()})
    try:
        result = _execute_settlement_cycle()
        _write_scheduler_event(
            "scheduler_cycle_completed",
            {
                "settlement": result.get("settlement", {}),
                "snapshots_created": result.get("snapshots_created", 0),
            },
        )
    except Exception as exc:
        _write_scheduler_event(
            "scheduler_cycle_failed",
            {
                "error": str(exc),
                "traceback": traceback.format_exc(),
            },
        )


def _new_session() -> Session:
    settings = load_settings()
    init_db(settings.db_path)
    engine = make_engine(settings.db_path)
    SessionFactory = make_session_factory(engine)
    return SessionFactory()


def _to_decimal_str(value: object) -> str:
    return f"{Decimal(str(value or 0)):.8f}"


def _normalize_reward_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in {"manual", "blocks"} else "blocks"


def _compute_interval_blocks_delta(
    session: Session,
    current_blocks_found_by_channel: dict[int, int],
) -> tuple[int, list[dict[str, int | bool]]]:
    rows = session.execute(select(BlockCounterState)).scalars().all()
    previous_by_channel = {int(row.channel_id): int(row.last_blocks_found_total or 0) for row in rows}
    state_by_channel = {int(row.channel_id): row for row in rows}

    details: list[dict[str, int | bool]] = []
    interval_blocks = 0
    now = datetime.now(UTC).replace(tzinfo=None)

    for channel_id, current in sorted(current_blocks_found_by_channel.items(), key=lambda item: item[0]):
        previous = int(previous_by_channel.get(channel_id, 0))
        reset_detected = current < previous
        delta = current - previous if not reset_detected else 0
        interval_blocks += delta

        state = state_by_channel.get(channel_id)
        if state is None:
            state = BlockCounterState(
                channel_id=channel_id,
                last_blocks_found_total=current,
                updated_at=now,
            )
            session.add(state)
            state_by_channel[channel_id] = state
        else:
            state.last_blocks_found_total = current
            state.updated_at = now

        details.append(
            {
                "channel_id": channel_id,
                "previous_blocks_found": previous,
                "current_blocks_found": current,
                "delta_blocks": delta,
                "reset_detected": reset_detected,
            }
        )

    session.flush()
    return interval_blocks, details


@app.get("/service-metrics")
def service_metrics() -> dict:
    with _new_session() as session:
        settlements_total = session.execute(select(func.count(Settlement.id))).scalar_one()
        payouts_sent_total = session.execute(
            select(func.count(UserPayout.id)).where(UserPayout.status == "sent")
        ).scalar_one()
        payout_failures_total = session.execute(
            select(func.count(PayoutEvent.id)).where(PayoutEvent.status == "pending_sent")
        ).scalar_one()
        last_settlement_timestamp = session.execute(select(func.max(Settlement.period_end))).scalar_one()

    return {
        "settlements_total": int(settlements_total or 0),
        "payouts_sent_total": int(payouts_sent_total or 0),
        "payout_failures_total": int(payout_failures_total or 0),
        "last_settlement_timestamp": last_settlement_timestamp.isoformat()
        if last_settlement_timestamp
        else None,
    }


@app.get("/audit/logs")
def audit_logs(limit: int = 50) -> dict:
    settings = load_settings()
    payload = read_recent_audit_entries(settings.payout_audit_log_path, limit=limit)
    payload["scheduler_enabled"] = bool(settings.scheduler_enabled)
    payload["scheduler_interval_seconds"] = int(settings.scheduler_interval_seconds)
    return payload


@app.get("/settlements/latest")
def latest_settlement() -> dict:
    with _new_session() as session:
        settlement = session.execute(
            select(Settlement).order_by(Settlement.period_end.desc(), Settlement.id.desc())
        ).scalar_one_or_none()
        if settlement is None:
            return {"settlement": None, "users": []}

        rows = session.execute(
            select(UserPayout, User)
            .join(User, User.id == UserPayout.user_id)
            .where(UserPayout.settlement_id == settlement.id)
            .order_by(User.username.asc(), UserPayout.id.asc())
        ).all()

    return {
        "settlement": {
            "settlement_id": settlement.id,
            "status": settlement.status,
            "period_start": settlement.period_start.isoformat(),
            "period_end": settlement.period_end.isoformat(),
            "pool_reward_btc": _to_decimal_str(settlement.pool_reward_btc),
            "total_shares": int(settlement.total_shares or 0),
            "total_work": _to_decimal_str(settlement.total_work),
        },
        "users": [
            {
                "username": user.username,
                "contribution_value": _to_decimal_str(payout.contribution_value),
                "payout_fraction": str(payout.payout_fraction),
                "amount_btc": _to_decimal_str(payout.amount_btc),
                "status": payout.status,
            }
            for payout, user in rows
        ],
    }


@app.post("/settlements/run")
def run_settlement_cycle() -> dict:
    return _execute_settlement_cycle()


def _execute_settlement_cycle() -> dict:
    settings = load_settings()
    reward_mode = _normalize_reward_mode(settings.reward_mode)
    block_reward_btc = Decimal(str(settings.block_reward_btc or "1.87500000"))
    attempt_id = str(uuid.uuid4())

    with _new_session() as session:
        attempt_time = datetime.now(UTC).replace(tzinfo=None)
        period_start = attempt_time - timedelta(minutes=settings.payout_interval_minutes)
        block_reward_payload: dict[str, object] | None = None
        interval_blocks = 0
        block_delta_details: list[dict[str, int | bool]] = []

        if settings.translator_channels_url:
            snapshots_created, current_blocks_found_by_channel = poll_channels_once_with_blocks(
                session,
                settings.translator_channels_url,
                downstream_url=settings.translator_downstreams_url,
                bearer_token=settings.translator_bearer_token,
            )
            if reward_mode == "blocks":
                interval_blocks, block_delta_details = _compute_interval_blocks_delta(
                    session,
                    current_blocks_found_by_channel,
                )
                computed_reward = Decimal(interval_blocks) * block_reward_btc
                block_reward_payload = {
                    "reward_mode": "blocks",
                    "block_reward_btc": _to_decimal_str(block_reward_btc),
                    "interval_blocks": int(interval_blocks),
                    "computed_reward_btc": _to_decimal_str(computed_reward),
                    "channels": block_delta_details,
                }
                reward_fetcher = lambda _start, _end: computed_reward
            else:
                reward_fetcher = None
        else:
            snapshots_created = poll_metrics_once(session, settings.translator_metrics_url)
            reward_fetcher = None

        settlement_kwargs = {
            "interval_minutes": settings.payout_interval_minutes,
            "payout_decimals": settings.payout_decimals,
        }
        if reward_fetcher is not None:
            settlement_kwargs["reward_fetcher"] = reward_fetcher

        settlement_result = run_settlement(
            session,
            attempt_time,
            **settlement_kwargs,
        )
        sender_stats = process_payout_events(session, dry_run=settings.dry_run)

        audit_event = build_payout_audit_event(
            session,
            attempt_id=attempt_id,
            attempted_at=attempt_time,
            period_start=period_start,
            period_end=attempt_time,
            snapshots_created=snapshots_created,
            settlement_id=settlement_result.settlement_id,
            settlement_status=settlement_result.status,
            reward_mode=reward_mode,
            pool_reward_btc=settlement_result.pool_reward_btc,
            total_work_btc_basis=settlement_result.total_work,
            total_share_delta=settlement_result.total_shares,
            block_reward=block_reward_payload,
        )
        try:
            write_payout_audit_log(settings.payout_audit_log_path, audit_event)
        except OSError:
            _write_scheduler_event(
                "audit_log_write_failed",
                {
                    "attempt_id": attempt_id,
                    "audit_log_path": settings.payout_audit_log_path,
                },
            )

    response = {
        "snapshots_created": snapshots_created,
        "settlement": {
            "settlement_id": settlement_result.settlement_id,
            "status": settlement_result.status,
            "user_count": settlement_result.user_count,
            "total_shares": settlement_result.total_shares,
            "total_work": _to_decimal_str(settlement_result.total_work),
            "pool_reward_btc": _to_decimal_str(settlement_result.pool_reward_btc),
            "carry_btc": _to_decimal_str(settlement_result.carry_btc),
        },
        "sender": {
            "attempted": sender_stats.attempted,
            "sent": sender_stats.sent,
            "failed": sender_stats.failed,
            "created_events": sender_stats.created_events,
        },
    }

    if settings.translator_channels_url and reward_mode == "blocks":
        response["block_reward"] = {
            "reward_mode": "blocks",
            "block_reward_btc": _to_decimal_str(block_reward_btc),
            "interval_blocks": int(interval_blocks),
            "computed_reward_btc": _to_decimal_str(Decimal(interval_blocks) * block_reward_btc),
            "channels": block_delta_details,
        }

    return response

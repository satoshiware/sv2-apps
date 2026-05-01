from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
import os
import traceback
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.audit import (
    build_payout_audit_event,
    read_recent_audit_entries,
    rotate_payout_audit_log,
    write_payout_audit_log,
)
from app.config import load_settings
from app.db import make_engine, make_session_factory
from app.hooks import (
    run_block_event_replay_hook,
    run_reward_refetch_hook,
    run_settlement_replay_hook,
    run_startup_reconciliation_hook,
)
from app.init_db import init_db
from app.mapping import parse_identity
from app.models import BlockCounterState, PayoutEvent, Settlement, SnapshotBlock, User, UserPayout
from app.poller import poll_channels_once_with_blocks, poll_metrics_once, upsert_snapshot_blocks
from app.pool_client import PoolApiError, fetch_block_rewards_by_hashes, fetch_blocks_found_in_window
from app.reward_contract import compute_matured_window
from app.scheduler import start_scheduler, stop_scheduler
from app.sender import process_payout_events
from app.settlement import run_settlement

app = FastAPI(title="Mining Payout Service", version="0.1.0")
_SERVICE_STARTED_AT: datetime | None = None


def _sum_payout_amount(payout_rows: list[dict[str, object]]) -> Decimal:
    total = Decimal("0")
    for row in payout_rows:
        total += Decimal(str(row.get("amount_btc") or "0"))
    return total


def _to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _contribution_index(user_contributions: list[dict[str, object]]) -> dict[str, dict[str, Decimal | int]]:
    index: dict[str, dict[str, Decimal | int]] = {}
    for row in user_contributions:
        username = str(row.get("username") or "")
        if not username:
            continue
        index[username] = {
            "share_delta": _to_int(row.get("share_delta")),
            "work_delta": Decimal(str(row.get("work_delta") or "0")),
        }
    return index


def _payout_user_breakdown(
    payout_rows: list[dict[str, object]],
    user_contributions: list[dict[str, object]],
) -> list[dict[str, object]]:
    contributions = _contribution_index(user_contributions)
    breakdown: list[dict[str, object]] = []
    for payout in payout_rows:
        username = str(payout.get("username") or "")
        contrib = contributions.get(username, {"share_delta": 0, "work_delta": Decimal("0")})
        breakdown.append(
            {
                "username": username,
                "amount_btc": _to_decimal_str(payout.get("amount_btc") or "0"),
                "status": payout.get("status"),
                "payout_fraction": str(payout.get("payout_fraction") or "0"),
                "contribution_value": _to_decimal_str(payout.get("contribution_value") or "0"),
                "share_delta": int(contrib["share_delta"]),
                "work_delta": _to_decimal_str(contrib["work_delta"]),
            }
        )
    return breakdown


def _load_block_rows_by_settlement(session: Session, settlement_ids: list[int]) -> dict[int, list[dict[str, object]]]:
    if not settlement_ids:
        return {}

    rows = session.execute(
        select(SnapshotBlock)
        .where(SnapshotBlock.settlement_id.in_(settlement_ids))
        .order_by(
            SnapshotBlock.settlement_id.asc(),
            SnapshotBlock.found_at.asc(),
            SnapshotBlock.id.asc(),
        )
    ).scalars().all()

    grouped: dict[int, list[dict[str, object]]] = {}
    for row in rows:
        settlement_id = int(row.settlement_id or 0)
        if settlement_id <= 0:
            continue
        grouped.setdefault(settlement_id, []).append(
            {
                "found_at": row.found_at.isoformat(),
                "channel_id": int(row.channel_id or 0),
                "worker_identity": row.worker_identity,
                "blockhash": row.blockhash,
                "source": row.source,
                "reward_sats": int(row.reward_sats or 0),
                "reward_btc": _to_decimal_str(Decimal(int(row.reward_sats or 0)) / Decimal("100000000")),
            }
        )

    return grouped


def _compare_with_previous_payout(
    current_user_contributions: list[dict[str, object]],
    previous_user_contributions: list[dict[str, object]],
) -> list[dict[str, object]]:
    current = _contribution_index(current_user_contributions)
    previous = _contribution_index(previous_user_contributions)
    usernames = sorted(set(current.keys()) | set(previous.keys()))
    rows: list[dict[str, object]] = []

    for username in usernames:
        curr_share = int((current.get(username) or {}).get("share_delta", 0))
        curr_work = Decimal(str((current.get(username) or {}).get("work_delta", "0")))
        prev_share = int((previous.get(username) or {}).get("share_delta", 0))
        prev_work = Decimal(str((previous.get(username) or {}).get("work_delta", "0")))

        rows.append(
            {
                "username": username,
                "current_share_delta": curr_share,
                "current_work_delta": _to_decimal_str(curr_work),
                "previous_share_delta": prev_share,
                "previous_work_delta": _to_decimal_str(prev_work),
                "share_delta_change": curr_share - prev_share,
                "work_delta_change": _to_decimal_str(curr_work - prev_work),
            }
        )
    return rows


def _build_interval_ratio_rows(user_contributions: list[dict[str, object]]) -> list[dict[str, object]]:
    total_share_delta = sum(_to_int(row.get("share_delta")) for row in user_contributions)
    total_work_delta = sum(Decimal(str(row.get("work_delta") or "0")) for row in user_contributions)

    rows: list[dict[str, object]] = []
    for row in user_contributions:
        username = str(row.get("username") or "")
        if not username:
            continue
        share_delta = _to_int(row.get("share_delta"))
        work_delta = Decimal(str(row.get("work_delta") or "0"))
        share_ratio = (
            Decimal(share_delta) / Decimal(total_share_delta)
            if total_share_delta > 0
            else Decimal("0")
        )
        work_ratio = (
            work_delta / total_work_delta
            if total_work_delta > 0
            else Decimal("0")
        )
        rows.append(
            {
                "username": username,
                "share_delta": share_delta,
                "work_delta": _to_decimal_str(work_delta),
                "share_ratio": f"{share_ratio:.8f}",
                "work_ratio": f"{work_ratio:.8f}",
                "share_ratio_percent": f"{(share_ratio * Decimal('100')):.4f}",
                "work_ratio_percent": f"{(work_ratio * Decimal('100')):.4f}",
            }
        )

    rows.sort(key=lambda item: str(item.get("username") or ""))
    return rows


def _build_work_delta_explanation(snapshot_alignment: dict[str, object]) -> dict[str, object]:
    miners = snapshot_alignment.get("miners", []) if isinstance(snapshot_alignment, dict) else []
    per_identity: list[dict[str, object]] = []
    per_user_index: dict[str, dict[str, object]] = {}

    for miner in miners:
        if not isinstance(miner, dict):
            continue
        identity = str(miner.get("identity") or "")
        try:
            username = parse_identity(identity).username
        except ValueError:
            username = "unmapped"

        baseline_work = Decimal(str(miner.get("baseline_work") or "0"))
        current_work = Decimal(str(miner.get("current_work") or "0"))
        work_delta = Decimal(str(miner.get("work_delta") or "0"))

        per_identity.append(
            {
                "username": username,
                "identity": identity,
                "channel_id": miner.get("channel_id"),
                "baseline_work": _to_decimal_str(baseline_work),
                "current_work": _to_decimal_str(current_work),
                "work_delta": _to_decimal_str(work_delta),
                "formula": f"{_to_decimal_str(current_work)} - {_to_decimal_str(baseline_work)} = {_to_decimal_str(work_delta)}",
                "reset_detected": bool(miner.get("reset_detected", False)),
            }
        )

        user_row = per_user_index.setdefault(
            username,
            {
                "username": username,
                "identity_count": 0,
                "baseline_work_sum": Decimal("0"),
                "current_work_sum": Decimal("0"),
                "work_delta_sum": Decimal("0"),
            },
        )
        user_row["identity_count"] = int(user_row["identity_count"]) + 1
        user_row["baseline_work_sum"] = Decimal(str(user_row["baseline_work_sum"])) + baseline_work
        user_row["current_work_sum"] = Decimal(str(user_row["current_work_sum"])) + current_work
        user_row["work_delta_sum"] = Decimal(str(user_row["work_delta_sum"])) + work_delta

    per_user = [
        {
            "username": str(row["username"]),
            "identity_count": int(row["identity_count"]),
            "baseline_work_sum": _to_decimal_str(row["baseline_work_sum"]),
            "current_work_sum": _to_decimal_str(row["current_work_sum"]),
            "work_delta_sum": _to_decimal_str(row["work_delta_sum"]),
            "formula": (
                f"{_to_decimal_str(row['current_work_sum'])} - "
                f"{_to_decimal_str(row['baseline_work_sum'])} = {_to_decimal_str(row['work_delta_sum'])}"
            ),
        }
        for _, row in sorted(per_user_index.items(), key=lambda item: item[0])
    ]

    return {
        "source_metric": "accepted_work_total",
        "description": (
            "Work delta is calculated from stored metric snapshots, not read directly from a translator "
            "endpoint field named work_delta. For each identity/channel in the settlement window, the service "
            "subtracts baseline accepted_work_total from current accepted_work_total, then sums those positive deltas by user."
        ),
        "reset_rule": "If accepted_work_total goes backwards, it is treated as a reset and that negative step contributes 0.",
        "per_user": per_user,
        "per_identity": per_identity,
    }


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
def on_startup() -> None:
    global _SERVICE_STARTED_AT
    settings = load_settings()
    _SERVICE_STARTED_AT = datetime.now(UTC).replace(tzinfo=None)
    try:
        archived_log = rotate_payout_audit_log(settings.payout_audit_log_path)
    except OSError:
        archived_log = None

    if settings.enable_startup_reconciliation_hook:
        try:
            with _new_session() as session:
                run_startup_reconciliation_hook(session, settings)
        except Exception as exc:
            _write_scheduler_event(
                "startup_reconciliation_hook_failed",
                {
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )

    if not settings.scheduler_enabled:
        _write_scheduler_event(
            "scheduler_disabled",
            {
                "reason": "SCHEDULER_ENABLED is false",
                "raw_env_value": os.getenv("SCHEDULER_ENABLED"),
                "archived_log_path": archived_log,
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
            "archived_log_path": archived_log,
            "payout_interval_minutes": int(settings.payout_interval_minutes),
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
        result = _execute_settlement_cycle(force_settlement=False)
        _write_scheduler_event(
            "scheduler_cycle_completed",
            {
                "settlement": result.get("settlement", {}),
                "snapshots_created": result.get("snapshots_created", 0),
                "settlement_skipped": bool(result.get("settlement_skipped", False)),
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


@app.get("/audit/settlements")
def audit_settlements(limit: int = 120) -> dict:
    settings = load_settings()
    payload = read_recent_audit_entries(settings.payout_audit_log_path, limit=max(limit * 3, 500))

    attempts: list[dict[str, object]] = []
    scheduler_events: list[dict[str, object]] = []
    for entry in payload.get("entries", []):
        if "attempt_id" in entry:
            attempts.append(entry)
        elif entry.get("event_type"):
            scheduler_events.append(entry)

    attempts.sort(key=lambda row: str(row.get("attempted_at") or ""))

    settlement_ids = [
        _to_int((entry.get("settlement") or {}).get("settlement_id"))
        for entry in attempts
        if _to_int((entry.get("settlement") or {}).get("settlement_id")) > 0
    ]
    with _new_session() as session:
        blocks_by_settlement = _load_block_rows_by_settlement(session, settlement_ids)

    normalized: list[dict[str, object]] = []
    previous_payout_settlement_id: int | None = None
    previous_payout_contributions: list[dict[str, object]] = []

    for attempt in attempts:
        settlement = attempt.get("settlement", {})
        checks = attempt.get("checks", {})
        payout_rows = attempt.get("payout_rows", [])
        block_reward = attempt.get("block_reward", {})
        user_contributions = attempt.get("user_contributions", [])
        snapshot_alignment = attempt.get("snapshot_alignment", {})
        snapshot_total_shares = _to_int(snapshot_alignment.get("total_share_delta"))
        snapshot_total_work = _to_decimal_str(snapshot_alignment.get("total_work_delta") or "0")
        interval_ratio_rows = _build_interval_ratio_rows(user_contributions)
        contribution_window_start = attempt.get("contribution_window_start") or attempt.get("period_start")
        contribution_window_end = attempt.get("contribution_window_end") or attempt.get("period_end")

        payout_count = len(payout_rows)
        settlement_id = settlement.get("settlement_id")
        payout_user_breakdown = _payout_user_breakdown(payout_rows, user_contributions)
        block_rows = blocks_by_settlement.get(_to_int(settlement_id), [])

        previous_payout_comparison: list[dict[str, object]] = []
        if payout_count > 0:
            previous_payout_comparison = _compare_with_previous_payout(
                user_contributions,
                previous_payout_contributions,
            )

        normalized.append(
            {
                "attempt_id": attempt.get("attempt_id"),
                "attempted_at": attempt.get("attempted_at"),
                "period_start": attempt.get("period_start"),
                "period_end": attempt.get("period_end"),
                "contribution_window_start": contribution_window_start,
                "contribution_window_end": contribution_window_end,
                "settlement_id": settlement_id,
                "status": settlement.get("status"),
                "reward_mode": settlement.get("reward_mode"),
                "pool_reward_btc": settlement.get("pool_reward_btc"),
                "carry_btc": settlement.get("carry_btc"),
                "total_shares": settlement.get("total_shares", 0),
                "total_work": settlement.get("total_work"),
                "snapshot_total_shares": snapshot_total_shares,
                "snapshot_total_work": snapshot_total_work,
                "user_count": len(payout_rows),
                "payout_count": payout_count,
                "payout_total_btc": _to_decimal_str(_sum_payout_amount(payout_rows)),
                "unrewarded_user_count": checks.get("unrewarded_user_count", 0),
                "interval_blocks": int(block_reward.get("interval_blocks", 0) or 0),
                "computed_reward_btc": block_reward.get("computed_reward_btc", "0.00000000"),
                "settlement_reward_btc": block_reward.get("settlement_reward_btc", settlement.get("pool_reward_btc")),
                "block_rows": block_rows,
                "payout_user_breakdown": payout_user_breakdown,
                "interval_ratio_rows": interval_ratio_rows,
                "work_delta_explanation": _build_work_delta_explanation(snapshot_alignment),
                "last_payout_settlement_id": previous_payout_settlement_id,
                "last_payout_contributions": previous_payout_contributions,
                "payout_vs_last_payout": previous_payout_comparison,
                "raw": attempt,
            }
        )

        if payout_count > 0:
            previous_payout_settlement_id = _to_int(settlement_id)
            previous_payout_contributions = list(user_contributions)

    normalized = normalized[-limit:]
    normalized.sort(key=lambda row: str(row.get("attempted_at") or ""), reverse=True)
    scheduler_events = scheduler_events[-20:]

    return {
        "log_path": payload.get("log_path"),
        "exists": payload.get("exists", False),
        "entry_count": payload.get("entry_count", 0),
        "invalid_line_count": payload.get("invalid_line_count", 0),
        "scheduler_enabled": bool(settings.scheduler_enabled),
        "scheduler_interval_seconds": int(settings.scheduler_interval_seconds),
        "scheduler_events": scheduler_events,
        "settlements": normalized,
    }


@app.get("/audit/dashboard", response_class=HTMLResponse)
def audit_dashboard() -> HTMLResponse:
    html = """<!doctype html>
<html lang=\"en\">
<head>
    <meta charset=\"utf-8\" />
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
    <title>Payout Settlements Dashboard</title>
    <style>
        :root {
            --bg0: #f4f1ea;
            --bg1: #e8dcc8;
            --panel: #fffdfa;
            --ink: #1f1a15;
            --muted: #6a5c4d;
            --good: #1f7a4c;
            --warn: #8a5b00;
            --border: #d7c8b3;
            --accent: #0f5c78;
            --accent-soft: #d7e8ee;
            --shadow: 0 10px 30px rgba(34, 26, 15, 0.12);
        }
        * { box-sizing: border-box; }
        body {
            margin: 0;
            color: var(--ink);
            font-family: Georgia, "Times New Roman", serif;
            background:
                radial-gradient(1300px 700px at -10% -20%, #f8edd6 10%, transparent 60%),
                radial-gradient(900px 500px at 120% -10%, #d2e8ef 10%, transparent 55%),
                linear-gradient(180deg, var(--bg0), var(--bg1));
            min-height: 100vh;
        }
        .wrap {
            max-width: 1260px;
            margin: 0 auto;
            padding: 24px;
        }
        .top {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 12px;
            margin-bottom: 16px;
        }
        h1 {
            margin: 0;
            font-size: clamp(1.4rem, 2.7vw, 2rem);
            letter-spacing: 0.4px;
        }
        .muted { color: var(--muted); font-size: 0.95rem; }
        .btn {
            background: var(--accent);
            color: #fff;
            border: 0;
            border-radius: 10px;
            padding: 10px 14px;
            cursor: pointer;
            box-shadow: var(--shadow);
        }
        .kpis {
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 14px;
        }
        .kpi {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px 12px;
            box-shadow: var(--shadow);
            animation: rise 220ms ease;
        }
        .kpi b { display: block; font-size: 1.15rem; margin-top: 4px; }
        .layout {
            display: grid;
            grid-template-columns: 390px 1fr;
            gap: 12px;
            margin-bottom: 12px;
        }
        .panel {
            background: var(--panel);
            border: 1px solid var(--border);
            border-radius: 14px;
            box-shadow: var(--shadow);
            overflow: hidden;
            min-height: 65vh;
        }
        .panel h2 {
            margin: 0;
            font-size: 1rem;
            padding: 11px 12px;
            border-bottom: 1px solid var(--border);
            background: linear-gradient(90deg, #f9f1e5, #eef7fa);
        }
        .list {
            max-height: calc(65vh - 44px);
            overflow: auto;
        }
        .item {
            width: 100%;
            border: 0;
            border-left: 5px solid transparent;
            background: transparent;
            text-align: left;
            padding: 10px 12px;
            border-bottom: 1px solid #eee1d0;
            cursor: pointer;
        }
        .item:hover { background: #f9f5ee; }
        .item.active { background: var(--accent-soft); }
        .item.has-blocks { border-left-color: #1f7a4c; }
        .item.has-payouts { border-left-color: #0f5c78; }
        .item.has-blocks.has-payouts {
            border-left-color: #1f7a4c;
            background: linear-gradient(90deg, #eaf8f0, transparent 35%);
        }
        .line { display: flex; justify-content: space-between; gap: 6px; }
        .badge {
            display: inline-block;
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 0.78rem;
            border-radius: 999px;
            border: 1px solid var(--border);
            padding: 2px 8px;
            background: #faf4ea;
            color: #563e22;
        }
        .good { color: var(--good); }
        .warn { color: var(--warn); }
        .detail {
            padding: 14px;
            overflow: auto;
            max-height: calc(65vh - 44px);
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 13px;
            line-height: 1.45;
        }
        .detail-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 10px;
            margin-bottom: 12px;
        }
        .box {
            border: 1px solid var(--border);
            border-radius: 10px;
            padding: 10px;
            background: #fff;
        }
        pre {
            margin: 0;
            white-space: pre-wrap;
            word-break: break-word;
            background: #f9f6ef;
            border: 1px solid #eadfcd;
            border-radius: 10px;
            padding: 10px;
        }
        .small { font-size: 12px; color: var(--muted); }
        .events {
            padding: 10px 12px;
            max-height: 38vh;
            overflow: auto;
            display: grid;
            gap: 8px;
        }
        .event {
            border: 1px solid var(--border);
            border-radius: 10px;
            background: #fff;
            padding: 10px;
        }
        .event-top {
            display: flex;
            justify-content: space-between;
            gap: 8px;
            margin-bottom: 6px;
            align-items: center;
        }
        .event-type {
            font-family: ui-monospace, Menlo, Consolas, monospace;
            font-size: 12px;
            border: 1px solid var(--border);
            border-radius: 999px;
            padding: 2px 8px;
            background: #f6efe2;
            color: #4f391f;
        }
        @keyframes rise {
            from { transform: translateY(6px); opacity: 0; }
            to { transform: translateY(0); opacity: 1; }
        }
        @media (max-width: 980px) {
            .kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .layout { grid-template-columns: 1fr; }
            .panel { min-height: auto; }
            .list, .detail { max-height: 45vh; }
            .events { max-height: 45vh; }
        }
    </style>
</head>
<body>
    <div class=\"wrap\">
        <div class=\"top\">
            <div>
                <h1>Payout Settlements Dashboard</h1>
                <div class=\"muted\">Live view built from payout audit logs</div>
            </div>
            <label class=\"muted\" style=\"display:flex;align-items:center;gap:8px;\">
                <input type=\"checkbox\" id=\"rewardOnly\" />
                Reward/Payout only
            </label>
            <label class=\"muted\" style=\"display:flex;align-items:center;gap:8px;\">
                <input type=\"checkbox\" id=\"autoRefresh\" />
                Auto Refresh (10s)
            </label>
            <button class=\"btn\" id=\"refreshBtn\">Refresh</button>
        </div>

        <div class=\"kpis\" id=\"kpis\"></div>

        <div class=\"layout\">
            <section class=\"panel\">
                <h2>Recent Settlements</h2>
                <div class=\"list\" id=\"list\"></div>
            </section>
            <section class=\"panel\">
                <h2>Settlement Details</h2>
                <div class=\"detail\" id=\"detail\">Loading...</div>
            </section>
        </div>

        <section class=\"panel\">
            <h2>Scheduler & System Events</h2>
            <div class=\"events\" id=\"events\">Loading...</div>
        </section>
    </div>

    <script>
        const state = { data: null, selected: 0, autoRefreshTimer: null };

        function fmtNum(value) {
            if (value === null || value === undefined) return '-';
            const n = Number(value);
            if (Number.isNaN(n)) return String(value);
            return n.toLocaleString();
        }

        function fmtBtc(value) {
            if (value === null || value === undefined) return '0.00000000';
            return String(value);
        }

        function fmtMst(value) {
            if (!value) return '-';
            const utc = new Date(`${value}Z`);
            if (Number.isNaN(utc.getTime())) return value;
            const mst = new Date(utc.getTime() - 7 * 60 * 60 * 1000);
            return `${mst.toISOString().slice(0, 19).replace('T', ' ')} MST`;
        }

        function fmtPeriod(start, end) {
            return `${fmtMst(start)} -> ${fmtMst(end)}`;
        }

        function renderKpis() {
            const kpis = document.getElementById('kpis');
            const rows = (state.data && state.data.settlements) || [];
            const latest = rows[0] || null;
            const blocksInWindow = rows.reduce((acc, row) => acc + Number(row.interval_blocks || 0), 0);
            const payoutsInWindow = rows.reduce((acc, row) => acc + Number(row.payout_count || 0), 0);
            const html = [
                ['Settlements Loaded', fmtNum(rows.length)],
                ['Scheduler', state.data && state.data.scheduler_enabled ? 'Enabled' : 'Disabled'],
                ['Blocks in Window', fmtNum(blocksInWindow)],
                ['Last Settlement ID', latest ? fmtNum(latest.settlement_id) : '-'],
                ['Payout Rows in Window', fmtNum(payoutsInWindow)],
                ['Last Reward BTC', latest ? fmtBtc(latest.pool_reward_btc) : '0.00000000'],
                ['Last Unrewarded Users', latest ? fmtNum(latest.unrewarded_user_count) : '0'],
                ['Invalid Log Lines', fmtNum(state.data ? state.data.invalid_line_count : 0)],
            ];
            kpis.innerHTML = html.map(([k, v]) => `<div class=\"kpi\"><span class=\"muted\">${k}</span><b>${v}</b></div>`).join('');
        }

        function renderList() {
            const list = document.getElementById('list');
            const rows = (state.data && state.data.settlements) || [];
            const rewardOnly = Boolean(document.getElementById('rewardOnly').checked);
            const visibleRows = rewardOnly
                ? rows.filter((row) => Number(row.interval_blocks || 0) > 0 || Number(row.payout_count || 0) > 0)
                : rows;

            if (state.selected >= visibleRows.length) {
                state.selected = 0;
            }
            list.innerHTML = visibleRows.map((row, idx) => {
                const reward = Number(row.pool_reward_btc || 0);
                const hasBlocks = Number(row.interval_blocks || 0) > 0;
                const hasPayouts = Number(row.payout_count || 0) > 0;
                const parts = ['item'];
                if (idx === state.selected) parts.push('active');
                if (hasBlocks) parts.push('has-blocks');
                if (hasPayouts) parts.push('has-payouts');
                const cls = parts.join(' ');
                return `
                    <button class=\"${cls}\" data-idx=\"${idx}\">
                        <div class=\"line\"><strong>#${row.settlement_id || '-'}</strong><span class=\"badge\">${row.status || 'unknown'}</span></div>
                        <div class=\"line small\"><span>${fmtMst(row.attempted_at)}</span><span>${row.reward_mode || '-'}</span></div>
                        <div class=\"line small\"><span>${fmtPeriod(row.period_start, row.period_end)}</span></div>
                        <div class=\"line\"><span class=\"${reward > 0 ? 'good' : 'warn'}\">reward ${fmtBtc(row.pool_reward_btc)} BTC</span><span>payouts ${fmtNum(row.payout_count)}</span></div>
                        <div class=\"line small\"><span>blocks ${fmtNum(row.interval_blocks)}</span><span>unrewarded ${fmtNum(row.unrewarded_user_count)}</span></div>
                    </button>
                `;
            }).join('');

            [...list.querySelectorAll('.item')].forEach((el) => {
                el.addEventListener('click', () => {
                    state.selected = Number(el.getAttribute('data-idx'));
                    renderList();
                    renderDetail();
                });
            });
        }

        function renderDetail() {
            const panel = document.getElementById('detail');
            const rows = (state.data && state.data.settlements) || [];
            const rewardOnly = Boolean(document.getElementById('rewardOnly').checked);
            const visibleRows = rewardOnly
                ? rows.filter((row) => Number(row.interval_blocks || 0) > 0 || Number(row.payout_count || 0) > 0)
                : rows;
            const row = visibleRows[state.selected];
            if (!row) {
                panel.textContent = 'No settlement data available.';
                return;
            }
            const raw = row.raw || {};
                        const workDeltaExplanation = row.work_delta_explanation || {};
                        const workPerUser = workDeltaExplanation.per_user || [];
                        const workPerIdentity = workDeltaExplanation.per_identity || [];
            const intervalRatioRows = row.interval_ratio_rows || [];
            const payoutRows = raw.payout_rows || [];
            const unrewarded = (raw.checks && raw.checks.unrewarded_users) || [];
            const channels = (raw.block_reward && raw.block_reward.channels) || [];
            const userBreakdown = row.payout_user_breakdown || [];
            const compareWithLast = row.payout_vs_last_payout || [];
            const blockRows = row.block_rows || [];
            const blockReward = raw.block_reward || {};
            const snapshotAlignment = raw.snapshot_alignment || {};
            const snapshotRows = snapshotAlignment.miners || [];
            const latestSnapshotState = snapshotAlignment.latest_snapshot_state || [];
            const coverage = snapshotAlignment.coverage || {};
            const maturedStart = blockReward.matured_window_start ? fmtMst(blockReward.matured_window_start) : null;
            const maturedEnd = blockReward.matured_window_end ? fmtMst(blockReward.matured_window_end) : null;
            const maturedWindowStr = maturedStart && maturedEnd ? `${maturedStart} → ${maturedEnd}` : '—';
            const contributionWindowStr = fmtPeriod(row.contribution_window_start, row.contribution_window_end);
            const snapshotWindowShares = fmtNum(row.snapshot_total_shares || 0);
            const snapshotWindowWork = row.snapshot_total_work || '0.00000000';
            const payoutRatios = userBreakdown.map((entry) => ({
                username: entry.username,
                payout_fraction: entry.payout_fraction || '0',
                contribution_value: entry.contribution_value || '0.00000000',
                share_delta: entry.share_delta || 0,
                work_delta: entry.work_delta || '0.00000000',
                amount_btc: entry.amount_btc || '0.00000000',
                status: entry.status || '-',
            }));
            panel.innerHTML = `
                <div class="detail-grid">
                    <div class="box"><div class="small">Window Shares / Window Work (From Snapshots)</div><div>${snapshotWindowShares} / ${snapshotWindowWork}</div></div>
                    <div class=\"box\"><div class=\"small\">Attempt</div><div>${row.attempt_id || '-'}</div></div>
                    <div class=\"box\"><div class=\"small\">Settlement Period (MST)</div><div>${fmtPeriod(row.period_start, row.period_end)}</div></div>
                    <div class=\"box\"><div class=\"small\">Contribution Window (MST)</div><div>${contributionWindowStr}</div></div>
                    <div class=\"box\"><div class=\"small\">Matured Reward Window (MST)</div><div>${maturedWindowStr}</div></div>
                    <div class=\"box\"><div class=\"small\">Pool Reward BTC</div><div>${fmtBtc(row.pool_reward_btc)}</div></div>
                    <div class=\"box\"><div class=\"small\">Computed Reward BTC</div><div>${fmtBtc(row.computed_reward_btc)}</div></div>
                    <div class=\"box\"><div class=\"small\">Settlement Reward BTC</div><div>${fmtBtc(row.settlement_reward_btc)}</div></div>
                    <div class=\"box\"><div class=\"small\">Payout Total BTC</div><div>${fmtBtc(row.payout_total_btc)}</div></div>
                    <div class=\"box\"><div class=\"small\">Blocks Found This Cycle</div><div>${fmtNum(row.interval_blocks)}</div></div>
                    <div class=\"box\"><div class=\"small\">Linked Matured Blocks</div><div>${fmtNum(blockRows.length)}</div></div>
                    <div class=\"box\"><div class=\"small\">Snapshot Coverage</div><div>${fmtNum(coverage.snapshots_in_window || 0)} in-window / ${fmtNum(coverage.tracked_miners_total || 0)} miners</div></div>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">How Work Delta Is Calculated</div>
                    <pre>${JSON.stringify({
                        contribution_window_start: row.contribution_window_start || null,
                        contribution_window_end: row.contribution_window_end || null,
                        source_metric: workDeltaExplanation.source_metric || 'accepted_work_total',
                        description: workDeltaExplanation.description || 'current accepted_work_total - baseline accepted_work_total, summed by user',
                        reset_rule: workDeltaExplanation.reset_rule || 'negative steps are treated as counter resets and contribute 0',
                        coverage,
                        per_user: workPerUser,
                    }, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Snapshot Rows Used For Delta (Baseline -> Current)</div>
                    <pre>${JSON.stringify(snapshotRows, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Latest Snapshot State By Identity</div>
                    <pre>${JSON.stringify(latestSnapshotState, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">accepted_work_total By Identity In Contribution Window</div>
                    <pre>${JSON.stringify(workPerIdentity, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Payout Ratio By User</div>
                    <pre>${JSON.stringify(payoutRatios, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Interval Ratio From Snapshot Deltas (user_delta / total_delta)</div>
                    <pre>${JSON.stringify(intervalRatioRows, null, 2)}</pre>
                </div>
                <div class="box" style="margin-bottom:10px;">
                    <div class="small">Matured Blocks And Rewards</div>
                    <pre>${JSON.stringify(blockRows, null, 2)}</pre>
                </div>
                <div class=\"box\" style=\"margin-bottom:10px;\">
                    <div class=\"small\">Payout Split By User (this settlement)</div>
                    <pre>${JSON.stringify(userBreakdown, null, 2)}</pre>
                </div>
                <div class=\"box\" style=\"margin-bottom:10px;\">
                    <div class=\"small\">This Payout vs Last Payout Basis (share/work)</div>
                    <pre>${JSON.stringify(compareWithLast, null, 2)}</pre>
                </div>
                <div class=\"box\" style=\"margin-bottom:10px;\">
                    <div class=\"small\">Payout Rows</div>
                    <pre>${JSON.stringify(payoutRows, null, 2)}</pre>
                </div>
                <div class=\"box\" style=\"margin-bottom:10px;\">
                    <div class=\"small\">Unrewarded Users</div>
                    <pre>${JSON.stringify(unrewarded, null, 2)}</pre>
                </div>
                <div class=\"box\">
                    <div class=\"small\">Block Delta By Channel</div>
                    <pre>${JSON.stringify(channels, null, 2)}</pre>
                </div>
            `;
        }

        function renderEvents() {
            const panel = document.getElementById('events');
            const events = (state.data && state.data.scheduler_events) || [];
            if (!events.length) {
                panel.innerHTML = '<div class=\"muted\">No scheduler/system events found yet in the audit log.</div>';
                return;
            }

            const sorted = [...events].sort((a, b) => {
                return String(b.timestamp || '').localeCompare(String(a.timestamp || ''));
            });

            panel.innerHTML = sorted.map((event) => {
                const eventType = event.event_type || 'unknown_event';
                const eventTime = fmtMst(event.timestamp);
                const payload = event.payload || {};
                return `
                    <div class=\"event\">
                        <div class=\"event-top\">
                            <span class=\"event-type\">${eventType}</span>
                            <span class=\"small\">${eventTime}</span>
                        </div>
                        <pre>${JSON.stringify(payload, null, 2)}</pre>
                    </div>
                `;
            }).join('');
        }

        function updateAutoRefresh() {
            const enabled = Boolean(document.getElementById('autoRefresh').checked);
            if (state.autoRefreshTimer) {
                clearInterval(state.autoRefreshTimer);
                state.autoRefreshTimer = null;
            }
            if (enabled) {
                state.autoRefreshTimer = setInterval(() => {
                    loadData();
                }, 10000);
            }
        }

        async function loadData() {
            const detail = document.getElementById('detail');
            const eventsPanel = document.getElementById('events');
            detail.textContent = 'Loading latest settlements...';
            eventsPanel.textContent = 'Loading scheduler/system events...';
            const res = await fetch('/audit/settlements?limit=200');
            if (!res.ok) {
                detail.textContent = 'Failed to load settlements feed.';
                eventsPanel.textContent = 'Failed to load scheduler/system events.';
                return;
            }
            state.data = await res.json();
            state.selected = 0;
            renderKpis();
            renderList();
            renderDetail();
            renderEvents();
        }

        document.getElementById('refreshBtn').addEventListener('click', loadData);
        document.getElementById('autoRefresh').addEventListener('change', updateAutoRefresh);
        document.getElementById('rewardOnly').addEventListener('change', () => {
            state.selected = 0;
            renderList();
            renderDetail();
        });
        updateAutoRefresh();
        loadData();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html)


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
    return _execute_settlement_cycle(force_settlement=True)


def _execute_settlement_cycle(*, force_settlement: bool = False) -> dict:
    settings = load_settings()
    reward_mode = _normalize_reward_mode(settings.reward_mode)
    block_reward_btc = Decimal(str(settings.block_reward_btc or "1.87500000"))
    attempt_id = str(uuid.uuid4())

    with _new_session() as session:
        attempt_time = datetime.now(UTC).replace(tzinfo=None)
        interval_minutes = int(settings.payout_interval_minutes)
        interval_delta = timedelta(minutes=interval_minutes)
        latest_settlement = session.execute(
            select(Settlement).order_by(Settlement.period_end.desc(), Settlement.id.desc()).limit(1)
        ).scalar_one_or_none()
        next_settlement_due_at = None
        if latest_settlement is not None:
            next_settlement_due_at = latest_settlement.period_end + interval_delta
        elif _SERVICE_STARTED_AT is not None:
            next_settlement_due_at = _SERVICE_STARTED_AT + interval_delta

        block_reward_payload: dict[str, object] | None = None
        interval_blocks = 0
        block_delta_details: list[dict[str, int | bool]] = []

        reward_fetcher = None
        selected_snapshot_block_ids: list[int] = []
        selected_matured_hashes: list[str] = []

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
            snapshots_created = poll_metrics_once(session, settings.translator_metrics_url)

        should_settle = force_settlement
        if not should_settle:
            due_time_tolerance = timedelta(seconds=1)
            should_settle = (
                next_settlement_due_at is None
                or attempt_time + due_time_tolerance >= next_settlement_due_at
            )

        if not should_settle:
            sender_stats = process_payout_events(session, dry_run=settings.dry_run)
            response: dict[str, object] = {
                "snapshots_created": snapshots_created,
                "settlement": None,
                "settlement_skipped": True,
                "skip_reason": "payout_interval_not_elapsed",
                "next_settlement_due_at": next_settlement_due_at.isoformat()
                if next_settlement_due_at
                else None,
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

        if settings.enable_block_event_rewards:
            matured_start, matured_end = compute_matured_window(
                attempt_time,
                interval_minutes=interval_minutes,
                maturity_window_minutes=int(settings.maturity_window_minutes),
            )
            fetched_block_rows: list[dict[str, object]] = []
            if settings.translator_blocks_found_url:
                try:
                    fetched_block_rows = fetch_blocks_found_in_window(matured_start, matured_end)
                except PoolApiError:
                    fetched_block_rows = []

            if settings.enable_block_event_replay_hook:
                try:
                    replay_rows = run_block_event_replay_hook(session, matured_start, matured_end)
                except Exception:
                    replay_rows = []
                if replay_rows:
                    fetched_block_rows.extend(replay_rows)

            inserted_blocks = upsert_snapshot_blocks(
                session,
                fetched_block_rows,
                source_default="translator_blocks_api",
            )
            matured_rows = session.execute(
                select(SnapshotBlock)
                .where(
                    SnapshotBlock.found_at >= matured_start,
                    SnapshotBlock.found_at < matured_end,
                    SnapshotBlock.settlement_id.is_(None),
                )
                .order_by(SnapshotBlock.found_at.asc(), SnapshotBlock.id.asc())
            ).scalars().all()

            # Retry previously seen block hashes that still have no resolved reward,
            # even if their found_at is outside the current matured window.
            retry_rows = session.execute(
                select(SnapshotBlock)
                .where(
                    SnapshotBlock.settlement_id.is_not(None),
                    SnapshotBlock.found_at < matured_end,
                    or_(
                        SnapshotBlock.reward_sats.is_(None),
                        SnapshotBlock.reward_sats <= 0,
                    ),
                )
                .order_by(SnapshotBlock.found_at.asc(), SnapshotBlock.id.asc())
                .limit(1000)
            ).scalars().all()

            rows_by_hash: dict[str, SnapshotBlock] = {}
            for row in matured_rows:
                rows_by_hash[row.blockhash] = row
            for row in retry_rows:
                if row.blockhash not in rows_by_hash:
                    rows_by_hash[row.blockhash] = row

            selected_rows = list(rows_by_hash.values())
            current_matured_hashes = [row.blockhash for row in matured_rows if row.blockhash]
            current_matured_hash_set = set(current_matured_hashes)
            retry_hashes = [
                row.blockhash
                for row in selected_rows
                if row.blockhash and row.blockhash not in current_matured_hash_set
            ]

            selected_snapshot_block_ids = [int(row.id) for row in matured_rows]
            selected_matured_hashes = [row.blockhash for row in selected_rows if row.blockhash]

            rewards_sats_by_hash: dict[str, int] = {}
            if selected_matured_hashes:
                try:
                    rewards_sats_by_hash = fetch_block_rewards_by_hashes(selected_matured_hashes)
                except PoolApiError:
                    rewards_sats_by_hash = {}

            if settings.enable_reward_refetch_hook and selected_matured_hashes:
                try:
                    rewards_sats_by_hash = run_reward_refetch_hook(
                        selected_matured_hashes,
                        rewards_sats_by_hash,
                    )
                except Exception:
                    pass

            missing_reward_hashes = [
                blockhash
                for blockhash in selected_matured_hashes
                if blockhash not in rewards_sats_by_hash
            ]
            missing_current_matured_hashes = [
                blockhash
                for blockhash in current_matured_hashes
                if blockhash not in rewards_sats_by_hash
            ]
            reward_entries_complete = not missing_current_matured_hashes

            total_sats = 0
            now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
            for row in selected_rows:
                sats = int(rewards_sats_by_hash.get(row.blockhash, 0) or 0)
                total_sats += sats
                row.reward_sats = sats
                row.reward_fetched_at = now_utc_naive
                row.updated_at = now_utc_naive

            computed_reward = Decimal(total_sats) / Decimal("100000000")
            settlement_reward = computed_reward if reward_entries_complete else Decimal("0")
            reward_fetcher = lambda _start, _end: settlement_reward
            block_reward_payload = {
                "reward_mode": "block_events",
                "matured_window_start": matured_start.isoformat(),
                "matured_window_end": matured_end.isoformat(),
                "fetched_block_count": len(fetched_block_rows),
                "inserted_block_count": int(inserted_blocks),
                "matured_hash_count": len([row for row in matured_rows if row.blockhash]),
                "retry_hash_count": len(selected_matured_hashes),
                "retry_only_hash_count": len(retry_hashes),
                "computed_reward_btc": _to_decimal_str(computed_reward),
                "settlement_reward_btc": _to_decimal_str(settlement_reward),
                "reward_entries_complete": reward_entries_complete,
                "missing_reward_hash_count": len(missing_reward_hashes),
                "missing_current_matured_hash_count": len(missing_current_matured_hashes),
            }

            session.flush()

        settlement_kwargs = {
            "interval_minutes": settings.payout_interval_minutes,
            "payout_decimals": settings.payout_decimals,
        }
        if reward_fetcher is not None:
            settlement_kwargs["reward_fetcher"] = reward_fetcher
        if settings.enable_block_event_rewards:
            settlement_kwargs["defer_on_zero_reward"] = bool(settings.defer_on_zero_matured_reward)
            settlement_kwargs["use_work_accrual"] = True
            settlement_kwargs["work_window_start"] = matured_start
            settlement_kwargs["work_window_end"] = matured_end

        settlement_result = run_settlement(
            session,
            attempt_time,
            **settlement_kwargs,
        )

        if settings.enable_settlement_replay_hook:
            try:
                run_settlement_replay_hook(session, settlement_result)
            except Exception:
                pass

        if selected_snapshot_block_ids:
            rows_to_link = session.execute(
                select(SnapshotBlock).where(SnapshotBlock.id.in_(selected_snapshot_block_ids))
            ).scalars().all()
            now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
            for row in rows_to_link:
                row.settlement_id = settlement_result.settlement_id
                row.updated_at = now_utc_naive
            session.flush()

        sender_stats = process_payout_events(session, dry_run=settings.dry_run)

        audit_event = build_payout_audit_event(
            session,
            attempt_id=attempt_id,
            attempted_at=attempt_time,
            period_start=settlement_result.period_start,
            period_end=settlement_result.period_end,
            snapshots_created=snapshots_created,
            settlement_id=settlement_result.settlement_id,
            settlement_status=settlement_result.status,
            reward_mode=reward_mode,
            pool_reward_btc=settlement_result.pool_reward_btc,
            total_work_btc_basis=settlement_result.total_work,
            total_share_delta=settlement_result.total_shares,
            block_reward=block_reward_payload,
            contribution_window_start=settlement_kwargs.get("work_window_start"),
            contribution_window_end=settlement_kwargs.get("work_window_end"),
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
        "settlement_skipped": False,
        "settlement": {
            "settlement_id": settlement_result.settlement_id,
            "status": settlement_result.status,
            "period_start": settlement_result.period_start.isoformat(),
            "period_end": settlement_result.period_end.isoformat(),
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

    if block_reward_payload is not None:
        response["block_reward"] = block_reward_payload
    elif settings.translator_channels_url and reward_mode == "blocks":
        response["block_reward"] = {
            "reward_mode": "blocks",
            "block_reward_btc": _to_decimal_str(block_reward_btc),
            "interval_blocks": int(interval_blocks),
            "computed_reward_btc": _to_decimal_str(Decimal(interval_blocks) * block_reward_btc),
            "channels": block_delta_details,
        }

    return response

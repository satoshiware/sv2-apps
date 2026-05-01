from __future__ import annotations
"""Quick commands:

1) Activate venv
    source /Users/baveetsinghhora/Desktop/stratumv2/.venv/bin/activate

2) Reset DB + run static demo
    python demo_interval_run.py --mode static --reset-db --db-path ./demo_payouts.db

3) Reset DB + run live demo (continuous)
    python demo_interval_run.py --mode live --reset-db --db-path ./demo_live.db

4) Live demo with 1 cycle (quick check)
    python demo_interval_run.py --mode live --reset-db --loop-cycles 1
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import os
import sys
import argparse
import time
from collections import defaultdict

from sqlalchemy import select
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if (PROJECT_ROOT / ".env").exists():
    load_dotenv(PROJECT_ROOT / ".env")

from app.db import make_engine, make_session_factory, Base
from app.delta import compute_user_contribution_deltas
from app.models import MetricSnapshot, Settlement, User, UserPayout
from app.poller import (
    fetch_channel_payload,
    parse_channel_snapshots,
    parse_downstream_identity_by_channel,
    poll_channels_once,
)
from app.settlement import run_settlement


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _env_int(name: str, default: int) -> int:
    value = (os.getenv(name, "") or "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_decimal(name: str, default: str) -> Decimal:
    value = (os.getenv(name, default) or default).strip()
    return Decimal(value)


def _reset_db_if_requested(db_path: str, reset_db: bool) -> None:
    if not reset_db:
        return
    db_file = Path(db_path)
    if db_file.exists():
        db_file.unlink()
        print(f"Reset DB: removed existing file {db_file}")


def _extract_blocks_found_by_channel(payload: dict) -> dict[int, int]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}

    counters: dict[int, int] = {}
    for key in ("extended_channels", "standard_channels"):
        channels = data.get(key)
        if not isinstance(channels, list):
            continue
        for item in channels:
            if not isinstance(item, dict):
                continue
            try:
                channel_id = int(item.get("channel_id") or 0)
            except (TypeError, ValueError):
                continue
            if channel_id <= 0:
                continue

            try:
                blocks_found = int(item.get("blocks_found") or 0)
            except (TypeError, ValueError):
                blocks_found = 0

            counters[channel_id] = max(blocks_found, 0)

    return counters


def _compute_blocks_delta(
    previous_by_channel: dict[int, int] | None,
    current_by_channel: dict[int, int],
) -> tuple[int, list[tuple[int, int, int, int]]]:
    if previous_by_channel is None:
        details = sorted(
            ((channel_id, 0, current, 0) for channel_id, current in current_by_channel.items()),
            key=lambda x: x[0],
        )
        return 0, details

    delta_total = 0
    details: list[tuple[int, int, int, int]] = []

    for channel_id in sorted(set(previous_by_channel) | set(current_by_channel)):
        previous = int(previous_by_channel.get(channel_id, 0))
        current = int(current_by_channel.get(channel_id, 0))

        # Counter reset handling: negative jumps contribute 0 in this cycle.
        delta = current - previous if current >= previous else 0
        delta_total += delta
        details.append((channel_id, previous, current, delta))

    return delta_total, details


def _add_snapshot(
    session,
    *,
    created_at: datetime,
    channel_id: int,
    identity: str,
    shares_ack: int,
    work_sum: Decimal,
    shares_rejected: int = 0,
) -> None:
    session.add(
        MetricSnapshot(
            channel_id=channel_id,
            identity=identity,
            accepted_shares_total=shares_ack,
            accepted_work_total=work_sum,
            shares_rejected_total=shares_rejected,
            created_at=created_at,
        )
    )


def _seed_round_from_payloads(
    session,
    *,
    created_at: datetime,
    upstream_payload: dict,
    downstream_payload: dict,
) -> int:
    identities_by_channel = parse_downstream_identity_by_channel(downstream_payload)
    snapshots = parse_channel_snapshots(upstream_payload, identities_by_channel=identities_by_channel)
    for snapshot in snapshots:
        _add_snapshot(
            session,
            created_at=created_at,
            channel_id=int(snapshot.channel_id or 0),
            identity=snapshot.identity,
            shares_ack=snapshot.accepted_shares_total,
            work_sum=_d(snapshot.accepted_work_total),
            shares_rejected=snapshot.shares_rejected_total,
        )
    return len(snapshots)


def _print_snapshot_rows(session) -> None:
    rows = session.execute(
        select(MetricSnapshot)
        .order_by(MetricSnapshot.created_at.asc(), MetricSnapshot.channel_id.asc())
    ).scalars().all()

    print("\nStored snapshot rows:")
    print("snapshot_id | created_at | channel_id | identity | shares_ack_total | work_sum_total | shares_rejected_total")
    for row in rows:
        print(
            f"{row.id} | {row.created_at.isoformat()} | {int(row.channel_id or 0)} | {row.identity} | "
            f"{int(row.accepted_shares_total or 0)} | {Decimal(str(row.accepted_work_total or 0)):.8f} | "
            f"{int(row.shares_rejected_total or 0)}"
        )


def _print_user_work_delta_table(
    session,
    *,
    period_start: datetime,
    period_end: datetime,
) -> None:
    rows = session.execute(
        select(MetricSnapshot)
        .where(MetricSnapshot.created_at <= period_end)
        .order_by(MetricSnapshot.channel_id.asc(), MetricSnapshot.created_at.asc())
    ).scalars().all()

    by_channel: dict[int, list[MetricSnapshot]] = defaultdict(list)
    for row in rows:
        channel_id = int(row.channel_id or 0)
        if channel_id <= 0:
            continue
        by_channel[channel_id].append(row)

    by_user: dict[str, dict[str, Decimal]] = defaultdict(
        lambda: {
            "prev_work": Decimal("0"),
            "curr_work": Decimal("0"),
            "delta_work": Decimal("0"),
            "prev_shares": Decimal("0"),
            "curr_shares": Decimal("0"),
            "delta_shares": Decimal("0"),
        }
    )

    for _, snapshots in by_channel.items():
        baseline_row: MetricSnapshot | None = None
        in_window_rows: list[MetricSnapshot] = []

        for snapshot in snapshots:
            if snapshot.created_at < period_start:
                baseline_row = snapshot
                continue
            if snapshot.created_at <= period_end:
                in_window_rows.append(snapshot)

        if not in_window_rows:
            continue

        curr_row = in_window_rows[-1]
        prev_row = baseline_row or in_window_rows[0]

        username = curr_row.identity.split(".", 1)[0]

        prev_work = Decimal(str(prev_row.accepted_work_total or 0))
        curr_work = Decimal(str(curr_row.accepted_work_total or 0))
        delta_work = max(curr_work - prev_work, Decimal("0"))

        prev_shares = Decimal(str(prev_row.accepted_shares_total or 0))
        curr_shares = Decimal(str(curr_row.accepted_shares_total or 0))
        delta_shares = max(curr_shares - prev_shares, Decimal("0"))

        by_user[username]["prev_work"] += prev_work
        by_user[username]["curr_work"] += curr_work
        by_user[username]["delta_work"] += delta_work
        by_user[username]["prev_shares"] += prev_shares
        by_user[username]["curr_shares"] += curr_shares
        by_user[username]["delta_shares"] += delta_shares

    translator_prev_work = sum((v["prev_work"] for v in by_user.values()), Decimal("0"))
    translator_curr_work = sum((v["curr_work"] for v in by_user.values()), Decimal("0"))
    translator_delta_work = sum((v["delta_work"] for v in by_user.values()), Decimal("0"))

    print("\nPer-user work summary from settlement window (baseline -> current -> delta):")
    print(
        "username | prev_work_sum | curr_work_sum | delta_work | prev_shares | curr_shares | delta_shares"
    )
    for username in sorted(by_user):
        row = by_user[username]
        print(
            f"{username} | {row['prev_work']:.8f} | {row['curr_work']:.8f} | {row['delta_work']:.8f} | "
            f"{int(row['prev_shares'])} | {int(row['curr_shares'])} | {int(row['delta_shares'])}"
        )

    print(
        f"TOTAL | {translator_prev_work:.8f} | {translator_curr_work:.8f} | {translator_delta_work:.8f} | - | - | -"
    )


def run_demo(
    db_path: str = "./demo_payouts.db",
    interval_minutes: int = 90,
    reward_btc: Decimal = Decimal("0.01000000"),
    reset_db: bool = False,
) -> None:
    _reset_db_if_requested(db_path, reset_db)
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    period_end = datetime(2026, 4, 21, 12, 0, 0)
    period_start = period_end - timedelta(minutes=interval_minutes)

    baseline_time = period_start - timedelta(minutes=5)
    in_window_time = period_end - timedelta(seconds=30)

    upstream_round_1 = {
        "status": "ok",
        "configured": True,
        "data": {
            "extended_channels": [
                {
                    "channel_id": 2,
                    "user_identity": "baveetstudy.miner1",
                    "shares_acknowledged": 10,
                    "shares_rejected": 0,
                    "share_work_sum": 232827.0,
                },
                {
                    "channel_id": 3,
                    "user_identity": "baveetstudy.miner2",
                    "shares_acknowledged": 21,
                    "shares_rejected": 0,
                    "share_work_sum": 488936.0,
                },
            ],
            "standard_channels": [],
        },
    }

    upstream_round_2 = {
        "status": "ok",
        "configured": True,
        "data": {
            "extended_channels": [
                {
                    "channel_id": 2,
                    "user_identity": "baveetstudy.miner1",
                    "shares_acknowledged": 13,
                    "shares_rejected": 0,
                    "share_work_sum": 300000.0,
                },
                {
                    "channel_id": 3,
                    "user_identity": "baveetstudy.miner2",
                    "shares_acknowledged": 26,
                    "shares_rejected": 0,
                    "share_work_sum": 620000.0,
                },
            ],
            "standard_channels": [],
        },
    }

    downstream_payload = {
        "status": "ok",
        "configured": True,
        "data": {
            "offset": 0,
            "limit": 50,
            "total": 2,
            "items": [
                {
                    "client_id": 1,
                    "channel_id": 2,
                    "authorized_worker_name": "baveet.worker3",
                    "user_identity": "baveet.worker3",
                },
                {
                    "client_id": 2,
                    "channel_id": 3,
                    "authorized_worker_name": "Ben.Cust1",
                    "user_identity": "Ben.Cust1",
                },
            ],
        },
    }

    with Session() as session:
        created_round_1 = _seed_round_from_payloads(
            session,
            created_at=baseline_time,
            upstream_payload=upstream_round_1,
            downstream_payload=downstream_payload,
        )
        created_round_2 = _seed_round_from_payloads(
            session,
            created_at=in_window_time,
            upstream_payload=upstream_round_2,
            downstream_payload=downstream_payload,
        )
        session.commit()

        _print_snapshot_rows(session)
        _print_user_work_delta_table(session, period_start=period_start, period_end=period_end)

        result = run_settlement(
            session,
            now=period_end,
            interval_minutes=interval_minutes,
            payout_decimals=8,
            reward_fetcher=lambda _start, _end: reward_btc,
        )

        settlement = session.execute(
            select(Settlement).where(Settlement.id == result.settlement_id)
        ).scalar_one()

        contributions = compute_user_contribution_deltas(session, period_start, period_end)

        payouts = session.execute(
            select(UserPayout, User)
            .join(User, User.id == UserPayout.user_id)
            .where(UserPayout.settlement_id == settlement.id)
            .order_by(User.id.asc())
        ).all()

    print("\n=== Snapshot + Interval + Payout Demo ===")
    print(f"Database: {db_path}")
    print(f"Round-1 snapshots written: {created_round_1}")
    print(f"Round-2 snapshots written: {created_round_2}")
    print(f"Settlement ID: {settlement.id}")
    print(f"Interval: {settlement.period_start.isoformat()} -> {settlement.period_end.isoformat()}")
    print(f"Total translator work (delta, all miners): {_d(settlement.total_work):.8f}")
    print(f"Total translator shares (delta, all miners): {int(settlement.total_shares or 0)}")
    print(f"Reward R: {_d(settlement.pool_reward_btc):.8f} BTC")

    print("\nFinal payout-ready rows:")
    print(
        "payout_id | payout_row_id | user_id | username | interval_start | interval_end | "
        "user_share_delta | user_work_delta | payout_fraction | amount_btc | translator_total_work"
    )

    for payout, user in payouts:
        contribution = contributions.get(user.username)
        user_share_delta = contribution.share_delta if contribution else 0
        user_work_delta = contribution.work_delta if contribution else Decimal("0")
        print(
            f"{payout.settlement_id} | {payout.id} | {user.id} | {user.username} | "
            f"{settlement.period_start.isoformat()} | {settlement.period_end.isoformat()} | "
            f"{user_share_delta} | {user_work_delta:.8f} | {Decimal(str(payout.payout_fraction)):.12f} | "
            f"{Decimal(str(payout.amount_btc)):.8f} | {Decimal(str(settlement.total_work)):.8f}"
        )


def run_live_demo(
    *,
    db_path: str,
    payout_interval_minutes: int,
    snapshot_interval_seconds: int,
    loop_cycles: int,
    reward_btc: Decimal,
    upstream_url: str,
    downstream_url: str,
    timeout_seconds: int,
    bearer_token: str | None,
    reset_db: bool = False,
    reward_mode: str = "blocks",
    block_reward_btc: Decimal = Decimal("1.87500000"),
) -> None:
    _reset_db_if_requested(db_path, reset_db)
    engine = make_engine(db_path)
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)

    print("\n=== Live Snapshot + Interval + Payout Demo ===")
    print(f"Database: {db_path}")
    print(f"Snapshot cadence: every {snapshot_interval_seconds}s")
    print(f"Payout cadence: every {payout_interval_minutes} minute(s)")
    if loop_cycles == 0:
        print("Loop cycles: infinite (Ctrl-C to stop)")
    else:
        print(f"Loop cycles: {loop_cycles}")
    print(f"Reward mode: {reward_mode}")
    if reward_mode == "blocks":
        print(f"Block reward: {block_reward_btc:.8f} BTC per block")
    else:
        print(f"Manual reward per settlement: {reward_btc:.8f} BTC")

    last_settlement_time: datetime | None = None
    previous_blocks_found_by_channel: dict[int, int] | None = None
    blocks_accumulated_in_interval = 0
    cycle = 0

    try:
        while loop_cycles == 0 or cycle < loop_cycles:
            cycle += 1
            cycle_label = str(cycle) if loop_cycles == 0 else f"{cycle}/{loop_cycles}"
            with Session() as session:
                current_payload = fetch_channel_payload(
                    upstream_url,
                    timeout_seconds=timeout_seconds,
                    bearer_token=bearer_token,
                )
                current_snapshots = parse_channel_snapshots(current_payload)
                current_total_work = sum(
                    (Decimal(str(item.accepted_work_total)) for item in current_snapshots),
                    Decimal("0"),
                )
                current_blocks_found_by_channel = _extract_blocks_found_by_channel(current_payload)
                cycle_blocks_delta, block_delta_details = _compute_blocks_delta(
                    previous_blocks_found_by_channel,
                    current_blocks_found_by_channel,
                )
                previous_blocks_found_by_channel = current_blocks_found_by_channel
                blocks_accumulated_in_interval += cycle_blocks_delta

                created = poll_channels_once(
                    session,
                    upstream_url,
                    timeout_seconds=timeout_seconds,
                    downstream_url=downstream_url,
                    bearer_token=bearer_token,
                )

                now_utc = datetime.now(UTC).replace(tzinfo=None)
                should_settle = (
                    last_settlement_time is None
                    or (now_utc - last_settlement_time).total_seconds() >= payout_interval_minutes * 60
                )

                settlement = None
                contributions = {}
                payouts = []
                if should_settle:
                    period_end = now_utc
                    period_start = period_end - timedelta(minutes=payout_interval_minutes)

                    if reward_mode == "blocks":
                        interval_blocks = blocks_accumulated_in_interval
                        computed_reward = Decimal(interval_blocks) * block_reward_btc
                    else:
                        interval_blocks = 0
                        computed_reward = reward_btc

                    result = run_settlement(
                        session,
                        now=period_end,
                        interval_minutes=payout_interval_minutes,
                        payout_decimals=8,
                        reward_fetcher=lambda _start, _end: computed_reward,
                    )
                    last_settlement_time = period_end
                    blocks_accumulated_in_interval = 0

                    settlement = session.execute(
                        select(Settlement).where(Settlement.id == result.settlement_id)
                    ).scalar_one()

                    contributions = compute_user_contribution_deltas(session, period_start, period_end)
                    payouts = session.execute(
                        select(UserPayout, User)
                        .join(User, User.id == UserPayout.user_id)
                        .where(UserPayout.settlement_id == settlement.id)
                        .order_by(User.id.asc())
                    ).all()

                rows = session.execute(
                    select(MetricSnapshot)
                    .order_by(MetricSnapshot.channel_id.asc(), MetricSnapshot.created_at.asc())
                ).scalars().all()

                by_channel: dict[int, list[MetricSnapshot]] = defaultdict(list)
                for row in rows:
                    channel_id = int(row.channel_id or 0)
                    if channel_id > 0:
                        by_channel[channel_id].append(row)
                prev_total_work = Decimal("0")
                curr_total_work = Decimal("0")
                for snapshots in by_channel.values():
                    if not snapshots:
                        continue
                    curr_total_work += Decimal(str(snapshots[-1].accepted_work_total or 0))
                    if len(snapshots) >= 2:
                        prev_total_work += Decimal(str(snapshots[-2].accepted_work_total or 0))
                    else:
                        prev_total_work += Decimal(str(snapshots[-1].accepted_work_total or 0))

                print(f"\n--- Cycle {cycle_label} ---")
                print(f"Live snapshots written this cycle: {created}")
                print(f"Raw polled translator total work (current API read): {current_total_work:.8f}")
                print(f"Stored translator total work (previous snapshot): {prev_total_work:.8f}")
                print(f"Stored translator total work (current snapshot): {curr_total_work:.8f}")
                print(f"Observed snapshot-to-snapshot work delta: {(curr_total_work - prev_total_work):.8f}")
                print(f"Observed snapshot-to-snapshot blocks_found delta: {cycle_blocks_delta}")

                print("blocks_found by channel (channel_id | prev | curr | delta):")
                for channel_id, prev_blocks, curr_blocks, delta_blocks in block_delta_details:
                    print(f"{channel_id} | {prev_blocks} | {curr_blocks} | {delta_blocks}")
                print(f"Blocks accumulated for current payout window: {blocks_accumulated_in_interval}")

                _print_snapshot_rows(session)
                live_period_start = now_utc - timedelta(minutes=payout_interval_minutes)
                _print_user_work_delta_table(
                    session,
                    period_start=live_period_start,
                    period_end=now_utc,
                )

                if not should_settle:
                    wait_s = int(payout_interval_minutes * 60 - (now_utc - last_settlement_time).total_seconds())
                    print(f"Settlement skipped this cycle; next payout run in ~{max(wait_s, 0)}s")
                else:
                    print(f"Settlement ID: {settlement.id}")
                    print(f"Interval: {settlement.period_start.isoformat()} -> {settlement.period_end.isoformat()}")
                    if reward_mode == "blocks":
                        print(f"Blocks credited in interval: {interval_blocks}")
                        print(f"Computed reward (blocks * {block_reward_btc:.8f}): {computed_reward:.8f} BTC")
                    print(f"Total translator work (delta, all miners): {_d(settlement.total_work):.8f}")
                    print(f"Total translator shares (delta, all miners): {int(settlement.total_shares or 0)}")
                    print(f"Reward R: {_d(settlement.pool_reward_btc):.8f} BTC")

                    print("\nFinal payout-ready rows:")
                    print(
                        "payout_id | payout_row_id | user_id | username | interval_start | interval_end | "
                        "user_share_delta | user_work_delta | payout_fraction | amount_btc | translator_total_work"
                    )

                    if not payouts:
                        print("(no payout rows yet: counters may not have moved in this interval)")

                    for payout, user in payouts:
                        contribution = contributions.get(user.username)
                        user_share_delta = contribution.share_delta if contribution else 0
                        user_work_delta = contribution.work_delta if contribution else Decimal("0")
                        print(
                            f"{payout.settlement_id} | {payout.id} | {user.id} | {user.username} | "
                            f"{settlement.period_start.isoformat()} | {settlement.period_end.isoformat()} | "
                            f"{user_share_delta} | {user_work_delta:.8f} | {Decimal(str(payout.payout_fraction)):.12f} | "
                            f"{Decimal(str(payout.amount_btc)):.8f} | {Decimal(str(settlement.total_work)):.8f}"
                        )

            time.sleep(snapshot_interval_seconds)
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run snapshot->settlement->payout demo")
    parser.add_argument(
        "--mode",
        choices=["static", "live"],
        default="static",
        help="Use static embedded payloads or live API polling",
    )
    default_interval = _env_int("DEMO_PAYOUT_INTERVAL_MINUTES", _env_int("PAYOUT_INTERVAL_MINUTES", 5))
    default_snapshot_seconds = _env_int("DEMO_SNAPSHOT_INTERVAL_SECONDS", 60)
    default_cycles = _env_int("DEMO_LOOP_CYCLES", 0)
    default_reward = str(_env_decimal("DEMO_REWARD_BTC", "0.01000000"))
    default_block_reward = str(_env_decimal("DEMO_BLOCK_REWARD_BTC", "1.87500000"))
    default_reward_mode = (os.getenv("DEMO_REWARD_MODE", "blocks") or "blocks").strip().lower()
    if default_reward_mode not in {"manual", "blocks"}:
        default_reward_mode = "blocks"

    parser.add_argument("--db-path", default=os.getenv("DEMO_DB_PATH", "./demo_payouts.db"), help="SQLite DB path for demo run")
    parser.add_argument("--interval-minutes", type=int, default=default_interval, help="Settlement interval in minutes")
    parser.add_argument("--snapshot-interval-seconds", type=int, default=default_snapshot_seconds, help="Snapshot poll cadence in seconds (live mode)")
    parser.add_argument("--loop-cycles", type=int, default=default_cycles, help="How many live cycles to run (0 = run forever until Ctrl-C)")
    parser.add_argument("--reward-btc", default=default_reward, help="Reward R for this demo interval")
    parser.add_argument(
        "--upstream-url",
        default="http://192.168.38.155:8080/v1/translator/upstream/channels",
        help="Upstream channels API URL for live mode",
    )
    parser.add_argument(
        "--downstream-url",
        default="http://192.168.38.155:8080/v1/translator/downstreams",
        help="Downstreams API URL for live mode",
    )
    parser.add_argument("--timeout-seconds", type=int, default=10, help="HTTP timeout for live mode")
    parser.add_argument("--bearer-token", default="", help="Bearer token for protected translator APIs")
    parser.add_argument("--reset-db", action="store_true", help="Delete db-path file before running demo")
    parser.add_argument(
        "--reward-mode",
        choices=["manual", "blocks"],
        default=default_reward_mode,
        help="Reward source in live mode: manual fixed reward or blocks_found-based",
    )
    parser.add_argument(
        "--block-reward-btc",
        default=default_block_reward,
        help="BTC reward per found block when --reward-mode=blocks",
    )
    args = parser.parse_args()

    if args.mode == "live":
        bearer_token = args.bearer_token.strip() or os.getenv("TRANSLATOR_BEARER_TOKEN", "").strip() or None
        run_live_demo(
            db_path=args.db_path,
            payout_interval_minutes=args.interval_minutes,
            snapshot_interval_seconds=args.snapshot_interval_seconds,
            loop_cycles=args.loop_cycles,
            reward_btc=Decimal(str(args.reward_btc)),
            upstream_url=args.upstream_url,
            downstream_url=args.downstream_url,
            timeout_seconds=args.timeout_seconds,
            bearer_token=bearer_token,
            reset_db=args.reset_db,
            reward_mode=args.reward_mode,
            block_reward_btc=Decimal(str(args.block_reward_btc)),
        )
    else:
        run_demo(
            db_path=args.db_path,
            interval_minutes=args.interval_minutes,
            reward_btc=Decimal(str(args.reward_btc)),
            reset_db=args.reset_db,
        )

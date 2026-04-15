from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import load_settings
from app.delta import compute_user_share_deltas
from app.models import CarryState, Settlement, User, UserPayout
from app.pool_client import PoolApiError, fetch_pool_reward


@dataclass(frozen=True)
class SettlementResult:
    settlement_id: int
    status: str
    user_count: int
    total_shares: int
    pool_reward_btc: Decimal
    carry_btc: Decimal


def _q(value: Decimal, decimals: int) -> Decimal:
    quantum = Decimal("1").scaleb(-decimals)
    return value.quantize(quantum, rounding=ROUND_DOWN)


def _get_or_create_user(session: Session, username: str) -> User:
    user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if user is None:
        user = User(username=username)
        session.add(user)
        session.flush()
    return user


def _get_or_create_carry(session: Session, bucket: str = "default") -> CarryState:
    carry = session.execute(select(CarryState).where(CarryState.bucket == bucket)).scalar_one_or_none()
    if carry is None:
        carry = CarryState(bucket=bucket, carry_btc=0)
        session.add(carry)
        session.flush()
    return carry


def _result_from_existing_settlement(session: Session, settlement: Settlement) -> SettlementResult:
    payout_rows = session.execute(
        select(UserPayout).where(UserPayout.settlement_id == settlement.id)
    ).scalars().all()
    user_count = len(payout_rows)
    total_shares = sum(compute_user_share_deltas(session, settlement.period_start, settlement.period_end).values())

    carry = _get_or_create_carry(session)
    return SettlementResult(
        settlement_id=settlement.id,
        status=settlement.status,
        user_count=user_count,
        total_shares=total_shares,
        pool_reward_btc=Decimal(str(settlement.pool_reward_btc or 0)),
        carry_btc=Decimal(str(carry.carry_btc or 0)),
    )


def run_settlement(
    session: Session,
    now: datetime,
    *,
    interval_minutes: int | None = None,
    payout_decimals: int | None = None,
    reward_fetcher=fetch_pool_reward,
) -> SettlementResult:
    """Run one settlement cycle and persist settlement + user payouts."""
    settings = load_settings()
    interval = interval_minutes or settings.payout_interval_minutes
    decimals = payout_decimals or settings.payout_decimals

    period_end = now
    period_start = now - timedelta(minutes=interval)

    existing_settlement = session.execute(
        select(Settlement).where(
            Settlement.period_start == period_start,
            Settlement.period_end == period_end,
        )
    ).scalar_one_or_none()
    if existing_settlement is not None:
        return _result_from_existing_settlement(session, existing_settlement)

    settlement = Settlement(
        status="pending",
        period_start=period_start,
        period_end=period_end,
        pool_reward_btc=0,
    )
    session.add(settlement)
    session.flush()

    try:
        pool_reward = Decimal(
            str(
                reward_fetcher(
                    period_start,
                    period_end,
                )
            )
        )
    except PoolApiError:
        settlement.status = "blocked"
        session.commit()
        return SettlementResult(
            settlement_id=settlement.id,
            status=settlement.status,
            user_count=0,
            total_shares=0,
            pool_reward_btc=Decimal("0"),
            carry_btc=Decimal("0"),
        )

    pool_reward = _q(pool_reward, decimals)
    settlement.pool_reward_btc = pool_reward

    user_shares = compute_user_share_deltas(session, period_start, period_end)
    total_shares = sum(user_shares.values())

    carry = _get_or_create_carry(session)
    previous_carry = _q(Decimal(str(carry.carry_btc or 0)), decimals)
    distributable = _q(pool_reward + previous_carry, decimals)

    allocated_sum = Decimal("0")
    user_count = 0

    if total_shares > 0 and distributable > 0:
        for username, shares in sorted(user_shares.items(), key=lambda item: item[0]):
            if shares <= 0:
                continue

            user = _get_or_create_user(session, username)
            raw_amount = distributable * Decimal(shares) / Decimal(total_shares)
            payout_amount = _q(raw_amount, decimals)
            if payout_amount <= 0:
                continue

            payout = UserPayout(
                settlement_id=settlement.id,
                user_id=user.id,
                amount_btc=payout_amount,
                idempotency_key=f"settlement-{settlement.id}-user-{user.id}",
                status="pending",
            )
            session.add(payout)
            allocated_sum += payout_amount
            user_count += 1

    carry.carry_btc = _q(distributable - allocated_sum, decimals)
    settlement.status = "completed"
    session.commit()

    return SettlementResult(
        settlement_id=settlement.id,
        status=settlement.status,
        user_count=user_count,
        total_shares=total_shares,
        pool_reward_btc=pool_reward,
        carry_btc=Decimal(str(carry.carry_btc)),
    )

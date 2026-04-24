from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import load_settings
from app.delta import UserContribution, compute_user_contribution_deltas
from app.models import CarryState, Settlement, User, UserPayout
from app.pool_client import PoolApiError, fetch_pool_reward

ZERO = Decimal("0")


@dataclass(frozen=True)
class SettlementResult:
    settlement_id: int
    status: str
    user_count: int
    total_shares: int
    total_work: Decimal
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


def _summarize_contributions(
    user_contributions: dict[str, UserContribution],
) -> tuple[int, Decimal]:
    total_shares = sum(item.share_delta for item in user_contributions.values())
    total_work = sum((item.work_delta for item in user_contributions.values()), ZERO)
    return total_shares, total_work


def _result_from_existing_settlement(session: Session, settlement: Settlement) -> SettlementResult:
    payout_rows = session.execute(
        select(UserPayout).where(UserPayout.settlement_id == settlement.id)
    ).scalars().all()
    user_count = len(payout_rows)

    carry = _get_or_create_carry(session)
    return SettlementResult(
        settlement_id=settlement.id,
        status=settlement.status,
        user_count=user_count,
        total_shares=int(settlement.total_shares or 0),
        total_work=Decimal(str(settlement.total_work or 0)),
        pool_reward_btc=Decimal(str(settlement.pool_reward_btc or 0)),
        carry_btc=Decimal(str(carry.carry_btc or 0)),
    )


def _build_allocation_rows(
    user_contributions: dict[str, UserContribution],
    distributable: Decimal,
    decimals: int,
) -> list[dict[str, Decimal | str]]:
    positive_work = {
        username: contribution.work_delta
        for username, contribution in user_contributions.items()
        if contribution.work_delta > ZERO
    }
    if positive_work:
        basis = positive_work
    else:
        basis = {
            username: Decimal(contribution.share_delta)
            for username, contribution in user_contributions.items()
            if contribution.share_delta > 0
        }

    basis_total = sum(basis.values(), ZERO)
    if basis_total <= ZERO or distributable <= ZERO:
        return []

    rows: list[dict[str, Decimal | str]] = []
    for username, basis_value in sorted(basis.items(), key=lambda item: item[0]):
        raw_amount = distributable * basis_value / basis_total
        rows.append(
            {
                "username": username,
                "basis_value": _q(basis_value, decimals),
                "payout_fraction": (basis_value / basis_total),
                "payout_amount": _q(raw_amount, decimals),
            }
        )

    allocated_sum = sum((Decimal(str(row["payout_amount"])) for row in rows), ZERO)
    remainder = _q(distributable - allocated_sum, decimals)
    if remainder > ZERO and rows:
        target_index = max(
            range(len(rows)),
            key=lambda i: (
                Decimal(str(rows[i]["basis_value"])),
                str(rows[i]["username"]),
            ),
        )
        rows[target_index]["payout_amount"] = _q(
            Decimal(str(rows[target_index]["payout_amount"])) + remainder,
            decimals,
        )

    return rows


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
        total_shares=0,
        total_work=0,
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
            total_work=ZERO,
            pool_reward_btc=ZERO,
            carry_btc=ZERO,
        )

    pool_reward = _q(pool_reward, decimals)
    settlement.pool_reward_btc = pool_reward

    user_contributions = compute_user_contribution_deltas(session, period_start, period_end)
    total_shares, total_work = _summarize_contributions(user_contributions)
    settlement.total_shares = total_shares
    settlement.total_work = _q(total_work, decimals)

    carry = _get_or_create_carry(session)
    previous_carry = _q(Decimal(str(carry.carry_btc or 0)), decimals)
    distributable = _q(pool_reward + previous_carry, decimals)

    allocated_sum = ZERO
    user_count = 0

    allocation_rows = _build_allocation_rows(user_contributions, distributable, decimals)
    for row in allocation_rows:
        payout_amount = Decimal(str(row["payout_amount"]))
        if payout_amount <= ZERO:
            continue

        username = str(row["username"])
        user = _get_or_create_user(session, username)
        payout = UserPayout(
            settlement_id=settlement.id,
            user_id=user.id,
            contribution_value=Decimal(str(row["basis_value"])),
            payout_fraction=_q(Decimal(str(row["payout_fraction"])), 12),
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
        total_work=_q(total_work, decimals),
        pool_reward_btc=pool_reward,
        carry_btc=Decimal(str(carry.carry_btc)),
    )

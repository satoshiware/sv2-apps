from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import load_settings
from app.delta import UserContribution, compute_user_contribution_deltas
from app.models import (
    CarryState,
    PendingBlockReward,
    Settlement,
    User,
    UserPayout,
    WorkAccrualBucket,
    WorkEpoch,
    WorkEpochUserBasis,
)
from app.pool_client import PoolApiError, fetch_pool_reward

ZERO = Decimal("0")


@dataclass(frozen=True)
class SettlementResult:
    settlement_id: int
    status: str
    user_count: int
    period_start: datetime
    period_end: datetime
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


def _get_or_create_accrual_bucket(session: Session, user: User) -> WorkAccrualBucket:
    bucket = session.execute(
        select(WorkAccrualBucket).where(WorkAccrualBucket.user_id == user.id)
    ).scalar_one_or_none()
    if bucket is None:
        bucket = WorkAccrualBucket(user_id=user.id, accumulated_work=ZERO)
        session.add(bucket)
        session.flush()
    return bucket


def _add_work_to_accrual(
    session: Session,
    user_contributions: dict[str, UserContribution],
    now: datetime,
    decimals: int,
) -> None:
    """Add current interval work deltas into WorkAccrualBucket for all users with positive work."""
    now_naive = now.replace(tzinfo=None) if hasattr(now, "tzinfo") and now.tzinfo else now
    for username, contribution in user_contributions.items():
        if contribution.work_delta <= ZERO:
            continue
        user = _get_or_create_user(session, username)
        accrual = _get_or_create_accrual_bucket(session, user)
        accrual.accumulated_work = _q(
            Decimal(str(accrual.accumulated_work or 0)) + contribution.work_delta,
            decimals,
        )
        accrual.updated_at = now_naive
    session.flush()


def _get_or_create_work_epoch(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
) -> WorkEpoch:
    epoch = session.execute(
        select(WorkEpoch).where(
            WorkEpoch.window_start == window_start,
            WorkEpoch.window_end == window_end,
        )
    ).scalar_one_or_none()
    if epoch is None:
        now_naive = datetime.now().replace(microsecond=0)
        epoch = WorkEpoch(
            window_start=window_start,
            window_end=window_end,
            status="open",
            accrued_applied=False,
            created_at=now_naive,
            updated_at=now_naive,
        )
        session.add(epoch)
        session.flush()
    return epoch


def _ensure_work_epoch_user_basis(
    session: Session,
    *,
    epoch: WorkEpoch,
    user_contributions: dict[str, UserContribution],
    decimals: int,
) -> None:
    existing = session.execute(
        select(WorkEpochUserBasis).where(WorkEpochUserBasis.epoch_id == epoch.id)
    ).scalars().all()
    if existing:
        return

    for username, contribution in user_contributions.items():
        if contribution.share_delta <= 0 and contribution.work_delta <= ZERO:
            continue
        user = _get_or_create_user(session, username)
        row = WorkEpochUserBasis(
            epoch_id=epoch.id,
            user_id=user.id,
            share_delta=int(contribution.share_delta),
            work_delta=_q(Decimal(str(contribution.work_delta)), decimals),
        )
        session.add(row)
    session.flush()


def accrue_work_epoch_once(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    decimals: int,
) -> tuple[WorkEpoch, int, Decimal, bool]:
    """Ensure epoch+basis exist and apply accrual at most once.

    Returns: (epoch, total_shares, total_work, accrued_applied_now)
    """
    user_contributions = compute_user_contribution_deltas(session, window_start, window_end)
    total_shares, total_work = _summarize_contributions(user_contributions)
    epoch = _get_or_create_work_epoch(
        session,
        window_start=window_start,
        window_end=window_end,
    )
    _ensure_work_epoch_user_basis(
        session,
        epoch=epoch,
        user_contributions=user_contributions,
        decimals=decimals,
    )

    applied_now = False
    if not bool(epoch.accrued_applied):
        _add_work_to_accrual(session, user_contributions, now, decimals)
        epoch.accrued_applied = True
        epoch.updated_at = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now
        session.flush()
        applied_now = True

    return epoch, total_shares, total_work, applied_now


def accrue_work_window(
    session: Session,
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime,
    decimals: int,
) -> tuple[int, Decimal]:
    """Accrue work for a specific window without creating a settlement row.

    Returns (total_shares, total_work) for visibility/debugging.
    """
    user_contributions = compute_user_contribution_deltas(session, window_start, window_end)
    total_shares, total_work = _summarize_contributions(user_contributions)
    _add_work_to_accrual(session, user_contributions, now, decimals)
    return total_shares, total_work


def _apply_accrual_to_contributions(
    session: Session,
    user_contributions: dict[str, UserContribution],
) -> dict[str, UserContribution]:
    """Return a new contributions dict where each user's work_delta is increased by their accrued work."""
    enhanced: dict[str, UserContribution] = {
        username: UserContribution(
            username=username,
            share_delta=contribution.share_delta,
            work_delta=contribution.work_delta,
        )
        for username, contribution in user_contributions.items()
    }

    accrual_rows = session.execute(select(WorkAccrualBucket)).scalars().all()
    for bucket in accrual_rows:
        accrued = Decimal(str(bucket.accumulated_work or 0))
        if accrued <= ZERO:
            continue

        user = session.execute(select(User).where(User.id == bucket.user_id)).scalar_one_or_none()
        if user is None:
            continue

        existing = enhanced.get(user.username)
        if existing is None:
            enhanced[user.username] = UserContribution(
                username=user.username,
                share_delta=0,
                work_delta=accrued,
            )
            continue

        enhanced[user.username] = UserContribution(
            username=user.username,
            share_delta=existing.share_delta,
            work_delta=existing.work_delta + accrued,
        )

    return enhanced


def _clear_accrual_for_users(
    session: Session,
    usernames: list[str],
    now: datetime,
) -> None:
    """Zero out accumulated work for users who received payouts in this cycle."""
    now_naive = now.replace(tzinfo=None) if hasattr(now, "tzinfo") and now.tzinfo else now
    for username in usernames:
        user = session.execute(select(User).where(User.username == username)).scalar_one_or_none()
        if user is None:
            continue
        bucket = session.execute(
            select(WorkAccrualBucket).where(WorkAccrualBucket.user_id == user.id)
        ).scalar_one_or_none()
        if bucket is not None and Decimal(str(bucket.accumulated_work or 0)) > ZERO:
            bucket.accumulated_work = ZERO
            bucket.updated_at = now_naive
    session.flush()


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
        period_start=settlement.period_start,
        period_end=settlement.period_end,
        total_shares=int(settlement.total_shares or 0),
        total_work=Decimal(str(settlement.total_work or 0)),
        pool_reward_btc=Decimal(str(settlement.pool_reward_btc or 0)),
        carry_btc=Decimal(str(carry.carry_btc or 0)),
    )


def _build_allocation_rows(
    user_contributions: dict[str, UserContribution],
    distributable: Decimal,
    decimals: int,
    *,
    strict_work_basis_required: bool = False,
) -> list[dict[str, Decimal | str]]:
    positive_work = {
        username: contribution.work_delta
        for username, contribution in user_contributions.items()
        if contribution.work_delta > ZERO
    }
    if positive_work:
        basis = positive_work
    else:
        if strict_work_basis_required:
            return []
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
    defer_on_zero_reward: bool = False,
    use_work_accrual: bool = False,
    work_window_start: datetime | None = None,
    work_window_end: datetime | None = None,
) -> SettlementResult:
    """Run one settlement cycle and persist settlement + user payouts.

    work_window_start / work_window_end — when provided (e.g. the matured block
    window), use these bounds for computing share/work contribution deltas instead
    of the settlement period bounds.  This keeps contribution attribution aligned
    with where the rewarded blocks were actually found.
    """
    settings = load_settings()
    interval = interval_minutes or settings.payout_interval_minutes
    decimals = payout_decimals or settings.payout_decimals

    latest_settlement = session.execute(
        select(Settlement).order_by(Settlement.period_end.desc(), Settlement.id.desc()).limit(1)
    ).scalar_one_or_none()

    period_end = now
    if latest_settlement is None:
        period_start = now - timedelta(minutes=interval)
    else:
        if period_end <= latest_settlement.period_end:
            return _result_from_existing_settlement(session, latest_settlement)
        period_start = latest_settlement.period_end
        # Cap the settlement window to at most interval_minutes to prevent
        # scheduler jitter from accumulating a larger-than-T contribution window.
        capped_start = period_end - timedelta(minutes=interval)
        if period_start < capped_start:
            period_start = capped_start

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
            period_start=period_start,
            period_end=period_end,
            total_shares=0,
            total_work=ZERO,
            pool_reward_btc=ZERO,
            carry_btc=ZERO,
        )

    pool_reward = _q(pool_reward, decimals)
    settlement.pool_reward_btc = pool_reward

    contrib_start = work_window_start if work_window_start is not None else period_start
    contrib_end = work_window_end if work_window_end is not None else period_end
    user_contributions = compute_user_contribution_deltas(session, contrib_start, contrib_end)
    total_shares, total_work = _summarize_contributions(user_contributions)
    settlement.total_shares = total_shares
    settlement.total_work = _q(total_work, decimals)

    # --- Deferred branch: no reward this interval ---
    if defer_on_zero_reward and pool_reward <= ZERO:
        if use_work_accrual:
            _add_work_to_accrual(session, user_contributions, now, decimals)
        settlement.status = "deferred"
        session.commit()
        carry = _get_or_create_carry(session)
        return SettlementResult(
            settlement_id=settlement.id,
            status="deferred",
            user_count=0,
            period_start=period_start,
            period_end=period_end,
            total_shares=total_shares,
            total_work=_q(total_work, decimals),
            pool_reward_btc=ZERO,
            carry_btc=_q(Decimal(str(carry.carry_btc or 0)), decimals),
        )

    # --- Rewarded branch: merge accrued work into allocation basis ---
    if use_work_accrual:
        user_contributions = _apply_accrual_to_contributions(session, user_contributions)

    _, effective_total_work = _summarize_contributions(user_contributions)

    carry = _get_or_create_carry(session)
    previous_carry = _q(Decimal(str(carry.carry_btc or 0)), decimals)
    distributable = _q(pool_reward + previous_carry, decimals)

    if settings.strict_work_basis_required and distributable > ZERO and effective_total_work <= ZERO:
        settlement.status = "blocked"
        session.commit()
        return SettlementResult(
            settlement_id=settlement.id,
            status=settlement.status,
            user_count=0,
            period_start=period_start,
            period_end=period_end,
            total_shares=total_shares,
            total_work=_q(effective_total_work, decimals),
            pool_reward_btc=pool_reward,
            carry_btc=previous_carry,
        )

    allocated_sum = ZERO
    user_count = 0
    settled_usernames: list[str] = []

    allocation_rows = _build_allocation_rows(
        user_contributions,
        distributable,
        decimals,
        strict_work_basis_required=settings.strict_work_basis_required,
    )
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
        settled_usernames.append(username)

    if use_work_accrual and settled_usernames:
        _clear_accrual_for_users(session, settled_usernames, now)

    carry.carry_btc = _q(distributable - allocated_sum, decimals)
    settlement.status = "completed"
    session.commit()

    return SettlementResult(
        settlement_id=settlement.id,
        status=settlement.status,
        user_count=user_count,
        period_start=period_start,
        period_end=period_end,
        total_shares=total_shares,
        total_work=_q(total_work, decimals),
        pool_reward_btc=pool_reward,
        carry_btc=Decimal(str(carry.carry_btc)),
    )


def run_epoch_group_settlement(
    session: Session,
    now: datetime,
    *,
    epoch_reward_sats: dict[int, int],
    interval_minutes: int | None = None,
    payout_decimals: int | None = None,
) -> SettlementResult:
    """Settle resolved rewards grouped by work epoch basis.

    Each epoch reward is allocated independently using that epoch's frozen
    user basis, then amounts are aggregated per user into one settlement.
    """
    settings = load_settings()
    interval = interval_minutes or settings.payout_interval_minutes
    decimals = payout_decimals or settings.payout_decimals

    latest_settlement = session.execute(
        select(Settlement).order_by(Settlement.period_end.desc(), Settlement.id.desc()).limit(1)
    ).scalar_one_or_none()

    period_end = now
    if latest_settlement is None:
        period_start = now - timedelta(minutes=interval)
    else:
        if period_end <= latest_settlement.period_end:
            return _result_from_existing_settlement(session, latest_settlement)
        period_start = latest_settlement.period_end
        capped_start = period_end - timedelta(minutes=interval)
        if period_start < capped_start:
            period_start = capped_start

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

    reward_btc_by_epoch: dict[int, Decimal] = {
        int(epoch_id): _q(Decimal(int(sats)) / Decimal("100000000"), decimals)
        for epoch_id, sats in epoch_reward_sats.items()
        if int(sats) > 0
    }
    total_epoch_reward = _q(sum(reward_btc_by_epoch.values(), ZERO), decimals)

    carry = _get_or_create_carry(session)
    previous_carry = _q(Decimal(str(carry.carry_btc or 0)), decimals)
    settlement.pool_reward_btc = total_epoch_reward

    if total_epoch_reward <= ZERO and previous_carry <= ZERO:
        settlement.status = "deferred"
        session.commit()
        return SettlementResult(
            settlement_id=settlement.id,
            status="deferred",
            user_count=0,
            period_start=period_start,
            period_end=period_end,
            total_shares=0,
            total_work=ZERO,
            pool_reward_btc=ZERO,
            carry_btc=_q(Decimal(str(carry.carry_btc or 0)), decimals),
        )

    if previous_carry > ZERO and reward_btc_by_epoch:
        first_epoch = sorted(reward_btc_by_epoch.keys())[0]
        reward_btc_by_epoch[first_epoch] = _q(reward_btc_by_epoch[first_epoch] + previous_carry, decimals)

    user_amounts: dict[str, Decimal] = {}
    user_basis_totals: dict[str, Decimal] = {}
    total_shares = 0
    total_work = ZERO

    for epoch_id in sorted(reward_btc_by_epoch.keys()):
        epoch_reward = reward_btc_by_epoch[epoch_id]
        if epoch_reward <= ZERO:
            continue

        basis_rows = session.execute(
            select(WorkEpochUserBasis, User)
            .join(User, User.id == WorkEpochUserBasis.user_id)
            .where(WorkEpochUserBasis.epoch_id == epoch_id)
        ).all()

        epoch_contributions: dict[str, UserContribution] = {}
        for basis_row, user in basis_rows:
            share_delta = int(basis_row.share_delta or 0)
            work_delta = Decimal(str(basis_row.work_delta or 0))
            if share_delta <= 0 and work_delta <= ZERO:
                continue
            epoch_contributions[user.username] = UserContribution(
                username=user.username,
                share_delta=share_delta,
                work_delta=work_delta,
            )
            total_shares += share_delta
            total_work += work_delta

        allocation_rows = _build_allocation_rows(
            epoch_contributions,
            epoch_reward,
            decimals,
            strict_work_basis_required=settings.strict_work_basis_required,
        )
        for row in allocation_rows:
            username = str(row["username"])
            payout_amount = Decimal(str(row["payout_amount"]))
            basis_value = Decimal(str(row["basis_value"]))
            if payout_amount <= ZERO:
                continue
            user_amounts[username] = _q(user_amounts.get(username, ZERO) + payout_amount, decimals)
            user_basis_totals[username] = _q(user_basis_totals.get(username, ZERO) + basis_value, decimals)

    distributable = _q(sum(reward_btc_by_epoch.values(), ZERO), decimals)

    if settings.strict_work_basis_required and distributable > ZERO and total_work <= ZERO:
        settlement.status = "blocked"
        session.commit()
        return SettlementResult(
            settlement_id=settlement.id,
            status=settlement.status,
            user_count=0,
            period_start=period_start,
            period_end=period_end,
            total_shares=int(total_shares),
            total_work=_q(total_work, decimals),
            pool_reward_btc=Decimal(str(settlement.pool_reward_btc or 0)),
            carry_btc=previous_carry,
        )

    allocated_sum = ZERO
    user_count = 0
    settled_usernames: list[str] = []

    for username in sorted(user_amounts.keys()):
        payout_amount = _q(user_amounts[username], decimals)
        if payout_amount <= ZERO:
            continue
        user = _get_or_create_user(session, username)
        payout_fraction = (payout_amount / distributable) if distributable > ZERO else ZERO
        payout = UserPayout(
            settlement_id=settlement.id,
            user_id=user.id,
            contribution_value=_q(user_basis_totals.get(username, ZERO), decimals),
            payout_fraction=_q(payout_fraction, 12),
            amount_btc=payout_amount,
            idempotency_key=f"settlement-{settlement.id}-user-{user.id}",
            status="pending",
        )
        session.add(payout)
        allocated_sum += payout_amount
        user_count += 1
        settled_usernames.append(username)

    if settled_usernames:
        _clear_accrual_for_users(session, settled_usernames, now)

    carry.carry_btc = _q(distributable - allocated_sum, decimals)
    settlement.status = "completed"
    settlement.total_shares = int(total_shares)
    settlement.total_work = _q(total_work, decimals)
    session.flush()

    paid_epoch_ids = set(reward_btc_by_epoch.keys())
    if paid_epoch_ids:
        pending_rows = session.execute(
            select(PendingBlockReward).where(PendingBlockReward.epoch_id.in_(paid_epoch_ids))
        ).scalars().all()
        for row in pending_rows:
            if int(row.reward_sats or 0) > 0 and not bool(row.paid):
                row.paid = True
                row.paid_settlement_id = settlement.id
                row.updated_at = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now

        for epoch_id in paid_epoch_ids:
            epoch = session.execute(select(WorkEpoch).where(WorkEpoch.id == epoch_id)).scalar_one_or_none()
            if epoch is None:
                continue
            unpaid_count = session.execute(
                select(PendingBlockReward)
                .where(
                    PendingBlockReward.epoch_id == epoch_id,
                    PendingBlockReward.paid.is_(False),
                )
            ).scalars().all()
            epoch.status = "paid" if not unpaid_count else "partially_paid"
            epoch.updated_at = now.replace(tzinfo=None) if getattr(now, "tzinfo", None) else now

    session.commit()

    return SettlementResult(
        settlement_id=settlement.id,
        status=settlement.status,
        user_count=user_count,
        period_start=period_start,
        period_end=period_end,
        total_shares=int(settlement.total_shares or 0),
        total_work=Decimal(str(settlement.total_work or 0)),
        pool_reward_btc=Decimal(str(settlement.pool_reward_btc or 0)),
        carry_btc=Decimal(str(carry.carry_btc or 0)),
    )

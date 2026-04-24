from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mapping import parse_identity
from app.models import MetricSnapshot

ZERO = Decimal("0")


@dataclass(frozen=True)
class IdentityDelta:
    identity: str
    share_delta: int
    reset_count: int
    sample_count: int
    work_delta: Decimal = ZERO
    channel_count: int = 1


@dataclass(frozen=True)
class UserContribution:
    username: str
    share_delta: int
    work_delta: Decimal


def _to_decimal(value: object | None) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value))


def compute_counter_delta(samples: list[int]) -> tuple[int, int]:
    """Return positive delta and reset count for a monotonic counter series.

    A negative jump is treated as a counter reset and does not subtract from
    earned shares.
    """
    if len(samples) < 2:
        return 0, 0

    delta = 0
    resets = 0
    previous = samples[0]

    for current in samples[1:]:
        if current >= previous:
            delta += current - previous
        else:
            resets += 1
        previous = current

    return delta, resets


def compute_decimal_counter_delta(samples: list[Decimal]) -> tuple[Decimal, int]:
    """Return positive Decimal delta and reset count for a monotonic series."""
    if len(samples) < 2:
        return ZERO, 0

    delta = ZERO
    resets = 0
    previous = samples[0]

    for current in samples[1:]:
        if current >= previous:
            delta += current - previous
        else:
            resets += 1
        previous = current

    return delta, resets


def compute_identity_share_deltas(
    session: Session,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, IdentityDelta]:
    """Compute accepted share and work deltas per identity for a settlement window."""
    rows = session.execute(
        select(
            MetricSnapshot.identity,
            MetricSnapshot.channel_id,
            MetricSnapshot.accepted_shares_total,
            MetricSnapshot.accepted_work_total,
            MetricSnapshot.created_at,
        )
        .where(MetricSnapshot.created_at <= period_end)
        .order_by(
            MetricSnapshot.identity.asc(),
            MetricSnapshot.channel_id.asc(),
            MetricSnapshot.created_at.asc(),
        )
    ).all()

    grouped: dict[tuple[str, int | None], list[tuple[int, Decimal, datetime]]] = defaultdict(list)
    for identity, channel_id, accepted_total, accepted_work_total, created_at in rows:
        grouped[(identity, channel_id)].append((accepted_total, _to_decimal(accepted_work_total), created_at))

    result: dict[str, IdentityDelta] = {}
    for (identity, _channel_id), samples in grouped.items():
        share_baseline: int | None = None
        work_baseline: Decimal | None = None
        in_window_shares: list[int] = []
        in_window_work: list[Decimal] = []

        for accepted_total, accepted_work_total, created_at in samples:
            if created_at < period_start:
                share_baseline = accepted_total
                work_baseline = accepted_work_total
                continue
            in_window_shares.append(accepted_total)
            in_window_work.append(accepted_work_total)

        share_series = ([share_baseline] if share_baseline is not None else []) + in_window_shares
        work_series = ([work_baseline] if work_baseline is not None else []) + in_window_work

        share_delta, share_resets = compute_counter_delta(share_series)
        work_delta, work_resets = compute_decimal_counter_delta(work_series)

        if share_delta <= 0 and work_delta <= ZERO:
            continue

        existing = result.get(identity)
        reset_count = max(share_resets, work_resets)
        sample_count = max(len(share_series), len(work_series))

        if existing is None:
            result[identity] = IdentityDelta(
                identity=identity,
                share_delta=share_delta,
                reset_count=reset_count,
                sample_count=sample_count,
                work_delta=work_delta,
                channel_count=1,
            )
            continue

        result[identity] = IdentityDelta(
            identity=identity,
            share_delta=existing.share_delta + share_delta,
            reset_count=existing.reset_count + reset_count,
            sample_count=existing.sample_count + sample_count,
            work_delta=existing.work_delta + work_delta,
            channel_count=existing.channel_count + 1,
        )

    return result


def aggregate_user_share_deltas(identity_deltas: dict[str, IdentityDelta]) -> dict[str, int]:
    """Aggregate identity deltas into user totals using username.worker mapping."""
    user_totals: dict[str, int] = defaultdict(int)
    for identity, delta in identity_deltas.items():
        try:
            parts = parse_identity(identity)
        except ValueError:
            continue
        user_totals[parts.username] += delta.share_delta

    return dict(user_totals)


def aggregate_user_work_deltas(identity_deltas: dict[str, IdentityDelta]) -> dict[str, Decimal]:
    """Aggregate identity work deltas into user totals using username.worker mapping."""
    user_totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for identity, delta in identity_deltas.items():
        try:
            parts = parse_identity(identity)
        except ValueError:
            continue
        user_totals[parts.username] += delta.work_delta

    return dict(user_totals)


def compute_user_contribution_deltas(
    session: Session,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, UserContribution]:
    """Compute settlement-window contributions aggregated by username."""
    identity_deltas = compute_identity_share_deltas(session, period_start, period_end)
    user_shares = aggregate_user_share_deltas(identity_deltas)
    user_work = aggregate_user_work_deltas(identity_deltas)

    usernames = sorted(set(user_shares) | set(user_work))
    return {
        username: UserContribution(
            username=username,
            share_delta=user_shares.get(username, 0),
            work_delta=user_work.get(username, ZERO),
        )
        for username in usernames
    }


def compute_user_share_deltas(
    session: Session,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, int]:
    """Compute settlement-window share deltas aggregated by username."""
    contributions = compute_user_contribution_deltas(session, period_start, period_end)
    return {username: item.share_delta for username, item in contributions.items()}


def compute_user_work_deltas(
    session: Session,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, Decimal]:
    """Compute settlement-window work deltas aggregated by username."""
    contributions = compute_user_contribution_deltas(session, period_start, period_end)
    return {username: item.work_delta for username, item in contributions.items() if item.work_delta > ZERO}

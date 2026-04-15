from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mapping import parse_identity
from app.models import MetricSnapshot


@dataclass(frozen=True)
class IdentityDelta:
    identity: str
    share_delta: int
    reset_count: int
    sample_count: int


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


def compute_identity_share_deltas(
    session: Session,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, IdentityDelta]:
    """Compute accepted share deltas per identity for a settlement window."""
    rows = session.execute(
        select(
            MetricSnapshot.identity,
            MetricSnapshot.accepted_shares_total,
            MetricSnapshot.created_at,
        )
        .where(MetricSnapshot.created_at <= period_end)
        .order_by(MetricSnapshot.identity.asc(), MetricSnapshot.created_at.asc())
    ).all()

    grouped: dict[str, list[tuple[int, datetime]]] = defaultdict(list)
    for identity, accepted_total, created_at in rows:
        grouped[identity].append((accepted_total, created_at))

    result: dict[str, IdentityDelta] = {}
    for identity, samples in grouped.items():
        baseline: int | None = None
        in_window: list[int] = []

        for accepted_total, created_at in samples:
            if created_at < period_start:
                baseline = accepted_total
                continue
            in_window.append(accepted_total)

        series = ([baseline] if baseline is not None else []) + in_window
        share_delta, reset_count = compute_counter_delta(series)

        if share_delta > 0:
            result[identity] = IdentityDelta(
                identity=identity,
                share_delta=share_delta,
                reset_count=reset_count,
                sample_count=len(series),
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


def compute_user_share_deltas(
    session: Session,
    period_start: datetime,
    period_end: datetime,
) -> dict[str, int]:
    """Compute settlement-window share deltas aggregated by username."""
    identity_deltas = compute_identity_share_deltas(session, period_start, period_end)
    return aggregate_user_share_deltas(identity_deltas)

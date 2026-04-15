from __future__ import annotations

from datetime import UTC, datetime
from typing import Dict

import requests
from sqlalchemy.orm import Session

from app.metrics_parser import parse_accepted_shares
from app.models import MetricSnapshot


def fetch_metrics(metrics_url: str, timeout_seconds: int = 10) -> str:
    response = requests.get(metrics_url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.text


def persist_metric_snapshots(session: Session, counters_by_identity: Dict[str, int]) -> int:
    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
    created = 0
    for identity, accepted_total in counters_by_identity.items():
        session.add(
            MetricSnapshot(
                identity=identity,
                accepted_shares_total=accepted_total,
                created_at=now_utc_naive,
            )
        )
        created += 1
    session.commit()
    return created


def poll_metrics_once(session: Session, metrics_url: str, timeout_seconds: int = 10) -> int:
    metrics_text = fetch_metrics(metrics_url, timeout_seconds=timeout_seconds)
    counters_by_identity = parse_accepted_shares(metrics_text)
    if not counters_by_identity:
        return 0
    return persist_metric_snapshots(session, counters_by_identity)

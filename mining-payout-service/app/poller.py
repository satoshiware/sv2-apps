from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Dict

import requests
from sqlalchemy.orm import Session

from app.metrics_parser import parse_accepted_shares
from app.models import MetricSnapshot


@dataclass(frozen=True)
class ChannelSnapshot:
    channel_id: int | None
    identity: str
    accepted_shares_total: int
    accepted_work_total: Decimal
    shares_rejected_total: int


def fetch_metrics(metrics_url: str, timeout_seconds: int = 10) -> str:
    response = requests.get(metrics_url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.text


def fetch_channel_payload(
    api_url: str,
    timeout_seconds: int = 10,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    response = requests.get(api_url, timeout=timeout_seconds, headers=headers)
    response.raise_for_status()
    return response.json()


def fetch_downstream_payload(
    api_url: str,
    timeout_seconds: int = 10,
    bearer_token: str | None = None,
) -> dict[str, Any]:
    headers: dict[str, str] = {}
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    response = requests.get(api_url, timeout=timeout_seconds, headers=headers)
    response.raise_for_status()
    return response.json()


def persist_metric_snapshots(session: Session, counters_by_identity: Dict[str, int]) -> int:
    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
    created = 0
    for identity, accepted_total in counters_by_identity.items():
        session.add(
            MetricSnapshot(
                identity=identity,
                accepted_shares_total=accepted_total,
                accepted_work_total=0,
                shares_rejected_total=0,
                created_at=now_utc_naive,
            )
        )
        created += 1
    session.commit()
    return created


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def parse_downstream_identity_by_channel(payload: dict[str, Any]) -> dict[int, str]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return {}

    items = data.get("items")
    if not isinstance(items, list):
        return {}

    identities_by_channel: dict[int, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue

        channel_id = _to_int(item.get("channel_id"), default=0)
        if channel_id <= 0:
            continue

        identity = str(item.get("user_identity") or "").strip()
        if not identity:
            identity = str(item.get("authorized_worker_name") or "").strip()
        if not identity:
            continue

        identities_by_channel[channel_id] = identity

    return identities_by_channel


def parse_channel_snapshots(
    payload: dict[str, Any],
    identities_by_channel: dict[int, str] | None = None,
) -> list[ChannelSnapshot]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []

    channels: list[dict[str, Any]] = []
    for key in ("extended_channels", "standard_channels"):
        value = data.get(key)
        if isinstance(value, list):
            channels.extend(item for item in value if isinstance(item, dict))

    snapshots: list[ChannelSnapshot] = []
    for channel in channels:
        channel_id = _to_int(channel.get("channel_id"), default=0)
        identity = ""
        if identities_by_channel and channel_id > 0:
            identity = str(identities_by_channel.get(channel_id) or "").strip()
        if not identity:
            identity = str(channel.get("user_identity") or "").strip()
        if not identity:
            continue
        snapshots.append(
            ChannelSnapshot(
                channel_id=channel_id,
                identity=identity,
                accepted_shares_total=_to_int(channel.get("shares_acknowledged"), default=0),
                accepted_work_total=_to_decimal(channel.get("share_work_sum")),
                shares_rejected_total=_to_int(channel.get("shares_rejected"), default=0),
            )
        )
    return snapshots


def parse_blocks_found_by_channel(payload: dict[str, Any]) -> dict[int, int]:
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

            channel_id = _to_int(item.get("channel_id"), default=0)
            if channel_id <= 0:
                continue

            blocks_found = _to_int(item.get("blocks_found"), default=0)
            counters[channel_id] = max(blocks_found, 0)

    return counters


def persist_channel_snapshots(session: Session, channel_snapshots: list[ChannelSnapshot]) -> int:
    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
    created = 0
    for snapshot in channel_snapshots:
        session.add(
            MetricSnapshot(
                channel_id=snapshot.channel_id,
                identity=snapshot.identity,
                accepted_shares_total=snapshot.accepted_shares_total,
                accepted_work_total=snapshot.accepted_work_total,
                shares_rejected_total=snapshot.shares_rejected_total,
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


def poll_channels_once(
    session: Session,
    api_url: str,
    timeout_seconds: int = 10,
    downstream_url: str | None = None,
    bearer_token: str | None = None,
) -> int:
    created, _blocks_found_by_channel = poll_channels_once_with_blocks(
        session,
        api_url,
        timeout_seconds=timeout_seconds,
        downstream_url=downstream_url,
        bearer_token=bearer_token,
    )
    return created


def poll_channels_once_with_blocks(
    session: Session,
    api_url: str,
    timeout_seconds: int = 10,
    downstream_url: str | None = None,
    bearer_token: str | None = None,
) -> tuple[int, dict[int, int]]:
    payload = fetch_channel_payload(
        api_url,
        timeout_seconds=timeout_seconds,
        bearer_token=bearer_token,
    )
    identities_by_channel: dict[int, str] | None = None
    if downstream_url:
        downstream_payload = fetch_downstream_payload(
            downstream_url,
            timeout_seconds=timeout_seconds,
            bearer_token=bearer_token,
        )
        identities_by_channel = parse_downstream_identity_by_channel(downstream_payload)

    snapshots = parse_channel_snapshots(payload, identities_by_channel=identities_by_channel)
    blocks_found_by_channel = parse_blocks_found_by_channel(payload)
    if not snapshots:
        return 0, blocks_found_by_channel
    return persist_channel_snapshots(session, snapshots), blocks_found_by_channel

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Dict

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.metrics_parser import parse_accepted_shares
from app.models import MetricSnapshot, SnapshotBlock


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


def _parse_found_at(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC).replace(tzinfo=None)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError("found_at cannot be empty")
        try:
            return datetime.fromtimestamp(float(raw), tz=UTC).replace(tzinfo=None)
        except ValueError:
            pass
        iso_value = raw.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(iso_value)
        if parsed.tzinfo is None:
            return parsed
        return parsed.astimezone(UTC).replace(tzinfo=None)
    raise ValueError("found_at must be epoch seconds or ISO datetime string")


def _normalize_snapshot_block_row(
    payload: dict[str, Any],
    *,
    source_default: str,
) -> dict[str, Any] | None:
    blockhash = str(payload.get("blockhash") or payload.get("block_hash") or "").strip()
    if not blockhash:
        # Translator may emit unresolved counter-delta events with a best candidate
        # hash in nearest_candidate_blockhash before blockhash is finalized.
        blockhash = str(payload.get("nearest_candidate_blockhash") or "").strip()

    if not blockhash:
        candidate_blocks = payload.get("candidate_blocks")
        if isinstance(candidate_blocks, list):
            for candidate in candidate_blocks:
                if not isinstance(candidate, dict):
                    continue
                blockhash = str(candidate.get("blockhash") or "").strip()
                if blockhash:
                    break

    if not blockhash:
        return None

    found_at_raw = payload.get(
        "detected_time",
        payload.get("found_at", payload.get("timestamp", payload.get("time"))),
    )
    if found_at_raw is None:
        return None

    worker_identity = str(
        payload.get("worker_identity")
        or payload.get("worker_name")
        or payload.get("user_identity")
        or payload.get("authorized_worker_name")
        or ""
    ).strip()

    return {
        "blockhash": blockhash,
        "found_at": _parse_found_at(found_at_raw),
        "channel_id": _to_int(payload.get("channel_id"), default=0),
        "worker_identity": worker_identity,
        "source": str(payload.get("source") or source_default).strip() or source_default,
    }


def upsert_snapshot_blocks(
    session: Session,
    block_rows: list[dict[str, Any]],
    *,
    source_default: str = "translator_blocks_api",
) -> int:
    """Insert unique snapshot_block rows by blockhash from a translator blocks response."""
    normalized: dict[str, dict[str, Any]] = {}
    for row in block_rows:
        item = _normalize_snapshot_block_row(row, source_default=source_default)
        if item is None:
            continue
        blockhash = str(item["blockhash"])
        if blockhash not in normalized:
            normalized[blockhash] = item

    if not normalized:
        return 0

    blockhashes = list(normalized.keys())
    existing_hashes = set(
        session.execute(
            select(SnapshotBlock.blockhash).where(SnapshotBlock.blockhash.in_(blockhashes))
        ).scalars()
    )

    now_utc_naive = datetime.now(UTC).replace(tzinfo=None)
    created = 0
    for blockhash, row in normalized.items():
        if blockhash in existing_hashes:
            continue
        session.add(
            SnapshotBlock(
                found_at=row["found_at"],
                channel_id=int(row["channel_id"]),
                worker_identity=str(row["worker_identity"]),
                blockhash=blockhash,
                source=str(row["source"]),
                reward_sats=None,
                reward_fetched_at=None,
                settlement_id=None,
                created_at=now_utc_naive,
                updated_at=now_utc_naive,
            )
        )
        created += 1

    if created > 0:
        session.flush()
    return created

from __future__ import annotations

from datetime import datetime, timedelta


def compute_matured_window(
    now: datetime,
    *,
    interval_minutes: int,
    maturity_window_minutes: int,
) -> tuple[datetime, datetime]:
    """Return shifted matured window [start, end) for a settlement run.

    For settlement interval T at time now, the matured selection window is:
    [now - maturity_window_minutes - T, now - maturity_window_minutes)
    """
    if interval_minutes <= 0:
        raise ValueError("interval_minutes must be positive")
    if maturity_window_minutes <= 0:
        raise ValueError("maturity_window_minutes must be positive")

    matured_end = now - timedelta(minutes=maturity_window_minutes)
    matured_start = matured_end - timedelta(minutes=interval_minutes)
    return matured_start, matured_end


def is_within_matured_window(found_at: datetime, start: datetime, end: datetime) -> bool:
    """Return true if found_at falls in [start, end)."""
    return start <= found_at < end


def parse_reward_sats_by_hash(payload: object) -> dict[str, int]:
    """Extract blockhash->reward_sats map from reward API payload.

    Accepted shapes:
    - {"blocks": [{"blockhash": "...", "coinbase_total_sats": 123}, ...]}
    - {"rewards": [{"blockhash": "...", "reward_sats": 123}, ...]}
    - {"rewards": {"<hash>": 123, ...}}
    """
    if not isinstance(payload, dict):
        raise ValueError("reward payload must be a JSON object")

    rewards: dict[str, int] = {}

    blocks = payload.get("blocks")
    if isinstance(blocks, list):
        for item in blocks:
            if not isinstance(item, dict):
                continue
            blockhash = str(item.get("blockhash") or "").strip()
            if not blockhash:
                continue
            raw_value = item.get("coinbase_total_sats")
            if raw_value is None:
                continue
            try:
                sats = int(raw_value)
            except (TypeError, ValueError):
                continue
            if sats < 0:
                continue
            rewards[blockhash] = sats
        return rewards

    rewards_section = payload.get("rewards")
    if isinstance(rewards_section, list):
        for item in rewards_section:
            if not isinstance(item, dict):
                continue
            blockhash = str(item.get("blockhash") or "").strip()
            if not blockhash:
                continue
            raw_value = item.get("reward_sats")
            if raw_value is None:
                continue
            try:
                sats = int(raw_value)
            except (TypeError, ValueError):
                continue
            if sats < 0:
                continue
            rewards[blockhash] = sats
        return rewards

    if isinstance(rewards_section, dict):
        for key, value in rewards_section.items():
            blockhash = str(key or "").strip()
            if not blockhash:
                continue
            try:
                sats = int(value)
            except (TypeError, ValueError):
                continue
            if sats < 0:
                continue
            rewards[blockhash] = sats
        return rewards

    raise ValueError("reward payload missing supported rewards shape")

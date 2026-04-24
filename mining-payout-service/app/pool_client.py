from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests

from app.config import load_settings


class PoolApiError(RuntimeError):
    """Raised when pool API calls fail or return invalid payloads."""


class PoolApiTimeout(PoolApiError):
    """Raised when pool API call times out."""


def _to_utc_date(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.date().isoformat()


def _extract_reward_btc(payload: Any) -> float:
    if not isinstance(payload, dict):
        raise PoolApiError("Pool API payload must be a JSON object")

    # Braiins Pool daily rewards shape:
    # {"btc": {"daily_rewards": [{"total_reward": "..."}, ...]}}
    btc_section = payload.get("btc")
    if isinstance(btc_section, dict) and isinstance(btc_section.get("daily_rewards"), list):
        total = 0.0
        for item in btc_section["daily_rewards"]:
            if not isinstance(item, dict):
                continue
            value = item.get("total_reward")
            if value is None:
                continue
            try:
                reward = float(value)
            except (TypeError, ValueError) as exc:
                raise PoolApiError("Braiins total_reward field is not numeric") from exc
            if reward < 0:
                raise PoolApiError("Pool API reward cannot be negative")
            total += reward
        return total

    keys = (
        "reward_btc",
        "total_reward_btc",
        "reward",
        "total_reward",
        "amount_btc",
    )
    for key in keys:
        if key in payload:
            try:
                reward = float(payload[key])
            except (TypeError, ValueError) as exc:
                raise PoolApiError(f"Pool API reward field '{key}' is not numeric") from exc
            if reward < 0:
                raise PoolApiError("Pool API reward cannot be negative")
            return reward

    raise PoolApiError(
        "Pool API payload missing reward amount field; expected one of "
        f"{', '.join(keys)}"
    )


def fetch_pool_reward(
    period_start: datetime,
    period_end: datetime,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout_seconds: int = 10,
) -> float:
    """Fetch total pool reward in BTC for a settlement window."""
    settings = load_settings()
    fixed_reward = (settings.fixed_reward_btc or "").strip()
    if fixed_reward:
        try:
            reward = float(fixed_reward)
        except ValueError as exc:
            raise PoolApiError("FIXED_REWARD_BTC must be numeric") from exc
        if reward < 0:
            raise PoolApiError("FIXED_REWARD_BTC cannot be negative")
        return reward

    resolved_base_url = (base_url or settings.pool_api_base_url).strip()
    resolved_reward_url = (settings.pool_reward_url or "").strip()
    resolved_api_key = (api_key or settings.pool_api_key).strip()

    if resolved_reward_url:
        url = resolved_reward_url
    elif resolved_base_url:
        url = f"{resolved_base_url.rstrip('/')}/accounts/rewards/json/btc"
    else:
        raise PoolApiError(
            "Set FIXED_REWARD_BTC or POOL_REWARD_URL (or POOL_API_BASE_URL) to fetch rewards"
        )

    params = {
        "from": _to_utc_date(period_start),
        "to": _to_utc_date(period_end),
    }

    headers = {"Accept": "application/json"}
    if resolved_api_key:
        headers["Pool-Auth-Token"] = resolved_api_key
        headers["X-Pool-Auth-Token"] = resolved_api_key

    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout_seconds)
        response.raise_for_status()
    except requests.Timeout as exc:
        raise PoolApiTimeout("Pool API request timed out") from exc
    except requests.RequestException as exc:
        raise PoolApiError(f"Pool API request failed: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise PoolApiError("Pool API returned non-JSON response") from exc

    return _extract_reward_btc(payload)

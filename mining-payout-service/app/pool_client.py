from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests

from app.config import load_settings
from app.reward_contract import parse_reward_sats_by_hash


class PoolApiError(RuntimeError):
    """Raised when pool API calls fail or return invalid payloads."""


class PoolApiTimeout(PoolApiError):
    """Raised when pool API call times out."""


def _to_unix_ts(value: datetime) -> int:
    if value.tzinfo is None:
        return int(value.replace(tzinfo=UTC).timestamp())
    return int(value.astimezone(UTC).timestamp())


def _extract_blocks_found_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]

    if not isinstance(payload, dict):
        raise PoolApiError("Blocks-found API payload must be a JSON object or list")

    # Translator shape: {"status": "ok", "items": [...]}
    if isinstance(payload.get("items"), list):
        return [item for item in payload["items"] if isinstance(item, dict)]

    if isinstance(payload.get("blocks"), list):
        return [item for item in payload["blocks"] if isinstance(item, dict)]

    data = payload.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("items", "blocks", "results", "entries"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]

    raise PoolApiError("Blocks-found API payload missing blocks array")


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


def fetch_block_rewards_by_hashes(
    block_hashes: list[str],
    *,
    api_url: str | None = None,
    bearer_token: str | None = None,
    timeout_seconds: int | None = None,
) -> dict[str, int]:
    """Fetch per-block rewards (sats) by blockhash lookup.

    Uses GET /v1/az/blocks/rewards with query params:
    - owned_only=false
    - blockhash=<hash>
    - time_field=mediantime
    """
    normalized_hashes = [str(item).strip() for item in block_hashes if str(item).strip()]
    if not normalized_hashes:
        return {}

    settings = load_settings()
    resolved_url = (api_url or settings.block_reward_batch_url or "").strip()
    if not resolved_url:
        raise PoolApiError("BLOCK_REWARD_BATCH_URL is required for block event rewards")

    resolved_timeout = int(timeout_seconds or settings.block_reward_batch_timeout_seconds or 10)
    resolved_token = (bearer_token or settings.translator_bearer_token or "").strip()

    headers = {"Accept": "application/json"}
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"

    rewards_by_hash: dict[str, int] = {}
    for blockhash in normalized_hashes:
        params = {
            "owned_only": "false",
            "blockhash": blockhash,
            "time_field": "mediantime",
        }
        try:
            response = requests.get(
                resolved_url,
                params=params,
                headers=headers,
                timeout=resolved_timeout,
            )
            response.raise_for_status()
        except requests.Timeout as exc:
            raise PoolApiTimeout("Block reward API request timed out") from exc
        except requests.RequestException as exc:
            raise PoolApiError(f"Block reward API request failed: {exc}") from exc

        try:
            response_payload = response.json()
        except ValueError as exc:
            raise PoolApiError("Block reward API returned non-JSON response") from exc

        try:
            parsed_rewards = parse_reward_sats_by_hash(response_payload)
        except ValueError as exc:
            raise PoolApiError(str(exc)) from exc

        if blockhash in parsed_rewards:
            rewards_by_hash[blockhash] = parsed_rewards[blockhash]

    return rewards_by_hash


def fetch_blocks_found_in_window(
    window_start: datetime,
    window_end: datetime,
    *,
    api_url: str | None = None,
    bearer_token: str | None = None,
    timeout_seconds: int | None = None,
    limit: int | None = None,
    candidate_window_seconds: int | None = None,
    candidate_limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch block-found rows for the shifted matured settlement window.

    Calls GET /v1/translator/blocks-found?start_time=<unix>&end_time=<unix>&limit=<n>
    &include_candidate_blocks=true&candidate_window_seconds=<n>&candidate_limit_per_event=<n>
    """
    settings = load_settings()
    resolved_url = (api_url or settings.translator_blocks_found_url or "").strip()
    if not resolved_url:
        raise PoolApiError("TRANSLATOR_BLOCKS_FOUND_URL is required for block window fetch")

    resolved_timeout = int(timeout_seconds or settings.translator_blocks_found_timeout_seconds or 10)
    resolved_token = (bearer_token or settings.translator_bearer_token or "").strip()
    resolved_limit = int(limit or settings.translator_blocks_found_limit or 100)
    resolved_candidate_window = int(
        candidate_window_seconds
        or settings.translator_blocks_found_candidate_window_seconds
        or 30
    )
    resolved_candidate_limit = int(
        candidate_limit
        or settings.translator_blocks_found_candidate_limit
        or 5
    )

    headers = {"Accept": "application/json"}
    if resolved_token:
        headers["Authorization"] = f"Bearer {resolved_token}"

    params = {
        "start_time": _to_unix_ts(window_start),
        "end_time": _to_unix_ts(window_end),
        "limit": resolved_limit,
        "include_candidate_blocks": "true",
        "candidate_window_seconds": resolved_candidate_window,
        "candidate_limit_per_event": resolved_candidate_limit,
    }

    try:
        response = requests.get(
            resolved_url,
            params=params,
            headers=headers,
            timeout=resolved_timeout,
        )
        response.raise_for_status()
    except requests.Timeout as exc:
        raise PoolApiTimeout("Blocks-found API request timed out") from exc
    except requests.RequestException as exc:
        raise PoolApiError(f"Blocks-found API request failed: {exc}") from exc

    try:
        response_payload = response.json()
    except ValueError as exc:
        raise PoolApiError("Blocks-found API returned non-JSON response") from exc

    return _extract_blocks_found_items(response_payload)

from datetime import datetime

import pytest
import requests

from app.pool_client import (
    PoolApiError,
    PoolApiTimeout,
    fetch_blocks_found_in_window,
    fetch_block_rewards_by_hashes,
    fetch_pool_reward,
)


class _Response:
    def __init__(self, status_code: int, payload=None, json_error: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._json_error:
            raise ValueError("bad json")
        return self._payload


def test_fetch_pool_reward_success(monkeypatch) -> None:
    captured = {}

    def _fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Response(
            200,
            {
                "btc": {
                    "daily_rewards": [
                        {"date": 1735689600, "total_reward": "0.01000000"},
                        {"date": 1735776000, "total_reward": "0.00234567"},
                    ]
                }
            },
        )

    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    start = datetime(2026, 1, 1, 0, 0, 0)
    end = datetime(2026, 1, 2, 0, 10, 0)
    reward = fetch_pool_reward(
        start,
        end,
        base_url="https://pool.example",
        api_key="abc123",
        timeout_seconds=7,
    )

    assert reward == 0.01234567
    assert captured["url"] == "https://pool.example/accounts/rewards/json/btc"
    assert captured["params"]["from"] == "2026-01-01"
    assert captured["params"]["to"] == "2026-01-02"
    assert captured["headers"]["Pool-Auth-Token"] == "abc123"
    assert captured["headers"]["X-Pool-Auth-Token"] == "abc123"
    assert captured["timeout"] == 7


def test_fetch_pool_reward_timeout(monkeypatch) -> None:
    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        raise requests.Timeout("timeout")

    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    with pytest.raises(PoolApiTimeout):
        fetch_pool_reward(
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 1, 1, 0, 10, 0),
            base_url="https://pool.example",
        )


def test_fetch_pool_reward_http_error(monkeypatch) -> None:
    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        return _Response(503, {"error": "service unavailable"})

    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    with pytest.raises(PoolApiError):
        fetch_pool_reward(
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 1, 1, 0, 10, 0),
            base_url="https://pool.example",
        )


def test_fetch_pool_reward_invalid_payload(monkeypatch) -> None:
    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        return _Response(200, {"not_reward": 1})

    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    with pytest.raises(PoolApiError):
        fetch_pool_reward(
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 1, 1, 0, 10, 0),
            base_url="https://pool.example",
        )


def test_fetch_pool_reward_invalid_daily_reward_value(monkeypatch) -> None:
    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        return _Response(200, {"btc": {"daily_rewards": [{"total_reward": "bad"}]}})

    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    with pytest.raises(PoolApiError):
        fetch_pool_reward(
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 1, 1, 0, 10, 0),
            base_url="https://pool.example",
        )


def test_fetch_pool_reward_non_json(monkeypatch) -> None:
    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        return _Response(200, payload=None, json_error=True)

    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    with pytest.raises(PoolApiError):
        fetch_pool_reward(
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 1, 1, 0, 10, 0),
            base_url="https://pool.example",
        )


def test_fetch_pool_reward_uses_fixed_reward_env(monkeypatch) -> None:
    monkeypatch.setenv("FIXED_REWARD_BTC", "0.12345678")

    reward = fetch_pool_reward(
        datetime(2026, 1, 1, 0, 0, 0),
        datetime(2026, 1, 1, 0, 10, 0),
    )

    assert reward == 0.12345678


def test_fetch_pool_reward_uses_pool_reward_url(monkeypatch) -> None:
    captured = {}

    def _fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        return _Response(200, {"reward_btc": "0.025"})

    monkeypatch.setenv("POOL_REWARD_URL", "https://rewards.example/v1/reward")
    monkeypatch.setenv("POOL_API_KEY", "token-1")
    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    reward = fetch_pool_reward(
        datetime(2026, 1, 1, 0, 0, 0),
        datetime(2026, 1, 1, 0, 10, 0),
    )

    assert reward == 0.025
    assert captured["url"] == "https://rewards.example/v1/reward"
    assert captured["params"]["from"] == "2026-01-01"
    assert captured["params"]["to"] == "2026-01-01"
    assert captured["headers"]["Pool-Auth-Token"] == "token-1"


def test_fetch_block_rewards_by_hashes_success(monkeypatch) -> None:
    calls = []

    def _fake_get(url, params, headers, timeout):
        calls.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if params.get("blockhash") == "h1":
            return _Response(200, {"blocks": [{"blockhash": "h1", "coinbase_total_sats": 100}]})
        if params.get("blockhash") == "h2":
            return _Response(200, {"blocks": [{"blockhash": "h2", "coinbase_total_sats": "200"}]})
        return _Response(200, {"blocks": []})

    monkeypatch.setenv("BLOCK_REWARD_BATCH_URL", "http://127.0.0.1:8080/v1/az/blocks/rewards")
    monkeypatch.setenv("TRANSLATOR_BEARER_TOKEN", "token-1")
    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    rewards = fetch_block_rewards_by_hashes(["h1", "h2", "h3"])

    assert rewards == {"h1": 100, "h2": 200}
    assert len(calls) == 3
    assert calls[0]["url"] == "http://127.0.0.1:8080/v1/az/blocks/rewards"
    assert calls[0]["params"]["owned_only"] == "false"
    assert calls[0]["params"]["time_field"] == "mediantime"
    assert calls[0]["params"]["blockhash"] == "h1"
    assert calls[0]["headers"]["Authorization"] == "Bearer token-1"


def test_fetch_block_rewards_by_hashes_timeout(monkeypatch) -> None:
    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        raise requests.Timeout("timeout")

    monkeypatch.setenv("BLOCK_REWARD_BATCH_URL", "http://127.0.0.1:8080/v1/az/blocks/rewards")
    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    with pytest.raises(PoolApiTimeout):
        fetch_block_rewards_by_hashes(["h1"])


def test_fetch_block_rewards_by_hashes_requires_url(monkeypatch) -> None:
    monkeypatch.delenv("BLOCK_REWARD_BATCH_URL", raising=False)

    with pytest.raises(PoolApiError):
        fetch_block_rewards_by_hashes(["h1"])


def test_fetch_blocks_found_in_window_success(monkeypatch) -> None:
    captured = {}

    def _fake_get(url, params, headers, timeout):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        captured["timeout"] = timeout
        # Translator shape: top-level "items" with detected_time as unix int
        return _Response(
            200,
            {
                "status": "ok",
                "source": "translator_blocks_found_events",
                "total": 1,
                "items": [
                    {
                        "detected_time": 1777403179,
                        "channel_id": 2,
                        "worker_identity": "alice.rig1",
                        "blockhash": "hash-1",
                        "blockhash_status": "resolved",
                    }
                ],
            },
        )

    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "http://127.0.0.1:8080/v1/translator/blocks-found")
    monkeypatch.setenv("TRANSLATOR_BEARER_TOKEN", "token-1")
    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    window_start = datetime(2026, 1, 1, 0, 0, 0)
    window_end = datetime(2026, 1, 1, 0, 10, 0)
    rows = fetch_blocks_found_in_window(window_start, window_end)

    assert len(rows) == 1
    assert rows[0]["blockhash"] == "hash-1"
    assert captured["url"] == "http://127.0.0.1:8080/v1/translator/blocks-found"
    assert isinstance(captured["params"]["start_time"], int)
    assert isinstance(captured["params"]["end_time"], int)
    assert captured["params"]["start_time"] < captured["params"]["end_time"]
    assert "limit" in captured["params"]
    assert captured["params"]["include_candidate_blocks"] == "true"
    assert "candidate_window_seconds" in captured["params"]
    assert "candidate_limit_per_event" in captured["params"]
    assert captured["headers"]["Authorization"] == "Bearer token-1"


def test_fetch_blocks_found_in_window_null_blockhash_skipped(monkeypatch) -> None:
    """Rows with null blockhash (blockhash_status=unresolved) are returned raw;
    the normalizer in poller.py will filter them out."""

    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        return _Response(
            200,
            {
                "status": "ok",
                "items": [
                    {
                        "detected_time": 1777403179,
                        "channel_id": 2,
                        "worker_identity": "alice.rig1",
                        "blockhash": None,
                        "blockhash_status": "unresolved",
                    }
                ],
            },
        )

    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "http://127.0.0.1:8080/v1/translator/blocks-found")
    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    rows = fetch_blocks_found_in_window(
        datetime(2026, 1, 1, 0, 0, 0),
        datetime(2026, 1, 1, 0, 10, 0),
    )
    # pool_client returns the raw item; normalizer filters null blockhash
    assert len(rows) == 1
    assert rows[0]["blockhash"] is None


def test_fetch_blocks_found_in_window_timeout(monkeypatch) -> None:
    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        raise requests.Timeout("timeout")

    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "http://127.0.0.1:8080/v1/translator/blocks-found")
    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    with pytest.raises(PoolApiTimeout):
        fetch_blocks_found_in_window(
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 1, 1, 0, 10, 0),
        )


def test_fetch_blocks_found_in_window_requires_url(monkeypatch) -> None:
    monkeypatch.delenv("TRANSLATOR_BLOCKS_FOUND_URL", raising=False)

    with pytest.raises(PoolApiError):
        fetch_blocks_found_in_window(
            datetime(2026, 1, 1, 0, 0, 0),
            datetime(2026, 1, 1, 0, 10, 0),
        )


def test_fetch_blocks_found_in_window_exact_live_payload_shape(monkeypatch) -> None:
    def _fake_get(url, params, headers, timeout):
        _ = (url, params, headers, timeout)
        return _Response(
            200,
            {
                "status": "ok",
                "source": "translator_blocks_found_events",
                "total": 1,
                "time_filter": {
                    "start_time": 1777402579,
                    "end_time": 1777403779,
                    "time_field": "detected_time",
                    "interval_rule": "start_time <= detected_time < end_time",
                },
                "items": [
                    {
                        "detected_time": 1777403179,
                        "detected_time_iso": "2026-04-28T19:06:19Z",
                        "channel_id": 2,
                        "worker_identity": "Ben.Cust1",
                        "authorized_worker_name": "Ben.Cust1",
                        "downstream_user_identity": "Ben.Cust1",
                        "upstream_user_identity": "baveetstudy.miner1",
                        "blocks_found_before": 43,
                        "blocks_found_after": 44,
                        "blocks_found_delta": 1,
                        "share_work_sum_at_detection": "395446715.0",
                        "shares_acknowledged_at_detection": 34245,
                        "shares_submitted_at_detection": 34245,
                        "shares_rejected_at_detection": 0,
                        "blockhash": None,
                        "blockhash_status": "unresolved",
                        "correlation_status": "counter_delta_only",
                    }
                ],
            },
        )

    monkeypatch.setenv("TRANSLATOR_BLOCKS_FOUND_URL", "http://127.0.0.1:8080/v1/translator/blocks-found")
    monkeypatch.setattr("app.pool_client.requests.get", _fake_get)

    rows = fetch_blocks_found_in_window(
        datetime(2026, 4, 28, 19, 2, 59),
        datetime(2026, 4, 28, 19, 22, 59),
    )

    assert len(rows) == 1
    assert rows[0]["detected_time"] == 1777403179
    assert rows[0]["blockhash"] is None
    assert rows[0]["correlation_status"] == "counter_delta_only"

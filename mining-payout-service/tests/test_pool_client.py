from datetime import datetime

import pytest
import requests

from app.pool_client import PoolApiError, PoolApiTimeout, fetch_pool_reward


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

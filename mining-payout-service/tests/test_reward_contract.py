from datetime import datetime

import pytest

from app.config import load_settings
from app.reward_contract import (
    compute_matured_window,
    is_within_matured_window,
    parse_reward_sats_by_hash,
)


def test_compute_matured_window_shifted_interval() -> None:
    now = datetime(2026, 4, 28, 12, 0, 0)
    start, end = compute_matured_window(
        now,
        interval_minutes=10,
        maturity_window_minutes=200,
    )

    assert start == datetime(2026, 4, 28, 8, 30, 0)
    assert end == datetime(2026, 4, 28, 8, 40, 0)


def test_compute_matured_window_validates_positive_values() -> None:
    with pytest.raises(ValueError):
        compute_matured_window(datetime(2026, 4, 28, 12, 0, 0), interval_minutes=0, maturity_window_minutes=200)
    with pytest.raises(ValueError):
        compute_matured_window(datetime(2026, 4, 28, 12, 0, 0), interval_minutes=10, maturity_window_minutes=0)


def test_is_within_matured_window_is_start_inclusive_end_exclusive() -> None:
    start = datetime(2026, 4, 28, 8, 30, 0)
    end = datetime(2026, 4, 28, 8, 40, 0)

    assert is_within_matured_window(datetime(2026, 4, 28, 8, 30, 0), start, end)
    assert is_within_matured_window(datetime(2026, 4, 28, 8, 39, 59), start, end)
    assert not is_within_matured_window(datetime(2026, 4, 28, 8, 40, 0), start, end)


def test_parse_reward_sats_by_hash_from_blocks_shape() -> None:
    payload = {
        "blocks": [
            {"blockhash": "0001", "coinbase_total_sats": 1_875_000_00},
            {"blockhash": "0002", "coinbase_total_sats": "200"},
            {"blockhash": "0003", "coinbase_total_sats": -1},
        ]
    }

    result = parse_reward_sats_by_hash(payload)

    assert result == {"0001": 187500000, "0002": 200}


def test_parse_reward_sats_by_hash_from_rewards_shapes() -> None:
    list_payload = {
        "rewards": [
            {"blockhash": "a", "reward_sats": 10},
            {"blockhash": "b", "reward_sats": "20"},
        ]
    }
    map_payload = {"rewards": {"c": 30, "d": "40"}}

    assert parse_reward_sats_by_hash(list_payload) == {"a": 10, "b": 20}
    assert parse_reward_sats_by_hash(map_payload) == {"c": 30, "d": 40}


def test_parse_reward_sats_by_hash_rejects_unknown_shape() -> None:
    with pytest.raises(ValueError):
        parse_reward_sats_by_hash({"tip_height": 123})


def test_parse_reward_sats_by_hash_exact_live_blocks_payload() -> None:
    payload = {
        "tip_height": 843205,
        "tip_hash": "00000000000000fb62f0ecfbefc8d6d1667486992fdceb42650fae649835c369",
        "chain": "main",
        "maturity_confirmations": 100,
        "owned_only": False,
        "lookup_mode": "blockhashes",
        "requested_blockhash_count": 1,
        "resolved_blockhash_count": 1,
        "unresolved_blockhashes": [],
        "time_filter": {
            "start_time": None,
            "end_time": None,
            "time_field": "mediantime",
            "interval_rule": "start_time <= selected_time < end_time",
        },
        "blocks": [
            {
                "height": 842521,
                "blockhash": "000000000000014b774d5ff29803def01bf3222e479436bfa2cd735181530446",
                "confirmations": 685,
                "mediantime": 1777403101,
                "is_mature": True,
                "coinbase_total_sats": 187500000,
            }
        ],
    }

    result = parse_reward_sats_by_hash(payload)

    assert result == {
        "000000000000014b774d5ff29803def01bf3222e479436bfa2cd735181530446": 187500000
    }


def test_phase0_new_settings_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ENABLE_BLOCK_EVENT_REWARDS", raising=False)
    monkeypatch.delenv("MATURITY_WINDOW_MINUTES", raising=False)
    monkeypatch.delenv("BLOCK_REWARD_BATCH_URL", raising=False)
    monkeypatch.delenv("BLOCK_REWARD_BATCH_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("DEFER_ON_ZERO_MATURED_REWARD", raising=False)

    settings = load_settings()

    assert settings.enable_block_event_rewards is False
    assert settings.maturity_window_minutes == 200
    assert settings.block_reward_batch_url == ""
    assert settings.block_reward_batch_timeout_seconds == 10
    assert settings.defer_on_zero_matured_reward is True


def test_phase0_new_settings_env_override(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_BLOCK_EVENT_REWARDS", "true")
    monkeypatch.setenv("MATURITY_WINDOW_MINUTES", "210")
    monkeypatch.setenv("BLOCK_REWARD_BATCH_URL", "http://127.0.0.1:8080/v1/az/blocks/rewards")
    monkeypatch.setenv("BLOCK_REWARD_BATCH_TIMEOUT_SECONDS", "15")
    monkeypatch.setenv("DEFER_ON_ZERO_MATURED_REWARD", "false")

    settings = load_settings()

    assert settings.enable_block_event_rewards is True
    assert settings.maturity_window_minutes == 210
    assert settings.block_reward_batch_url == "http://127.0.0.1:8080/v1/az/blocks/rewards"
    assert settings.block_reward_batch_timeout_seconds == 15
    assert settings.defer_on_zero_matured_reward is False

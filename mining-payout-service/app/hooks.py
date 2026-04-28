from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from sqlalchemy.orm import Session

from app.config import Settings
from app.settlement import SettlementResult

StartupReconciliationHook = Callable[[Session, Settings], None]
BlockEventReplayHook = Callable[[Session, datetime, datetime], list[dict[str, Any]]]
RewardRefetchHook = Callable[[list[str], dict[str, int]], dict[str, int]]
SettlementReplayHook = Callable[[Session, SettlementResult], None]


def _noop_startup_reconciliation(_session: Session, _settings: Settings) -> None:
    return None


def _noop_block_event_replay(
    _session: Session,
    _matured_start: datetime,
    _matured_end: datetime,
) -> list[dict[str, Any]]:
    return []


def _noop_reward_refetch(
    _selected_hashes: list[str],
    rewards_by_hash: dict[str, int],
) -> dict[str, int]:
    return rewards_by_hash


def _noop_settlement_replay(_session: Session, _settlement_result: SettlementResult) -> None:
    return None


_startup_reconciliation_hook: StartupReconciliationHook = _noop_startup_reconciliation
_block_event_replay_hook: BlockEventReplayHook = _noop_block_event_replay
_reward_refetch_hook: RewardRefetchHook = _noop_reward_refetch
_settlement_replay_hook: SettlementReplayHook = _noop_settlement_replay


def set_startup_reconciliation_hook(hook: StartupReconciliationHook) -> None:
    global _startup_reconciliation_hook
    _startup_reconciliation_hook = hook


def set_block_event_replay_hook(hook: BlockEventReplayHook) -> None:
    global _block_event_replay_hook
    _block_event_replay_hook = hook


def set_reward_refetch_hook(hook: RewardRefetchHook) -> None:
    global _reward_refetch_hook
    _reward_refetch_hook = hook


def set_settlement_replay_hook(hook: SettlementReplayHook) -> None:
    global _settlement_replay_hook
    _settlement_replay_hook = hook


def run_startup_reconciliation_hook(session: Session, settings: Settings) -> None:
    _startup_reconciliation_hook(session, settings)


def run_block_event_replay_hook(
    session: Session,
    matured_start: datetime,
    matured_end: datetime,
) -> list[dict[str, Any]]:
    return _block_event_replay_hook(session, matured_start, matured_end)


def run_reward_refetch_hook(
    selected_hashes: list[str],
    rewards_by_hash: dict[str, int],
) -> dict[str, int]:
    return _reward_refetch_hook(selected_hashes, rewards_by_hash)


def run_settlement_replay_hook(session: Session, settlement_result: SettlementResult) -> None:
    _settlement_replay_hook(session, settlement_result)


def reset_hooks_to_noop() -> None:
    set_startup_reconciliation_hook(_noop_startup_reconciliation)
    set_block_event_replay_hook(_noop_block_event_replay)
    set_reward_refetch_hook(_noop_reward_refetch)
    set_settlement_replay_hook(_noop_settlement_replay)

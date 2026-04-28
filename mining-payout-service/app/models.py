from decimal import Decimal
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Numeric, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class Miner(Base):
    __tablename__ = "miners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    worker_name: Mapped[str] = mapped_column(String(128))
    identity: Mapped[str] = mapped_column(String(256), unique=True, index=True)


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(Integer, nullable=True, index=True)
    identity: Mapped[str] = mapped_column(String(256), index=True)
    accepted_shares_total: Mapped[int] = mapped_column(Integer)
    accepted_work_total: Mapped[Decimal] = mapped_column(Numeric(28, 8), default=0)
    shares_rejected_total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    period_start: Mapped[datetime] = mapped_column(DateTime, index=True)
    period_end: Mapped[datetime] = mapped_column(DateTime, index=True)
    total_shares: Mapped[int] = mapped_column(Integer, default=0)
    total_work: Mapped[Decimal] = mapped_column(Numeric(28, 8), default=0)
    pool_reward_btc: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0)


class UserPayout(Base):
    __tablename__ = "user_payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settlement_id: Mapped[int] = mapped_column(ForeignKey("settlements.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    contribution_value: Mapped[Decimal] = mapped_column(Numeric(28, 8), default=0)
    payout_fraction: Mapped[Decimal] = mapped_column(Numeric(18, 12), default=0)
    amount_btc: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")


class PayoutEvent(Base):
    __tablename__ = "payout_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payout_id: Mapped[int] = mapped_column(ForeignKey("user_payouts.id"), index=True)
    payload_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class CarryState(Base):
    __tablename__ = "carry_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    carry_btc: Mapped[float] = mapped_column(Numeric(18, 8), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive)


class BlockCounterState(Base):
    __tablename__ = "block_counter_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    channel_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    last_blocks_found_total: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class SnapshotBlock(Base):
    __tablename__ = "snapshot_block"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    found_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    channel_id: Mapped[int] = mapped_column(Integer, index=True)
    worker_identity: Mapped[str] = mapped_column(String(256), index=True)
    blockhash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(64), default="translator_log")
    reward_sats: Mapped[int] = mapped_column(Integer, nullable=True)
    reward_fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=True, index=True)
    settlement_id: Mapped[int] = mapped_column(
        ForeignKey("settlements.id"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)


class WorkAccrualBucket(Base):
    __tablename__ = "work_accrual_bucket"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    accumulated_work: Mapped[Decimal] = mapped_column(Numeric(28, 8), default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow_naive, index=True)

from pathlib import Path
from sqlalchemy import inspect
import sqlite3

from app.db import make_engine
from app.init_db import init_db


def test_schema_tables_created(tmp_path: Path) -> None:
    db_file = tmp_path / "schema_test.db"
    init_db(str(db_file))
    init_db(str(db_file))  # idempotency check

    engine = make_engine(str(db_file))
    table_names = set(inspect(engine).get_table_names())

    assert table_names == {
        "users",
        "miners",
        "metric_snapshots",
        "settlements",
        "user_payouts",
        "payout_events",
        "carry_state",
        "block_counter_state",
    }


def test_init_db_migrates_existing_sqlite_schema(tmp_path: Path) -> None:
    db_file = tmp_path / "schema_migrate.db"
    connection = sqlite3.connect(db_file)
    try:
        connection.execute(
            "CREATE TABLE metric_snapshots (id INTEGER PRIMARY KEY, identity VARCHAR(256), accepted_shares_total INTEGER, created_at DATETIME)"
        )
        connection.execute(
            "CREATE TABLE settlements (id INTEGER PRIMARY KEY, status VARCHAR(32), period_start DATETIME, period_end DATETIME, pool_reward_btc NUMERIC(18, 8))"
        )
        connection.execute(
            "CREATE TABLE user_payouts (id INTEGER PRIMARY KEY, settlement_id INTEGER, user_id INTEGER, amount_btc NUMERIC(18, 8), idempotency_key VARCHAR(128), status VARCHAR(32))"
        )
        connection.commit()
    finally:
        connection.close()

    init_db(str(db_file))

    engine = make_engine(str(db_file))
    inspector = inspect(engine)

    metric_snapshot_columns = {column["name"] for column in inspector.get_columns("metric_snapshots")}
    settlement_columns = {column["name"] for column in inspector.get_columns("settlements")}
    user_payout_columns = {column["name"] for column in inspector.get_columns("user_payouts")}

    assert {"channel_id", "accepted_work_total", "shares_rejected_total"}.issubset(metric_snapshot_columns)
    assert {"total_shares", "total_work"}.issubset(settlement_columns)
    assert {"contribution_value", "payout_fraction"}.issubset(user_payout_columns)
    assert "block_counter_state" in set(inspector.get_table_names())

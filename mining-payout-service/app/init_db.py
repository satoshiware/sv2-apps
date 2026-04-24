import os

from sqlalchemy import inspect, text

from app.db import Base, make_engine
import app.models  # noqa: F401 - ensure models are imported before create_all


SQLITE_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "metric_snapshots": {
        "channel_id": "INTEGER",
        "accepted_work_total": "NUMERIC(28, 8) DEFAULT 0",
        "shares_rejected_total": "INTEGER DEFAULT 0",
    },
    "settlements": {
        "total_shares": "INTEGER DEFAULT 0",
        "total_work": "NUMERIC(28, 8) DEFAULT 0",
    },
    "user_payouts": {
        "contribution_value": "NUMERIC(28, 8) DEFAULT 0",
        "payout_fraction": "NUMERIC(18, 12) DEFAULT 0",
    },
    "payout_events": {
        "created_at": "DATETIME",
    },
    "carry_state": {
        "updated_at": "DATETIME",
    },
}


def _apply_sqlite_migrations(engine) -> None:
    inspector = inspect(engine)
    with engine.begin() as connection:
        for table_name, column_defs in SQLITE_COLUMN_MIGRATIONS.items():
            if table_name not in inspector.get_table_names():
                continue

            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            for column_name, column_sql in column_defs.items():
                if column_name in existing_columns:
                    continue
                connection.execute(
                    text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
                )


def init_db(db_path: str | None = None) -> str:
    path = db_path or os.getenv("DB_PATH", "./payouts.db")
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    _apply_sqlite_migrations(engine)
    return path


if __name__ == "__main__":
    db_file = init_db()
    print(f"Initialized database at {db_file}")

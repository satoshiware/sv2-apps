from pathlib import Path
from sqlalchemy import inspect

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
    }

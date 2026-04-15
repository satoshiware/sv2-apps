import os

from app.db import Base, make_engine
import app.models  # noqa: F401 - ensure models are imported before create_all


def init_db(db_path: str | None = None) -> str:
    path = db_path or os.getenv("DB_PATH", "./payouts.db")
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    return path


if __name__ == "__main__":
    db_file = init_db()
    print(f"Initialized database at {db_file}")

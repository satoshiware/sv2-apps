import pytest

from app.db import Base, make_engine, make_session_factory
from app.mapping import map_miner_identity, parse_identity
from app.models import Miner, User


@pytest.fixture
def session(tmp_path):
    db_file = tmp_path / "mapping_test.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)
    with Session() as s:
        yield s


def test_parse_identity_valid() -> None:
    parts = parse_identity("baveet.miner1")
    assert parts.username == "baveet"
    assert parts.worker == "miner1"


@pytest.mark.parametrize(
    "identity",
    ["", "baveet", ".miner1", "baveet.", "baveet miner1", "baveet. miner1"],
)
def test_parse_identity_invalid(identity: str) -> None:
    with pytest.raises(ValueError):
        parse_identity(identity)


def test_map_identity_creates_single_user_multiple_miners(session) -> None:
    user1, miner1 = map_miner_identity(session, "baveet.miner1")
    user2, miner2 = map_miner_identity(session, "baveet.miner2")
    session.commit()

    assert user1.id == user2.id
    assert miner1.id != miner2.id

    users = session.query(User).all()
    miners = session.query(Miner).all()
    assert len(users) == 1
    assert len(miners) == 2


def test_map_identity_is_idempotent_for_existing_identity(session) -> None:
    user1, miner1 = map_miner_identity(session, "baveet.miner1")
    user2, miner2 = map_miner_identity(session, "baveet.miner1")
    session.commit()

    assert user1.id == user2.id
    assert miner1.id == miner2.id
    assert session.query(User).count() == 1
    assert session.query(Miner).count() == 1

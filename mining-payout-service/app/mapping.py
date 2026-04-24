from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Miner, User


@dataclass(frozen=True)
class IdentityParts:
    username: str
    worker: str


def parse_identity(identity: str) -> IdentityParts:
    """Parse a worker identity in the format 'username.worker'."""
    value = (identity or "").strip()
    if not value or "." not in value:
        raise ValueError("Identity must be in 'username.worker' format")

    username, worker = value.split(".", 1)
    if not username or not worker:
        raise ValueError("Identity must include non-empty username and worker")
    if " " in username or " " in worker:
        raise ValueError("Identity cannot contain spaces")

    return IdentityParts(username=username, worker=worker)


def map_miner_identity(session: Session, identity: str) -> tuple[User, Miner]:
    """Map identity to user/miner records, creating missing rows as needed."""
    parts = parse_identity(identity)

    user = session.execute(select(User).where(User.username == parts.username)).scalar_one_or_none()
    if user is None:
        user = User(username=parts.username)
        session.add(user)
        session.flush()

    miner = session.execute(select(Miner).where(Miner.identity == identity)).scalar_one_or_none()
    if miner is None:
        miner = Miner(user_id=user.id, worker_name=parts.worker, identity=identity)
        session.add(miner)
        session.flush()

    return user, miner

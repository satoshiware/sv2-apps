from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from fastapi import FastAPI

from app.config import load_settings
from app.db import make_engine, make_session_factory
from app.models import PayoutEvent, Settlement, UserPayout

app = FastAPI(title="Mining Payout Service", version="0.1.0")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


def _new_session() -> Session:
    settings = load_settings()
    engine = make_engine(settings.db_path)
    SessionFactory = make_session_factory(engine)
    return SessionFactory()


@app.get("/service-metrics")
def service_metrics() -> dict:
    with _new_session() as session:
        settlements_total = session.execute(select(func.count(Settlement.id))).scalar_one()
        payouts_sent_total = session.execute(
            select(func.count(UserPayout.id)).where(UserPayout.status == "sent")
        ).scalar_one()
        payout_failures_total = session.execute(
            select(func.count(PayoutEvent.id)).where(PayoutEvent.status == "pending_sent")
        ).scalar_one()
        last_settlement_timestamp = session.execute(select(func.max(Settlement.period_end))).scalar_one()

    return {
        "settlements_total": int(settlements_total or 0),
        "payouts_sent_total": int(payouts_sent_total or 0),
        "payout_failures_total": int(payout_failures_total or 0),
        "last_settlement_timestamp": last_settlement_timestamp.isoformat()
        if last_settlement_timestamp
        else None,
    }

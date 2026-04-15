from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from app.models import MetricSnapshot
from app.poller import poll_metrics_once


class _Response:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def session(tmp_path: Path):
    db_file = tmp_path / "poller_test.db"
    engine = make_engine(str(db_file))
    Base.metadata.create_all(engine)
    Session = make_session_factory(engine)
    with Session() as s:
        yield s


def test_two_consecutive_polls_create_two_snapshot_rows(monkeypatch, session) -> None:
    payloads = [
        '\n'.join([
            '# HELP sv2_server_shares_accepted_total ...',
            'sv2_server_shares_accepted_total{channel_id="1",user_identity="baveet.miner1"} 10',
        ]),
        '\n'.join([
            '# HELP sv2_server_shares_accepted_total ...',
            'sv2_server_shares_accepted_total{channel_id="1",user_identity="baveet.miner1"} 11',
        ]),
    ]

    def _fake_get(_url: str, timeout: int):
        _ = timeout
        return _Response(payloads.pop(0))

    monkeypatch.setattr("app.poller.requests.get", _fake_get)

    created_1 = poll_metrics_once(session, "http://127.0.0.1:9092/metrics")
    created_2 = poll_metrics_once(session, "http://127.0.0.1:9092/metrics")

    rows = (
        session.query(MetricSnapshot)
        .filter(MetricSnapshot.identity == "baveet.miner1")
        .order_by(MetricSnapshot.id.asc())
        .all()
    )

    assert created_1 == 1
    assert created_2 == 1
    assert len(rows) == 2
    assert rows[0].accepted_shares_total == 10
    assert rows[1].accepted_shares_total == 11


def test_poller_parses_sv1_client_metric(monkeypatch, session) -> None:
    payload = '\n'.join([
        '# HELP sv1_client_shares_accepted_total ...',
        'sv1_client_shares_accepted_total{client_id="1",user_identity="baveet.miner1"} 12',
        'sv1_client_shares_accepted_total{client_id="2",user_identity="baveet.miner2"} 7',
    ])

    def _fake_get(_url: str, timeout: int):
        _ = timeout
        return _Response(payload)

    monkeypatch.setattr("app.poller.requests.get", _fake_get)

    created = poll_metrics_once(session, "http://127.0.0.1:9092/metrics")

    rows = (
        session.query(MetricSnapshot)
        .order_by(MetricSnapshot.identity.asc())
        .all()
    )

    assert created == 2
    assert [row.identity for row in rows] == ["baveet.miner1", "baveet.miner2"]
    assert [row.accepted_shares_total for row in rows] == [12, 7]


def test_poller_returns_zero_if_metric_missing(monkeypatch, session) -> None:
    payload = '# HELP something_else x\nother_metric 1\n'

    def _fake_get(_url: str, timeout: int):
        _ = timeout
        return _Response(payload)

    monkeypatch.setattr("app.poller.requests.get", _fake_get)

    created = poll_metrics_once(session, "http://127.0.0.1:9092/metrics")
    assert created == 0
    assert session.query(MetricSnapshot).count() == 0

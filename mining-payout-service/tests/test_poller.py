from pathlib import Path

import pytest

from app.db import Base, make_engine, make_session_factory
from app.models import MetricSnapshot, SnapshotBlock
from app.poller import poll_channels_once, poll_metrics_once, upsert_snapshot_blocks


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


class _JsonResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> dict:
        return self._payload


def test_poll_channels_once_persists_work_and_channel_ids(monkeypatch, session) -> None:
    payload = {
        "status": "ok",
        "configured": True,
        "data": {
            "extended_channels": [
                {
                    "channel_id": 3,
                    "user_identity": "baveet.miner1",
                    "shares_acknowledged": 10,
                    "shares_rejected": 1,
                    "share_work_sum": 128.5,
                },
                {
                    "channel_id": 7,
                    "user_identity": "baveet.miner2",
                    "shares_acknowledged": 12,
                    "shares_rejected": 0,
                    "share_work_sum": 256.0,
                },
            ],
            "standard_channels": [],
        },
    }

    def _fake_get(_url: str, timeout: int, headers=None):
        _ = (timeout, headers)
        return _JsonResponse(payload)

    monkeypatch.setattr("app.poller.requests.get", _fake_get)

    created = poll_channels_once(session, "http://127.0.0.1:8080/v1/translator/upstream/channels")

    rows = session.query(MetricSnapshot).order_by(MetricSnapshot.channel_id.asc()).all()

    assert created == 2
    assert [(row.channel_id, row.identity) for row in rows] == [
        (3, "baveet.miner1"),
        (7, "baveet.miner2"),
    ]
    assert [row.accepted_shares_total for row in rows] == [10, 12]
    assert [float(row.accepted_work_total) for row in rows] == [128.5, 256.0]
    assert [row.shares_rejected_total for row in rows] == [1, 0]


def test_poll_channels_once_prefers_downstream_identity_by_channel(monkeypatch, session) -> None:
    upstream_payload = {
        "status": "ok",
        "configured": True,
        "data": {
            "extended_channels": [
                {
                    "channel_id": 2,
                    "user_identity": "baveetstudy.miner1",
                    "shares_acknowledged": 4,
                    "shares_rejected": 0,
                    "share_work_sum": 93130.0,
                },
                {
                    "channel_id": 3,
                    "user_identity": "baveetstudy.miner2",
                    "shares_acknowledged": 0,
                    "shares_rejected": 0,
                    "share_work_sum": 0.0,
                },
            ],
            "standard_channels": [],
        },
    }

    downstream_payload = {
        "status": "ok",
        "configured": True,
        "data": {
            "items": [
                {
                    "client_id": 1,
                    "channel_id": 2,
                    "authorized_worker_name": "baveet.worker3",
                    "user_identity": "baveet.worker3",
                },
                {
                    "client_id": 2,
                    "channel_id": 3,
                    "authorized_worker_name": "Ben.Cust1",
                    "user_identity": "Ben.Cust1",
                },
            ]
        },
    }

    payloads = [upstream_payload, downstream_payload]

    def _fake_get(_url: str, timeout: int, headers=None):
        _ = (timeout, headers)
        return _JsonResponse(payloads.pop(0))

    monkeypatch.setattr("app.poller.requests.get", _fake_get)

    created = poll_channels_once(
        session,
        "http://127.0.0.1:8080/v1/translator/upstream/channels",
        downstream_url="http://127.0.0.1:8080/v1/translator/downstreams",
    )

    rows = session.query(MetricSnapshot).order_by(MetricSnapshot.channel_id.asc()).all()

    assert created == 2
    assert [(row.channel_id, row.identity) for row in rows] == [
        (2, "baveet.worker3"),
        (3, "Ben.Cust1"),
    ]


def test_upsert_snapshot_blocks_creates_rows_from_blocks_payload(session) -> None:
    created = upsert_snapshot_blocks(
        session,
        [
            {
                "detected_time": 1777314807,
                "channel_id": 2,
                "worker_identity": "Ben.Cust1",
                "blockhash": "000000abc",
            }
        ],
    )

    assert created == 1
    all_rows = session.query(SnapshotBlock).all()
    assert len(all_rows) == 1
    assert all_rows[0].blockhash == "000000abc"
    assert all_rows[0].channel_id == 2
    assert all_rows[0].worker_identity == "Ben.Cust1"
    assert all_rows[0].source == "translator_blocks_api"


def test_upsert_snapshot_blocks_is_idempotent_by_blockhash(session) -> None:
    payload = [
        {
            "detected_time": 1777314807,
            "channel_id": 2,
            "worker_identity": "Ben.Cust1",
            "blockhash": "000000dup",
            "source": "translator_api",
        }
    ]

    created_first = upsert_snapshot_blocks(session, payload)
    created_second = upsert_snapshot_blocks(session, payload)

    assert created_first == 1
    assert created_second == 0
    assert session.query(SnapshotBlock).count() == 1


def test_upsert_snapshot_blocks_skips_null_blockhash(session) -> None:
    """Rows with blockhash=null (unresolved) are silently skipped by the normalizer."""
    created = upsert_snapshot_blocks(
        session,
        [
            {
                "detected_time": 1777403179,
                "channel_id": 2,
                "worker_identity": "Ben.Cust1",
                "blockhash": None,
                "blockhash_status": "unresolved",
            }
        ],
    )
    assert created == 0
    assert session.query(SnapshotBlock).count() == 0


def test_upsert_snapshot_blocks_uses_nearest_candidate_blockhash(session) -> None:
    created = upsert_snapshot_blocks(
        session,
        [
            {
                "detected_time": 1777572622,
                "channel_id": 4,
                "worker_identity": "baveet.worker3",
                "blockhash": None,
                "blockhash_status": "unresolved",
                "correlation_status": "counter_delta_only",
                "nearest_candidate_blockhash": "000000000000011507e44e123a627c814d420cf4a144aadfa0fe51509b9c44b7",
            }
        ],
    )

    assert created == 1
    row = session.query(SnapshotBlock).one()
    assert row.channel_id == 4
    assert row.worker_identity == "baveet.worker3"
    assert row.blockhash == "000000000000011507e44e123a627c814d420cf4a144aadfa0fe51509b9c44b7"


def test_upsert_snapshot_blocks_uses_candidate_blocks_list_when_nearest_missing(session) -> None:
    created = upsert_snapshot_blocks(
        session,
        [
            {
                "detected_time": 1777572622,
                "channel_id": 4,
                "worker_identity": "baveet.worker3",
                "blockhash": None,
                "candidate_blocks": [
                    {
                        "blockhash": "000000000000011507e44e123a627c814d420cf4a144aadfa0fe51509b9c44b7"
                    }
                ],
            }
        ],
    )

    assert created == 1
    row = session.query(SnapshotBlock).one()
    assert row.blockhash == "000000000000011507e44e123a627c814d420cf4a144aadfa0fe51509b9c44b7"

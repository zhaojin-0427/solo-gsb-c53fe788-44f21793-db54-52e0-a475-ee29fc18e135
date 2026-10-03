"""API-level tests: both query entry points share one adjudication, and
verification records are solidified (old records are never recomputed).
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db.init_db()
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


NETWORK = {
    "floors": [{"id": "f1", "name": "1F", "level": 0}],
    "nodes": [
        {"id": "s", "floor_id": "f1", "x": 0, "y": 0, "kind": "entrance"},
        {"id": "m", "floor_id": "f1", "x": 10, "y": 0, "kind": "junction"},
        {"id": "t", "floor_id": "f1", "x": 20, "y": 0, "kind": "room"},
    ],
    "edges": [
        {"id": "e1", "from": "s", "to": "m", "type": "corridor",
         "length": 10, "seconds": 600, "width": 2.0, "slope": 0},
        {"id": "e2", "from": "s", "to": "m", "type": "corridor",
         "length": 10, "seconds": 300, "width": 2.0, "slope": 0},
        {"id": "e3", "from": "m", "to": "t", "type": "corridor",
         "length": 5, "seconds": 60, "width": 2.0, "slope": 0},
    ],
}

QUERY = {
    "start": "s",
    "end": "t",
    "departure": "2026-10-01T08:00:00+08:00",
    "min_width": 0.9,
    "max_slope": 8,
}


def _import_network(client):
    resp = client.post("/api/network/import", json=NETWORK)
    assert resp.status_code == 200, resp.text


def _register_e3_closure(client):
    resp = client.post("/api/closures", json={
        "edge_id": "e3",
        "start": "2026-10-01T08:00:00+08:00",
        "end": "2026-10-01T09:00:00+08:00",
        "tz": "Asia/Shanghai",
    })
    assert resp.status_code == 200, resp.text


def test_api_query_applies_closure_tiebreak(client):
    """The /api/route/query entry point returns the adjudicated route:
    both walks wait out the e3 closure, tie on arrival and length, and
    the smaller edge-ID sequence (e1) wins."""
    _import_network(client)
    _register_e3_closure(client)
    resp = client.post("/api/route/query", json=QUERY)
    assert resp.status_code == 200, resp.text
    res = resp.json()
    assert res["reachable"]
    assert [s["edge_id"] for s in res["steps"]] == ["e1", "e3"]
    assert res["arrival"] == "2026-10-01T01:01:00+00:00"
    assert res["wait_seconds"] == 3000.0
    assert res["record_id"] >= 1


def test_frontend_is_served_and_uses_same_endpoint(client):
    """The page entry point is served at / and queries the very same
    /api/route/query endpoint, so page and API share one adjudication."""
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "轮椅路线查询" in html
    js = client.get("/static/app.js")
    assert js.status_code == 200
    assert "/api/route/query" in js.text


def test_records_are_solidified_and_never_recomputed(client):
    """A record snapshots its result; later closure changes produce a new
    adjudication for new queries but leave the old record untouched."""
    _import_network(client)
    _register_e3_closure(client)

    first = client.post("/api/route/query", json=QUERY).json()
    assert [s["edge_id"] for s in first["steps"]] == ["e1", "e3"]

    # Remove the closure: the faster e2 channel now wins outright.
    closures = client.get("/api/closures").json()["closures"]
    for c in closures:
        client.delete(f"/api/closures/{c['id']}")
    second = client.post("/api/route/query", json=QUERY).json()
    assert [s["edge_id"] for s in second["steps"]] == ["e2", "e3"]
    assert second["arrival"] == "2026-10-01T00:06:00+00:00"

    # The old record still shows the original solidified result.
    old = client.get(f"/api/records/{first['record_id']}").json()
    assert [s["edge_id"] for s in old["result"]["steps"]] == ["e1", "e3"]
    assert old["result"]["arrival"] == "2026-10-01T01:01:00+00:00"
    assert old["closure_snapshot"], "record must snapshot its closures"
    assert old["network_version"] == first["network_version"]

    # And the new record holds the new adjudication.
    new = client.get(f"/api/records/{second['record_id']}").json()
    assert [s["edge_id"] for s in new["result"]["steps"]] == ["e2", "e3"]

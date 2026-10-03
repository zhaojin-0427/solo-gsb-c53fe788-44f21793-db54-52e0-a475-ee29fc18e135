"""Regression tests for time-dependent wheelchair routing.

Covers the closure tie-break adjudication (final arrival, then total
length, then edge-ID sequence), parallel-channel filtering, and the
unreachability diagnosis.
"""
from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import routing  # noqa: E402

DEPARTURE = "2026-10-01T08:00:00+08:00"


def utc_closure(start: str, end: str):
    return (routing.to_utc(routing.parse_dt(start)),
            routing.to_utc(routing.parse_dt(end)))


def build(edges, extra_nodes=()):
    nodes = [
        {"id": "s", "floor_id": "f1", "x": 0, "y": 0, "kind": "entrance"},
        {"id": "m", "floor_id": "f1", "x": 10, "y": 0, "kind": "junction"},
        {"id": "t", "floor_id": "f1", "x": 20, "y": 0, "kind": "room"},
    ] + list(extra_nodes)
    data = {
        "floors": [{"id": "f1", "name": "1F", "level": 0}],
        "nodes": nodes,
        "edges": edges,
    }
    return routing.build_graph(data)


def corridor(eid, frm, to, length, seconds, width=2.0, slope=0.0):
    return {"id": eid, "from": frm, "to": to, "type": "corridor",
            "length": length, "seconds": seconds, "width": width, "slope": slope}


def query(G, closures, start="s", end="t", departure=DEPARTURE,
          min_width=0.9, max_slope=8.0):
    return routing.find_route(
        G, start, end, routing.parse_dt(departure),
        min_width, max_slope, closures,
    )


def edge_ids(res):
    return [s["edge_id"] for s in res["steps"]]


# ---------------------------------------------------------------------------
# Closure tie-break adjudication
# ---------------------------------------------------------------------------

def test_closure_tiebreak_keeps_smaller_edge_id_sequence():
    """Two parallel channels converge at m; the downstream closure forces
    both walks to wait for the same release time. Arrival and distance
    tie, so the smaller edge-ID sequence must win even though its walk
    reached the merge node later."""
    G = build([
        corridor("e1", "s", "m", 10, 600),  # slower, smaller ID
        corridor("e2", "s", "m", 10, 300),  # faster, larger ID
        corridor("e3", "m", "t", 5, 60),
    ])
    closures = {"e3": [utc_closure("2026-10-01T08:00:00+08:00",
                                   "2026-10-01T09:00:00+08:00")]}
    res = query(G, closures)
    assert res["reachable"]
    # Both walks: wait until 09:00 (+08:00), arrive 09:01, length 15.
    assert res["arrival"] == "2026-10-01T01:01:00+00:00"
    assert res["total_length"] == 15.0
    # ("e1","e3") < ("e2","e3"): the e1 walk waits 50 min, not 55.
    assert edge_ids(res) == ["e1", "e3"]
    assert res["wait_seconds"] == 3000.0


def test_closure_tiebreak_multi_hop_convergence():
    """Same adjudication when the converging walks have more than one
    edge: the later-but-lexicographically-smaller walk must survive label
    dominance at the merge node."""
    extra = [
        {"id": "p", "floor_id": "f1", "x": 5, "y": 5, "kind": "junction"},
        {"id": "q", "floor_id": "f1", "x": 5, "y": -5, "kind": "junction"},
    ]
    G = build([
        corridor("e8", "p", "m", 5, 100),
        corridor("e9", "s", "p", 5, 100),   # via p: arrives m at +200, seq ("e9","e8")
        corridor("e1", "s", "q", 5, 150),
        corridor("e2", "q", "m", 5, 150),   # via q: arrives m at +300, seq ("e1","e2")
        corridor("e3", "m", "t", 5, 60),
    ], extra_nodes=extra)
    closures = {"e3": [utc_closure("2026-10-01T08:00:00+08:00",
                                   "2026-10-01T09:00:00+08:00")]}
    res = query(G, closures)
    assert res["reachable"]
    assert res["arrival"] == "2026-10-01T01:01:00+00:00"
    assert res["total_length"] == 15.0
    assert edge_ids(res) == ["e1", "e2", "e3"]


def test_closure_tiebreak_distance_decides_before_sequence():
    """When arrival ties after the closure but distances differ, the
    shorter walk wins even with the larger edge-ID sequence."""
    G = build([
        corridor("e1", "s", "m", 12, 600),  # slower and longer
        corridor("e2", "s", "m", 10, 300),  # faster and shorter
        corridor("e3", "m", "t", 5, 60),
    ])
    closures = {"e3": [utc_closure("2026-10-01T08:00:00+08:00",
                                   "2026-10-01T09:00:00+08:00")]}
    res = query(G, closures)
    assert res["reachable"]
    assert res["total_length"] == 15.0
    assert edge_ids(res) == ["e2", "e3"]


def test_earlier_arrival_wins_without_closure():
    """No closure: the faster channel simply arrives earlier; the edge-ID
    sequence is only a tie-break and must not override arrival."""
    G = build([
        corridor("e1", "s", "m", 10, 600),
        corridor("e2", "s", "m", 10, 300),
        corridor("e3", "m", "t", 5, 60),
    ])
    res = query(G, {})
    assert res["reachable"]
    assert edge_ids(res) == ["e2", "e3"]
    assert res["arrival"] == "2026-10-01T00:06:00+00:00"


def test_closure_tiebreak_sequence_decides_when_fully_tied():
    """Identical travel times and distances: the smaller edge-ID sequence
    wins outright."""
    G = build([
        corridor("e1", "s", "m", 10, 300),
        corridor("e2", "s", "m", 10, 300),
        corridor("e3", "m", "t", 5, 60),
    ])
    res = query(G, {})
    assert res["reachable"]
    assert edge_ids(res) == ["e1", "e3"]


def test_timezone_aware_closure_window():
    """Closures registered in another timezone still apply at the right
    UTC instant: 01:00-02:00 UTC equals 09:00-10:00 in Asia/Shanghai."""
    G = build([
        corridor("e1", "s", "m", 10, 600),
        corridor("e2", "s", "m", 10, 300),
        corridor("e3", "m", "t", 5, 60),
    ])
    closures = {"e3": [utc_closure("2026-10-01T09:00:00+08:00",
                                   "2026-10-01T10:00:00+08:00")]}
    res = query(G, closures)
    assert res["reachable"]
    # e3 is free until 09:00 (+08:00): e2 walk departs m at 08:05, well
    # before the closure, so it keeps its earlier arrival.
    assert edge_ids(res) == ["e2", "e3"]
    assert res["arrival"] == "2026-10-01T00:06:00+00:00"


# ---------------------------------------------------------------------------
# Parallel-channel filtering and unreachability diagnosis (no regression)
# ---------------------------------------------------------------------------

def sample_graph():
    import json
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))), "data", "sample_network.json")
    with open(path, encoding="utf-8") as fh:
        return routing.build_graph(json.load(fh))


def test_parallel_channels_filtered_independently_by_width():
    """e27 (width 2.0) and e28 (width 0.9) share (n2, n14): a 1.0 m
    minimum width disables only e28; 0.9 m enables both and the faster
    e27 wins."""
    G = sample_graph()
    res = query(G, {}, start="n2", end="n14", min_width=1.0)
    assert res["reachable"]
    assert edge_ids(res) == ["e27"]

    res = query(G, {}, start="n2", end="n14", min_width=0.9)
    assert res["reachable"]
    assert edge_ids(res) == ["e27"]  # 6 s beats 30 s


def test_parallel_channel_detour_when_fast_one_closed():
    """Closing e27 for the departure hour makes the slower parallel e28
    the best option (waiting 10 min loses to a 30 s ride)."""
    G = sample_graph()
    closures = {"e27": [utc_closure("2026-10-01T08:00:00+08:00",
                                    "2026-10-01T08:10:00+08:00")]}
    res = query(G, closures, start="n2", end="n14", min_width=0.9)
    assert res["reachable"]
    assert edge_ids(res) == ["e28"]
    assert res["arrival"] == "2026-10-01T00:00:30+00:00"


def test_unreachable_when_closure_exceeds_24h():
    """e27 closed beyond the 24 h horizon and e28 too narrow: unreachable
    with the blocking closure listed for diagnosis."""
    G = sample_graph()
    closures = {"e27": [utc_closure("2026-10-01T08:00:00+08:00",
                                    "2026-10-03T08:00:00+08:00")]}
    res = query(G, closures, start="n2", end="n14", min_width=1.0)
    assert not res["reachable"]
    assert res["reasons"] == ["路径被封闭时段阻断，出发后 24 小时内无法到达"]
    assert [c["edge_id"] for c in res["blocking_closures"]] == ["e27"]


def test_unreachable_reports_static_blocking():
    """Stairs are never passable: n7 is only reachable via stairs, so the
    diagnosis reports constraint blocking rather than closures."""
    G = sample_graph()
    res = query(G, {}, start="n1", end="n7")
    assert not res["reachable"]
    assert "不存在满足净宽/坡度/楼梯/跨层限制的可达路径" in res["reasons"]
    assert "所有路径均被净宽、坡度、楼梯或跨层限制阻断" in res["reasons"]
    assert res["blocking_closures"] == []


def test_unreachable_when_nodes_missing():
    G = sample_graph()
    res = query(G, {}, start="n1", end="nope")
    assert not res["reachable"]
    assert res["reasons"] == ["终点 nope 不在路网中"]


def test_cross_floor_route_uses_elevator():
    """A legal cross-floor trip uses the elevator and is reported with
    the per-step channel details."""
    G = sample_graph()
    res = query(G, {}, start="n1", end="n11", min_width=1.0)
    assert res["reachable"]
    assert edge_ids(res) == ["e1", "e3", "e5", "e7", "e15"]
    assert any(s["cross_floor"] for s in res["steps"])
    assert all(s["type"] != "stairs" for s in res["steps"])

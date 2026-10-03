"""Network validation and time-dependent wheelchair routing.

The routing problem is a time-dependent shortest path in a directed
multi-graph:
  * Multiple channels may share the same (from, to) pair; every channel is
    keyed by its own ID and participates independently in all feasibility
    checks and searches.
  * Stairs are never passable.
  * Cross-floor edges are only allowed for elevators or ramps.
  * Corridors connect nodes on the same floor.
  * The wheelchair must satisfy min clear width and max slope.
  * Waiting at nodes is permitted.
  * Traversal of an edge must not intersect any registered closure period.
  * We seek the earliest arrival within 24h of departure; ties are broken
    by total edge length, then by lexicographic edge-ID sequence.
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import networkx as nx

# Edge types
TYPE_CORRIDOR = "corridor"
TYPE_STAIRS = "stairs"
TYPE_ELEVATOR = "elevator"
TYPE_RAMP = "ramp"

CROSS_FLOOR_TYPES = {TYPE_ELEVATOR, TYPE_RAMP}
PASSABLE_TYPES = {TYPE_CORRIDOR, TYPE_ELEVATOR, TYPE_RAMP}

HOURS_24 = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class NetworkValidationError(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_network(data: dict[str, Any]) -> None:
    """Validate the imported network. Raises NetworkValidationError on issues."""
    errors: list[str] = []

    floors = data.get("floors", [])
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    floor_ids = {f["id"] for f in floors}
    node_ids = {n["id"] for n in nodes}

    # Duplicate ids
    if len(floor_ids) != len(floors):
        errors.append("楼层 ID 存在重复")
    if len(node_ids) != len(nodes):
        errors.append("节点 ID 存在重复")
    edge_ids = [e["id"] for e in edges]
    if len(set(edge_ids)) != len(edge_ids):
        errors.append("通道 ID 存在重复")

    # Node floor references
    for n in nodes:
        if n["floor_id"] not in floor_ids:
            errors.append(f"节点 {n['id']} 引用了不存在的楼层 {n['floor_id']}")

    # Edge references and type checks
    for e in edges:
        if e["from"] not in node_ids:
            errors.append(f"通道 {e['id']} 的起点 {e['from']} 不存在")
        if e["to"] not in node_ids:
            errors.append(f"通道 {e['id']} 的终点 {e['to']} 不存在")

    # Dangling nodes: a node that appears in no edge at all.
    connected: set[str] = set()
    for e in edges:
        connected.add(e["from"])
        connected.add(e["to"])
    for n in nodes:
        if n["id"] not in connected:
            errors.append(f"节点 {n['id']} 是悬空节点（未连接任何通道）")

    # Cross-floor legality — every parallel channel is checked individually.
    node_floor = {n["id"]: n["floor_id"] for n in nodes}
    for e in edges:
        if e["from"] in node_ids and e["to"] in node_ids:
            f1 = node_floor[e["from"]]
            f2 = node_floor[e["to"]]
            etype = e.get("type", TYPE_CORRIDOR)
            if f1 != f2 and etype not in CROSS_FLOOR_TYPES:
                errors.append(
                    f"通道 {e['id']} 跨楼层但类型为 {etype}，"
                    f"跨层仅允许电梯或坡道"
                )
            if f1 == f2 and etype in CROSS_FLOOR_TYPES:
                errors.append(
                    f"通道 {e['id']} 同楼层但类型为 {etype}，类型与楼层不一致"
                )

    # Numeric sanity
    for e in edges:
        if e.get("length", 0) < 0:
            errors.append(f"通道 {e['id']} 长度为负")
        if e.get("seconds", 0) < 0:
            errors.append(f"通道 {e['id']} 通行秒数为负")
        if e.get("width", 0) < 0:
            errors.append(f"通道 {e['id']} 净宽为负")
        if not (0 <= e.get("slope", 0) <= 90):
            errors.append(f"通道 {e['id']} 坡度应在 0~90 之间")

    if errors:
        raise NetworkValidationError(errors)


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------

def build_graph(data: dict[str, Any]) -> nx.MultiDiGraph:
    """Build a NetworkX MultiDiGraph from validated network data.

    A MultiDiGraph is required because several channels may share the same
    (from, to) pair with different IDs / widths / seconds / closures. Each
    edge is keyed by its channel ID so no channel can overwrite another.
    """
    G = nx.MultiDiGraph()
    G.graph["floor_names"] = {f["id"]: f.get("name", f["id"]) for f in data["floors"]}
    for n in data["nodes"]:
        G.add_node(
            n["id"],
            floor_id=n["floor_id"],
            x=n["x"],
            y=n["y"],
            kind=n.get("kind", "junction"),
            name=n.get("name"),
        )
    node_floor = {n["id"]: n["floor_id"] for n in data["nodes"]}
    for e in data["edges"]:
        G.add_edge(
            e["from"],
            e["to"],
            key=e["id"],
            id=e["id"],
            type=e.get("type", TYPE_CORRIDOR),
            length=float(e.get("length", 0.0)),
            seconds=float(e.get("seconds", 0.0)),
            width=float(e.get("width", 0.0)),
            slope=float(e.get("slope", 0.0)),
            cross_floor=node_floor[e["from"]] != node_floor[e["to"]],
        )
    return G


# ---------------------------------------------------------------------------
# Timezone-aware datetime helpers
# ---------------------------------------------------------------------------

def parse_dt(s: str) -> datetime:
    """Parse an ISO 8601 string. If naive, assume UTC."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Closure-aware edge departure
# ---------------------------------------------------------------------------

def earliest_departure(
    arrival: datetime,
    seconds: float,
    closures: list[tuple[datetime, datetime]],
) -> datetime:
    """Given arrival time at the from-node, return the earliest departure
    time >= arrival such that traversing the edge does not intersect any
    closure period.

    closures is a list of (start, end) UTC datetimes with start < end.
    """
    dep = arrival
    secs = timedelta(seconds=seconds)
    changed = True
    while changed:
        changed = False
        for cs, ce in closures:
            # Intersection of [dep, dep+secs) with [cs, ce)
            if dep < ce and dep + secs > cs:
                # Cannot finish before cs (would require dep <= cs - secs,
                # but dep >= arrival and arrival+secs > cs here). So depart
                # after the closure ends.
                if dep < ce:
                    dep = ce
                    changed = True
    return dep


# ---------------------------------------------------------------------------
# Label-setting shortest path
# ---------------------------------------------------------------------------

@dataclass
class Label:
    node: str
    arrival: datetime
    distance: float
    seq: tuple[str, ...]
    path: list[dict[str, Any]] = field(default_factory=list)

    def key(self) -> tuple:
        return (self.arrival, self.distance, self.seq)


def _dominates(a: Label, b: Label) -> bool:
    """Return True if label a dominates label b (a can never lose to b at
    the destination under the final-arrival / total-length / edge-ID
    adjudication order).

    Waiting is allowed, so an earlier arrival can always mimic a later
    one, and a strictly shorter accumulated distance can never be caught
    up — either one makes a strictly better regardless of the edge-ID
    sequence. When the distance ties, however, a downstream closure can
    force both walks to wait for the same release time, erasing any
    arrival advantage and leaving the edge-ID sequence as the decider.
    Domination then requires the sequence order to survive every common
    extension: equal length and not lexicographically larger. Sequences
    of different lengths are only produced through zero-cost cycles; keep
    both (the search bounds walk length) rather than using prefix
    ordering, which is not preserved under extension.
    """
    if a.arrival > b.arrival:
        return False
    if a.distance > b.distance:
        return False
    if a.distance < b.distance:
        return True
    # Equal distance: the edge-ID sequence may still decide the final
    # ranking once closures equalize the remaining travel times.
    if len(a.seq) == len(b.seq) and a.seq <= b.seq:
        return True
    return False


def _add_label(labels: list[Label], new: Label) -> bool:
    """Add new label to labels, removing dominated ones. Return True if added."""
    for lab in labels:
        if lab.arrival == new.arrival and lab.distance == new.distance and lab.seq == new.seq:
            return False  # identical label
        if _dominates(lab, new):
            return False
    # Remove labels dominated by new.
    kept = [lab for lab in labels if not _dominates(new, lab)]
    kept.append(new)
    labels[:] = kept
    return True


def _edge_static_ok(edata: dict[str, Any], min_width: float, max_slope: float) -> bool:
    """Static wheelchair feasibility of a single channel (independent of
    other parallel channels)."""
    if edata["type"] == TYPE_STAIRS:
        return False
    if edata["type"] not in PASSABLE_TYPES:
        return False
    if edata["width"] < min_width:
        return False
    if edata["slope"] > max_slope:
        return False
    return True


def find_route(
    G: nx.MultiDiGraph,
    start: str,
    end: str,
    departure: datetime,
    min_width: float,
    max_slope: float,
    closures_by_edge: dict[str, list[tuple[datetime, datetime]]],
) -> dict[str, Any]:
    """Find the best wheelchair route. Returns a result dict."""
    departure_utc = to_utc(departure)
    deadline = departure_utc + HOURS_24

    if start not in G:
        return {
            "reachable": False,
            "reasons": [f"起点 {start} 不在路网中"],
            "blocking_closures": [],
        }
    if end not in G:
        return {
            "reachable": False,
            "reasons": [f"终点 {end} 不在路网中"],
            "blocking_closures": [],
        }

    def static_ok(edata: dict[str, Any]) -> bool:
        return _edge_static_ok(edata, min_width, max_slope)

    # Static feasibility over the complete channel set: a subgraph view that
    # keeps every (u, v, key) channel passing the wheelchair constraints.
    static_G = nx.subgraph_view(
        G,
        filter_edge=lambda u, v, k: static_ok(G[u][v][k]),
    )
    if not nx.has_path(static_G, start, end):
        reasons = ["不存在满足净宽/坡度/楼梯/跨层限制的可达路径"]
        # Distinguish disconnection vs constraint blocking.
        if not nx.has_path(G, start, end):
            reasons.append("起终点在路网中不连通")
        else:
            reasons.append("所有路径均被净宽、坡度、楼梯或跨层限制阻断")
        return {
            "reachable": False,
            "reasons": reasons,
            "blocking_closures": [],
        }

    floor_names: dict[str, str] = G.graph.get("floor_names", {})

    # Label-setting search over all parallel channels.
    labels: dict[str, list[Label]] = {n: [] for n in G.nodes}
    start_label = Label(node=start, arrival=departure_utc, distance=0.0, seq=())
    labels[start].append(start_label)

    heap: list[tuple] = []
    counter = 0
    heapq.heappush(heap, (start_label.arrival, 0.0, (), counter, start_label))

    best: Label | None = None
    # A walk longer than the number of channels repeats a channel; cap it so
    # zero-cost cycles cannot create infinitely many labels.
    max_steps = G.number_of_edges()

    while heap:
        arr, dist, seq, _, lab = heapq.heappop(heap)
        # Skip if this label was dominated after being pushed.
        if lab not in labels.get(lab.node, []):
            continue
        # Prune against the incumbent best route. The adjudication order
        # is (final arrival, total length, edge-ID sequence); the label
        # key grows monotonically along every extension, so once a label
        # is no better than the incumbent none of its extensions can
        # still win at the destination.
        if best is not None and lab.key() >= best.key():
            continue

        if lab.node == end:
            # Labels are popped in nondecreasing key order, so the first
            # destination label settled is the adjudication winner.
            if best is None or lab.key() < best.key():
                best = lab
            continue

        if len(lab.seq) >= max_steps:
            continue

        # MultiDiGraph adjacency: G[u] maps v -> {edge_key: edata}, so every
        # parallel channel with its own ID is relaxed independently.
        for v, parallel in G[lab.node].items():
            for ekey, edata in parallel.items():
                if not static_ok(edata):
                    continue
                eid = edata["id"]
                closures = closures_by_edge.get(eid, [])
                dep = earliest_departure(lab.arrival, edata["seconds"], closures)
                arr_v = dep + timedelta(seconds=edata["seconds"])
                if arr_v > deadline:
                    continue
                dist_v = lab.distance + edata["length"]
                seq_v = lab.seq + (eid,)

                floor_id = G.nodes[v]["floor_id"]
                step = {
                    "edge_id": eid,
                    "from_node": lab.node,
                    "to_node": v,
                    "from_name": G.nodes[lab.node].get("name"),
                    "to_name": G.nodes[v].get("name"),
                    "floor_id": floor_id,
                    "floor_name": floor_names.get(floor_id, floor_id),
                    "type": edata["type"],
                    "length": edata["length"],
                    "seconds": edata["seconds"],
                    "width": edata["width"],
                    "slope": edata["slope"],
                    "wait_seconds": (dep - lab.arrival).total_seconds(),
                    "arrival": arr_v.isoformat(),
                    "cross_floor": edata["cross_floor"],
                }
                new_label = Label(
                    node=v,
                    arrival=arr_v,
                    distance=dist_v,
                    seq=seq_v,
                    path=lab.path + [step],
                )
                if _add_label(labels[v], new_label):
                    counter += 1
                    heapq.heappush(
                        heap,
                        (new_label.arrival, new_label.distance, new_label.seq, counter, new_label),
                    )

    if best is None:
        # Static path exists but closures block every option within 24h.
        blocking = _find_blocking_closures(
            G, start, end, min_width, max_slope, closures_by_edge
        )
        return {
            "reachable": False,
            "reasons": ["路径被封闭时段阻断，出发后 24 小时内无法到达"],
            "blocking_closures": blocking,
        }

    # Build result.
    wait_total = sum(s["wait_seconds"] for s in best.path)
    travel_total = sum(s["seconds"] for s in best.path)
    return {
        "reachable": True,
        "arrival": best.arrival.isoformat(),
        "total_seconds": travel_total + wait_total,
        "total_length": best.distance,
        "wait_seconds": wait_total,
        "steps": best.path,
        "reasons": [],
        "blocking_closures": [],
    }


# ---------------------------------------------------------------------------
# Unreachability diagnosis
# ---------------------------------------------------------------------------

def _static_shortest_path(
    G: nx.MultiDiGraph,
    start: str,
    end: str,
    min_width: float,
    max_slope: float,
) -> list[tuple[str, str, str]] | None:
    """Free-flow shortest static path over the complete parallel channel set.

    Primary weight is total traversal seconds; ties break by total length and
    then by the lexicographic edge-ID (channel key) sequence. Returns a list
    of (u, v, edge_key) triples, or None if no static path exists.
    """
    inf = (float("inf"), float("inf"), ())
    best_key: dict[str, tuple] = {start: (0.0, 0.0, ())}
    prev: dict[str, tuple[str, str]] = {}
    heap: list[tuple] = [(0.0, 0.0, (), start)]
    settled: set[str] = set()

    while heap:
        secs, length, seq, u = heapq.heappop(heap)
        if u in settled:
            continue
        settled.add(u)
        if u == end:
            break
        for v, parallel in G[u].items():
            for ekey, edata in parallel.items():
                if not _edge_static_ok(edata, min_width, max_slope):
                    continue
                cand = (
                    secs + edata["seconds"],
                    length + edata["length"],
                    seq + (ekey,),
                )
                if cand < best_key.get(v, inf):
                    best_key[v] = cand
                    prev[v] = (u, ekey)
                    heapq.heappush(heap, (*cand, v))

    if end not in prev:
        return None if end != start else []

    # Reconstruct (u, v, key) triples.
    triples: list[tuple[str, str, str]] = []
    cur = end
    while cur != start:
        u, ekey = prev[cur]
        triples.append((u, cur, ekey))
        cur = u
    triples.reverse()
    return triples


def _find_blocking_closures(
    G: nx.MultiDiGraph,
    start: str,
    end: str,
    min_width: float,
    max_slope: float,
    closures_by_edge: dict[str, list[tuple[datetime, datetime]]],
) -> list[dict[str, Any]]:
    """List closure periods registered on the channels of the best free-flow
    static path (one specific channel per hop, even among parallel ones)."""
    triples = _static_shortest_path(G, start, end, min_width, max_slope)
    if triples is None:
        return []

    blocking: list[dict[str, Any]] = []
    for u, v, ekey in triples:
        edata = G[u][v][ekey]
        eid = edata["id"]
        for cs, ce in closures_by_edge.get(eid, []):
            blocking.append(
                {
                    "edge_id": eid,
                    "from_node": u,
                    "to_node": v,
                    "start": cs.isoformat(),
                    "end": ce.isoformat(),
                }
            )
    return blocking

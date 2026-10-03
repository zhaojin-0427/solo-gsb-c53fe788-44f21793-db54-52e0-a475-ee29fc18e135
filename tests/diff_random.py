"""Randomized differential test: dominance-pruned search vs exhaustive.

Enumerates every feasible simple path over every parallel channel (waiting
at nodes is always allowed and edge costs are non-negative, so a repeated
node can never improve a walk), and compares its optimum against find_route
over many random small graphs with random closures, widths and slopes.
"""
import random
import sys
from datetime import datetime, timedelta, timezone

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))

import routing

BASE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def random_graph(rng):
    n = rng.randint(3, 6)
    floors = [{"id": "f1", "name": "1F", "level": 0}]
    nodes = [
        {"id": f"n{i}", "floor_id": "f1", "x": rng.randrange(0, 50),
         "y": rng.randrange(0, 50), "kind": "junction"}
        for i in range(n)
    ]
    edges = []
    eid_counter = 1
    for _ in range(rng.randint(n, n * 2)):
        u, v = rng.sample(range(n), 2)
        edges.append({
            "id": f"e{eid_counter:02d}", "from": f"n{u}", "to": f"n{v}",
            "type": "corridor", "length": rng.choice([1, 2, 5, 10, 20]),
            "seconds": rng.choice([0, 1, 5, 10, 30]),
            "width": rng.choice([0.8, 1.0, 1.2, 2.0]),
            "slope": rng.choice([0, 0, 0, 5, 30]),
        })
        eid_counter += 1
    if edges and rng.random() < 0.5:
        src = rng.choice(edges)
        edges.append({
            "id": f"e{eid_counter:02d}", "from": src["from"], "to": src["to"],
            "type": "corridor", "length": rng.choice([1, 5, 20]),
            "seconds": rng.choice([1, 10, 30]),
            "width": rng.choice([0.9, 2.0]), "slope": 0,
        })
    return {"floors": floors, "nodes": nodes, "edges": edges}


def exhaustive_route(G, start, end, dep, min_w, max_s, closures, deadline):
    best = None  # (arrival, dist, seq)

    def better(cand):
        nonlocal best
        if best is None:
            return True
        if cand[0] != best[0]:
            return cand[0] < best[0]
        if cand[1] != best[1]:
            return cand[1] < best[1]
        return cand[2] < best[2]

    stack = [(start, dep, 0.0, (), frozenset({start}))]
    while stack:
        u, arr, dist, seq, visited = stack.pop()
        if u == end:
            cand = (arr, dist, seq)
            if better(cand):
                best = cand
            continue
        for v, parallel in G[u].items():
            if v in visited:
                continue
            for ekey, edata in parallel.items():
                if not routing._edge_static_ok(edata, min_w, max_s):
                    continue
                eid = edata["id"]
                d2 = routing.earliest_departure(
                    arr, edata["seconds"], closures.get(eid, []))
                a2 = d2 + timedelta(seconds=edata["seconds"])
                if a2 > deadline:
                    continue
                stack.append((v, a2, dist + edata["length"],
                              seq + (eid,), visited | {v}))
    return best


trials = 500
mismatches = 0
checked = 0
for t in range(trials):
    rng = random.Random(1000 + t)
    data = random_graph(rng)
    try:
        routing.validate_network(data)
    except routing.NetworkValidationError:
        continue
    G = routing.build_graph(data)
    nodes = list(G.nodes)
    if len(nodes) < 2:
        continue
    start, end = rng.sample(nodes, 2)
    min_w = rng.choice([0.0, 0.9, 1.1])
    max_s = rng.choice([4, 90])
    dep = BASE + timedelta(minutes=rng.randrange(0, 600))
    deadline = dep + timedelta(hours=24)
    closures = {}
    for eid in {d["id"] for d in data["edges"]}:
        if rng.random() < 0.25:
            s = dep + timedelta(minutes=rng.randrange(-60, 120))
            e = s + timedelta(minutes=rng.choice([1, 5, 30, 1500]))
            closures.setdefault(eid, []).append((s, e))

    fast = routing.find_route(G, start, end, dep, min_w, max_s, closures)
    ref = exhaustive_route(G, start, end, dep, min_w, max_s, closures, deadline)
    checked += 1
    if ref is None:
        if fast["reachable"]:
            print(f"[{t}] FALSE REACHABLE start={start} end={end}")
            mismatches += 1
        continue
    if not fast["reachable"]:
        print(f"[{t}] MISSED ROUTE start={start} end={end} ref={ref[2]}")
        mismatches += 1
        continue
    fseq = tuple(s["edge_id"] for s in fast["steps"])
    farr = datetime.fromisoformat(fast["arrival"])
    if (farr, fast["total_length"], fseq) != (ref[0], ref[1], ref[2]):
        print(f"[{t}] MISMATCH start={start} end={end} "
              f"fast={(farr, fast['total_length'], fseq)} "
              f"ref={(ref[0], ref[1], ref[2])}")
        mismatches += 1

print(f"\ntrials={checked} mismatches={mismatches}")
sys.exit(1 if mismatches else 0)

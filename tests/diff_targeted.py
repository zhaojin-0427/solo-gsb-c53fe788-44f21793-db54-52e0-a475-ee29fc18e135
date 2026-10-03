"""Targeted differential oracle: diamond merges with closure timed to
equalise both branches at the merge — the exact misjudgment pattern."""
import random, sys
from datetime import datetime, timedelta, timezone
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "backend"))
import routing

BASE = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
mismatches = 0
checked = 0

for t in range(300):
    rng = random.Random(7000 + t)
    # S -> M direct (early, big ID, arbitrary length), S -> X -> M (slower,
    # small IDs), M -> T shared suffix.
    secs_direct = rng.randint(1, 20)
    secs_leg = rng.randint(1, 15)
    len_direct = rng.choice([1, 5, 10, 30])
    len_leg = rng.choice([1, 5, 10, 30])
    secs_tail = rng.randint(1, 10)
    len_tail = rng.choice([1, 5, 10])
    # ID ordering: direct edge gets the BIG id, branch legs small ids.
    ids = ["e1", "e2", "e3", "e9"]
    rng.shuffle(ids)
    id_a_leg, id_b_leg, id_tail, id_direct = ids
    data = {"floors": [{"id": "f1", "name": "F", "level": 0}],
            "nodes": [{"id": "S", "floor_id": "f1", "x": 0, "y": 0, "kind": "junction"},
                      {"id": "M", "floor_id": "f1", "x": 30, "y": 0, "kind": "junction"},
                      {"id": "X", "floor_id": "f1", "x": 15, "y": -10, "kind": "junction"},
                      {"id": "T", "floor_id": "f1", "x": 40, "y": 0, "kind": "room"}],
            "edges": [
                {"id": id_direct, "from": "S", "to": "M", "type": "corridor",
                 "length": len_direct, "seconds": secs_direct, "width": 2, "slope": 0},
                {"id": id_a_leg, "from": "S", "to": "X", "type": "corridor",
                 "length": len_leg, "seconds": secs_leg, "width": 2, "slope": 0},
                {"id": id_b_leg, "from": "X", "to": "M", "type": "corridor",
                 "length": len_leg, "seconds": secs_leg, "width": 2, "slope": 0},
                {"id": id_tail, "from": "M", "to": "T", "type": "corridor",
                 "length": len_tail, "seconds": secs_tail, "width": 2, "slope": 0},
            ]}
    early = secs_direct
    late = secs_leg * 2
    if early >= late:
        continue  # need direct to actually be earlier at M
    # Closure on tail covering [0, end) with end at/after late arrival:
    # both labels must wait. Try end aligned to late, and later offsets.
    end_wait = late + rng.choice([0, 1, 5, 30])
    closures = {id_tail: [(BASE, BASE + timedelta(seconds=end_wait))]}
    G = routing.build_graph(data)
    deadline = BASE + timedelta(hours=24)

    # Exhaustive simple-path optimum.
    best = None
    for seq, edges_used in [
        ((id_direct, id_tail), [("M", id_tail, len_direct, early)]),
        ((id_a_leg, id_b_leg, id_tail), [("M", id_tail, len_leg*2, late)]),
    ]:
        arrM = early if seq[0] == id_direct else late
        distM = len_direct if seq[0] == id_direct else len_leg*2
        dep = max(arrM, end_wait)
        arrT = dep + secs_tail
        cand = (BASE + timedelta(seconds=arrT), distM + len_tail, seq)
        if best is None or (cand[0], cand[1], cand[2]) < best:
            best = cand

    fast = routing.find_route(G, "S", "T", BASE, 0.0, 90.0, closures)
    checked += 1
    fseq = tuple(s["edge_id"] for s in fast["steps"])
    farr = datetime.fromisoformat(fast["arrival"])
    if (farr, fast["total_length"], fseq) != best:
        print(f"MISMATCH t={t}: fast={(farr, fast['total_length'], fseq)} ref={best}")
        mismatches += 1

print(f"checked={checked} mismatches={mismatches}")
sys.exit(1 if mismatches else 0)

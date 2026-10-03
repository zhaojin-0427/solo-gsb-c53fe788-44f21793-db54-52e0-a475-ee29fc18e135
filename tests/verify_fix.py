"""End-to-end verification for the accessible-path platform.

Covers:
  1. Closure-wait tie adjudication at a merge node (the fixed bug).
  2. Earlier arrival still wins outright; distance then edge-ID tie breaks.
  3. Parallel channel width filtering (e27 vs e28) is unchanged.
  4. Unreachability diagnosis (static + closure > 24h) is unchanged.
  5. Cross-floor stairs/elevator/ramp rules are unchanged.
  6. Records solidify results and are not recomputed after network/closure
     changes (page and /api/route/query share the same backend result).
  7. Timezone-aware closure handling (Asia/Shanghai offsets).
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

import database as db
import routing
from main import app
from starlette.testclient import TestClient

SAMPLE = json.loads((ROOT / "data" / "sample_network.json").read_text())

passed = []
failed = []


def check(name, cond, detail=""):
    (passed if cond else failed).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{detail}]" if detail else ""))


def closures_from(triples):
    out = {}
    for eid, s, e in triples:
        out.setdefault(eid, []).append(
            (datetime.fromisoformat(s), datetime.fromisoformat(e))
        )
    return out


def find(data, start, end, dep, closures=None, w=0.0, slope=90.0):
    G = routing.build_graph(data)
    return routing.find_route(
        G, start, end, datetime.fromisoformat(dep), w, slope, closures or {}
    )


# ---------------------------------------------------------------------------
# 1. Diamond merge with downstream closure: the fixed tie misjudgment
# ---------------------------------------------------------------------------

def test_merge_tie_after_wait():
    print("\n[1] 封闭等待后的平局裁决 (n15 -> n18)")
    # e37 (8s, len 20, ID big) beats e29+e30 (10s, len 20, IDs small) at n16,
    # but e31 is closed until 00:05; both then depart n16 at 00:05 and arrive
    # n18 at 00:05:10. Arrival tied, length tied (25) -> edge-ID seq decides:
    # (e29, e30, e31) < (e37, e31).
    cls = closures_from([
        ("e31", "2026-10-01T00:00:00+00:00", "2026-10-01T00:05:00+00:00"),
    ])
    res = find(SAMPLE, "n15", "n18", "2026-10-01T00:00:00+00:00", cls)
    seq = [s["edge_id"] for s in res["steps"]]
    check("可达", res["reachable"], str(res.get("reasons")))
    check("最终到达 00:05:10", res["arrival"] == "2026-10-01T00:05:10+00:00", res["arrival"])
    check("总路程 25", res["total_length"] == 25.0, str(res["total_length"]))
    check("选择 e29,e30,e31（小边 ID 序列）", seq == ["e29", "e30", "e31"], str(seq))
    waits = [s["wait_seconds"] for s in res["steps"]]
    check("逐段等待 [0,0,290]", waits == [0.0, 0.0, 290.0], str(waits))
    check("总等待 290 秒", res["wait_seconds"] == 290.0, str(res["wait_seconds"]))
    check("各步到达时刻",
          [s["arrival"] for s in res["steps"]] == [
              "2026-10-01T00:00:05+00:00",
              "2026-10-01T00:00:10+00:00",
              "2026-10-01T00:05:10+00:00",
          ])


def test_merge_no_closure_earlier_wins():
    print("\n[2] 无封闭时早到达者胜出")
    res = find(SAMPLE, "n15", "n18", "2026-10-01T00:00:00+00:00")
    seq = [s["edge_id"] for s in res["steps"]]
    check("无封闭选择 e37 直达（8s 早到）", seq == ["e37", "e31"], str(seq))
    check("到达 00:00:18", res["arrival"] == "2026-10-01T00:00:18+00:00", res["arrival"])


def test_merge_distance_tiebreak():
    print("\n[3] 最终到达相同、边 ID 相同时比总路程")
    # Custom diamond: both branches take 10s; e40 length 30 (big ID, long),
    # e31+e32 length 10+10=20 (small IDs, short). Closure on e33 until both
    # have arrived at n16, so arrival ties; shorter total distance wins.
    data = {
        "floors": [{"id": "f1", "name": "1F", "level": 0}],
        "nodes": [
            {"id": "s", "floor_id": "f1", "x": 0, "y": 0, "kind": "junction"},
            {"id": "m", "floor_id": "f1", "x": 30, "y": 0, "kind": "junction"},
            {"id": "x", "floor_id": "f1", "x": 15, "y": -10, "kind": "junction"},
            {"id": "t", "floor_id": "f1", "x": 40, "y": 0, "kind": "room"},
        ],
        "edges": [
            {"id": "e40", "from": "s", "to": "m", "type": "corridor", "length": 30, "seconds": 10, "width": 2, "slope": 0},
            {"id": "e31", "from": "s", "to": "x", "type": "corridor", "length": 10, "seconds": 5, "width": 2, "slope": 0},
            {"id": "e32", "from": "x", "to": "m", "type": "corridor", "length": 10, "seconds": 5, "width": 2, "slope": 0},
            {"id": "e33", "from": "m", "to": "t", "type": "corridor", "length": 5, "seconds": 10, "width": 2, "slope": 0},
        ],
    }
    cls = closures_from([("e33", "2026-10-01T00:00:00+00:00",
                          "2026-10-01T00:00:30+00:00")])
    res = find(data, "s", "t", "2026-10-01T00:00:00+00:00", cls)
    seq = [s["edge_id"] for s in res["steps"]]
    check("路程更短的 e31,e32,e33 胜出", seq == ["e31", "e32", "e33"], str(seq))
    check("总路程 25", res["total_length"] == 25.0, str(res["total_length"]))


def test_early_with_longer_distance_kept():
    print("\n[4] 早到但更长的候选不被错误支配（下游等待可让晚到者追平）")
    # Branch A: e40 8s length 30 (early, long, big ID)
    # Branch B: e31+e32 10s length 20 (late, short, small IDs)
    # e33 closed until 00:05: arrival ties 00:05:10; B is shorter -> B wins.
    data = {
        "floors": [{"id": "f1", "name": "1F", "level": 0}],
        "nodes": [
            {"id": "s", "floor_id": "f1", "x": 0, "y": 0, "kind": "junction"},
            {"id": "m", "floor_id": "f1", "x": 30, "y": 0, "kind": "junction"},
            {"id": "x", "floor_id": "f1", "x": 15, "y": -10, "kind": "junction"},
            {"id": "t", "floor_id": "f1", "x": 40, "y": 0, "kind": "room"},
        ],
        "edges": [
            {"id": "e40", "from": "s", "to": "m", "type": "corridor", "length": 30, "seconds": 8, "width": 2, "slope": 0},
            {"id": "e31", "from": "s", "to": "x", "type": "corridor", "length": 10, "seconds": 5, "width": 2, "slope": 0},
            {"id": "e32", "from": "x", "to": "m", "type": "corridor", "length": 10, "seconds": 5, "width": 2, "slope": 0},
            {"id": "e33", "from": "m", "to": "t", "type": "corridor", "length": 5, "seconds": 10, "width": 2, "slope": 0},
        ],
    }
    cls = closures_from([("e33", "2026-10-01T00:00:00+00:00",
                          "2026-10-01T00:05:00+00:00")])
    res = find(data, "s", "t", "2026-10-01T00:00:00+00:00", cls)
    seq = [s["edge_id"] for s in res["steps"]]
    check("晚到但更短的 e31,e32,e33 胜出", seq == ["e31", "e32", "e33"], str(seq))
    # And without the closure, the early branch wins on arrival.
    res2 = find(data, "s", "t", "2026-10-01T00:00:00+00:00")
    check("无封闭时 e40,e33 早到胜出",
          [s["edge_id"] for s in res2["steps"]] == ["e40", "e33"])


# ---------------------------------------------------------------------------
# 5. Parallel channels
# ---------------------------------------------------------------------------

def test_parallel_channels():
    print("\n[5] 并行通道净宽筛选不退化 (n2 -> n14)")
    res = find(SAMPLE, "n2", "n14", "2026-10-01T00:00:00+00:00", w=1.0)
    check("净宽 1.0 只走 e27", [s["edge_id"] for s in res["steps"]] == ["e27"])
    check("6 秒到达", res["total_seconds"] == 6.0, str(res["total_seconds"]))
    res2 = find(SAMPLE, "n2", "n14", "2026-10-01T00:00:00+00:00", w=0.9)
    check("净宽 0.9 可走更快的 e27",
          [s["edge_id"] for s in res2["steps"]] == ["e27"])
    # e27 closed for 10 min, w 0.9: wait 600s vs e28 30s -> e28.
    cls = closures_from([
        ("e27", "2026-10-01T00:00:00+00:00", "2026-10-01T00:10:00+00:00"),
    ])
    res3 = find(SAMPLE, "n2", "n14", "2026-10-01T00:00:00+00:00", cls, w=0.9)
    check("封闭后改走 e28（30s）",
          [s["edge_id"] for s in res3["steps"]] == ["e28"],
          str([s["edge_id"] for s in res3["steps"]]))
    check("e28 方案无等待", res3["wait_seconds"] == 0.0)


# ---------------------------------------------------------------------------
# 6. Unreachability diagnosis
# ---------------------------------------------------------------------------

def test_unreachable_diagnosis():
    print("\n[6] 不可达诊断不退化")
    # Stairs-only destination: n7 reachable only via e23/e24 (stairs).
    res = find(SAMPLE, "n2", "n7", "2026-10-01T00:00:00+00:00")
    check("楼梯路径静态不可达", not res["reachable"])
    check("给出静态阻断原因", any("限制" in r for r in res["reasons"]), str(res["reasons"]))
    check("无封闭字段", res["blocking_closures"] == [])

    # Closure longer than 24h on the only width-1.0 channel to n14.
    cls = closures_from([
        ("e27", "2026-10-01T00:00:00+00:00", "2026-10-03T00:00:00+00:00"),
    ])
    res2 = find(SAMPLE, "n2", "n14", "2026-10-01T00:00:00+00:00", cls, w=1.0)
    check("超 24h 封闭不可达", not res2["reachable"])
    check("提示 24 小时内无法到达",
          any("24" in r for r in res2["reasons"]), str(res2["reasons"]))
    bc = res2["blocking_closures"]
    check("列出阻断封闭 e27", [c["edge_id"] for c in bc] == ["e27"], str(bc))
    check("阻断诊断含起止节点", bc and bc[0]["from_node"] == "n2" and bc[0]["to_node"] == "n14")

    # Missing node
    res3 = find(SAMPLE, "n2", "nope", "2026-10-01T00:00:00+00:00")
    check("终点不存在给出原因", not res3["reachable"] and "nope" in res3["reasons"][0])


# ---------------------------------------------------------------------------
# 7. Cross-floor rules
# ---------------------------------------------------------------------------

def test_cross_floor():
    print("\n[7] 跨层电梯/坡道/楼梯规则不退化")
    res = find(SAMPLE, "n2", "n11", "2026-10-01T00:00:00+00:00")
    seq = [s["edge_id"] for s in res["steps"]]
    check("电梯方案到 n11", seq[1] == "e5" and seq[-1] == "e15", str(seq))
    # Ramp alternative exists: n2->n4->n10->n9->n11
    check("电梯优先（更早到达）",
          res["arrival"] == "2026-10-01T00:00:56+00:00", res["arrival"])
    # Width 1.3 excludes ramp (1.2) AND elevator (1.2)? elevator width 1.2 < 1.3,
    # ramp 1.2 < 1.3 -> no cross-floor way -> unreachable statically.
    res2 = find(SAMPLE, "n2", "n11", "2026-10-01T00:00:00+00:00", w=1.3)
    check("净宽 1.3 跨层无通道", not res2["reachable"])
    # Slope 4 excludes ramp but elevator slope 0 survives.
    res3 = find(SAMPLE, "n2", "n12", "2026-10-01T00:00:00+00:00", slope=4)
    check("坡度 4 仍可走电梯到 n12", res3["reachable"], str(res3.get("reasons")))
    check("路径不含坡道 e11", "e11" not in [s["edge_id"] for s in res3["steps"]])


# ---------------------------------------------------------------------------
# 8. API + records (page and API share the same adjudication)
# ---------------------------------------------------------------------------

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "app.db"
        db.init_db()
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow_and_records(self):
        print("\n[8] API 端到端：页面与接口同一裁决、记录固化不重算")
        # Import
        r = self.client.post("/api/network/import", json=SAMPLE)
        self.assertEqual(r.status_code, 200, r.text)
        check("路网导入 v1", r.json()["version"] == 1)

        # Register closure on e31 in Asia/Shanghai: 08:00-08:05 +08
        # == 00:00-00:05 UTC.
        r = self.client.post("/api/closures", json={
            "edge_id": "e31",
            "start": "2026-10-01T08:00:00+08:00",
            "end": "2026-10-01T08:05:00+08:00",
            "tz": "Asia/Shanghai",
        })
        self.assertEqual(r.status_code, 200, r.text)
        check("时区封闭时段登记成功", r.json()["id"] == 1)

        # Query via /api/route/query (the page posts to exactly this endpoint).
        payload = {
            "start": "n15", "end": "n18",
            "departure": "2026-10-01T08:00:00+08:00",
            "min_width": 0.0, "max_slope": 90,
        }
        r = self.client.post("/api/route/query", json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        seq = [s["edge_id"] for s in body["steps"]]
        check("接口裁决 e29,e30,e31", seq == ["e29", "e30", "e31"], str(seq))
        check("接口到达 00:05:10Z", body["arrival"] == "2026-10-01T00:05:10+00:00",
              body["arrival"])
        check("接口返回逐段等待",
              [s["wait_seconds"] for s in body["steps"]] == [0, 0, 290])
        rid = body["record_id"]

        # Record snapshot stored
        rec = self.client.get(f"/api/records/{rid}").json()
        check("记录固化结果序列",
              [s["edge_id"] for s in rec["result"]["steps"]] ==
              ["e29", "e30", "e31"])
        check("记录固化封闭快照 e31",
              [c["edge_id"] for c in rec["closure_snapshot"]] == ["e31"])

        # Mutate world: delete closure, then re-import a slightly different
        # network (e29 length altered) as v2. Old record must be unchanged.
        self.client.delete("/api/closures/1")
        changed = json.loads(json.dumps(SAMPLE))
        for e in changed["edges"]:
            if e["id"] == "e29":
                e["length"] = 2
        r = self.client.post("/api/network/import", json=changed)
        self.assertEqual(r.status_code, 200)
        check("再次导入产生 v2", r.json()["version"] == 2)

        # A new query now takes the early branch; old record keeps old answer.
        r = self.client.post("/api/route/query", json=payload)
        seq2 = [s["edge_id"] for s in r.json()["steps"]]
        check("无封闭新查询走 e37 早到", seq2 == ["e37", "e31"], str(seq2))

        old = self.client.get(f"/api/records/{rid}").json()
        oldseq = [s["edge_id"] for s in old["result"]["steps"]]
        check("旧记录不重算，仍为 e29,e30,e31", oldseq == ["e29", "e30", "e31"],
              str(oldseq))
        check("旧记录挂在 v1", old["network_version"] == 1)
        check("旧记录保留封闭快照", len(old["closure_snapshot"]) == 1)

        # Records list
        lst = self.client.get("/api/records").json()["records"]
        check("历史记录列表含 2 条", len(lst) == 2 and lst[1]["id"] == rid)

    def test_parallel_via_api_and_invalid_import(self):
        print("\n[9] API 附加：并行通道示例与导入校验")
        r = self.client.post("/api/network/import", json=SAMPLE)
        self.assertEqual(r.status_code, 200)
        # README example: width 1.0 -> e27 only.
        r = self.client.post("/api/route/query", json={
            "start": "n2", "end": "n14",
            "departure": "2026-10-01T08:00:00+08:00",
            "min_width": 1.0, "max_slope": 8,
        })
        check("README 示例 e27",
              [s["edge_id"] for s in r.json()["steps"]] == ["e27"])

        # Invalid network: dangling node + cross-floor corridor are rejected.
        bad = {
            "floors": [{"id": "f1", "name": "1F", "level": 0},
                       {"id": "f2", "name": "2F", "level": 1}],
            "nodes": [
                {"id": "a", "floor_id": "f1", "x": 0, "y": 0, "kind": "junction"},
                {"id": "b", "floor_id": "f2", "x": 1, "y": 1, "kind": "junction"},
                {"id": "c", "floor_id": "f1", "x": 2, "y": 2, "kind": "junction"},
            ],
            "edges": [
                {"id": "x1", "from": "a", "to": "b", "type": "corridor",
                 "length": 1, "seconds": 1, "width": 1, "slope": 0},
            ],
        }
        r = self.client.post("/api/network/import", json=bad)
        check("非法跨层+悬空节点被拒", r.status_code == 400 and
              any("悬空" in e for e in r.json()["detail"]["errors"]) and
              any("跨楼层" in e for e in r.json()["detail"]["errors"]),
              str(r.json()))


if __name__ == "__main__":
    test_merge_tie_after_wait()
    test_merge_no_closure_earlier_wins()
    test_merge_distance_tiebreak()
    test_early_with_longer_distance_kept()
    test_parallel_channels()
    test_unreachable_diagnosis()
    test_cross_floor()
    suite = unittest.TestLoader().loadTestsFromTestCase(ApiTests)
    runner = unittest.TextTestRunner(verbosity=0, stream=open("/dev/null", "w"))
    result = runner.run(suite)
    print(f"\n{'='*60}")
    print(f"直接核验: {len(passed)} passed, {len(failed)} failed")
    print(f"unittest: {result.testsRun - len(result.failures) - len(result.errors)} "
          f"passed, {len(result.failures) + len(result.errors)} failed")
    if failed or result.failures or result.errors:
        for f in failed:
            print("  FAILED:", f)
        for t, tb in result.failures + result.errors:
            print(tb[-800:])
        sys.exit(1)
    print("ALL CHECKS PASSED")

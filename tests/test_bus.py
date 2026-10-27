import json

from brindle import bus


def test_publish_then_tail_since_id_and_filters(db):
    first = bus.publish(db, "agent.started", repo_root="/r", agent_id="a1", note="hi")
    bus.publish(db, "task.done", repo_root="/other", task_id="t1")
    third = bus.publish(db, "task.done", repo_root="/r", task_id="t2")
    rows = bus.tail(db, 0)
    assert [r["kind"] for r in rows] == ["agent.started", "task.done", "task.done"]
    assert rows[0]["agent_id"] == "a1" and rows[0]["payload"] == {"note": "hi"}
    assert [r["id"] for r in bus.tail(db, first, kinds=("task.done",), repo_root="/r")] == [third]


def test_publish_never_raises(db):
    db.conn.execute("DROP TABLE events")
    assert bus.publish(db, "x") is None




def test_stage_timing_rollup(db):
    def ev(ts, kind, ws, **payload):
        db.conn.execute("INSERT INTO events (ts, kind, repo_root, workspace_id, payload) VALUES (?,?,?,?,?)",
                        (ts, kind, "/r", ws, json.dumps(payload)))

    day = 1_700_000_000.0          # 2023-11-14 UTC
    # w1: report, reviewer starts 60s later, rejected after 100s, fixed 30s later, then reviewed and merged
    for ts, kind, p in [(0, "pipeline.reported", {}), (60, "review.started", {}),
                        (160, "pipeline.review", {"approved": False}), (190, "pipeline.reported", {}),
                        (200, "review.started", {}), (260, "pipeline.review", {"approved": True}),
                        (300, "pipeline.merged", {})]:
        ev(day + ts, kind, "w1", **p)
    # w2: a slower review, so the median/p90 have two samples
    for ts, kind in [(0, "pipeline.reported"), (10, "review.started"), (310, "pipeline.review")]:
        ev(day + ts, kind, "w2")
    ev(day + 1000, "pipeline.merged", "w2")
    ev(day + 10_000_000, "pipeline.reported", "w3")     # too new to prune

    assert bus.prune(db, older_than=3600, now=day + 10_000_000 + 10) == 11
    rows = {r["stage"]: (r["n"], r["median_s"], r["p90_s"]) for r in db.conn.execute(
        "SELECT * FROM stage_timings WHERE day='2023-11-14' AND repo_root='/r'")}
    assert rows == {"report_to_review": (3, 10, 60), "review": (3, 100, 300),
                    "fix_round": (1, 30, 30), "report_to_merge": (2, 555, 1000)}

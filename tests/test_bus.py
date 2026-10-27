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




def test_stage_timing_rollup(db, monkeypatch):
    day = 1_700_000_000.0          # 2023-11-14 UTC
    script = [   # (offset, kind, agent, payload)
        (0, "pipeline.reported", "w", {}), (60, "review.started", "r1", {}),
        (70, "review.started", "r2", {}),       # a second reviewer beside the first
        (160, "pipeline.review", "r1", {"actor": "r1", "approved": False}),
        (190, "pipeline.review", "r2", {"actor": "r2", "approved": False}),
        (220, "pipeline.reported", "w", {}), (230, "review.started", "r1", {}),
        (290, "pipeline.review", "r1", {"actor": "r1", "approved": True}),
        (300, "pipeline.merged", "w", {})]
    for off, kind, agent, payload in script:
        db.conn.execute("INSERT INTO events (ts, kind, repo_root, workspace_id, agent_id, payload)"
                        " VALUES (?,?,?,?,?,?)", (day + off, kind, "/r", "w1", agent, json.dumps(payload)))

    with monkeypatch.context() as m:       # a failing rollup keeps the events for the next pass
        m.setattr(bus.statistics, "median", lambda values: 1 / 0)
        assert bus.prune(db, older_than=0, now=day + 100) == 0
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == len(script)
    assert not db.conn.execute("SELECT * FROM stage_timings").fetchall()

    for off, *_ in script:                 # one pass per event: every stage spans a prune
        bus.prune(db, older_than=0, now=day + off + 0.5)
    assert db.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    rows = {r["stage"]: (r["n"], r["median_s"], r["p90_s"]) for r in db.conn.execute(
        "SELECT * FROM stage_timings WHERE day='2023-11-14' AND repo_root='/r'")}
    assert rows == {"report_to_review": (2, 35, 60), "review": (3, 100, 120),
                    "fix_round": (1, 30, 30), "report_to_merge": (1, 80, 80)}
    assert not db.conn.execute("SELECT * FROM stage_open").fetchall()    # the merge closed the workspace

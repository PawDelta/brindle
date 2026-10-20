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


def test_prune_drops_only_old_events(db):
    old = bus.publish(db, "old")
    db.conn.execute("UPDATE events SET ts = ts - 90000 WHERE id = ?", (old,))
    new = bus.publish(db, "new")
    assert bus.prune(db) == 1
    assert [r["id"] for r in bus.tail(db)] == [new]

"""Plan reads and approvals keep task dispatch off the response path."""

import asyncio
from contextlib import closing
from dataclasses import replace

import pytest
from starlette.testclient import TestClient

from brindle import agents, mcp_server, plans, tasks, visual_offers
from brindle.daemon import holds, routes_read
from brindle.daemon.app import build_app
from brindle.db import DB
from test_plans import AUTH, PLAN, TOKEN, messages, session, work  # noqa: F401 - session fixture


def _plan_with_task(db, boss):
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    asyncio.run(mcp_server.assign("developer", "build the preset schema", **work(db)))
    [task] = db.list_tasks(state="pending")
    return pid, task


@pytest.mark.parametrize("disconnect", [
    pytest.param(False, marks=pytest.mark.variant("Approve answers before held workers start")),
    pytest.param(True, marks=pytest.mark.variant("a disconnect must release the hold even if the response write is blocked")),
])
def test_approve_answers_before_dispatch(db, session, monkeypatch, disconnect):
    boss, started = session
    pid, task = _plan_with_task(db, boss)
    events = []
    delegate = agents.delegate

    def start_worker(*args, **kwargs):
        events.append("worker")
        return delegate(*args, **kwargs)

    monkeypatch.setattr(agents, "delegate", start_worker)
    dispatch = tasks.start_in_background

    def start_in_background(task_id):
        events.append("dispatch")
        dispatch(task_id)

    monkeypatch.setattr(tasks, "start_in_background", start_in_background)
    app = build_app(TOKEN)

    def sweep_while_reply_is_blocked():
        current = DB()
        with closing(current.conn):
            assert plans.get(current, pid)["state"] == "approved"
            assert len(messages(current)) > before
            return tasks.sweep(current)

    async def record_reply(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return

        request_complete = False
        client_left = asyncio.Event()

        async def receive_from_client():
            nonlocal request_complete
            if disconnect and request_complete:
                await client_left.wait()
                events.append("disconnect")
                return {"type": "http.disconnect"}
            message = await receive()
            request_complete = message["type"] == "http.request" and not message.get("more_body")
            return message

        async def record_body(message):
            if message["type"] == "http.response.body" and not message.get("more_body"):
                assert await asyncio.to_thread(sweep_while_reply_is_blocked) == 0
                if disconnect:
                    client_left.set()
                    try:
                        # The write never completes on its own. Bound a broken test
                        # so a route that ignores the disconnect fails promptly.
                        async with asyncio.timeout(3):
                            await asyncio.Future()
                    except asyncio.CancelledError:
                        events.append("write cancelled")
                        raise
                await send(message)
                events.append("reply")
            else:
                await send(message)

        await app(scope, receive_from_client, record_body)

    before = len(messages(db))
    with TestClient(record_reply, headers=AUTH) as client:
        response = client.post(f"/v1/plans/{pid}/decide", json={"rev": 1, "approved": True})
        if not disconnect:
            assert response.status_code == 200 and response.json()["state"] == "approved"

    transport_events = ["disconnect", "write cancelled"] if disconnect else ["reply"]
    assert events == [*transport_events, "dispatch", "worker"]
    assert plans.get(db, pid)["state"] == "approved"
    assert plans.holding(db, boss.id) is None
    assert db.get_task(task.id).state == "started" and started == [task.task_text]


@pytest.mark.variant("opening a plan with five held tasks stays under the query ceiling with one hold lookup")
def test_get_plan_counts_five_held_tasks_in_one_lookup(db, session, monkeypatch):
    boss, _ = session
    pid, task = _plan_with_task(db, boss)
    child = replace(boss, id="child", parent_id=boss.id)
    db.add_agent(child)
    db.add_agent(replace(boss, id="other-session"))
    for i in range(1, 5):
        db.add_task(replace(task, id=f"held-{i}", caller_id=child.id))
        db.set_task_milestone(f"held-{i}", "Org presets")
    db.add_task(replace(task, id="unrelated-here"))
    db.add_task(replace(task, id="unrelated-elsewhere", caller_id="other-session"))
    db.set_task_milestone("unrelated-elsewhere", "Org presets")

    queries, lookups, daemon_calls = [], [], []
    holding = plans.holding

    def read_db():
        current = DB()
        current.conn.set_trace_callback(queries.append)
        return current

    def read_holding(db_, root):
        lookups.append(root)
        return holding(db_, root)

    def app_connected():
        daemon_calls.append(True)
        return False

    monkeypatch.setattr(routes_read, "DB", read_db)
    monkeypatch.setattr(plans, "holding", read_holding)
    monkeypatch.setattr(holds, "app_connected", app_connected)
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        response = client.get(f"/v1/plans/{pid}")

    assert response.status_code == 200 and response.json()["queued"] == 5
    reads = [sql for sql in queries if sql.lstrip().upper().startswith(("SELECT", "WITH"))]
    assert len(reads) <= 30, len(reads)
    assert lookups == [boss.id]
    assert daemon_calls == []

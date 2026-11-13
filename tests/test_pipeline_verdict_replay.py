"""A verdict on record that nothing acted on is replayed once by the sweep
(even though the reviewer's row was deleted seconds after it gave it); and a
piped branch nothing is working on is reported, not closed."""
import time

import pytest

from brindle import agents, cull, gates, pipeline
from test_merge_order import add
from test_pipeline import _reported
from test_pipeline_idempotent import drain as inbox


@pytest.fixture
def acted(monkeypatch):
    """What acting on a verdict did: fix rounds started, drains run."""
    done = {"reopened": [], "drained": []}
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)   # the supervisor stays busy
    monkeypatch.setattr(agents, "reopen_worker",
                        lambda db_, a, ws_, prompt: done["reopened"].append(prompt) or True)
    monkeypatch.setattr(pipeline, "drain",
                        lambda db_, root, base, reviewer=None: done["drained"].append(base) or [])
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    return done


def _reported_merging(db, repo, reviewing_since):
    ws = _reported(db, repo, reviewing_since=reviewing_since)
    (repo / ".brindle" / "config.local.json").write_text('{"auto_merge_default_branch": true}')
    return ws


def _assert_acted(db, acted, approved):
    if approved:
        assert db.get_agent("w1").pipeline == "ready" and len(acted["drained"]) == 1
    else:
        assert db.get_agent("w1").pipeline == "fixing"
        assert len(acted["reopened"]) == 1 and "the null check" in acted["reopened"][0]


@pytest.mark.parametrize("approved", [True, False])
def test_sweep_replays_a_verdict_nothing_acted_on_once(db, repo, acted, approved):
    now = time.time()
    ws = _reported_merging(db, repo, now - 600)
    db.add_review(ws.id, gates.head(ws), "rev-gone", approved,
                  "lgtm" if approved else "Blocker: the null check is missing.")
    later = now + pipeline.UNACTED_AFTER + 1
    cull.note_reported_stuck(db, later, {})
    cull.note_reported_stuck(db, later + 60, {})
    _assert_acted(db, acted, approved)
    told = [b for b in inbox(db) if "never acted on" in b]
    assert len(told) == 1 and "feat" in told[0]


@pytest.mark.parametrize("approved", [False])
def test_sweep_reading_the_worker_before_its_verdict_was_acted_on_does_not_act_again(db, repo, acted,
                                                                                      monkeypatch, approved):
    """The sweep reads its workers once; a verdict acted on after that read
    is not replayed on the stale row."""
    now = time.time()
    ws = _reported_merging(db, repo, now - 600)
    sha = gates.head(ws)
    add(db, ws, "rev1", "review", "reviewer", parent="boss")
    db.update_agent("rev1", review_sha=sha)
    snapshot = db.list_agents()                          # the sweep's read: w1 still in review
    db.add_review(ws.id, sha, "rev1", approved, "lgtm" if approved else "Blocker: the null check is missing.")
    agents.finish_review(db, "rev1", ws.id)
    rounds = db.get_agent("w1").pipeline_rounds
    real = db.list_agents
    monkeypatch.setattr(db, "list_agents", lambda ws_id=None: snapshot if ws_id is None else real(ws_id))
    cull.note_reported_stuck(db, time.time() + pipeline.UNACTED_AFTER + 1, {})
    _assert_acted(db, acted, approved)
    assert db.get_agent("w1").pipeline_rounds == rounds
    assert not [b for b in inbox(db) if "never acted on" in b]


@pytest.mark.parametrize("state, worker_alive", [("fixing", False), ("ready", False), ("fixing", True),
                                                 ("reviewing", True)])
def test_a_piped_branch_nothing_works_on_is_reported_once_not_closed(db, repo, monkeypatch,
                                                                     state, worker_alive):
    """A dead worker's branch in fixing or ready, with no reviewer and no
    check run, is reported after stall_alert_after minutes; a live idle one
    past stale_after is reported instead of closed. One in review that nobody
    reviews is reported once, by note_reported_stuck alone: the stale note
    keeps its de-dupe value in the same stuck_noted."""
    ws = _reported(db, repo)
    old = time.time() - 7200
    db.update_agent("w1", pipeline=state, created_at=old, status_since=old)
    if state == "reviewing":   # its review already restarted once
        db.update_agent("w1", reviewing_since=old + 60, review_restarted=old + 60)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss" or worker_alive and a.id == "w1")
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "owns_pane", lambda db_, a, owners=None: True)
    monkeypatch.setattr(agents, "same_server", lambda a, panes=None: True)
    cull.sweep(db)
    cull.sweep(db)
    assert db.get_agent("w1").dismissed_at is None and db.get_agent("w1").pipeline == state
    told = [b for b in inbox(db) if "feat" in b and ("nothing is working on" in b or "left it open" in b
                                                   or "no reviewer has been running" in b)]
    assert len(told) == 1


@pytest.mark.parametrize("note", [cull.note_stalled, cull.note_silent, cull.note_idle_unreported,
                                "prompt", "doc",
                                pytest.param("reviewer-stop", marks=pytest.mark.variant(
                                    "Stop hook changes status before claiming reviewer recovery"))])
def test_two_sweeps_reading_one_snapshot_send_a_note_once(db, tmp_path, monkeypatch, note):
    """The daemon and each session's MCP server sweep side by side: two that
    read the workers before either noted it send the note once between them."""
    from brindle import knowledge, prompt_answer, tmux
    from brindle.db import DB, Workspace
    from brindle.pro import analysis_docs
    from brindle.pro.analysis_brief import DOC_ITEM

    ws = Workspace("ws", str(tmp_path), "feat", "worktree", "feat", "main", str(tmp_path), None, "", 0)
    db.add_workspace(ws)
    add(db, ws, "boss", "interactive", "supervisor", status="processing")
    add(db, ws, "w1", "assign", parent="boss", result="done")
    monkeypatch.setattr(gates, "head", lambda ws_: "head")
    old = time.time() - 7200
    if note is cull.note_stalled:   # a dead worker's branch in a fix round
        db.update_agent("w1", pipeline="fixing", created_at=old, status_since=old)
    else:
        db.update_agent("w1", result=None, status="idle", created_at=old, status_since=old)
    if note is cull.note_silent:
        db.update_agent("w1", status="unknown")
    fallbacks = []
    reviewer = note in ("reviewer", "reviewer-stop")
    if reviewer:
        db.update_agent("w1", result="done", pipeline="reviewing")
        add(db, ws, "rev", "review", "reviewer", parent="boss",
            status="processing" if note == "reviewer-stop" else "idle")
        monkeypatch.setattr(agents, "request_review", lambda *a, **kw: fallbacks.append(1))
        monkeypatch.setattr(agents, "close_later", lambda *a: None)
        monkeypatch.setattr(pipeline.learning, "note_reviewer", lambda *a, **kw: None)
        monkeypatch.setattr(pipeline, "_cached_check_summary", lambda *a: None)
        monkeypatch.setattr(pipeline, "_small_diff_stat", lambda *a: None)
    alive = {"boss"} if note is cull.note_stalled else {"boss", "w1", "rev"}
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id in alive)
    monkeypatch.setattr(agents, "owns_pane", lambda db_, a, owners=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "same_server", lambda a, panes=None: True)
    monkeypatch.setattr(tmux, "window_activity", lambda window, server=None: old)
    monkeypatch.setattr(tmux, "capture", lambda window, lines=40, server=None: "")
    monkeypatch.setattr(agents, "command_running", lambda db_, a: False)
    monkeypatch.setattr(agents, "transcript_active", lambda a: False)
    monkeypatch.setattr(cull, "_last_edit", lambda ws_: 0)
    knowledge.record_receipt(db, "w1", [(DOC_ITEM, "doc", "1")])
    pins = {kind: analysis_docs._pins(db, "w1", kind) for kind in (DOC_ITEM, analysis_docs.TOLD_ITEM)}
    monkeypatch.setattr(analysis_docs, "_pins", lambda db_, agent_id, kind: pins[kind])
    snapshot = db.list_agents()
    other = DB()
    for conn in (db, other):
        real = conn.list_agents
        monkeypatch.setattr(conn, "list_agents",
                            lambda ws_id=None, real=real: snapshot if ws_id is None else real(ws_id))
    now = time.time()
    worker = next(a for a in snapshot if a.id == ("rev" if reviewer else "w1"))
    version = 2

    def run(conn):
        if note == "reviewer-stop" and conn is db:
            agents._stop_decision(conn, worker, {"stop_hook_active": True})
        elif note is agents.tell_parent_unreported or reviewer:
            agents.tell_parent_unreported(conn, worker)
        elif note == "prompt":
            prompt_answer.tell(conn, worker, "Continue?")
        elif note == "doc":
            analysis_docs._tell_stale(conn, "w1", "doc", version)
        else:
            note(conn, now, {})

    sent = []

    def send(conn, to, body, **kw):
        sent.append(to)
        if len(sent) == 1:
            run(other)  # the other process enters before this send returns
        return "delivered"

    monkeypatch.setattr(agents, "send_message", send)

    def next_event():
        nonlocal old, worker, version
        old += 1
        version += 1
        db.update_agent("w1", status_since=old, unreported_noted=None)
        worker = db.get_agent("w1")
        snapshot[:] = [worker if a.id == "w1" else a for a in snapshot]

    try:
        run(db)
        run(other)
        assert len(sent) == 1
        if reviewer:
            assert fallbacks == []
            db.set_status("rev", "processing")
            db.set_status("rev", "idle")  # a genuinely new stop after the resume
            worker = db.get_agent("rev")
            run(other)
            assert fallbacks == [1] and len(sent) == 1
            return
        next_event()

        def fail(conn, to, body, **kw):
            raise RuntimeError("send failed")

        monkeypatch.setattr(agents, "send_message", fail)
        with pytest.raises(RuntimeError):
            run(db)
        monkeypatch.setattr(agents, "send_message", send)
        run(other)
        assert len(sent) == 2  # the failed send released its claim
        next_event()

        def newer_then_fail(conn, to, body, **kw):
            next_event()
            monkeypatch.setattr(agents, "send_message", send)
            run(other)
            raise RuntimeError("older send failed")

        monkeypatch.setattr(agents, "send_message", newer_then_fail)
        with pytest.raises(RuntimeError):
            run(db)
        run(other)
        assert len(sent) == 3  # releasing the older claim kept the newer one
    finally:
        other.conn.close()

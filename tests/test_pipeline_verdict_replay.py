"""A verdict the pipeline holds for its commit's checks is acted on when the
checks end, even though the reviewer's row was deleted seconds after it
gave it; a verdict on record that nothing acted on is replayed once by the
sweep; and a piped branch nothing is working on is reported, not closed."""
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
def test_verdict_held_for_checks_survives_its_reviewer_row(db, repo, acted, approved):
    ws = _reported_merging(db, repo, time.time() - 60)
    sha = gates.head(ws)
    add(db, ws, "rev1", "review", "reviewer", parent="boss")
    db.update_agent("rev1", review_sha=sha)
    summary = "lgtm" if approved else "Blocker: the null check is missing."
    with pipeline.review_run(ws.id):
        pipeline.checks_started(db, ws, sha)
        db.add_review(ws.id, sha, "rev1", approved, summary)
        agents.finish_review(db, "rev1", ws.id)          # held: the checks still run
        assert db.get_agent("w1").pipeline == "reviewing"
        agents.close_removed(db, "rev1")                 # what close_later does 5 s on
        assert db.get_agent("rev1") is not None          # kept while its verdict is held
        db.delete_agent("rev1")                          # and replayed even when gone
        pipeline.checks_finished(db, ws, sha, None)
    _assert_acted(db, acted, approved)


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


@pytest.mark.parametrize("approved", [True, False])
def test_sweep_reading_the_worker_before_checks_finished_does_not_act_again(db, repo, acted,
                                                                             monkeypatch, approved):
    """The sweep reads its workers once; a held verdict that checks_finished
    acted on after that read is not replayed on the stale row."""
    now = time.time()
    ws = _reported_merging(db, repo, now - 600)
    sha = gates.head(ws)
    add(db, ws, "rev1", "review", "reviewer", parent="boss")
    db.update_agent("rev1", review_sha=sha)
    with pipeline.review_run(ws.id):
        pipeline.checks_started(db, ws, sha)
        db.add_review(ws.id, sha, "rev1", approved, "lgtm" if approved else "Blocker: the null check is missing.")
        agents.finish_review(db, "rev1", ws.id)
        snapshot = db.list_agents()                      # the sweep's read: w1 still in review
        pipeline.checks_finished(db, ws, sha, None)
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


@pytest.mark.parametrize("note, says", [(cull.note_stalled, "nothing is working on"),
                                        (cull.note_silent, "has shown nothing new")])
def test_two_sweeps_reading_one_snapshot_send_a_note_once(db, repo, monkeypatch, note, says):
    """The daemon and each session's MCP server sweep side by side: two that
    read the workers before either noted it send the note once between them."""
    from brindle import tmux
    from brindle.db import DB

    ws = _reported(db, repo)
    old = time.time() - 7200
    if note is cull.note_stalled:   # a dead worker's branch in a fix round
        db.update_agent("w1", pipeline="fixing", created_at=old, status_since=old)
    else:                           # a live worker whose pane has printed nothing
        db.update_agent("w1", result=None, status="unknown", created_at=old)
    alive = {"boss", "w1"} if note is cull.note_silent else {"boss"}
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id in alive)
    monkeypatch.setattr(agents, "owns_pane", lambda db_, a, owners=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "same_server", lambda a, panes=None: True)
    monkeypatch.setattr(tmux, "window_activity", lambda window, server=None: old)
    monkeypatch.setattr(tmux, "capture", lambda window, lines=40, server=None: "")
    snapshot = db.list_agents()
    other = DB()
    for conn in (db, other):
        real = conn.list_agents
        monkeypatch.setattr(conn, "list_agents",
                            lambda ws_id=None, real=real: snapshot if ws_id is None else real(ws_id))
    now = time.time()
    note(db, now, {})
    note(other, now, {})
    told = [b for b in inbox(db) if ws.branch in b and says in b]
    assert len(told) == 1

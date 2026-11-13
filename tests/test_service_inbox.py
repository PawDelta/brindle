"""service.inbox.items: one row per kind of thing that waits on a person."""

import json
import time
from unittest.mock import patch

import pytest

from brindle import bus, identity_lock, workspaces
from brindle.db import DB, Agent, Workspace
from brindle.pro import status as org_status
from brindle.service import inbox


def _worker(**fields):
    return lambda db, root, worker, mp: db.update_agent(worker, **fields)


def _proposal(db, root):
    db.add_autopilot(root)
    db.update_autopilot(root, proposal=json.dumps({"checks": ["pytest -q"], "rules": [], "at": time.time()}))


def _checks(db, root, worker, mp):
    _proposal(db, root)


def _locked_checks(db, root, worker, mp):
    _proposal(db, root)
    mp.setattr(identity_lock, "locked_by", lambda agent_id: "another@example.com")


def _two_asking(db, root, worker, mp):
    db.add_agent(Agent("r2", db.get_agent(root).workspace_id, "supervisor", "claude", None,
                       "interactive", "idle", "@3", None, time.time()))
    for r in (root, "r2"):
        db.add_autopilot(r)
        db.update_autopilot(r, state="blocked")


def _login(db, root, worker, mp):
    db.set_login_state(root, state="auth_paused", refresh_cmd="aws sso login")


def _notice(db, root, worker, mp):
    org_status.save(org_status.Saved(org_id="org1", status=org_status.Status(
        notices=[{"id": "n1", "text": "maintenance tonight", "from_role": "admin",
                  "at": None, "scope": "org"}])))


@pytest.mark.parametrize("kind, setup, expected", [
    # Live: a Codex worker whose brief wasn't delivered read "Codex needs a sign-in", signed in or not.
    ("failed", _worker(provider="codex", status="failed", failed_why="delivery"), [("w1", None)]),
    ("question", _two_asking, [("r1", None), ("r2", None)]),
    ("plan", _worker(plan_state="proposed", plan_first=1, plan_text="1. add the column"), [("w1", "1. add the column")]),
    pytest.param("proposed_checks", _locked_checks, [],
                 marks=pytest.mark.variant("Check proposals locked to another identity must not appear as answerable inbox items")),
    ("login", _login, [("r1", "aws sso login")]),
])
def test_items_has_a_row_for_each_kind(db, repo, monkeypatch, tmp_path, kind, setup, expected):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(org_status, "_org_id", lambda: "org1")
    ws = workspaces.create(db, str(repo), "feat/inbox").workspace
    now = time.time()
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, now))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "r1", "assign", "idle", "@2", None, now))
    setup(db, "r1", "w1", monkeypatch)
    got = [i for i in inbox.items(db, str(repo), panes={"@1": True, "@2": True, "@3": True})
           if i.kind == kind]
    assert sorted((i.agent_id, i.detail) for i in got) == expected
    # Each agent's item links into its session; a sign-in carries the login the app can start.
    assert all(i.root_id == {"r2": "r2"}.get(i.agent_id, "r1") for i in got if i.agent_id)
    assert all(i.action.path == "/v1/providers/claude/login" for i in got if kind == "credentials")
    assert all(i.action is None for i in got if kind == "failed")
    assert all((i.fix, i.action.path) == ("codex_hook_trust", "/v1/setup/codex-hook") for i in got if kind == "setup")


def test_a_user_prompt_clears_the_question_card_but_a_brindle_message_does_not(db, repo):
    from brindle import agents, autopilot

    ws = workspaces.create(db, str(repo), "feat/ask").workspace
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, time.time()))
    db.add_autopilot("r1")
    autopilot.need_user(db, "r1", "Commit .brindle/.gitignore?", ask=True)

    def cards():
        return [i for i in inbox.items(db, str(repo), panes={"@1": True}) if i.kind == "question"]

    db.enqueue("r1", "Assigned task finished.", "w1")
    db.pop_pending("r1")
    agents.handle_hook(db, "r1", "prompt-submit", {"prompt": "Assigned task finished.\n"})
    assert [i.agent_id for i in cards()] == ["r1"]
    agents.handle_hook(db, "r1", "prompt-submit", {"prompt": "yes, commit it"})
    assert cards() == []
    assert db.get_autopilot("r1").question is None


@pytest.mark.variant("the reported bug: other tests reach this card but assert nothing of what it tells the person")
@pytest.mark.parametrize("count, repos, workers, detail, reasons", [
    (2, 2, 0, "Keep existing behavior", 1),
])
def test_plan_offer_explains_current_paused_session(db, tmp_path, monkeypatch,
                                                   count, repos, workers, detail, reasons):
    from brindle import autopilot

    monkeypatch.setattr("brindle.daemon.holds.app_connected", lambda: True)
    repo = str(tmp_path)
    db.add_workspace(Workspace("ws", repo, "project", "root", "main", None, repo, None, "", 0))
    db.add_agent(Agent("r1", "ws", "supervisor", "claude", None, "interactive", "paused", "", None,
                       0))
    db.update_agent("r1", name="Storage rollout")
    db.add_autopilot("r1")
    db.update_autopilot("r1", goal="Ship storage", detail=detail, offer_goal_at=10)
    db.set_milestones("r1", [(f"Step {i}", None, None, None, "web" if repos == 2 and i == 0 else None)
                              for i in range(count)])
    # Workers from an older goal and non-worker agents do not count toward this offer.
    for i in range(workers + 2):
        db.add_agent(Agent(f"w{i}", "ws", "developer", "claude", "r1",
                           "interactive" if i == workers + 1 else "assign", "exited", "", None,
                           0 if i == workers else 11))
    autopilot.consider_plan_offer(db, "r1")
    monkeypatch.setattr(inbox.view, "snapshot", lambda *a, **k: [])

    def card():
        return next(i for i in inbox.items(db, repo) if i.kind == "plan_offer")

    first = card()
    context = first.model_dump().get("plan_offer")
    assert context is not None
    assert (context["session_name"], context["goal"], context["detail"]) == (
        "Storage rollout", "Ship storage", detail)
    assert (context["repos"], context["workers"], len(context["reasons"])) == (repos, workers, reasons)
    assert context["milestones"] == [{"title": f"Step {i}", "verified": False} for i in range(count)]
    db.record_check(db.milestones("r1")[0].id, True, "done")
    db.update_agent("r1", name="Storage launch")
    current = card()
    assert current.plan_offer.session_name == "Storage launch"
    assert [m.verified for m in current.plan_offer.milestones] == [True] + [False] * (count - 1)
    assert (current.root_id, current.request_id) == ("r1", first.request_id)


def test_pr_not_now(db, tmp_path, monkeypatch):
    from brindle import git
    from brindle.daemon import session_review
    from brindle.daemon.routes_actions import inbox_action

    monkeypatch.setattr(session_review, "open_pr_for_head", lambda *args: None)
    monkeypatch.setattr(session_review, "head_pr_known_none", lambda *args: False)
    ws = Workspace("pr-card", str(tmp_path), "pr-card", "session", "feat/pr-card", "main",
                   str(tmp_path / "checkout"), None, "pr-card", time.time())
    db.add_workspace(ws)
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, time.time()))
    # A failed decision must stay answerable, just as a newer PR stays visible below.
    _proposal(db, "r1")
    proposal = db.get_autopilot("r1").proposal
    with patch("brindle.autopilot.add_checks", side_effect=OSError("disk full")), pytest.raises(OSError):
        inbox_action(db, {"id": "r1", "action": "accept"}, {})
    assert db.get_autopilot("r1").proposal == proposal
    replacement = proposal.replace("pytest -q", "pytest -x")
    with patch("brindle.autopilot.add_checks", side_effect=lambda *args: db.update_autopilot("r1", proposal=replacement)):
        inbox_action(db, {"id": "r1", "action": "accept"}, {})
    assert db.get_autopilot("r1").proposal == replacement
    [_, replaced] = bus.tail(db, kinds=("checks.changed",))
    inbox_action(db, {"id": "r1", "action": "decline"}, {})
    assert db.get_autopilot("r1").proposal is None
    [cleared] = bus.tail(db, since_id=replaced["id"], kinds=("checks.changed",))
    assert (cleared["repo_root"], cleared["agent_id"], cleared["root_id"]) == (ws.repo_root, "r1", "r1")
    db.conn.execute("INSERT INTO session_pr_prep (root_id, branch, sha, state, prepared_at) VALUES (?, ?, ?, ?, ?)",
                    ("r1", ws.branch, "head1", "passed", time.time()))
    head = "head1"
    monkeypatch.setattr(git, "out", lambda args, path: head)
    monkeypatch.setattr(inbox.view, "snapshot", lambda *args, **kwargs: [
        {"id": ws.id, "branch": ws.branch, "agents": [{"id": "r1", "status": "idle"}]}])

    def cards(database):
        return [i.kind for i in inbox.items(database, ws.repo_root, panes={"@1": True}) if i.kind.startswith("pr_")]

    assert cards(db) == ["pr_ready"]
    inbox_action(db, {"id": "r1", "action": "pr-not-now"}, {})
    restarted = DB(db.path)
    try:
        assert cards(restarted) == []
        restarted.conn.execute("UPDATE session_pr_prep SET state = 'failed'")
        assert cards(restarted) == ["pr_failed"]
        inbox_action(restarted, {"id": "r1", "action": "pr-not-now"}, {})
        assert cards(restarted) == []
        restarted.conn.execute("UPDATE session_pr_prep SET output = 'new failure'")
        assert cards(restarted) == ["pr_failed"]
        restarted.conn.execute("UPDATE session_pr_prep SET state = 'passed'")
        head = "head2"
        assert cards(restarted) == ["pr_ready"]
        # Not now on a card still showing head1 speaks for head1: head2's own result still appears.
        inbox_action(restarted, {"id": "r1", "action": "pr-not-now"}, {})
        restarted.conn.execute("UPDATE session_pr_prep SET sha = 'head2'")
        assert cards(restarted) == ["pr_ready"]
        url = "https://github.com/o/r/pull/7"
        restarted.conn.execute("INSERT INTO session_prs (root_id, branch, url, opened_at) VALUES (?, ?, ?, 1)",
                               ("r1", ws.branch, url))
        restarted.conn.execute("INSERT INTO session_pr_merges (url, state, error) VALUES (?, 'failed', 'merge failed')", (url,))
        assert cards(restarted) == ["pr_failed"]
        inbox_action(restarted, {"id": "r1", "action": "pr-not-now"}, {})
        assert cards(restarted) == []
        restarted.conn.execute("UPDATE session_pr_merges SET error = 'different problem'")
        assert cards(restarted) == ["pr_failed"]
    finally:
        restarted.conn.close()


def test_pr_opened_by_supervisor_replaces_the_ready_card(db, tmp_path, monkeypatch):
    from brindle import git, workspaces
    from brindle.daemon import session_review

    ws = Workspace("pr-open", str(tmp_path), "pr-open", "session", "feat/pr-open", "main",
                   str(tmp_path / "checkout"), None, "pr-open", time.time())
    db.add_workspace(ws)
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, time.time()))
    db.conn.execute("INSERT INTO session_pr_prep (root_id, branch, sha, state, prepared_at) VALUES (?, ?, ?, ?, ?)",
                    ("r1", ws.branch, "head1", "passed", time.time()))
    monkeypatch.setattr(git, "out", lambda args, path: "head1")
    monkeypatch.setattr(inbox.view, "snapshot", lambda *args, **kwargs: [
        {"id": ws.id, "branch": ws.branch, "agents": [{"id": "r1", "status": "idle"}]}])
    pr = {"number": 408, "title": "Fix things", "url": "https://github.com/o/r/pull/408"}
    asked = []
    session_review._head_cache.clear()
    monkeypatch.setattr(session_review._pr_pool, "submit", lambda fn, *a: asked.append(a) or fn(*a) or _Done())
    monkeypatch.setattr(workspaces, "open_pull_request_for_commit", lambda sha, cwd: pr)

    def cards():
        return [i for i in inbox.items(db, ws.repo_root, panes={"@1": True}) if i.kind.startswith("pr_")]

    # The first read starts in the background: this inbox read doesn't wait for gh.
    assert [i.kind for i in cards()] == ["pr_ready"]
    [card] = cards()
    assert (card.kind, card.reason, card.detail, card.url) == ("pr_opened", "PR opened", "#408 Fix things", pr["url"])
    assert len(asked) == 1   # cached: the second read didn't ask gh again
    from brindle.daemon.routes_actions import inbox_action

    from brindle import identity_lock

    # A session locked to another sign-in can still have its card dismissed: that acts as no one.
    monkeypatch.setattr(identity_lock, "locked_by", lambda agent_id: "another@example.com")
    inbox_action(db, {"id": "r1", "action": "pr-not-now"}, {})   # Dismiss on the opened card
    assert cards() == []
    # A push to the same PR moves HEAD: the dismissed card stays gone.
    monkeypatch.setattr(git, "out", lambda args, path: "head2")
    session_review._head_cache.clear()
    cards()   # the first read for a new HEAD only starts the background look
    assert cards() == []
    monkeypatch.setattr(git, "out", lambda args, path: "head1")
    pr = None
    session_review._head_cache.clear()
    assert [i.kind for i in cards()] == ["pr_ready"]
    # A PR brindle recorded stands while GitHub's answer is missing: Open PR never makes the card vanish.
    db.conn.execute("INSERT INTO session_prs (root_id, branch, url, opened_at) VALUES ('r1', ?, ?, 1)",
                    (ws.branch, "https://github.com/o/r/pull/7"))
    assert [(i.kind, i.detail) for i in cards()] == [("pr_opened", "#7")]
    # GitHub answering "none open" (closed unmerged) is not "no answer": the recorded PR stops standing.
    pr = {}
    session_review._head_cache.clear()
    assert [i.kind for i in cards()] == ["pr_ready"]     # the passed preparation can be opened again


class _Done:
    def done(self):
        return True





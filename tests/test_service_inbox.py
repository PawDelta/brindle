"""service.inbox.items: one row per kind of thing that waits on a person."""

import json
import time

import pytest

from brindle import identity_lock, workspaces
from brindle.db import Agent
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


def _hook_trust_removed(db, root, worker, mp):
    from brindle import codex_hook
    mp.setattr(codex_hook, "status", lambda command=None: codex_hook.REMOVED)


def _notice(db, root, worker, mp):
    org_status.save(org_status.Saved(org_id="org1", status=org_status.Status(
        notices=[{"id": "n1", "text": "maintenance tonight", "from_role": "admin",
                  "at": None, "scope": "org"}])))


@pytest.mark.parametrize("kind, setup, expected", [
    ("waiting", _worker(status="waiting"), [("w1", None)]),
    ("credentials", _worker(status="failed", failed_why="auth"), [("w1", None)]),
    # Live: a Codex worker whose brief wasn't delivered read "Codex needs a sign-in", signed in or not.
    ("failed", _worker(provider="codex", status="failed", failed_why="delivery"), [("w1", None)]),
    ("question", _two_asking, [("r1", None), ("r2", None)]),
    ("merge_failed", _worker(result="done", merge_failed="conflict in app.py"), [("w1", None)]),
    ("plan", _worker(plan_state="proposed", plan_first=1, plan_text="1. add the column"), [("w1", "1. add the column")]),
    ("proposed_checks", _checks, [("r1", "pytest -q")]),
    ("proposed_checks", _locked_checks, []),
    ("login", _login, [("r1", "aws sso login")]),
    ("setup", _hook_trust_removed, [(None, None)]),
    ("org_notice", _notice, [(None, "admin")]),
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





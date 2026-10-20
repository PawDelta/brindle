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


def _merge_failed_on_autopilot(db, root, worker, mp):
    db.add_autopilot(root)
    db.update_agent(worker, result="done", merge_failed="conflict in app.py")


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
    ("waiting", _worker(status="waiting"), [("w1", None)]),
    ("credentials", _worker(status="failed"), [("w1", None)]),
    ("question", _two_asking, [("r1", None), ("r2", None)]),
    ("review", _worker(result="done"), [("w1", None)]),
    ("merge_failed", _worker(result="done", merge_failed="conflict in app.py"), [("w1", None)]),
    ("merge_failed", _merge_failed_on_autopilot, [("w1", None)]),
    ("plan", _worker(plan_state="proposed"), [("w1", None)]),
    ("proposed_checks", _checks, [("r1", "pytest -q")]),
    ("proposed_checks", _locked_checks, []),
    ("login", _login, [("r1", "aws sso login")]),
    ("org_notice", _notice, [(None, "admin")]),
])
def test_items_has_a_row_for_each_kind(db, repo, monkeypatch, kind, setup, expected):
    monkeypatch.setattr(org_status, "_org_id", lambda: "org1")
    ws = workspaces.create(db, str(repo), "feat/inbox").workspace
    now = time.time()
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, now))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "r1", "assign", "idle", "@2", None, now))
    setup(db, "r1", "w1", monkeypatch)
    got = [i for i in inbox.items(db, str(repo), panes={"@1": True, "@2": True, "@3": True})
           if i.kind == kind]
    assert sorted((i.agent_id, i.detail) for i in got) == expected

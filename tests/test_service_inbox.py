"""service.inbox.items: one row per kind of thing that waits on a person."""

import json
import time

import pytest

from brindle import workspaces
from brindle.db import Agent
from brindle.pro import status as org_status
from brindle.service import inbox


def _worker(**fields):
    return lambda db, root, worker: db.update_agent(worker, **fields)


def _root(**fields):
    return lambda db, root, worker: db.update_agent(root, **fields)


def _checks(db, root, worker):
    db.add_autopilot(root)
    db.update_autopilot(root, proposal=json.dumps({"checks": ["pytest -q"], "rules": [], "at": time.time()}))


def _asking(db, root, worker):
    db.add_autopilot(root)
    db.update_autopilot(root, state="blocked")


def _login(db, root, worker):
    db.set_login_state(root, state="auth_paused", refresh_cmd="aws sso login")


def _notice(db, root, worker):
    org_status.save(org_status.Saved(org_id="org1", status=org_status.Status(
        notices=[{"id": "n1", "text": "maintenance tonight", "from_role": "admin",
                  "at": None, "scope": "org"}])))


@pytest.mark.parametrize("kind, setup, agent_id, detail", [
    ("waiting", _worker(status="waiting"), "w1", None),
    ("credentials", _worker(status="failed"), "w1", None),
    ("question", _asking, "r1", None),
    ("review", _worker(result="done"), "w1", None),
    ("merge_failed", _worker(result="done", merge_failed="conflict in app.py"), "w1", None),
    ("plan", _worker(plan_state="proposed"), "w1", None),
    ("proposed_checks", _checks, "r1", "pytest -q"),
    ("login", _login, "r1", "aws sso login"),
    ("org_notice", _notice, None, "admin"),
])
def test_items_has_a_row_for_each_kind(db, repo, monkeypatch, kind, setup, agent_id, detail):
    monkeypatch.setattr(org_status, "_org_id", lambda: "org1")
    ws = workspaces.create(db, str(repo), "feat/inbox").workspace
    now = time.time()
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, now))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "r1", "assign", "idle", "@2", None, now))
    setup(db, "r1", "w1")
    got = [i for i in inbox.items(db, str(repo), panes={"@1": True, "@2": True}) if i.kind == kind]
    assert [(i.agent_id, i.detail) for i in got] == [(agent_id, detail)]
    assert got[0].to_json()["kind"] == kind

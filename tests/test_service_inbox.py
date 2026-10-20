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
    ("plan", _worker(plan_state="proposed", plan_text="1. add the column"), [("w1", "1. add the column")]),
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
    # Each agent's item links into its session; a sign-in carries the login the app can start.
    assert all(i.root_id == {"r2": "r2"}.get(i.agent_id, "r1") for i in got if i.agent_id)
    assert all(i.action.path == "/v1/providers/claude/login" for i in got if kind == "credentials")


PROMPT = """ 1. No, skip the tests
 2. Yes, run them
 Bash command

   npm test
   Run the test suite

 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and don't ask again for npm test commands in /repo
   3. No, and tell Claude what to do differently (esc)
"""
SUGGESTED = [{"type": "addRules", "rules": [{"toolName": "Bash", "ruleContent": "npm test:*"}],
              "behavior": "allow", "destination": "localSettings"}]


ON_SCREEN = [("allow_once", "Yes", None),
             ("allow_always", "Yes, and don't ask again for npm test commands in /repo",
              "the CLI's own rule, as its option says: Yes, and don't ask again for npm test commands in /repo"),
             ("deny", "No, and tell Claude what to do differently (esc)", None)]
# The numbered list above the prompt is the agent's output, never an option.
WORDING = ("1. No, skip the tests\n2. Yes, run them\nBash command\nnpm test\nRun the test suite\n"
           "Do you want to proceed?")


# Claude Code's pane while its PermissionRequest hook waits: still its busy spinner.
BUSY = "✳ Clauding… (12s · esc to interrupt)\n\n❯ \n"


@pytest.mark.parametrize("provider, channel, recorded, screen, target, bound, choices, wording", [
    # brindle's hook holds it, whatever the pane shows meanwhile: Claude Code's own
    # suggestion is what allow-always writes.
    ("claude", "hook", "npm test", BUSY, "npm test", True,
     [("allow_once", "Allow once", None),
      ("allow_always", "Allow always", "Claude Code's own rule: allow Bash(npm test:*) (localSettings)"),
      ("deny", "Deny", None)], None),
    # The CLI's own prompt is on screen: its wording and its menu's options.
    ("claude", "prompt", "npm test", PROMPT, "npm test", True, ON_SCREEN, WORDING),
    # An older recorded request isn't what the prompt asks: only the screen speaks, as a request of its own.
    ("claude", "prompt", "rm -rf build", PROMPT, None, False, ON_SCREEN, WORDING),
    # OpenCode's buttons on one line.
    ("opencode", None, None, "┃ △ Permission required\n┃ $ npm test\n   Allow once   Allow always   Reject",
     None, False, [("allow_once", "Allow once", None),
                   ("allow_always", "Allow always", "the CLI's own rule, as its option says: Allow always"),
                   ("deny", "Reject", None)], "△ Permission required\n$ npm test"),
], ids=["hook", "prompt", "prompt-stale-record", "opencode"])
def test_a_pending_permission_request_is_an_item_with_its_choices(db, repo, monkeypatch, provider, channel, recorded,
                                                                   screen, target, bound, choices, wording):
    from brindle import permissions, tmux

    monkeypatch.setattr(tmux, "capture", lambda target, **kw: screen)
    ws = workspaces.create(db, str(repo), "feat/inbox").workspace
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", provider, "r1", "assign", "waiting", "@2", None, time.time()))
    if recorded:
        req = permissions.Request("claude", "bash", "Bash", command=recorded)
        db.add_permission_request("w1", "tu1", json.dumps(req.to_dict()), "", json.dumps(SUGGESTED), channel)
    got = [i for i in inbox.items(db, str(repo), panes={"@1": True, "@2": True}) if i.agent_id == "w1"]
    assert [(i.kind, i.root_id, i.permission.via, i.permission.target, i.permission.wording) for i in got] == [
        ("permission", "r1", channel or "prompt", target, wording)]
    assert [(c.choice, c.label, c.rule) for c in got[0].permission.choices] == choices
    # The answer must name this request: the recorded one when the screen shows it.
    assert (got[0].permission.request_id == "tu1") == bound

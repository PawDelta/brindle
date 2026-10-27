"""Capability groups (brindle.permission_groups): each request's group and
subcategory, and their place in the policy: deny > explicit rule >
subcategory > group baseline, as each preset seeds them and the daemon edits them."""

import os

import pytest
from test_permission_presets import _send, bash, write

from brindle import permission_groups, permissions, workspaces
from brindle.permissions import Request, decide


@pytest.fixture
def ws(db, repo, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())    # no rule allows npm, make ...
    return workspaces.create(db, str(repo), "feature").workspace


def fetch(ws, tool):
    return Request("claude", "fetch", tool, ws.path, ws.path, ws.repo_root, url="https://example.com")


@pytest.mark.parametrize("preset, change, request_of, decision, where", [
    # The preset's baselines, by subcategory.
    ("autonomous", {}, lambda ws: bash(ws, "git push origin feature"), "allow", [("git", "push-feature")]),
    ("permissive", {}, lambda ws: bash(ws, 'git commit -m "wip"'), "allow", [("git", "commit")]),
    ("standard", {}, lambda ws: bash(ws, "cd src && ls"), "allow", [("shell", "readonly"), ("shell", "readonly")]),
    ("permissive", {}, lambda ws: bash(ws, "make build && pip install foo"), "ask",
     [("shell", "dev-tools"), ("shell", "install")]),                       # the strictest part
    ("permissive", {}, lambda ws: write(ws, "src/new.py"), "allow", [("files", "write-worktree")]),
    ("permissive", {}, lambda ws: write(ws, os.path.expanduser("~/x.py")), "ask", [("files", "write-outside")]),
    ("standard", {}, lambda ws: bash(ws, "echo $(whoami)"), "ask", [("shell", "other")]),
    # A subcategory over its group's baseline, set through the daemon.
    ("autonomous", {"git": {"subcategories": {"push-feature": "deny"}}},
     lambda ws: bash(ws, "git push origin feature"), "deny", [("git", "push-feature")]),
    ("standard", {"shell": {"baseline": "allow", "subcategories": {"destructive": "deny"}}},
     lambda ws: bash(ws, "rm notes.txt"), "deny", [("shell", "destructive")]),
    ("standard", {"network": {"subcategories": {"search": "allow"}}},
     lambda ws: fetch(ws, "WebSearch"), "allow", [("network", "search")]),
    ("standard", {"network": {"subcategories": {"search": "allow"}}},
     lambda ws: fetch(ws, "WebFetch"), "ask", [("network", "fetch")]),
    ("autonomous", {"mcp": {"baseline": "deny"}},
     lambda ws: Request("claude", "mcp", "mcp__slack__send", ws.path, ws.path, ws.repo_root), "deny", [("mcp", "tool")]),
    # An explicit rule over both: d-git-ro allows, d-push-default asks.
    ("autonomous", {"git": {"baseline": "deny"}}, lambda ws: bash(ws, "git status"), "allow", [("git", "readonly")]),
    ("standard", {"git": {"baseline": "allow"}}, lambda ws: bash(ws, "git push origin main"), "ask",
     [("git", "push-protected")]),
])
def test_a_request_falls_in_a_group_whose_baseline_answers_what_no_rule_does(ws, preset, change, request_of,
                                                                             decision, where):
    permissions.apply_preset(preset)
    if change:
        code, policy = _send("PUT", "/v1/permissions/groups", {"groups": change})
        (group, entry), = change.items()
        shown = next(g for g in policy["groups"] if g["name"] == group)
        assert code == 200 and shown["baseline"] == entry.get("baseline", shown["baseline"])
        assert entry.get("subcategories", {}).items() <= shown["subcategories"].items()
    req = request_of(ws)
    assert permission_groups.classify(req, permissions.all_rules(ws.repo_root)) == where
    got = decide(req)
    assert got.decision == decision, got.reason

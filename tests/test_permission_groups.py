"""Capability groups (brindle.permission_groups): each request's group and
subcategory, and their place in the policy: deny > explicit rule >
subcategory > group baseline, as each preset seeds them and the daemon edits them."""

import json
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


def confirmed(ws, command):
    """``command`` where an ask rule covers ``confirm`` and a saved allow settles it."""
    permissions.add_rule("bash", "confirm", "ask")
    permissions.save_action("bash", "confirm *", "allow", scope="general")
    return bash(ws, command)


DESTRUCTIVE_DENIED = {"shell": {"baseline": "allow", "subcategories": {"destructive": "deny"}}}


@pytest.mark.parametrize("preset, change, request_of, decision, where", [
    # The preset's baselines, by subcategory.
    ("autonomous", {}, lambda ws: bash(ws, "git push origin feature"), "allow", [("git", "push-feature")]),
    ("permissive", {}, lambda ws: bash(ws, 'git commit -m "wip"'), "allow", [("git", "commit")]),
    ("permissive", {}, lambda ws: bash(ws, "git reset --hard"), "ask", [("git", "other")]),     # discards work
    ("standard", {}, lambda ws: bash(ws, "cd src && ls"), "allow", [("shell", "readonly"), ("shell", "readonly")]),
    ("permissive", {}, lambda ws: bash(ws, "make build && pip install foo"), "ask",
     [("shell", "dev-tools"), ("shell", "install")]),                       # the strictest part
    ("permissive", {}, lambda ws: write(ws, "src/new.py"), "allow", [("files", "write-worktree")]),
    ("permissive", {}, lambda ws: write(ws, os.path.expanduser("~/x.py")), "ask", [("files", "write-outside")]),
    ("standard", {}, lambda ws: bash(ws, "echo $(whoami)"), "allow",
     [("shell", "readonly"), ("shell", "readonly")]),   # read part by part: echo's word only the run knows is text
    # Read part by part: a pattern, a descriptor dup; a file a redirection writes; a fetch.
    ("permissive", {}, lambda ws: bash(ws, "wc -l *.md 2>&1 | cut -c1-80"), "allow",
     [("shell", "readonly"), ("shell", "readonly")]),
    ("permissive", {}, lambda ws: bash(ws, "echo hi > notes.txt"), "allow",
     [("shell", "readonly"), ("files", "write-worktree")]),
    ("permissive", {}, lambda ws: bash(ws, "mkdir -p src/x && curl -sS https://example.com"), "ask",
     [("files", "write-worktree"), ("network", "fetch")]),                # the strictest part
    # What brindle can't verify: every preset asks the person (shell/unchecked).
    ("autonomous", {}, lambda ws: bash(ws, "echo `whoami`"), "ask",
     [("shell", "other"), ("shell", "readonly"), ("shell", "readonly")]),   # unreadable: other, and its parts
    ("autonomous", {"shell": {"subcategories": {"unchecked": "deny"}}}, lambda ws: bash(ws, "echo `whoami`"), "deny",
     [("shell", "other"), ("shell", "readonly"), ("shell", "readonly")]),
    ("standard", {}, lambda ws: bash(ws, "echo `whoami`"), "ask",
     [("shell", "other"), ("shell", "readonly"), ("shell", "readonly")]),
    ("autonomous", {}, lambda ws: bash(ws, "rm -rf $DIR"), "ask", [("shell", "destructive")]),   # $DIR may be ~
    ("permissive", {}, lambda ws: bash(ws, "python3 - <<'EOF'\nprint(1)\nEOF"), "ask", [("shell", "other")] * 5),
    # eval of literal words is the command they make.
    ("autonomous", {}, lambda ws: bash(ws, "eval 'git push --force origin x'"), "deny", [("git", "push-protected")]),
    # Where a part runs: a cd that surely runs leads there, one that may not leaves it unknown.
    ("permissive", {}, lambda ws: bash(ws, "cd / && touch probe"), "ask",
     [("shell", "readonly"), ("files", "write-outside")]),
    ("permissive", {}, lambda ws: bash(ws, f"cd /; if false; then cd {ws.path}; fi; echo hi > probe"), "ask",
     [("shell", "readonly")] * 4 + [("files", "write-outside")]),
    ("permissive", {}, lambda ws: bash(ws, "cd / && ls"), "allow", [("shell", "readonly")] * 2),   # only reads
    # An eval runs where it is: in an if, or in a pipeline's subshell, its cd may not count.
    ("permissive", {}, lambda ws: bash(ws, f"cd /; if false; then eval 'cd {ws.path}'; fi; echo hi > probe"), "ask",
     [("shell", "readonly")] * 4 + [("files", "write-outside")]),
    ("permissive", {}, lambda ws: bash(ws, f"cd /; eval 'cd {ws.path}' | cat; echo hi > probe"), "ask",
     [("shell", "readonly")] * 4 + [("files", "write-outside")]),     # cat reads somewhere unknown
    # A pattern after git's --: names it takes only as paths.
    ("autonomous", {}, lambda ws: bash(ws, "git log --oneline -- *.md"), "allow", [("git", "readonly")]),
    # A destination in an option, or no script operand.
    ("permissive", {}, lambda ws: bash(ws, "cp --target-directory=/ notes.txt"), "ask", [("files", "write-outside")]),
    ("permissive", {}, lambda ws: bash(ws, "sed -i --expression=s/a/b/ /probe"), "ask", [("files", "write-outside")]),
    ("permissive", {}, lambda ws: bash(ws, "sed -i '' -e 'w /probe' notes.txt"), "ask",
     [("files", "write-outside")]),                                  # the script writes elsewhere
    ("permissive", {"files": {"subcategories": {"write-outside": "deny"}}},
     lambda ws: bash(ws, "sed -i '' 's#[#]#;a #w /etc/probe' notes.txt"), "deny",
     [("files", "write-outside")]),                                  # [#] hides a w flag
    # A subcategory over its group's baseline, set through the daemon.
    ("autonomous", {"git": {"subcategories": {"push-feature": "deny"}}},
     lambda ws: bash(ws, "git push origin feature"), "deny", [("git", "push-feature")]),
    ("standard", DESTRUCTIVE_DENIED, lambda ws: bash(ws, "rm notes.txt"), "deny", [("shell", "destructive")]),
    ("standard", DESTRUCTIVE_DENIED, lambda ws: bash(ws, "rm notes.txt > out.log"), "deny",
     [("shell", "destructive"), ("files", "write-worktree")]),              # a redirection hides nothing
    ("standard", DESTRUCTIVE_DENIED, lambda ws: confirmed(ws, "confirm && rm notes.txt"), "deny",
     [("shell", "other"), ("shell", "destructive")]),                       # nor a part an ask rule covers
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


def test_a_table_stored_before_unverifiable_existed_gets_its_presets_answer_and_never_allows(ws):
    permissions.apply_preset("autonomous")
    path = permissions.store_path()
    data = json.loads(path.read_text())
    # The old seeded deny of what brindle can't verify, under its old name: unchecked asks now.
    data["groups"]["shell"]["subcategories"]["unverifiable"] = "deny"
    del data["groups"]["shell"]["subcategories"]["unchecked"]
    path.write_text(json.dumps(data))
    unverifiable = bash(ws, "echo `whoami`")
    assert decide(unverifiable).decision == "ask"
    # With no table at all, where unmatched actions run; an eval of what only the run knows
    # is unverifiable even with no rule that could have asked.
    assert decide(bash(ws, 'eval "$X"'), rules=[], unmatched="allow", saved=[]).decision == "ask"


def test_a_pattern_whose_listing_runs_past_the_budget_is_unknown(monkeypatch, tmp_path):
    seen = []

    class Entry:
        def __init__(self, n):
            self.name = f"x{n}.txt"    # none of them match

    class Listing:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            for n in range(3 * permissions._GLOB_BUDGET):
                seen.append(n)
                yield Entry(n)

    monkeypatch.setattr("brindle.permissions.os.scandir", lambda path: Listing())
    here = str(tmp_path)
    part, = permissions.shell_parts(Request("claude", "bash", "Bash", here, here, here, command="ls *.md"))
    assert part.unknown and len(seen) == permissions._GLOB_BUDGET + 1

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
    pytest.param("autonomous", {}, lambda ws: bash(ws, "git push origin feature"), "allow", [("git", "push-feature")],
                 marks=pytest.mark.variant("feature pushes use their own git subcategory")),
    pytest.param("permissive", {}, lambda ws: bash(ws, 'git commit -m "wip"'), "allow", [("git", "commit")],
                 marks=pytest.mark.variant("commit classification must not inherit the push allowance")),
    pytest.param("permissive", {}, lambda ws: bash(ws, "git reset --hard"), "deny", [("git", "other")],
                 marks=pytest.mark.variant("reset remains denied under the permissive preset")),    # discards work
    pytest.param("standard", {}, lambda ws: bash(ws, "cd src && ls"), "allow", [("shell", "readonly"), ("shell", "readonly")],
                 marks=pytest.mark.variant("a cd chain classifies both read-only parts")),
    pytest.param("permissive", {}, lambda ws: bash(ws, "make build && pip install foo"), "ask",
                 [("shell", "dev-tools"), ("shell", "install")],
                 marks=pytest.mark.variant("the install part must keep a mixed build command asking")),                       # the strictest part
    pytest.param("permissive", {}, lambda ws: write(ws, "src/new.py"), "allow", [("files", "write-worktree")],
                 marks=pytest.mark.variant("checkout writes use the worktree subcategory")),
    pytest.param("permissive", {}, lambda ws: write(ws, os.path.expanduser("~/x.py")), "ask", [("files", "write-outside")],
                 marks=pytest.mark.variant("a home-directory write must use the outside subcategory")),
    pytest.param("standard", {}, lambda ws: bash(ws, "echo $(whoami)"), "allow",
                 [("shell", "readonly"), ("shell", "readonly")],
                 marks=pytest.mark.variant("command substitution classifies the executed command as well as echo")),   # read part by part: echo's word only the run knows is text
    # Read part by part: a pattern, a descriptor dup; a file a redirection writes; a fetch.
    pytest.param("permissive", {}, lambda ws: bash(ws, "wc -l *.md 2>&1 | cut -c1-80"), "allow",
                 [("shell", "readonly"), ("shell", "readonly")],
                 marks=pytest.mark.variant("descriptor duplication and a file pattern remain read-only")),
    pytest.param("permissive", {}, lambda ws: bash(ws, "echo hi > notes.txt"), "allow",
                 [("shell", "readonly"), ("files", "write-worktree")],
                 marks=pytest.mark.variant("output redirection adds a write classification to echo")),
    pytest.param("permissive", {}, lambda ws: bash(ws, "mkdir -p src/x && curl -sS https://example.com"), "ask",
                 [("files", "write-worktree"), ("network", "fetch")],
                 marks=pytest.mark.variant("a fetch keeps a mixed file/network command asking")),                # the strictest part
    # Escapes inside backticks remain unverified (shell/unchecked): it asks, but where commands
    # run unasked, the agent is told to split it.
    pytest.param("autonomous", {}, lambda ws: bash(ws, "echo `\\whoami`"), "deny",
                 [("shell", "other"), ("shell", "readonly"), ("shell", "readonly")],
                 marks=pytest.mark.variant("unreadable backticks stay denied where unmatched commands run")),   # unreadable: other, and its parts
    pytest.param("standard", {}, lambda ws: bash(ws, "echo `\\whoami`"), "ask",
                 [("shell", "other"), ("shell", "readonly"), ("shell", "readonly")],
                 marks=pytest.mark.variant("unreadable backticks ask under Standard")),
    pytest.param("standard", {"shell": {"subcategories": {"unchecked": "deny"}}}, lambda ws: bash(ws, "echo `\\whoami`"), "deny",
                 [("shell", "other"), ("shell", "readonly"), ("shell", "readonly")],
                 marks=pytest.mark.variant("an explicit unchecked deny wins over the Standard baseline")),
    pytest.param("autonomous", {}, lambda ws: bash(ws, "rm -rf $DIR"), "deny", [("shell", "destructive")],
                 marks=pytest.mark.variant("an unknown rm target may name a protected directory")),   # $DIR may be ~
    pytest.param("permissive", {}, lambda ws: bash(ws, "python3 - <<'EOF'\nprint(1)\nEOF"), "ask", [("shell", "other")] * 5,
                 marks=pytest.mark.variant("a Python heredoc must not inherit the read-only shell allowance")),
    # eval of literal words is the command they make.
    pytest.param("autonomous", {}, lambda ws: bash(ws, "eval 'git push --force origin x'"), "deny", [("git", "push-protected")],
                 marks=pytest.mark.variant("eval must expose the forced push to git restrictions")),
    # Where a part runs: a cd that surely runs leads there, one that may not leaves it unknown.
    pytest.param("permissive", {}, lambda ws: bash(ws, "cd / && touch probe"), "ask",
                 [("shell", "readonly"), ("files", "write-outside")],
                 marks=pytest.mark.variant("cd to root makes the subsequent write outside the checkout")),
    pytest.param("permissive", {}, lambda ws: bash(ws, f"cd /; if false; then cd {ws.path}; fi; echo hi > probe"), "ask",
                 [("shell", "readonly")] * 4 + [("files", "write-outside")],
                 marks=pytest.mark.variant("a conditional cd cannot move the guaranteed write location into the checkout")),
    pytest.param("permissive", {}, lambda ws: bash(ws, "cd / && ls"), "allow", [("shell", "readonly")] * 2,
                 marks=pytest.mark.variant("reading outside the checkout does not grant an outside write")),   # only reads
    # An eval runs where it is: in an if, or in a pipeline's subshell, its cd may not count.
    ("permissive", {}, lambda ws: bash(ws, f"cd /; if false; then eval 'cd {ws.path}'; fi; echo hi > probe"), "ask",
     [("shell", "readonly")] * 4 + [("files", "write-outside")]),
    pytest.param("permissive", {}, lambda ws: bash(ws, f"cd /; eval 'cd {ws.path}' | cat; echo hi > probe"), "ask",
                 [("shell", "readonly")] * 4 + [("files", "write-outside")],
                 marks=pytest.mark.variant("a pipeline eval cannot move the parent shell into the checkout")),     # cat reads somewhere unknown
    # A pattern after git's --: names it takes only as paths.
    ("autonomous", {}, lambda ws: bash(ws, "git log --oneline -- *.md"), "allow", [("git", "readonly")]),
    # A destination in an option, or no script operand.
    pytest.param("permissive", {}, lambda ws: bash(ws, "cp --target-directory=/ notes.txt"), "ask", [("files", "write-outside")],
                 marks=pytest.mark.variant("a copy target hidden in a long option is still an outside write")),
    ("permissive", {}, lambda ws: bash(ws, "sed -i --expression=s/a/b/ /probe"), "ask", [("files", "write-outside")]),
    ("permissive", {}, lambda ws: bash(ws, "sed -i '' -e 'w /probe' notes.txt"), "ask",
     [("files", "write-outside")]),                                  # the script writes elsewhere
    ("permissive", {"files": {"subcategories": {"write-outside": "deny"}}},
     lambda ws: bash(ws, "sed -i '' 's#[#]#;a #w /etc/probe' notes.txt"), "deny",
     [("files", "write-outside")]),                                  # [#] hides a w flag
    # A subcategory over its group's baseline, set through the daemon.
    pytest.param("autonomous", {"git": {"subcategories": {"push-feature": "deny"}}},
                 lambda ws: bash(ws, "git push origin feature"), "deny", [("git", "push-feature")],
                 marks=pytest.mark.variant("a push-feature subcategory deny overrides the autonomous baseline")),
    pytest.param("standard", DESTRUCTIVE_DENIED, lambda ws: bash(ws, "rm notes.txt > out.log"), "deny",
                 [("shell", "destructive"), ("files", "write-worktree")],
                 marks=pytest.mark.variant("a redirection must not hide a destructive rm classification")),              # a redirection hides nothing
    ("standard", DESTRUCTIVE_DENIED, lambda ws: confirmed(ws, "confirm && rm notes.txt"), "deny",
     [("shell", "other"), ("shell", "destructive")]),                       # nor a part an ask rule covers
    pytest.param("standard", {"network": {"subcategories": {"search": "allow"}}},
                 lambda ws: fetch(ws, "WebSearch"), "allow", [("network", "search")],
                 marks=pytest.mark.variant("search subcategory permission allows WebSearch")),
    pytest.param("standard", {"network": {"subcategories": {"search": "allow"}}},
                 lambda ws: fetch(ws, "WebFetch"), "ask", [("network", "fetch")],
                 marks=pytest.mark.variant("search permission must not authorize WebFetch")),
    pytest.param("autonomous", {"mcp": {"baseline": "deny"}},
                 lambda ws: Request("claude", "mcp", "mcp__slack__send", ws.path, ws.path, ws.repo_root), "deny", [("mcp", "tool")],
                 marks=pytest.mark.variant("MCP group deny overrides the autonomous baseline")),
    # An explicit rule over both: d-git-ro allows, d-push-default asks.
    pytest.param("autonomous", {"git": {"baseline": "deny"}}, lambda ws: bash(ws, "git status"), "allow", [("git", "readonly")],
                 marks=pytest.mark.variant("an explicit read-only rule wins over the git group baseline")),
    pytest.param("standard", {"git": {"baseline": "allow"}}, lambda ws: bash(ws, "git push origin main"), "ask",
                 [("git", "push-protected")],
                 marks=pytest.mark.variant("an explicit protected-push ask wins over the git group baseline")),
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
    # The old seeded deny of what brindle can't verify, under its old name: unchecked is left to inherit.
    data["groups"]["shell"]["subcategories"]["unverifiable"] = "deny"
    del data["groups"]["shell"]["subcategories"]["unchecked"]
    path.write_text(json.dumps(data))
    unverifiable = bash(ws, "echo `\\whoami`")
    assert decide(unverifiable).decision == "deny"
    # With no table at all, where unmatched actions run; an eval of what only the run knows
    # is unverifiable even with no rule that could have asked.
    assert decide(bash(ws, 'eval "$X"'), rules=[], unmatched="allow", saved=[]).decision == "deny"
    # Where a command would ask anyway, it still asks.
    assert decide(bash(ws, 'eval "$X"'), rules=[], unmatched="ask", saved=[]).decision == "ask"


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

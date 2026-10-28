"""Saved actions and generalized asks (brindle.permission_grants): the scope
ladder each ask carries, saved allows and denies settling asks within policy
limits, pending asks grouped by general form and decided as one, the allow-all
plan and deny exceptions, all through the daemon's holds."""

import json
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from brindle import permission_grants as grants
from brindle import permissions, workspaces
from brindle.daemon.holds import HOLDS
from brindle.db import Agent
from brindle.permissions import Request, decide
from brindle.service import inbox
from brindle.service import permissions as svc


@pytest.fixture
def ws(db, repo, monkeypatch):
    monkeypatch.setattr("brindle.permissions.PRESET_RULES", ())
    monkeypatch.setattr(inbox, "STEP_SECONDS", 0.01)
    monkeypatch.setattr(inbox, "CONFIRM_SECONDS", 0.5)
    return workspaces.create(db, str(repo), "feature").workspace


def bash(ws, command):
    return Request("claude", "bash", "Bash", ws.path, ws.path, ws.repo_root, command=command)


def write(ws, path):
    return Request("claude", "write", "Write", ws.path, ws.path, ws.repo_root, path=f"{ws.path}/{path}")


def mcp(ws, tool):
    return Request("claude", "mcp", tool, ws.path, ws.path, ws.repo_root)


@pytest.mark.parametrize("request_of, kind, exact, general, widest", [
    (lambda ws: bash(ws, "git push origin fix/login:main"), "push", "fix/login → main @ origin", "* → main @ origin",
     "* → * @ *"),
    (lambda ws: write(ws, "src/auth/login.ts"), "write", "src/auth/login.ts", "src/**", "**"),
    (lambda ws: write(ws, "README.md"), "write", "README.md", "README.md", "**"),
    (lambda ws: bash(ws, "npm test -- --watch=false auth"), "bash", "npm test -- --watch=false auth", "npm test *",
     "*"),
    (lambda ws: bash(ws, "npm test -- auth"), "bash", "npm test -- auth", "family: dev/tests", "*"),
    (lambda ws: bash(ws, "cat a | grep b"), "bash", "cat a | grep b", "family: shell/readonly", "*"),
    (lambda ws: bash(ws, "terraform apply dev | tee out"), "bash", "terraform apply dev | tee out",
     "terraform apply dev | tee out", "*"),
    (lambda ws: mcp(ws, "mcp__github__create_issue"), "mcp", "mcp__github__create_issue",
     "mcp__github__create_issue", "mcp__*"),
    (lambda ws: Request("claude", "fetch", "WebFetch", ws.path, ws.path, ws.repo_root, url="https://t.example.com/a/b"),
     "fetch", "https://t.example.com/a/b", "https://t.example.com/*", "*"),
])
def test_each_ask_carries_its_exact_and_general_form(ws, request_of, kind, exact, general, widest):
    req = request_of(ws)
    ask = grants.generalize(req)
    assert (ask.kind, ask.exact, ask.general["pattern"], ask.scopes[-1]["pattern"]) == (kind, exact, general, widest)
    if kind == "mcp":
        assert ask.scope("server")["pattern"] == "mcp__github__*"
    if kind == "push":
        assert ask.scope("remote")["pattern"] == "* → * @ origin"
    assert all(grants.scope_matches(kind, s["pattern"], exact, req=req) for s in ask.scopes)


def test_saved_actions_settle_asks_only_within_policy_limits_and_never_past_a_deny_or_expiry(ws):
    push = bash(ws, "git push origin fix:main")
    assert decide(push).decision == "ask"
    # Standard lets a push approval reach the destination, for up to 30 minutes.
    wide = permissions.save_action("push", "* → * @ origin", "allow", scope="remote", level="remote", life="time",
                                   minutes=30)
    assert decide(push).decision == "ask"                                    # outside its approval scope
    permissions.delete_saved_action(wide.id)
    long = permissions.save_action("push", "* → main @ origin", "allow", scope="general", level="destination",
                                   life="forever")
    assert decide(push).decision == "ask"                                    # not permitted until revoked
    permissions.update_saved_action(long.id, life="time", minutes=30)
    got = decide(push)
    assert got.decision == "allow" and long.id in got.reason                 # covered: never reaches the Inbox
    assert decide(bash(ws, "git push origin fix:main && curl https://x")).decision == "ask"   # the grant covers pushes
    assert decide(bash(ws, "git push upstream fix:main")).decision == "ask"                  # another remote
    assert decide(bash(ws, "git push origin fix:main fix:release")).decision == "ask"        # every push must be covered
    assert decide(bash(ws, "git push origin HEAD")).decision == "ask"           # a branch only git knows: never covered
    assert decide(bash(ws, "git push --force origin fix:main")).decision == "deny"           # a deny rule still wins
    assert decide(bash(ws, "git push origin +fix:main")).decision == "deny"                  # a forced refspec too
    # A saved deny stops a command as soon as one of its pushes matches, whatever else it pushes,
    # or may match: a wildcard refspec (or HEAD) can land on the denied branch.
    release = permissions.save_action("push", "* → release @ origin", "deny", scope="general", level="destination")
    assert decide(bash(ws, "git push origin fix:main fix:release")).decision == "deny"
    assert decide(bash(ws, "git push origin refs/heads/*:refs/heads/*")).decision == "deny"
    assert decide(push).decision == "allow"
    permissions.update_saved_action(long.id, minutes=1)
    permissions.load_store()
    with permissions.editing() as store:
        store.saved_action(long.id).expires = time.time() - 1
    assert decide(push).decision == "ask"                                    # expired: listed, never applied
    assert permissions.load_store().saved_action(long.id).expired()

    # No rule covers writes under Standard (unmatched asks): a saved allow has no limits to stay within.
    assert decide(write(ws, "src/auth/login.ts")).decision == "ask"
    mine = permissions.save_action("write", "src/**", "allow", scope="general", life="session", session="boss")
    assert decide(write(ws, "src/auth/login.ts")).decision == "ask"         # another session's grant
    assert decide(write(ws, "src/auth/login.ts"), session="boss").decision == "allow"
    stop = permissions.save_action("write", "src/auth/**", "deny", scope="general")
    assert decide(write(ws, "src/auth/login.ts"), session="boss").decision == "deny"   # a saved deny beats a saved allow
    assert decide(write(ws, "src/api/client.ts"), session="boss").decision == "allow"
    assert {s.id for s in permissions.load_store().saved} == {long.id, release.id, mine.id, stop.id}

    # A saved deny also beats an automatic allow rule, so it keeps working when the ask goes away.
    permissions.add_rule("bash", "npm ", "allow", "prefix")
    assert decide(bash(ws, "npm test -- api")).decision == "allow"
    permissions.save_action("bash", "npm test *", "deny", scope="general")
    assert decide(bash(ws, "npm test -- api")).decision == "deny"
    assert decide(bash(ws, "npm run build")).decision == "allow"
    # A saved allow applies only within the current policy: tightened to Secure, where nothing
    # covers MCP tools and unmatched actions are denied, it no longer does.
    tool = Request("claude", "mcp", "mcp__slack__send_message", ws.path, ws.path, ws.repo_root)
    permissions.save_action("mcp", "mcp__slack__*", "allow", scope="server")
    assert decide(tool).decision == "allow"
    permissions.apply_preset("secure")
    assert decide(tool).decision == "deny"
    # Autonomous lets a push approval reach every push, for good; a push into a release
    # branch still asks every time, even beside one into main whose rule matched first.
    permissions.apply_preset("autonomous")
    permissions.save_action("push", "* → * @ *", "allow", scope="all", level="all")
    assert decide(bash(ws, "git push origin HEAD:main")).decision == "allow"
    got = decide(bash(ws, "git push origin HEAD:main HEAD:release/desktop"))
    assert got.decision == "ask" and got.rule.id == "d-push-release-sub"


class Hook:
    """A hook holding a request in the daemon, taking the answer when it comes."""

    def __init__(self, db, agent_id, req, suggestions=None):
        self.agent_id, self.request_id, self.answer = agent_id, f"r-{agent_id}-{uuid.uuid4().hex[:6]}", None
        recorded = json.dumps(req.to_dict()), json.dumps(suggestions) if suggestions else None
        self.digest = permissions.request_digest(*recorded)
        self.nonce = "n-" + self.request_id
        assert HOLDS.register(agent_id, self.request_id, self.digest, self.nonce, time.time() + 600)
        db.add_permission_request(agent_id, self.request_id, recorded[0], "asking", recorded[1], "hook",
                                  hold_until=time.time() + 600)
        self.done = threading.Event()
        threading.Thread(target=self._wait, daemon=True).start()

    def _wait(self):
        pause = threading.Event()
        while True:
            held, answer = HOLDS.take(self.request_id, self.nonce)
            if not held or answer is not None:
                self.answer = answer
                self.done.set()
                return
            pause.wait(0.01)


def worker(db, ws, agent_id, parent="boss"):
    a = Agent(agent_id, ws.id, "developer", "claude", parent, "assign", "waiting", f"%{agent_id}", None, time.time())
    db.add_agent(a)
    return a


def test_pending_asks_group_by_general_form_and_one_decision_resolves_the_group(db, ws):
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "%b", None, time.time()))
    worker(db, ws, "w1"), worker(db, ws, "w2"), worker(db, ws, "w3")
    hooks = [Hook(db, "w1", write(ws, "src/auth/login.ts")), Hook(db, "w2", write(ws, "src/auth/session.ts")),
             Hook(db, "w3", bash(ws, "git push origin fix:main")), Hook(db, "w1", bash(ws, "npm test -- auth")),
             Hook(db, "w2", mcp(ws, "mcp__github__create_issue")), Hook(db, "w3", mcp(ws, "mcp__github__create_issue")),
             Hook(db, "w1", bash(ws, "git push origin fix:main fix:staging")),
             Hook(db, "w2", bash(ws, "git push origin HEAD")),
             Hook(db, "w3", bash(ws, "git push origin fix:main fix:release/desktop"))]
    items = grants.groups(grants.pending_asks(db, ws.repo_root))
    # A command pushing to two destinations is its own item, generalized to both destinations.
    assert [(g.kind, g.rep.ask.general["pattern"], len(g.items)) for g in items] == [
        ("write", "src/**", 2), ("push", "* → main @ origin", 1), ("bash", "family: dev/tests", 1),
        ("mcp", "mcp__github__create_issue", 2), ("push", "* → main @ origin, * → staging @ origin", 1),
        ("push", "* → ? @ origin", 1), ("push", "* → main @ origin, * → release/desktop @ origin", 1)]
    assert items[4].rep.ask.exact == "fix → main @ origin, fix → staging @ origin"
    # A push into a release branch asks under its own rule, whichever rule matched first,
    # and offers nothing that would save an approval.
    release = items[6]
    assert release.policy.rule.id == "d-push-release-sub"
    assert svc.group_dto(release).modes == ["once", "deny", "always-deny"]
    with pytest.raises(Exception, match="approvable only once"):
        grants.decide_group(db, release.key, "general", "timed", 5)
    assert grants.decide_group(db, release.key, "general", "once")["grant"] is None
    assert hooks[8].done.wait(5) and hooks[8].answer == "allow_once"
    assert items[4].rep.ask.scope("exact")["pattern"] == items[4].rep.ask.exact
    assert items[1].policy.rule.id == "d-push-default" and items[0].policy.rule is None
    # An MCP tool's general form is the tool itself: "once" at that scope still resolves the whole item.
    out = grants.decide_group(db, items[3].key, "general", "once")
    assert len(out["resumed"]) == 2 and all(h.done.wait(5) and h.answer == "allow_once" for h in hooks[4:6])
    assert svc.decision_dto(out).grant is None

    writes = items[0]
    assert grants.decide_group(db, writes.key, "general", "timed", 5)["resumed"] == [
        {"agent_id": "w1", "request_id": hooks[0].request_id, "exact": "src/auth/login.ts"},
        {"agent_id": "w2", "request_id": hooks[1].request_id, "exact": "src/auth/session.ts"}]
    assert all(h.done.wait(5) and h.answer == "allow_once" for h in hooks[:2])
    [grant] = permissions.load_store().saved
    assert (grant.kind, grant.pattern, grant.life, grant.minutes, grant.repo) == ("write", "src/**", "time", 5, ws.repo_root)
    assert grant.request["path"].endswith("src/auth/login.ts") and grant.expires > time.time()
    # The next such ask never reaches the Inbox: the grant covers it.
    assert decide(write(ws, "src/auth/token.ts")).decision == "allow"
    assert {g.kind for g in grants.groups(grants.pending_asks(db, ws.repo_root))} == {"push", "bash"}

    # Forever is outside the push rule's 30-minute limit; "once" at the exact scope saves nothing.
    pushes = next(g for g in grants.groups(grants.pending_asks(db, ws.repo_root)) if g.kind == "push")
    with pytest.raises(Exception, match="nothing in this scope can be approved: not permitted until revoked"):
        grants.decide_group(db, pushes.key, "general", "forever")
    out = grants.decide_group(db, pushes.key, "general", "once")
    assert out["grant"] is None and len(out["resumed"]) == 1 and hooks[2].done.wait(5) and hooks[2].answer == "allow_once"
    assert not hooks[6].done.is_set()                                   # the two-destination push waits on its own
    both = next(g for g in grants.groups(grants.pending_asks(db, ws.repo_root)) if g.rep.agent_id == "w1" and g.kind == "push")
    with pytest.raises(Exception, match="outside its approval scope"):  # the remote is past the push rule's limit
        grants.decide_group(db, both.key, "remote", "timed", 5)
    # Its general form covers both destinations, each push checked against them.
    out = grants.decide_group(db, both.key, "general", "timed", 5)
    assert out["grant"]["pattern"] == "* → main @ origin, * → staging @ origin" and len(out["resumed"]) == 1
    assert hooks[6].done.wait(5) and hooks[6].answer == "allow_once"
    assert decide(bash(ws, "git push origin fix:main fix:staging")).decision == "allow"
    assert decide(bash(ws, "git push origin fix:main fix:dev")).decision == "ask"
    # A push whose branch only git knows is approved once by its own identity; nothing saved covers it.
    head = next(g for g in grants.groups(grants.pending_asks(db, ws.repo_root)) if g.rep.agent_id == "w2" and g.kind == "push")
    with pytest.raises(Exception, match="approve it once"):
        grants.decide_group(db, head.key, "exact", "session")
    out = grants.decide_group(db, head.key, "exact", "once")
    assert out["grant"] is None and len(out["resumed"]) == 1 and hooks[7].done.wait(5) and hooks[7].answer == "allow_once"
    assert decide(bash(ws, "git push origin HEAD")).decision == "ask"
    # Always deny stops the group and saves the deny.
    tests = next(g for g in grants.groups(grants.pending_asks(db, ws.repo_root)) if g.kind == "bash")
    out = grants.decide_group(db, tests.key, "general", "always-deny")
    assert len(out["stopped"]) == 1 and hooks[3].done.wait(5) and hooks[3].answer == "deny"
    assert decide(bash(ws, "npm test -- api")).decision == "deny"
    assert grants.pending_asks(db) == []


@pytest.mark.parametrize("approved, sibling, decision", [
    ("git log --oneline -5", "git diff HEAD~1 | head -50", "allow"),                          # read-only git
    ("ls -la src | head", "cd src && grep -rn foo . | wc -l", "allow"),                       # read-only shell
    ("uv run pytest tests/test_a.py -q 2>&1 | tail -5", "uv run pytest tests/test_b.py", "allow"),   # a test run
    ("npm ls --depth=0", "npm ls react | head -3", "allow"),                                  # a tool's query
    ("uv run pytest tests/test_a.py", "cd / && uv run pytest", "ask"),                        # another project's
    ("uv run pytest tests/test_a.py -q 2>&1 | tail -5", "uv run rm -rf build", "ask"),        # uv run, no test
    ("uv run pytest tests/test_a.py", "uv run pytest --basetemp=src", "ask"),                 # deletes src
    ("uv run pytest tests/test_a.py", "uv run pytest -k @args.txt", "ask"),                   # options from a file
    ("npm config get registry | head", "npm config set registry https://example.invalid", "ask"),   # a write
    ("ls -la src | head", "ls && cd $DIR && cat secret.txt", "ask"),                          # where, unknown
    ("git log --oneline -5", "git log -1 && git reset --hard", "ask"),                        # a write beside it
    ("grep -rn foo src | head", "grep -rn foo src > found.txt", "ask"),                       # a redirect writes
])
def test_approval_covers_its_family(db, ws, approved, sibling, decision):
    """Approving one ask at its proposed scope lets every command of the same
    family through, however it is piped or chained; never one with any other part."""
    permissions.apply_preset("secure")
    permissions.forget("d-git-ro")                                   # a policy that asks for every command
    (Path(ws.path) / "src").mkdir()
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "%b", None, time.time()))
    worker(db, ws, "w1")
    hook = Hook(db, "w1", bash(ws, approved))
    [item] = grants.groups(grants.pending_asks(db, ws.repo_root))
    grants.decide_group(db, item.key, "general", "timed", 15)
    assert hook.done.wait(5) and hook.answer == "allow_once"
    assert decide(bash(ws, sibling)).decision == decision


@pytest.mark.parametrize("command, found", [
    ("cd app && npm test", ["dev/tests"]),                              # a leading cd: the rest runs in app
    ("cd app && mkdir dist && git add dist", ["files/write-worktree", "git/local"]),   # changes, there too
    ("git status && cd {ws}/app && git log -3", ["git/readonly"]),      # a cd in the chain, inside the checkout
    ("cd $DIR && npm test", None),                                      # where only the run knows
    ("cd / && npm test", None),                                         # another project's
    ("cd app ; npm test", None),                                        # the cd may have failed
    ("cd app && npm test || npm test", None),                           # runs where the cd failed
    ("ls | cd app && npm test", None),                                  # the cd ran in the pipeline's subshell
    ("! cd app && npm test", None),                                     # runs only where the cd failed
    ("cd / && ls && {{ cd {ws} && ls; }} | npm test", None),           # the group's cd ran in its own subshell
])
def test_cd_chain_keeps_its_family(ws, command, found):
    """What runs after a cd it surely followed (``cd x && ...``) runs where
    the cd led; after one that may have failed, where is unknown."""
    (Path(ws.path) / "app").mkdir()
    assert grants.families(bash(ws, command.format(ws=ws.path))) == found


@pytest.mark.parametrize("approved, sibling, decision", [
    ("git add -A", "git commit -m wip && git switch -c topic && git stash pop && git rebase main", "allow"),
    ("git add -A", "git branch -D old && git tag -d v1 && git stash drop && git merge -s ours topic", "allow"),
    ("npm install", "uv add httpx && cd {lib} && npm add -D left-pad", "allow"),     # in another attached repo
    ("mkdir -p build", "sed -i '' s/a/b/ app.py && rm -rf build/x > out.log && cp app.py build/", "allow"),
    ("mkdir -p build", "rm -rf {lib}/dist && echo x > {other}/notes.txt", "allow"),   # another repo, its worktree
    ("uv run pytest tests/test_a.py", "cd {lib} && uv run pytest", "allow"),
    ("git add -A", "git commit -m wip && git push origin HEAD:main", "ask"),        # never a push
    ("git add -A", "git push -f origin feature", "deny"),
    ("git add -A", "git reset --hard", "ask"),
    ("git add -A", "git clean -fdx", "deny"),
    ("git add -A", "git checkout -- .", "ask"),                                     # the whole checkout
    ("git add -A", "git -c core.hooksPath=/x commit -m wip", "ask"),
    ("git add -A", "git rebase --exec make main", "ask"),                           # runs a command
    *(pytest.param("git add -A", c, "ask", marks=pytest.mark.variant("bypass: an option spelled another way"))
      for c in ["git rebase -x'touch {out}/x' main", "git rebase --exe=make main", "git merge -s evil topic",
                "git rebase -S --exec=make main", "git merge -S -sevil topic"]),
    ("npm install", "npm install -g left-pad", "ask"),                             # outside the project
    ("npm install", "npm install --cache={out} foo", "ask"),
    pytest.param("npm install", ".venv/bin/pip install --log={out}/log httpx", "ask",
                 marks=pytest.mark.variant("bypass: an option's value writes outside")),
    ("mkdir -p build", "cp notes.txt build/", "ask"),                              # build/notes.txt links out
    *(pytest.param("mkdir -p build", c, "ask", marks=pytest.mark.variant("bypass: a destination cp doesn't name last"))
      for c in ["cp --target-directory=build notes.txt", "cp -RT src build", "cp -R src/ build"]),
    ("mkdir -p build", "cp -R src copied && echo changed > copied/link/file", "ask"),   # src/link leads out
    ("mkdir -p build", "sed -i'{out}/*' s/a/b/ app.py", "ask"),                    # its backup, outside
    ("npm install", "sudo npm install", "ask"),
    ("npm install", "npm install $(cat pkgs)", "ask"),                             # brindle can't read it
    ("npm install", "cd {out} && npm install", "ask"),
    ("mkdir -p build", "rm -rf {ws}", "ask"),                                      # a repo root
    ("mkdir -p build", "rm -rf {lib}", "ask"),
    ("mkdir -p build", "rm -rf {out}/x", "ask"),                                   # outside every repo
    ("mkdir -p build", "mv app.py {out}/", "ask"),
    ("mkdir -p build", "echo x > {out}/notes.txt", "ask"),
    ("mkdir -p build", "echo x > .git/hooks/pre-commit", "ask"),
])
def test_approval_widens_to_change_families(db, ws, tmp_path, approved, sibling, decision):
    """An approved local git change, install or file change covers every
    other of its family in any repo of the asking agent's session or its worktrees;
    never a push, a reset or clean of a checkout, a delete of a repo, a write
    outside the repos, sudo, or what brindle can't read."""
    permissions.apply_preset("secure")
    permissions.forget("d-git-ro")
    (Path(ws.path) / "build").mkdir()
    lib, other, out = tmp_path / "lib", tmp_path / "other-wt", tmp_path / "elsewhere"
    for d in (lib, other, out):
        d.mkdir()
    (Path(ws.path) / "build" / "notes.txt").symlink_to(out / "notes.txt")
    (Path(ws.path) / "src").mkdir()
    (Path(ws.path) / "src" / "link").symlink_to(out)
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "%b", None, time.time()))
    db.add_session_repo("boss", "lib", str(lib))
    other_ws = replace(ws, id="other", path=str(other), repo_root=str(lib), tmux_session="o")
    db.add_workspace(other_ws)
    worker(db, ws, "w1"), worker(db, other_ws, "w2")
    hook = Hook(db, "w1", replace(bash(ws, approved), agent="w1"))
    [item] = grants.groups(grants.pending_asks(db, ws.repo_root))
    grants.decide_group(db, item.key, "general", "timed", 15)
    assert hook.done.wait(5) and hook.answer == "allow_once"
    sibling = sibling.format(ws=ws.path, lib=lib, other=other, out=out)
    assert decide(replace(bash(ws, sibling), agent="w1")).decision == decision


@pytest.mark.parametrize("agent, kind, target, group, decision", [
    ("w1", "write", "{lib}/x.py", ("files", "write-worktree"), "allow"),    # a second repo attached to the session
    ("w1", "bash", "sed -i '' s/a/b/ {lib}/x.py", ("files", "write-worktree"), "allow"),
    ("w1", "write", "{lib}/protected/x.py", ("files", "write-worktree"), "deny"),   # a relative deny reaches it
    ("w1", "bash", "git -C {lib} status", ("git", "readonly"), "allow"),
    ("w1", "write", "{alien}/x.py", ("files", "write-outside"), "ask"),    # attached only to another session
    ("w1", "bash", "sed -i '' s/a/b/ {alien}/x.py", ("files", "write-outside"), "ask"),
    ("w1", "bash", "git -C {alien} status", ("git", "other"), "ask"),
    ("", "write", "{lib}/x.py", ("files", "write-outside"), "ask"),         # no session known: its own checkout only
])
def test_session_repos_count_as_checkout(db, ws, tmp_path, agent, kind, target, group, decision):
    """Every repo attached to the asking agent's session is in the checkout
    for the worktree matcher, relative path rules, git -C, the write classes
    and the change families; another session's repo is not."""
    from brindle import permission_groups

    permissions.apply_preset("permissive")
    permissions.add_rule("write", "protected/**", "deny", "glob")
    for name in ("lib", "alien"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "x.py").write_text("a\n")
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "%b", None, time.time()))
    db.add_agent(Agent("boss2", ws.id, "supervisor", "claude", None, "interactive", "processing", "%c", None,
                       time.time()))
    db.add_session_repo("boss", "lib", str(tmp_path / "lib"))
    db.add_session_repo("boss2", "alien", str(tmp_path / "alien"))
    worker(db, ws, "w1")
    target = target.format(lib=tmp_path / "lib", alien=tmp_path / "alien")
    req = replace(write(ws, "x.py"), path=target) if kind == "write" else bash(ws, target)
    req = replace(req, agent=agent)
    assert permission_groups.classify(req, permissions.all_rules(ws.repo_root)) == [group]
    if kind == "bash":
        assert grants.families(req) == (["/".join(group)] if decision == "allow" else None)
    assert decide(req).decision == decision


def test_allow_all_grants_within_each_policy_and_unblock_narrows_the_deny_that_blocks(db, ws):
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "%b", None, time.time()))
    worker(db, ws, "w1"), worker(db, ws, "w2")
    hooks = [Hook(db, "w1", bash(ws, "git push origin fix:main")), Hook(db, "w2", bash(ws, "terraform apply prod")),
             Hook(db, "w2", bash(ws, "terraform apply dev")), Hook(db, "w1", bash(ws, "git push origin fix:main fix:staging"))]
    permissions.add_rule("bash", "terraform apply prod", "deny")      # added while it waited: now blocked
    planned = grants.plan_to_json(grants.plan_allow_all(grants.pending_asks(db, ws.repo_root)))
    assert [(g["kind"], g["scope"]["pattern"], g["mode"], g["minutes"]) for g in planned["grants"]] == [
        ("push", "* → main @ origin", "timed", 30), ("bash", "terraform apply *", "session", None),
        ("push", "* → main @ origin, * → staging @ origin", "timed", 30)]
    assert planned["resumes"] == 3 and planned["skipped"][0]["why"] == "blocked by a deny rule"
    assert svc.plan_dto(planned).skipped[0].agent_id == "w2"           # the API's shape, blocked requests included
    lasting = grants.plan_to_json(grants.plan_allow_all(grants.pending_asks(db, ws.repo_root), forever=True))
    assert [g["kind"] for g in lasting["grants"]] == ["bash"]           # the push rule permits no lasting grant

    out = grants.unblock_denied(db, ws.repo_root)
    assert out["excepted"] == 1 and out["added"] == 0 and out["locked"] == [] and svc.plan_dto(out).excepted == 1
    assert [r["exact"] for r in out["resumed"]] == ["terraform apply prod"]
    assert hooks[1].done.wait(5) and hooks[1].answer == "allow_once"
    rule = next(r for r in permissions.load_store().rules if r.match == "terraform apply prod" and r.decision == "deny")
    assert [x.match for x in rule.exclusions] == ["terraform apply prod"]
    # The exception, then the saved allow (for this session: that is all an unlimited policy grants here).
    assert decide(bash(ws, "terraform apply prod"), session="boss").decision == "allow"
    assert decide(bash(ws, "terraform apply prod")).decision == "ask"

    out = grants.allow_all(db, ws.repo_root)
    assert sorted(r["exact"] for r in out["resumed"]) == [
        "fix → main @ origin", "fix → main @ origin, fix → staging @ origin", "terraform apply dev"]
    assert all(h.done.wait(5) and h.answer == "allow_once" for h in hooks)
    lives = {(s.kind, s.life, s.minutes) for s in permissions.load_store().saved}
    assert lives == {("bash", "session", None), ("push", "time", 30.0)}

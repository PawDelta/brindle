"""Learned rules (brindle Pro): review findings that recur across tasks
become suggested rules, accepted into .brindle/rules/learned.md or rejected
for good. The grouping step's model call is faked throughout."""
import json
from itertools import product

import pytest

from brindle import git, learned_rules, rule_checks
from brindle.profiles import learned_pack_path, load_profile, load_rule_packs
from brindle.pro import license


@pytest.fixture
def pro(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "learned_rules")


def review(db, root, branch, summary, *, approved=False, profile="developer"):
    """A worker's report on ``branch`` and a review of it, as brindle records them."""
    db.add_history(root, "worker_result", agent_id=f"w-{branch}", branch=branch,
                   profile=profile, task="do it", result="done")
    verdict = "APPROVED" if approved else "CHANGES REQUESTED"
    db.add_history(root, "review", agent_id=f"r-{branch}", branch=branch, profile="reviewer",
                   result=f"Review of {branch} (workspace ws-{branch}) at abcdef12: {verdict}\n\n{summary}")


def rows(db, root):
    return db.history_after(root, "review", 0)


class FakeModel:
    """The grouping call: answers with the groups a test gives it (a function
    of the findings' ids), and counts calls."""

    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps({"groups": self.answer(self.ids(prompt))})

    @staticmethod
    def ids(prompt: str) -> list[int]:
        import re

        return [int(m) for m in re.findall(r"^\[(\d+)\]", prompt.split("New findings:", 1)[1], re.M)]


def bare_except(ids, **extra):
    return [{"title": "bare except", "rule": "Catch specific exceptions, never a bare `except:`.",
             "findings": ids, "deny_patterns": [r"except\s*:"], **extra}]


# -- findings ------------------------------------------------------------------------


def test_findings_are_change_requests_filed_under_the_workers_profile(db, tmp_path):
    root = str(tmp_path)
    review(db, root, "feat/a", "Bare except in app.py", profile="developer-codex")
    review(db, root, "feat/b", "Looks good", approved=True)
    db.add_history(root, "review", branch="main", profile="reviewer",
                   result="Completion audit: gaps\n\nmissing docs")          # an audit, not a branch review
    db.add_history(root, "review", branch="feat/nobody", profile="reviewer",
                   result="Review of feat/nobody (workspace w) at 1234: CHANGES REQUESTED\n\nx")
    found, last = learned_rules.new_findings(db, root)
    assert [(f.branch, f.profile, f.text) for f in found] == [
        ("feat/a", "developer-codex", "Bare except in app.py")]
    assert last == rows(db, root)[-1].id


# -- grouping and suggesting -----------------------------------------------------------


def test_a_failed_call_leaves_the_findings_for_next_time(db, tmp_path, pro):
    root = str(tmp_path)
    review(db, root, "feat/a", "bare except")

    def broken(prompt):
        return "sorry, I can't"

    with pytest.raises(learned_rules.LearnedRulesError):
        learned_rules.refresh(db, root, broken)
    assert db.learned_rules_mark(root) == 0
    assert learned_rules.refresh(db, root, FakeModel(bare_except)).findings == 1


def test_unusable_mechanical_checks_are_dropped(db, tmp_path, pro):
    checks = learned_rules.clean_checks({
        "deny_patterns": ["eval\\(", "(unclosed", "a --- b", "x\ry", "x # y"],
        "deny_deps": ["pickle", "pickle"], "require_tests_for": ["src/**/*.py"]})
    assert checks == {"deny_patterns": ["eval\\(", "x # y"], "deny_deps": ["pickle"],
                      "require_tests_for": ["src/**/*.py"]}


# -- accepting and rejecting -------------------------------------------------------------


def suggest_one(db, root, answer=bare_except):
    for b in ("feat/a", "feat/b", "feat/c"):
        review(db, root, b, "bare except")
    [s] = learned_rules.refresh(db, root, FakeModel(answer)).suggested
    return s


def test_accept_writes_a_rule_pack_every_profile_gets_and_checks(db, tmp_path, pro):
    root = str(tmp_path)
    s = suggest_one(db, root, lambda ids: bare_except(
        ids, deny_deps=["pickle"], require_tests_for=["src/**/*.py"]))
    r, path = learned_rules.accept(db, root, s.key[:4])
    assert path == learned_pack_path(root) and r.status == "accepted"
    text = path.read_text()
    assert "Catch specific exceptions" in text and f"rule {s.key}" in text

    packs = load_rule_packs(load_profile("developer", root), root)
    [learned] = [p for p in packs if p.name == "learned"]
    assert learned.deny_patterns == [r"except\s*:"]
    assert learned.deny_deps == ["pickle"] and learned.require_tests_for == ["src/**/*.py"]
    assert "Catch specific exceptions" in learned.prompt

    diff = rule_checks.parse_diff(
        "diff --git a/src/app.py b/src/app.py\n--- a/src/app.py\n+++ b/src/app.py\n"
        "@@ -1,1 +1,3 @@\n x = 1\n+try: f()\n+except: pass\n")
    found = rule_checks.check([learned], diff)
    assert {v.rule for v in found} == {"deny_patterns", "require_tests_for"}

    assert learned_rules.suggestions(db, root) == []
    with pytest.raises(learned_rules.LearnedRulesError):
        learned_rules.accept(db, root, s.key)


PACK_HEAD = ("---\nname: learned\n# why this pack exists\n"
             "description: mine   # trailing note\n"
             "# processes we start on purpose\n"
             "direct_spawn_allowed:\n  - src/spawn.py   # the spawner\n  # and the tmux wrapper\n"
             "  - src/tmux.py\n"
             "deny_patterns:\n  - 'print\\('   # no prints\n"
             "deny_deps: [marshal]  # keep this annotation\n"
             "\n# end of checks\n---\n")


@pytest.mark.parametrize("item", ["x # y", "[a-z]", "a,b", "it's", '"q"', "exec"])
@pytest.mark.variant("Review probes for quoting and layout preservation, including CRLF")
def test_accept_keeps_the_packs_comments(db, tmp_path, pro, item):
    layouts = product(["\n", "\r\n"], ["[eval]", "'eval'", "\n  # old check\n  - eval  # keep"])
    for i, (eol, seed) in enumerate(layouts):
        root = str(tmp_path / str(i))
        s = suggest_one(db, root, lambda ids: bare_except(
            ids, deny_patterns=[item], deny_deps=["pickle"], require_tests_for=["src/**/*.py"]))
        path = learned_pack_path(root)
        path.parent.mkdir(parents=True)
        start = f"deny_patterns: {seed}" + ("  # keep" if not seed.startswith("\n") else "")
        head = PACK_HEAD.replace("deny_patterns:\n  - 'print\\('   # no prints", start).replace("\n", eol)
        path.write_bytes((head + f"Intro.{eol}- old rule{eol}").encode())
        learned_rules.accept(db, root, s.key)
        text = path.read_bytes().decode()
        assert text.endswith(f"rule {s.key}){eol}") and f"Intro.{eol}- old rule{eol}- Catch" in text

        # every original line survives in order; only the inline list line grew, in place
        grown = f"deny_deps: [marshal, pickle]  # keep this annotation{eol}"
        expected = [grown if ln.startswith("deny_deps:") else ln for ln in head.splitlines(keepends=True)]
        if not seed.startswith("\n"):
            line = next(ln for ln in text.splitlines(keepends=True) if ln.startswith("deny_patterns:"))
            assert line.startswith(f"deny_patterns: {seed.removesuffix(']')}, ")
            assert line.endswith(("]" if seed.startswith("[") else "") + f"  # keep{eol}")
            expected = [line if ln.startswith("deny_patterns:") else ln for ln in expected]
        got = iter(text.splitlines(keepends=True))
        assert all(ln in got for ln in expected)
        assert all(ln[ln.index("#"):] in text for ln in head.splitlines(keepends=True) if "#" in ln)
        pack = [p for p in load_rule_packs(load_profile("reviewer", root), root) if p.name == "learned"][0]
        assert pack.deny_deps == ["marshal", "pickle"] and pack.deny_patterns == ["eval", item]
        assert pack.require_tests_for == ["src/**/*.py"]
        assert pack.direct_spawn_allowed == ["src/spawn.py", "src/tmux.py"]
        assert f"require_tests_for:{eol}  - src/**/*.py{eol}---{eol}" in text
        # Grow the result again: quoted # and brackets must not hide the insertion point.
        s.checks = json.dumps({"deny_patterns": [item, r'\b"next"']})
        learned_rules.write_rule(root, s)
        again = path.read_bytes().decode()
        pack = [p for p in load_rule_packs(load_profile("reviewer", root), root) if p.name == "learned"][0]
        assert pack.deny_patterns == ["eval", item, r'\b"next"']
        assert all(ln[ln.index("#"):] in again for ln in head.splitlines(keepends=True) if "#" in ln)


def test_a_rejected_rule_is_never_proposed_again(db, tmp_path, pro):
    root = str(tmp_path)
    s = suggest_one(db, root)
    learned_rules.reject(db, root, s.key)
    assert learned_rules.suggestions(db, root) == []

    # More of the same: filed under the rejected group, or proposed afresh under the same name.
    for b in ("feat/d", "feat/e", "feat/f"):
        review(db, root, b, "bare except yet again")
    assert learned_rules.refresh(db, root, FakeModel(
        lambda ids: [{"key": s.key, "findings": ids[:1]}] + bare_except(ids[1:]))).suggested == []
    [r] = db.learned_rules(root)
    assert r.status == "rejected" and learned_rules.task_count(r) == 6
    assert learned_rules.suggestions(db, root) == []
    assert not learned_pack_path(root).exists()


# -- which model the grouping step may use ---------------------------------------------------


def write_agent(root, name, base_url):
    d = root / ".brindle" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\nname: {name}\nprovider: native\napi: openai\n"
                                  f"base_url: {base_url}\nmodel: m\napi_key_env: OPENAI_API_KEY\n---\nx\n")


def test_a_repo_profile_cant_send_findings_off_the_machine(tmp_path, monkeypatch):
    """A cloned repo's committed config and profile must not route review
    findings (and the API key) to its own server."""
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    write_agent(tmp_path, "grouper", "https://localhost.evil.example/v1")
    (tmp_path / ".brindle" / "config.json").write_text('{"learned_rules_profile": "grouper"}')
    with pytest.raises(learned_rules.LearnedRulesError, match="defined in this repo"):
        learned_rules.default_call(str(tmp_path))


def test_a_lookalike_local_profile_is_not_picked(tmp_path, monkeypatch):
    from brindle.native import runner

    monkeypatch.setenv("OPENAI_API_KEY", "k")
    write_agent(tmp_path, "aaa-sneaky", "https://localhost.evil.example/v1")
    probed = []
    monkeypatch.setattr(runner, "probe", lambda ep, timeout=3.0: (probed.append(ep.base_url), (False, ""))[1])
    learned_rules.default_call(str(tmp_path))      # falls back to Claude; nothing is sent yet
    assert "https://localhost.evil.example/v1" not in probed


def test_a_claude_call_that_times_out_leaves_nothing_it_started_running(tmp_path, monkeypatch):
    """The grouping step's claude CLI starts helpers of its own (MCP
    servers, a shell); one that hangs past the limit takes them with it."""
    import subprocess
    import sys

    from brindle import providers
    from brindle.native import runner

    pidfile = tmp_path / "helper.pid"
    cli = tmp_path / "claude"
    cli.write_text(f"#!{sys.executable}\nimport os, signal\nhelper = os.fork()\nif helper == 0:\n"
                   "    signal.pause()\n"
                   f"open({str(pidfile)!r}, 'w').write(str(helper))\nsignal.pause()\n")
    cli.chmod(0o755)
    monkeypatch.setattr(providers, "claude_binary", lambda *a, **k: str(cli))
    monkeypatch.setattr(runner, "probe", lambda ep, **k: (False, ""))
    monkeypatch.setattr(learned_rules, "CALL_TIMEOUT", 0.5)
    call = learned_rules.default_call(str(tmp_path))
    with pytest.raises(learned_rules.LearnedRulesError):
        call("group these")
    helper = int(pidfile.read_text())
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(helper)], capture_output=True, text=True).stdout.strip()
    if state and not state.startswith("Z"):
        subprocess.run(["kill", "-9", str(helper)])     # this test's own helper: not left behind either way
    assert not state or state.startswith("Z")


# -- entitlement: fails closed -------------------------------------------------------------


def test_without_the_entitlement_nothing_is_grouped_or_suggested(db, tmp_path, monkeypatch):
    root = str(tmp_path)
    # A license check that raises counts as not entitled.
    monkeypatch.setattr(license, "has", lambda f: (_ for _ in ()).throw(RuntimeError("bad")))
    review(db, root, "feat/a", "bare except")
    fake = FakeModel(bare_except)
    with pytest.raises(learned_rules.LearnedRulesError, match="brindle Pro"):
        learned_rules.refresh(db, root, fake)
    assert fake.prompts == [] and learned_rules.notes(db, root) == []
    with pytest.raises(learned_rules.LearnedRulesError):
        learned_rules.accept(db, root, "x")

    spawned = []
    monkeypatch.setattr(learned_rules.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    learned_rules.refresh_later(root)
    assert spawned == []


# -- the CLI ------------------------------------------------------------


def test_rules_cli(db, repo, monkeypatch, pro):
    from typer.testing import CliRunner

    from brindle.cli import app

    monkeypatch.chdir(repo)
    root = git.main_repo_root(str(repo))
    for b in ("feat/a", "feat/b", "feat/c"):
        review(db, root, b, "bare except")
    fake = FakeModel(bare_except)
    monkeypatch.setattr(learned_rules, "default_call", lambda repo_root, db=None: fake)
    runner = CliRunner()

    res = runner.invoke(app, ["rules", "suggest"])
    assert res.exit_code == 0, res.output
    [s] = learned_rules.suggestions(db, root)
    assert "grouped 3 new review finding(s)" in res.output and s.key in res.output
    assert r"deny_patterns: except\s*:" in res.output
    assert runner.invoke(app, ["rules", "suggest"]).output.count(s.key) >= 1
    assert len(fake.prompts) == 1

    res = runner.invoke(app, ["rules", "accept", s.key])
    assert res.exit_code == 0, res.output
    assert learned_pack_path(root).is_file()
    assert "no suggestions" in runner.invoke(app, ["rules", "suggest", "--no-refresh"]).output
    assert runner.invoke(app, ["rules", "reject", s.key]).exit_code == 1    # already accepted
    assert runner.invoke(app, ["rules", "accept", "zzzz"]).exit_code == 1

    monkeypatch.setattr(license, "has", lambda feature: False)
    res = runner.invoke(app, ["rules", "suggest"])
    assert res.exit_code == 1 and "brindle Pro" in res.output

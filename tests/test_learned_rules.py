"""Learned rules (brindle Pro): review findings that recur across tasks
become suggested rules, accepted into .brindle/rules/learned.md or rejected
for good. The grouping step's model call is faked throughout."""
import json
from dataclasses import replace

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


def test_a_finding_in_three_tasks_is_suggested(db, tmp_path, pro):
    root = str(tmp_path)
    fake = FakeModel(bare_except)
    for b in ("feat/a", "feat/b"):
        review(db, root, b, f"bare except in {b}")
    done = learned_rules.refresh(db, root, fake)
    assert done.called and done.findings == 2 and done.suggested == []
    assert learned_rules.suggestions(db, root) == []
    [forming] = db.learned_rules(root)
    assert forming.status == "forming" and learned_rules.task_count(forming) == 2

    review(db, root, "feat/c", "you used a bare except again")
    done = learned_rules.refresh(db, root, fake)
    assert done.findings == 1          # only the new one goes to the model
    assert "key " + forming.key in fake.prompts[-1]   # with the known group, to file it under
    [s] = done.suggested
    assert s.key == forming.key and s.status == "pending" and learned_rules.task_count(s) == 3
    assert learned_rules.suggestions(db, root)[0].key == s.key
    assert len(fake.prompts) == 2


def test_the_same_task_reviewed_twice_counts_once(db, tmp_path, pro):
    root = str(tmp_path)
    for summary in ("bare except", "still a bare except", "bare except, third round"):
        review(db, root, "feat/a", summary)
    learned_rules.refresh(db, root, FakeModel(bare_except))
    [r] = db.learned_rules(root)
    assert learned_rules.task_count(r) == 1 and r.status == "forming"


def test_no_new_findings_no_call(db, tmp_path, pro):
    root = str(tmp_path)
    fake = FakeModel(bare_except)
    assert not learned_rules.refresh(db, root, fake).called
    review(db, root, "feat/a", "fine", approved=True)
    assert not learned_rules.refresh(db, root, fake).called
    assert fake.prompts == []
    assert db.learned_rules_mark(root) == rows(db, root)[-1].id


def test_threshold_is_configurable(db, tmp_path, pro):
    root = str(tmp_path)
    (tmp_path / ".brindle").mkdir()
    (tmp_path / ".brindle" / "config.json").write_text('{"learned_rules_repeats": 2}')
    for b in ("feat/a", "feat/b"):
        review(db, root, b, "bare except")
    assert len(learned_rules.refresh(db, root, FakeModel(bare_except)).suggested) == 1


def test_groups_are_per_profile(db, tmp_path, pro):
    root = str(tmp_path)
    for b in ("feat/a", "feat/b", "feat/c"):
        review(db, root, b, "bare except", profile="developer")
    for b in ("feat/x", "feat/y"):
        review(db, root, b, "bare except", profile="developer-codex")
    done = learned_rules.refresh(db, root, FakeModel(bare_except))
    by_profile = {r.profile: r for r in db.learned_rules(root)}
    assert set(by_profile) == {"developer", "developer-codex"}
    assert by_profile["developer"].key != by_profile["developer-codex"].key
    assert [s.profile for s in done.suggested] == ["developer"]
    assert by_profile["developer-codex"].status == "forming"


def test_a_failed_call_leaves_the_findings_for_next_time(db, tmp_path, pro):
    root = str(tmp_path)
    review(db, root, "feat/a", "bare except")

    def broken(prompt):
        return "sorry, I can't"

    with pytest.raises(learned_rules.LearnedRulesError):
        learned_rules.refresh(db, root, broken)
    assert db.learned_rules_mark(root) == 0
    assert learned_rules.refresh(db, root, FakeModel(bare_except)).findings == 1


def test_parse_groups_tolerates_fences_and_drops_junk():
    text = ('Here you go:\n```json\n{"groups": [{"title": "t", "rule": "r", "findings": [1, "2", "x"]},'
            ' {"title": "empty", "findings": []}, "junk"]}\n```')
    [g] = learned_rules.parse_groups(text)
    assert g.findings == [1, 2] and g.title == "t"


def test_unusable_mechanical_checks_are_dropped(db, tmp_path, pro):
    checks = learned_rules.clean_checks({
        "deny_patterns": ["eval\\(", "(unclosed", "a --- b", "x # y"],
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


def test_a_second_accepted_rule_adds_to_the_pack(db, tmp_path, pro):
    root = str(tmp_path)
    first = suggest_one(db, root)
    learned_rules.accept(db, root, first.key)
    for b in ("feat/d", "feat/e", "feat/f"):
        review(db, root, b, "print left in")
    [second] = learned_rules.refresh(db, root, FakeModel(lambda ids: [
        {"title": "debug prints", "rule": "Remove debug prints.", "findings": ids,
         "deny_patterns": [r"^\s*print\("]}])).suggested
    learned_rules.accept(db, root, second.key)
    pack = [p for p in load_rule_packs(load_profile("reviewer", root), root) if p.name == "learned"][0]
    assert pack.deny_patterns == [r"except\s*:", r"^\s*print\("]
    assert "Catch specific exceptions" in pack.prompt and "Remove debug prints." in pack.prompt


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


def test_unknown_or_ambiguous_keys(db, tmp_path, pro):
    root = str(tmp_path)
    with pytest.raises(learned_rules.LearnedRulesError, match="no learned rule"):
        learned_rules.accept(db, root, "nope")
    with pytest.raises(learned_rules.LearnedRulesError):
        learned_rules.reject(db, root, "")


# -- which model the grouping step may use ---------------------------------------------------


@pytest.mark.parametrize("url,local", [
    ("http://localhost:11434/v1", True), ("http://127.0.0.1:8080", True), ("http://[::1]:1/v1", True),
    ("https://localhost.evil.example/v1", False), ("https://evil.example/?h=127.0.0.1", False),
    ("https://user@evil.example/localhost", False), ("", False), (None, False),
])
def test_loopback_is_the_parsed_host(url, local):
    assert learned_rules.is_loopback(url) is local


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


# -- entitlement: fails closed -------------------------------------------------------------


@pytest.mark.parametrize("has", [lambda f: False, lambda f: (_ for _ in ()).throw(RuntimeError("bad"))])
def test_without_the_entitlement_nothing_is_grouped_or_suggested(db, tmp_path, monkeypatch, has):
    root = str(tmp_path)
    monkeypatch.setattr(license, "has", has)
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


def test_refresh_later_starts_the_helper_when_entitled(db, tmp_path, monkeypatch, pro):
    spawned = []
    monkeypatch.setattr(learned_rules.subprocess, "Popen", lambda argv, **k: spawned.append(argv))
    learned_rules.refresh_later(str(tmp_path))
    assert spawned and spawned[0][-2:] == ["_learn-rules", str(tmp_path)]


# -- the supervisor and the CLI ------------------------------------------------------------


def test_supervisor_notes_name_the_suggestion(db, tmp_path, pro):
    root = str(tmp_path)
    s = suggest_one(db, root)
    [note] = learned_rules.notes(db, root)
    assert s.key in note and "brindle rules accept" in note and "Catch specific exceptions" in note


def test_get_progress_carries_the_suggestion(db, tmp_path, monkeypatch, pro):
    from types import SimpleNamespace

    from brindle import mcp_server

    s = suggest_one(db, str(tmp_path))
    monkeypatch.setattr(mcp_server, "_session", lambda db: ("root", SimpleNamespace(repo_root=str(tmp_path))))
    monkeypatch.setattr(mcp_server.autopilot, "progress", lambda db, root: "Autopilot on.")
    assert s.key in mcp_server.get_progress()


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


def test_the_learned_pack_is_added_once_and_only_when_there(tmp_path):
    root = str(tmp_path)
    assert load_rule_packs(load_profile("developer", root), root) == []
    path = learned_pack_path(root)
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: learned\ndescription: d\n---\n- Be careful.\n")
    named = replace(load_profile("developer", root), rules=["learned"])
    assert [p.name for p in load_rule_packs(named, root)] == ["learned"]
    assert [p.name for p in load_rule_packs(load_profile("qa", root), root)] == ["tests/only", "learned"]

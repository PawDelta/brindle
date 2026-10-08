"""Review escapes (approved, merged work that later proves broken) and the
``second_review`` config that asks for another reviewer on heavier work."""
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from brindle import agents, autopilot, config, escapes, gates, history, pipeline, workspaces

REAL_REQUEST_REVIEW = agents.request_review
from brindle.config import RepoConfig
from brindle.db import Agent, Task


def add(db, ws, agent_id, mode, profile="developer", parent=None, status="idle", **kw):
    db.add_agent(Agent(agent_id, ws.id, profile, "claude", parent, mode, status, "@0", None,
                       time.time(), **kw))


def escape_rows(db, repo):
    return db.list_history(str(repo), "escape")


# -- signal (a): a milestone that passed now fails after a merge ---------------


@pytest.fixture
def goal(db, repo):
    ws = workspaces.adopt_root(db, str(repo))
    add(db, ws, "boss", "interactive", "supervisor", status="processing")
    db.add_autopilot("boss")
    # A second milestone that never passes keeps the goal open between checks.
    autopilot.set_goal(db, "boss", "Settings", [("API", "test -f api.txt", None),
                                                 ("UI", "test -f never.txt", None)])
    return ws


def merge_branch(repo, name, remove=None):
    sh(f"git switch -qc {name}", repo)
    if remove:
        sh(f"git rm -q {remove}", repo)
    else:
        (repo / f"{name.replace('/', '-')}.txt").write_text("x")
        sh("git add -A", repo)
    sh(f"git commit -qm {name.replace('/', '-')}", repo)
    sh("git switch -q -", repo)
    sh(f"git merge -q --no-ff {name} -m \"Merge branch '{name}'\"", repo)


def test_a_check_that_passed_before_a_merge_and_fails_after_is_an_escape(db, repo, goal):
    (repo / "api.txt").write_text("")
    sh("git add -A && git commit -qm api", repo)
    off = RepoConfig(goal_audit=False)
    assert "1 of 2" in autopilot.check_milestones(db, "boss", goal, cfg=off)
    history.record(db, str(repo), "review", branch="feat/bad", profile="reviewer-codex")
    merge_branch(repo, "feat/fine")
    merge_branch(repo, "feat/bad", remove="api.txt")
    out = autopilot.check_milestones(db, "boss", goal, cfg=off)
    assert "0 of 2" in out
    rows = escape_rows(db, repo)
    # Both branches merged between the two commits; the reviewer is the one on record.
    assert {r.branch for r in rows} == {"feat/fine", "feat/bad"}
    bad = next(r for r in rows if r.branch == "feat/bad")
    assert bad.kind == "escape" and bad.profile == "reviewer-codex" and bad.task == "milestone"
    assert "API" in bad.result and "passed at" in bad.result


def test_the_same_regression_is_recorded_once(db, repo, goal):
    (repo / "api.txt").write_text("")
    sh("git add -A && git commit -qm api", repo)
    off = RepoConfig(goal_audit=False)
    autopilot.check_milestones(db, "boss", goal, cfg=off)
    merge_branch(repo, "feat/bad", remove="api.txt")
    autopilot.check_milestones(db, "boss", goal, cfg=off)
    autopilot.check_milestones(db, "boss", goal, cfg=off)   # still failing: nothing new
    assert len(escape_rows(db, repo)) == 1


def test_a_milestone_that_never_passed_is_not_an_escape(db, repo, goal):
    merge_branch(repo, "feat/x")
    autopilot.check_milestones(db, "boss", goal, cfg=RepoConfig(goal_audit=False))
    assert escape_rows(db, repo) == []


def test_a_failure_with_no_merge_in_between_is_not_an_escape(db, repo, goal):
    (repo / "api.txt").write_text("")
    sh("git add -A && git commit -qm api", repo)
    off = RepoConfig(goal_audit=False)
    autopilot.check_milestones(db, "boss", goal, cfg=off)
    sh("git rm -q api.txt && git commit -qm drop", repo)   # a plain commit, not a merged branch
    autopilot.check_milestones(db, "boss", goal, cfg=off)
    assert escape_rows(db, repo) == []


def test_escape_without_a_review_row_has_no_profile(db, repo, goal):
    (repo / "api.txt").write_text("")
    sh("git add -A && git commit -qm api", repo)
    off = RepoConfig(goal_audit=False)
    autopilot.check_milestones(db, "boss", goal, cfg=off)
    merge_branch(repo, "feat/bad", remove="api.txt")
    autopilot.check_milestones(db, "boss", goal, cfg=off)
    assert [(r.branch, r.profile) for r in escape_rows(db, repo)] == [("feat/bad", None)]


# -- signal (b): a completion audit with gaps names a merged branch ------------


def test_audit_gaps_naming_a_merged_branch_are_an_escape(db, repo, goal):
    history.record(db, str(repo), "merge", branch="feat/ui", task="merge feat/ui into main")
    history.record(db, str(repo), "merge", branch="feat/api", task="merge feat/api into main")
    history.record(db, str(repo), "review", branch="feat/ui", profile="reviewer")
    out = autopilot.record_audit(db, "boss", False, "The form added in feat/ui never submits.")
    assert "GAPS FOUND" in out
    rows = escape_rows(db, repo)
    assert [(r.branch, r.profile, r.task) for r in rows] == [("feat/ui", "reviewer", "audit")]


def test_audit_gaps_naming_no_merged_branch_record_nothing(db, repo, goal):
    history.record(db, str(repo), "merge", branch="feat/ui", task="merge feat/ui into main")
    autopilot.record_audit(db, "boss", False, "The settings page is missing entirely.")
    autopilot.record_audit(db, "boss", False, "feat/ui-extra is not merged and unrelated.")
    assert escape_rows(db, repo) == []


def test_an_approved_audit_is_not_an_escape(db, repo, goal):
    history.record(db, str(repo), "merge", branch="feat/ui", task="merge feat/ui into main")
    db.update_autopilot("boss", audit_sha=sh("git rev-parse HEAD", repo))
    autopilot.record_audit(db, "boss", True, "feat/ui is exactly what was asked for.")
    assert escape_rows(db, repo) == []


def test_recording_an_escape_never_raises(db, repo, monkeypatch):
    monkeypatch.setattr(history, "record", lambda *a, **k: 1 / 0)
    assert escapes.record(db, str(repo), "feat/x", "audit", "boom") is False


# -- second_review -------------------------------------------------------------


def test_second_review_config_loads_per_weight(repo):
    (repo / ".brindle").mkdir()
    (repo / ".brindle" / "config.json").write_text(json.dumps(
        {"second_review": {"heavy": "reviewer-codex", "huge": "x", "light": 3}}))
    assert config.load_repo_config(repo).second_review == {"heavy": "reviewer-codex"}
    (repo / ".brindle" / "config.json").write_text("{}")
    assert config.load_repo_config(repo).second_review == {}


@pytest.fixture
def piped(db, repo, monkeypatch):
    def setup(second=None, weight="heavy"):
        cfg = {"review": True, "auto_merge_default_branch": True}
        if second is not None:
            cfg["second_review"] = second
        (repo / ".brindle").mkdir(exist_ok=True)
        (repo / ".brindle" / "config.json").write_text(json.dumps(cfg))
        root = workspaces.adopt_root(db, str(repo))
        add(db, root, "boss", "interactive", "supervisor", status="processing")
        ws = workspaces.create(db, str(repo), "feat").workspace
        (Path(ws.path) / "new.py").write_text("x = 1\n")
        sh("git add new.py && git commit -qm work", Path(ws.path))
        add(db, ws, "w1", "assign", parent="boss", status="processing")
        db.add_task(Task("t1", str(repo), "w1", "boss", root.id, "developer", "do it", "assign", 1,
                         "feat", None, None, None, "started", time.time(), weight=weight))
        return ws

    started = []

    def fake_review(db_, caller, ws_, profile=None, focus=None, cfg=None):
        agent_id = f"rev{len(started)}"
        add(db_, ws_, agent_id, "review", profile or "reviewer", parent=caller.id)
        started.append(profile)
        return db_.get_agent(agent_id)

    monkeypatch.setattr(agents, "request_review", fake_review)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    monkeypatch.setattr(agents, "warm_checks", lambda ws_: None)
    monkeypatch.setattr(agents, "close_later", lambda agent_id, delay=5.0: None)
    monkeypatch.setattr(agents, "_detach", lambda argv: None)
    monkeypatch.setattr(agents, "_stop", lambda db_, a: None)
    return setup, started


def merged(repo):
    return "new.py" in sh("git ls-tree --name-only HEAD", repo)


def test_unset_second_review_is_one_review(db, repo, piped):
    setup, started = piped
    ws = setup()
    agents.report_result(db, "w1", "added new.py")
    assert started == [None]
    agents.submit_review(db, "rev0", True, "lgtm")
    assert merged(repo) and db.get_workspace(ws.id) is None


def test_a_weight_without_a_second_reviewer_gets_one_review(db, repo, piped):
    setup, started = piped
    setup(second={"heavy": "reviewer-codex"}, weight="light")
    agents.report_result(db, "w1", "added new.py")
    assert started == [None]
    agents.submit_review(db, "rev0", True, "lgtm")
    assert merged(repo)


def test_second_review_needs_both_approvals_before_it_merges(db, repo, piped):
    setup, started = piped
    ws = setup(second={"heavy": "reviewer-codex"})
    agents.report_result(db, "w1", "added new.py")
    assert started == ["reviewer", "reviewer-codex"]   # both carry a concrete profile
    agents.submit_review(db, "rev0", True, "lgtm")
    assert not merged(repo) and db.get_agent("w1").pipeline == "reviewing"
    agents.submit_review(db, "rev1", True, "also lgtm")
    assert merged(repo) and db.get_workspace(ws.id) is None


def test_the_second_reviewer_approving_first_still_waits_for_the_other(db, repo, piped):
    setup, started = piped
    setup(second={"heavy": "reviewer-codex"})
    agents.report_result(db, "w1", "added new.py")
    agents.submit_review(db, "rev1", True, "codex approves")
    assert not merged(repo)
    agents.submit_review(db, "rev0", True, "reviewer approves")
    assert merged(repo)


def test_either_reviewer_asking_for_changes_blocks_the_merge(db, repo, piped):
    setup, started = piped
    ws = setup(second={"heavy": "reviewer-codex"})
    agents.report_result(db, "w1", "added new.py")
    agents.submit_review(db, "rev1", False, "missing a test")
    assert db.get_agent("w1").pipeline == "fixing"
    agents.submit_review(db, "rev0", True, "lgtm")   # a late approval can't outvote it
    assert not merged(repo) and db.get_agent("w1").pipeline == "fixing"
    assert db.latest_review(ws.id, gates.head(ws)) is not None


def test_a_second_review_by_the_first_reviewers_own_profile_changes_nothing(db, repo, piped):
    setup, started = piped
    setup(second={"heavy": "reviewer"})   # the default reviewer: nothing to add
    agents.report_result(db, "w1", "added new.py")
    assert started == [None]
    agents.submit_review(db, "rev0", True, "lgtm")
    assert merged(repo)
    assert pipeline.second_profile(db, RepoConfig(), db.get_agent("boss"), None) is None


def test_live_reviewer_dedupe_compares_profiles(db, repo, piped):
    setup, _ = piped
    ws = setup()
    sha = gates.head(ws)
    add(db, ws, "r-a", "review", "reviewer", parent="boss", review_sha=sha)
    assert agents._live_reviewer(db, ws, sha, "reviewer").id == "r-a"
    assert agents._live_reviewer(db, ws, sha, "reviewer-codex") is None
    add(db, ws, "r-b", "review", "reviewer-codex", parent="boss", review_sha=sha)
    assert agents._live_reviewer(db, ws, sha, "reviewer-codex").id == "r-b"


@pytest.mark.parametrize("checks", [False, True])
def test_both_reviewers_start_even_with_checks(db, repo, piped, monkeypatch, checks):
    setup, _ = piped
    ws = setup(second={"heavy": "reviewer-codex"})
    if checks:
        cfg = json.loads((repo / ".brindle" / "config.json").read_text())
        (repo / ".brindle" / "config.json").write_text(json.dumps({**cfg, "checks": ["true"]}))
    # The real request_review, not the fixture's fake. Never monkeypatch.undo():
    # it would also drop BRINDLE_HOME and run against the real ~/.brindle.
    monkeypatch.setattr(agents, "request_review", REAL_REQUEST_REVIEW)
    spawned = []

    def fake_spawn(db_, ws_, profile, **kw):
        add(db_, ws_, f"s{len(spawned)}", "review", profile, parent=kw.get("parent_id"),
            review_sha=kw.get("review_sha"))
        spawned.append(profile)
        return db_.get_agent(f"s{len(spawned) - 1}")

    detached = []
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "_detach", detached.append)
    monkeypatch.setattr(agents, "_review_profile", lambda db_, ws_, profile, cfg: profile or "reviewer")
    monkeypatch.setattr(agents, "_review_kill_switch", lambda ws_: None)
    cfg = config.load_repo_config(repo)
    parent = db.get_agent("boss")
    agents.start_review(db, parent, ws, "reviewer", None, cfg)
    agents.start_review(db, parent, ws, "reviewer-codex", None, cfg)
    if checks:
        assert len(detached) == 2 and spawned == []   # neither swallowed by the other's process
        # Whichever detached process gets there first, the other still isn't deduped away.
        agents.review_after_checks(db, "boss", ws, "reviewer-codex", cfg=cfg)
        agents.review_after_checks(db, "boss", ws, "reviewer", cfg=cfg)
    assert sorted(spawned) == ["reviewer", "reviewer-codex"]


# -- review-depth suggestions --------------------------------------------------


@pytest.fixture
def pro(monkeypatch):
    from brindle.pro import license

    monkeypatch.setattr(license, "has", lambda f: f == "cost")


def seed(db, repo, weight, n, reviewer="reviewer", escaped=0, tag=None):
    """``n`` merged branches of ``weight`` reviewed by ``reviewer``, ``escaped`` of them escaped."""
    tag = tag or f"{weight}-{reviewer}"
    for i in range(n):
        branch, agent = f"feat/{tag}-{i}", f"{tag}-{i}"
        db.add_routing_decision(str(repo), task_id=f"t-{agent}", agent_id=agent, weight=weight,
                                baseline_profile="developer", profile="developer", learned=False)
        db.add_history(str(repo), "worker_result", agent_id=agent, branch=branch, profile="developer")
        history.record(db, str(repo), "review", branch=branch, profile=reviewer)
        history.record(db, str(repo), "merge", branch=branch, task=f"merge {branch} into main")
        if i < escaped:
            escapes.record(db, str(repo), branch, "audit", "gaps")


def suggestions(db, repo, second=None, **kw):
    return escapes.review_depth_suggestions(db, str(repo), second or {}, **kw)


def test_a_high_escape_rate_suggests_a_second_review_for_that_weight(db, repo, pro):
    seed(db, repo, "heavy", 10, escaped=1)   # exactly 10% over exactly 10 merges
    seed(db, repo, "light", 10, escaped=0)
    assert suggestions(db, repo) == [
        'add a second review for heavy (reviewer: 1 of 10 merges escaped, 10%): '
        '"second_review": {"heavy": "reviewer-codex"}']


def test_the_second_reviewer_suggested_is_a_different_profile(db, repo, pro):
    seed(db, repo, "medium", 10, reviewer="reviewer-codex", escaped=3)
    [line] = suggestions(db, repo)
    assert '"second_review": {"medium": "reviewer"}' in line


def test_add_thresholds_are_ten_percent_over_ten_merges(db, repo, pro):
    seed(db, repo, "heavy", 9, escaped=9)      # too few merges
    seed(db, repo, "medium", 10, escaped=0)    # no escapes
    seed(db, repo, "light", 20, escaped=1)     # 5%: under the rate
    assert suggestions(db, repo) == []


def test_the_rate_is_per_reviewer_profile(db, repo, pro):
    seed(db, repo, "heavy", 10, reviewer="reviewer", escaped=0)
    seed(db, repo, "heavy", 10, reviewer="reviewer-codex", escaped=1)
    [line] = suggestions(db, repo)
    assert "reviewer-codex: 1 of 10" in line and '{"heavy": "reviewer"}' in line


def test_a_weight_with_a_second_review_is_not_asked_for_another(db, repo, pro):
    seed(db, repo, "heavy", 10, escaped=5)
    assert suggestions(db, repo, {"heavy": "reviewer-codex"}) == []


def test_no_escapes_over_twenty_merges_suggests_dropping_the_second_review(db, repo, pro):
    seed(db, repo, "heavy", 20)
    assert suggestions(db, repo, {"heavy": "reviewer-codex"}) == [
        'drop the second review for heavy (20 merges, no escapes in 90 days): '
        'remove "heavy" from "second_review"']


def test_drop_thresholds_are_zero_escapes_over_twenty_merges_with_one_set(db, repo, pro):
    seed(db, repo, "heavy", 19)                # too few merges
    seed(db, repo, "medium", 25, escaped=1)    # an escape
    seed(db, repo, "light", 30)                # no second review set for light
    assert suggestions(db, repo, {"heavy": "reviewer-codex", "medium": "reviewer-codex"}) == []


def test_only_the_last_ninety_days_count(db, repo, pro):
    seed(db, repo, "heavy", 10, escaped=2)
    assert len(suggestions(db, repo)) == 1
    assert suggestions(db, repo, now=time.time() + 91 * 86400) == []


def test_no_entitlement_means_no_suggestions(db, repo, monkeypatch):
    from brindle.pro import license

    monkeypatch.setattr(license, "has", lambda f: False)
    seed(db, repo, "heavy", 10, escaped=5)
    seed(db, repo, "light", 20)
    assert suggestions(db, repo, {"light": "reviewer"}) == []
    monkeypatch.setattr(license, "has", lambda f: 1 / 0)   # an unreadable license fails closed too
    assert suggestions(db, repo) == []


def test_cost_report_shows_the_suggestions_and_leaves_config_alone(db, repo, pro):
    from brindle import cost

    seed(db, repo, "heavy", 10, escaped=2)
    text = cost.describe_report(cost.report(db, str(repo)), str(repo))
    assert 'Review depth suggestions' in text
    assert '"second_review": {"heavy": "reviewer-codex"}' in text
    assert not (repo / ".brindle" / "config.json").exists()


def test_cost_report_without_the_entitlement_has_no_suggestions(db, repo, monkeypatch):
    from brindle import cost
    from brindle.pro import license

    monkeypatch.setattr(license, "has", lambda f: False)
    seed(db, repo, "heavy", 10, escaped=2)
    assert "Review depth" not in cost.describe_report(cost.report(db, str(repo)), str(repo))

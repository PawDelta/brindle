"""Opt-in review cost controls through real dispatch and commit-bound packs."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle import agents, bus, codemap, gates, git, pipeline, review_scope, workspaces
from brindle.config import RepoConfig, load_repo_config
from brindle.db import Agent
from brindle.profiles import load_profile
from test_merge_order import add
from test_pipeline_idempotent import drain
from test_reviewer_dedupe import fake_spawn
from test_routing import config


@pytest.mark.parametrize("stop,new_commit", [
    (True, True),
    pytest.param(False, True, marks=pytest.mark.variant(
        "opting out preserves fix dispatch even when findings repeat past the cap")),
    (True, False),
    pytest.param("late", True, marks=pytest.mark.variant(
        "enabling the guard after the continue-fixing notice still sends a separate stall notice")),
])
def test_review_stall_stops_only_repeated_findings_past_cap(db, repo, monkeypatch, stop, new_commit):
    config(repo, review_stall_stop=stop is True, review_rounds=2)
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "stall").workspace
    add(db, ws, "writer", "assign", parent="boss", result="done", status="done")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    monkeypatch.setattr(agents, "reconcile", lambda db_, a, **kw: a)
    launched = []
    notices = []
    monkeypatch.setattr(agents, "_launch", lambda db_, a, ws_, **kw: launched.append(a.id))
    first = "- blocker: src/auth.py accepts missing credentials."
    changed = "- blocker: src/auth.py accepts expired credentials."
    for round_, summary in enumerate((first, first, changed, changed), 1):
        if stop == "late" and round_ == 4:
            config(repo, review_stall_stop=True, review_rounds=2)
        sha = f"commit-{round_}" if new_commit else "unchanged-commit"
        monkeypatch.setattr(gates, "head", lambda ws_: sha)
        db.update_agent("writer", pipeline="reviewing")
        rid = f"reviewer-{round_}"
        add(db, ws, rid, "review", "reviewer", parent="boss", review_sha=sha)
        db.update_agent(rid, review_slot="")
        db.add_review(ws.id, sha, rid, False, summary)
        reviewer = db.get_agent(rid)
        assert pipeline.on_review(db, reviewer, ws, False, summary)
        messages = drain(db)
        notices.extend(messages)
        if stop and round_ == 4:
            assert db.get_agent("writer").pipeline is None
            assert len(launched) == 3
            assert len(messages) == 1
            assert len(notices) == (2 if stop == "late" else 1)
            assert not pipeline.on_review(db, reviewer, ws, False, summary)
            assert drain(db) == []
            assert len(bus.tail(db, kinds=("pipeline.escalated",))) == 1
        else:
            assert db.get_agent("writer").pipeline == "fixing"
            assert len(launched) == round_
            if not stop:
                assert len(messages) == (1 if round_ == 3 else 0)


@pytest.mark.parametrize("changed_slot,new_commit", [
    (None, True),
    pytest.param("", True, marks=pytest.mark.variant("progress in the first slot must prevent a stall")),
    pytest.param("reviewer-codex", True, marks=pytest.mark.variant(
        "progress arriving in the second verdict must prevent a premature stall")),
    pytest.param("approved", True, marks=pytest.mark.variant(
        "an approval delivered last must still dispatch the other slot's rejection")),
    pytest.param(None, False, marks=pytest.mark.variant(
        "two slots reviewing the same commit again must compare complete rounds")),
])
def test_review_stall_waits_for_both_slots(db, repo, monkeypatch, changed_slot, new_commit):
    cfg = RepoConfig(review_stall_stop=True, review_rounds=2,
                     second_review={"": "reviewer-codex"}, review_profile="reviewer", learning="off")
    monkeypatch.setattr(pipeline, "load_repo_config", lambda *a, **kw: cfg)
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "two-slots").workspace
    add(db, ws, "writer", "assign", parent="boss", result="done", pipeline="reviewing")
    db.update_agent("writer", pipeline_rounds=2)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **kw: db_.enqueue(to, body, None))
    slots = ("", "reviewer-codex")
    findings = {"": "src/auth.py: missing credentials accepted",
                "reviewer-codex": "src/cache.py: stale credentials accepted"}
    for round_ in range(3):
        sha = f"commit-{round_}" if new_commit else "unchanged-commit"
        monkeypatch.setattr(gates, "head", lambda ws_: sha)
        db.update_agent("writer", pipeline="reviewing")
        reviewers = []
        for index, slot in enumerate(slots):
            rid = f"reviewer-{round_}-{index}"
            add(db, ws, rid, "review", "reviewer", parent="boss", review_sha=sha)
            db.update_agent(rid, review_slot=slot)
            reviewers.append(db.get_agent(rid))
        if round_ == 1 and changed_slot in slots:
            findings[changed_slot] += " after refresh"
        for index, reviewer in enumerate(reviewers):
            summary = findings[slots[index]]
            approved = changed_slot == "approved" and round_ > 0 and index == 1
            db.add_review(ws.id, sha, reviewer.id, approved, summary)
            if round_ == 0:
                continue  # the complete preceding round
            assert pipeline.on_review(db, reviewer, ws, approved, summary)
            if index == 0:
                assert db.get_agent("writer").pipeline == "reviewing"
                assert drain(db, "writer") == []
        if round_ == 0:
            continue
        if round_ == 1 and changed_slot is not None:
            assert db.get_agent("writer").pipeline == "fixing"
            messages = drain(db, "writer")
            open_findings = [f for slot, f in findings.items()
                             if changed_slot != "approved" or slot == ""]
            assert len(messages) == 1 and all(f in messages[0] for f in open_findings)
            assert drain(db) == []
        else:
            assert db.get_agent("writer").pipeline is None
            assert drain(db, "writer") == []
            assert len(drain(db)) == 1
            assert not pipeline.on_review(db, reviewers[-1], ws, approved, summary)
            assert drain(db) == []
            break


@pytest.mark.parametrize("terminal", [False, pytest.param(True, marks=pytest.mark.variant(
    "the replacement's death must escalate once instead of waiting forever on the unfinished slot"))])
def test_review_stall_recovers_unfinished_slot(db, repo, monkeypatch, terminal):
    from brindle import launch_health

    cfg = RepoConfig(review_stall_stop=True, review_rounds=2,
                     second_review={"": "reviewer-codex"}, review_profile="reviewer", learning="off")
    monkeypatch.setattr(pipeline, "load_repo_config", lambda *a, **kw: cfg)
    root = workspaces.adopt_root(db, str(repo))
    add(db, root, "boss", "interactive", "supervisor", status="processing")
    ws = workspaces.create(db, str(repo), "unfinished-slot").workspace
    sha = gates.head(ws)
    add(db, ws, "writer", "assign", parent="boss", result="done", pipeline="reviewing")
    db.update_agent("writer", pipeline_rounds=2)
    for rid, slot in (("first", ""), ("dead", "reviewer-codex")):
        add(db, ws, rid, "review", slot or "reviewer", parent="boss", review_sha=sha)
        db.update_agent(rid, review_slot=slot)
    dead = {"dead"}
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id not in dead)
    monkeypatch.setattr(agents, "unusable", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "send_message", lambda db_, to, body, **kw: db_.enqueue(to, body, None))
    monkeypatch.setattr(launch_health, "last_error", lambda a: None)
    started = fake_spawn(monkeypatch)
    first = "src/auth.py: missing credentials accepted"
    db.add_review(ws.id, sha, "first", False, first)
    assert pipeline.on_review(db, db.get_agent("first"), ws, False, first)
    assert db.get_agent("writer").pipeline == "reviewing"
    assert drain(db, "writer") == []
    observed = db.get_agent("dead")
    assert pipeline.on_reviewer_dead(db, observed, ws)
    assert len(started) == 1
    replacement = db.get_agent(started[0].id)
    assert pipeline.on_reviewer_dead(db, observed, ws)
    assert len(started) == 1 and drain(db) == []
    if terminal:
        dead.add(replacement.id)
        assert pipeline.on_reviewer_dead(db, replacement, ws)
        assert db.get_agent("writer").pipeline is None
        assert len(drain(db)) == 1
        assert pipeline.on_reviewer_dead(db, replacement, ws)
        assert drain(db) == [] and drain(db, "writer") == []
    else:
        second = "src/cache.py: stale credentials accepted"
        db.add_review(ws.id, sha, replacement.id, False, second)
        assert pipeline.on_review(db, replacement, ws, False, second)
        assert db.get_agent("writer").pipeline == "fixing"
        messages = drain(db, "writer")
        assert len(messages) == 1 and first in messages[0] and second in messages[0]
        assert drain(db) == []


def commit(repo, path, text):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git.run(["add", "--", path], repo)
    git.run(["commit", "-qm", "change"], repo)
    return git.out(["rev-parse", "HEAD"], repo)


@pytest.mark.parametrize("writer_provider,light_profile", [
    ("codex", "reviewer-light"),
    ("claude", "reviewer-codex-light"),
    ("native", "reviewer-codex"),
])
def test_review_dispatch_scopes_docs_then_keeps_risky_rereview_strong(
        db, repo, monkeypatch, writer_provider, light_profile):
    config(repo, review_sizing=True, reviewer="reviewer-codex", learning="off")
    ws = workspaces.create(db, str(repo), "review-scope").workspace
    writer_profile = "developer-codex" if writer_provider == "codex" else "developer"
    db.add_agent(Agent("writer", ws.id, writer_profile, writer_provider, None,
                       "assign", "done", "@0", "done", 0))
    started = fake_spawn(monkeypatch)
    monkeypatch.setattr(agents, "alive_or_starting", lambda a: True)
    monkeypatch.setattr(agents, "unusable", lambda *a: None)
    first = commit(Path(ws.path), "docs/usage.md", "A scoped review.\n")
    reviewer = agents.start_review(db, None, ws, cfg=load_repo_config(repo))
    assert reviewer.profile == light_profile
    if writer_provider == "claude":
        assert load_profile(reviewer.profile, ws.repo_root).provider != writer_provider
    pack = reviewer.task.split("Scoped review context pack:")[1]
    assert pack.split("Touched files:\n")[1].split("\n\nDiff")[0] == "- docs/usage.md"
    assert "+A scoped review." in pack
    assert agents.start_review(db, None, ws, cfg=load_repo_config(repo)).id == reviewer.id
    assert len(started) == 1
    db.add_review(ws.id, first, reviewer.id, False, "Check the next change.")
    db.set_result(reviewer.id, "reviewed")
    second = commit(Path(ws.path), "src/auth.py", "def authorize():\n    return False\n")
    # A preflight pick must not pin the cheap profile after the branch grows risky.
    reviewer = agents.request_review(db, None, ws, light_profile, cfg=load_repo_config(repo), slot="")
    assert reviewer.profile == "reviewer-codex"
    pack = reviewer.task.split("Scoped review context pack:")[1]
    assert "src/auth.py" in pack and "docs/usage.md" not in pack
    assert first in pack
    db.add_review(ws.id, second, reviewer.id, False, "Clarify the docs.")
    db.set_result(reviewer.id, "reviewed")
    commit(Path(ws.path), "docs/usage.md", "A scoped review with auth.\n")
    reviewer = agents.request_review(db, None, ws, cfg=load_repo_config(repo))
    assert reviewer.profile == "reviewer-codex"  # earlier risky code still sizes the branch
    pack = reviewer.task.split("Scoped review context pack:")[1]
    assert "src/auth.py" not in pack and "docs/usage.md" in pack


@pytest.mark.parametrize("enabled,path,stats,options,expected", [
    pytest.param(True, "src/a.py", "2\t1", {}, "reviewer-light",
                 marks=pytest.mark.variant("small production changes qualify independently of docs-only branches")),
    pytest.param(True, "src/a.py", "2\t1", {"review_small_max_lines": 2}, "reviewer",
                 marks=pytest.mark.variant("line-count ceiling prevents downsizing code above the configured limit")),
    pytest.param(True, "src/a.py", "2\t1", {"review_small_max_files": 0}, "reviewer",
                 marks=pytest.mark.variant("file-count threshold must apply independently of changed-line count")),
    pytest.param(True, "tests/test_a.py", "900\t0", {}, "reviewer-light",
                 marks=pytest.mark.variant("test-only branches qualify beyond the small-code threshold too")),
    pytest.param(True, "src/sensitive.py", "1\t0", {"review_risky_paths": ["src/sensitive.py"]}, "reviewer",
                 marks=pytest.mark.variant("custom risky globs must override size eligibility just like built-in auth paths")),
    (True, "image.png", "-\t-", {}, "reviewer"),
])
def test_review_size_selection(db, tmp_path, monkeypatch, enabled, path, stats, options, expected):
    ws = SimpleNamespace(id="ws", path=str(tmp_path), repo_root=str(tmp_path), base_branch="main")
    monkeypatch.setattr(git, "out", lambda *a: f"{stats}\t{path}\0")
    cfg = RepoConfig(review_sizing=enabled, learning="off", **options)
    assert agents._review_choice(db, ws, None, cfg)[0] == expected


@pytest.mark.variant("opting out must preserve dispatch and prompts; explicit profiles bypass automatic sizing")
def test_disabled_review_preserves_prompt_and_explicit_profile(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "unchanged").workspace
    fake_spawn(monkeypatch)
    monkeypatch.setattr(agents, "unusable", lambda *a: None)
    with monkeypatch.context() as disabled:
        disabled.setattr(review_scope, "small_branch", lambda *a: pytest.fail("sizing was not requested"))
        disabled.setattr(review_scope, "context_pack", lambda *a: pytest.fail("pack was not requested"))
        reviewer = agents.request_review(db, None, ws, cfg=RepoConfig(learning="off"))
        assert reviewer.profile == "reviewer"
        assert "Scoped review context pack:" not in reviewer.task
        # Explicit selection also bypasses automatic sizing when the feature is on.
        assert agents._review_choice(db, ws, "reviewer", RepoConfig(review_sizing=True))[0] == "reviewer"
    db.set_result(reviewer.id, "reviewed")
    commit(Path(ws.path), "docs/usage.md", "A small docs change.\n")
    configured = agents.request_review(db, None, ws, cfg=RepoConfig(
        review_sizing=True, review_profile="reviewer-codex", learning="off"))
    assert configured.profile == "reviewer-codex"


@pytest.mark.parametrize("missing", [
    "current",
    pytest.param("cheap", marks=pytest.mark.variant(
        "a missing cheap-profile parent must preserve the valid original reviewer")),
])
def test_unresolved_sizing_profile_preserves_candidate_fallback(db, repo, monkeypatch, missing):
    ws = workspaces.create(db, str(repo), "missing-profile").workspace
    db.add_agent(Agent("writer", ws.id, "developer-codex", "codex", None,
                       "assign", "done", "@0", "done", 0))
    monkeypatch.setattr(git, "out", lambda *a: "1\t0\tdocs/a.md\0")
    monkeypatch.setattr(agents, "unusable", lambda *a: None)
    if missing == "cheap":
        profile = repo / ".brindle" / "agents" / "reviewer-light.md"
        profile.parent.mkdir(parents=True, exist_ok=True)
        profile.write_text("---\nextends: missing-reviewer-profile\n---\n")
    cfg = RepoConfig(review_sizing=True, learning="off", reviewer_fallbacks=["reviewer"],
                     reviewer="missing-reviewer-profile" if missing == "current" else "reviewer-codex")
    chosen, reason = agents._review_choice(db, ws, None, cfg)
    assert chosen == ("reviewer" if missing == "current" else "reviewer-codex")
    assert (reason is not None) == (missing == "current")


@pytest.mark.parametrize("graph", [
    pytest.param(False, marks=pytest.mark.variant("without a graph, grep must still find callers of a deleted function")),
    True,
])
def test_pack_includes_callers_of_changed_and_deleted_functions(repo, monkeypatch, graph):
    commit(repo, "code.py", "def changed():\n    return 1\n\ndef removed():\n    return 2\n")
    base = commit(repo, "caller.py", "from code import changed, removed\nchanged()\nremoved()\n")
    head = commit(repo, "code.py", "def changed():\n    return 3\n")
    ws = SimpleNamespace(path=str(repo))
    monkeypatch.setattr(codemap, "graph_path", lambda ws: repo / "graph.json" if graph else None)
    if graph:
        read = review_scope.spawn.read

        def graph_read(argv, **kwargs):
            if argv[0] != "graphify":
                return read(argv, **kwargs)
            # Supply an index entry only for the relevant changed symbols.
            found = all(name in argv[2] for name in ("changed", "removed"))
            return SimpleNamespace(returncode=0, stdout="indirect_caller.py:L7" if found else "")

        monkeypatch.setattr(review_scope.spawn, "read", graph_read)
    else:
        # Exercise the platform's Git, including Apple's POSIX regex engine.
        monkeypatch.setenv("PATH", os.defpath + os.pathsep + os.environ["PATH"])
    pack = review_scope.context_pack(ws, base, head)
    assert "+    return 3" in pack
    if graph:
        assert "indirect_caller.py:L7" in pack
    else:
        assert "caller.py" in pack
        assert "removed()" in pack.split("Grep call-site candidates")[1]


@pytest.mark.parametrize("model", ["haiku", "sonnet"])
def test_sizing_keeps_strong_reviewer_when_light_is_unsafe_or_unavailable(db, tmp_path, monkeypatch, model):
    ws = SimpleNamespace(id="ws", path=str(tmp_path), repo_root=str(tmp_path), base_branch="main", branch="feature")
    monkeypatch.setattr(git, "out", lambda *a: "1\t0\tdocs/a.md\0")
    light = load_profile("reviewer-light", str(tmp_path))
    light.model = model
    monkeypatch.setattr(agents, "load_profile", lambda name, repo: light if name == "reviewer-light"
                        else load_profile(name, repo))
    monkeypatch.setattr(agents, "unusable", lambda provider, *a:
                        "unavailable" if model == "sonnet" and provider == "claude" else None)
    cfg = RepoConfig(review_sizing=True, learning="off", reviewer="reviewer-codex")
    assert agents._review_choice(db, ws, None, cfg)[0] == "reviewer-codex"

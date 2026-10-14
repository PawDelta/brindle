import asyncio
import time

from typer.testing import CliRunner

from brindle import cli, agents, autopilot, cull, history, mcp_server, sessions, workspaces
from brindle.cli import app
from brindle.db import Agent, Workspace
from brindle.usage import Usage


def add(db, ws, aid, mode="interactive", parent=None, status="processing", since=None):
    a = Agent(aid, ws.id, "supervisor" if mode == "interactive" else "developer", "claude", parent,
              mode, status, "", None, time.time(), since or time.time())
    db.add_agent(a)
    return a


# -- rows written at each of the four call sites ------------------------------


def test_check_milestone_writes_check_and_milestone_rows(db, repo, monkeypatch):
    root_ws = workspaces.adopt_root(db, str(repo))
    add(db, root_ws, "boss")
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Goal", [("M1", "test -f done.txt", None)])
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    (repo / "done.txt").write_text("x")

    # The tool starts the check in the background; the helper it starts
    # runs it and delivers the result to the supervisor's inbox.
    started = []
    monkeypatch.setattr(autopilot, "_detach", started.append)
    out = asyncio.run(mcp_server.check_milestone())
    assert "background" in out and started and "_check-milestones" in started[0]
    cli.check_milestones_cmd("boss", root_ws.id, position=None)
    assert db.pending_count("boss") == 1 and db.get_autopilot("boss").checking_since is None

    rows = db.list_history(root_ws.repo_root)
    kinds = {r.kind for r in rows}
    assert kinds == {"check", "milestone"}
    milestone_row = next(r for r in rows if r.kind == "milestone")
    assert milestone_row.task == "M1"
    assert "passed" in milestone_row.result

    # Re-running with nothing changed shouldn't add another milestone row.
    cli.check_milestones_cmd("boss", root_ws.id, position=None)
    rows = db.list_history(root_ws.repo_root)
    assert sum(r.kind == "milestone" for r in rows) == 1
    assert sum(r.kind == "check" for r in rows) == 2


# -- survives pruning -----------------------------------------------------------


def test_history_survives_session_pruning(db, repo, tmp_path):
    import gzip
    import json

    root_ws = workspaces.adopt_root(db, str(repo))
    ws = workspaces.create(db, str(repo), "feature").workspace
    add(db, root_ws, "boss", status="paused", since=1)
    add(db, ws, "w1", mode="assign", parent="boss", status="done")
    transcript, sub = tmp_path / "s1.jsonl", tmp_path / "s1" / "subagents" / "agent-a.jsonl"
    sub.parent.mkdir(parents=True)
    transcript.write_text(_usage_line("m1", 1000))
    sub.write_text(_usage_line("m2", 50))
    db.update_agent("w1", result="all done", task="build it", inbox_token="secret-token",
                    transcript_path=str(transcript))
    history.record(db, ws.repo_root, "worker_result", agent_id="w1", branch="feature",
                   profile="developer", task="task", result="done")
    for i in range(3):
        add(db, root_ws, f"new{i}", status="paused", since=100 + i)  # push "boss" past KEEP

    dropped = sessions.enforce(db, root_ws.repo_root, now=200)

    assert dropped == 1
    assert db.get_agent("boss") is None and db.get_agent("w1") is None
    rows = db.list_history(ws.repo_root, "worker_result")
    assert len(rows) == 1 and rows[0].result == "done"
    # The dropped records are archived, locally and privately, first.
    archived = {r.agent_id: r for r in db.list_history(ws.repo_root, "archived")}
    assert (archived["w1"].task, archived["w1"].result) == ("build it", "all done")
    assert "boss" in archived
    path = history.archive_dir() / "proj.jsonl"
    assert path.stat().st_mode & 0o777 == 0o600
    assert history.archive_dir().stat().st_mode & 0o777 == 0o700
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["workspace"]["id"] == ws.id
    w1 = entry["agents"][0]
    assert w1["result"] == "all done" and "inbox_token" not in w1
    # Its conversation and costs outlive the CLI's files and history's cap.
    transcript.unlink()
    sub.unlink()
    assert w1["usage"]["output"] == 1050
    copies = [gzip.open(history.archive_dir() / p, "rt").read() for p in w1["transcripts"]]
    assert copies == [_usage_line("m1", 1000), _usage_line("m2", 50)]


def _row(db, root, path, kind="worktree"):
    ws = Workspace("proj/feat", str(root), "feat", kind, "feat", "main", str(path), None,
                   "brindle_proj_feat", 1.0)
    db.add_workspace(ws)
    return ws


def test_records_stay_when_they_cant_be_archived(db, repo, tmp_path):
    history.archive_dir().parent.mkdir(parents=True, exist_ok=True)
    history.archive_dir().write_text("a file in the way")
    ws = _row(db, repo, tmp_path / "gone")
    add(db, ws, "w1", mode="assign", status="done")

    cull.prune_retired(db)

    assert db.get_agent("w1") is not None and db.list_history(str(repo), "archived") == []


def test_dropping_a_scratch_session_archives_its_finished_chat(db, tmp_path, monkeypatch):
    # A done chat isn't a paused session, so it went with the workspace unkept.
    (tmp_path / "s").mkdir()
    ws = _row(db, tmp_path, tmp_path / "s", kind="main")
    add(db, ws, "boss", status="done")
    monkeypatch.setattr(sessions.scratch, "sessions", lambda db: [ws])
    monkeypatch.setattr(sessions.scratch, "commit_count", lambda ws: 0)
    monkeypatch.setattr(sessions.git, "dirty_files", lambda path: [])
    monkeypatch.setattr(sessions.tmux, "kill_session", lambda name: None)

    assert sessions.prune_scratch(db) == 1
    assert [r.agent_id for r in db.list_history(str(tmp_path), "archived")] == ["boss"]


# -- the CLI -------------------------------------------------------------------


def test_history_cli_prints_rows_and_a_total(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feature").workspace
    history.record(
        db, ws.repo_root, "worker_result", agent_id="w1", branch="feature", profile="developer",
        task="added login", result="done: added login",
        usage=Usage(input_tokens=2000, output_tokens=9000, cache_read_tokens=160000,
                   cache_creation_tokens=20000, model="claude-sonnet-5"),
    )
    history.record(db, ws.repo_root, "merge", branch="feature", result="Merged feature into main")

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["history"])

    assert res.exit_code == 0, res.output
    assert "worker_result" in res.output and "merge" in res.output
    assert "182k in" in res.output and "9k out" in res.output
    assert "total tokens: 191k" in res.output


def test_history_cli_kind_filter(db, repo, monkeypatch):
    ws = workspaces.create(db, str(repo), "feature").workspace
    history.record(db, ws.repo_root, "merge", branch="feature-merge", result="Merged into main")
    history.record(db, ws.repo_root, "check", branch="feature-check", result="check passed")

    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["history", "--kind", "merge"])

    assert res.exit_code == 0, res.output
    assert "feature-merge" in res.output
    assert "feature-check" not in res.output


def test_history_cli_empty(db, repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["history"])
    assert res.exit_code == 0
    assert "no history" in res.output


# -- never blocks the thing it records ------------------------------------------


def test_history_failure_does_not_block_merge_or_forward(db, repo, monkeypatch):
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text('{"pipeline": false}')  # the manual flow forwards
    from brindle import git

    def boom(*a, **k):
        raise RuntimeError("history is broken")

    monkeypatch.setattr(history, "record", boom)
    monkeypatch.setattr(agents, "is_alive", lambda a: True)

    root_ws = workspaces.adopt_root(db, str(repo))
    add(db, root_ws, "boss")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    ws = workspaces.create(db, str(repo), "feature").workspace
    add(db, ws, "w1", mode="assign", parent="boss")

    assert agents.report_result(db, "w1", "done: added login") == \
        "result recorded and sent to your supervisor"
    assert "done: added login" in db.pop_pending("boss").body

    with open(ws.path + "/new.py", "w") as f:
        f.write("x = 1\n")
    git.commit_all(ws.path, "work")
    out = asyncio.run(mcp_server.merge_workspace(ws.id))
    assert out.startswith("Merged feature into main")
    assert db.list_history(ws.repo_root) == []


# -- tokens: per-agent deltas, so totals add up ---------------------------------


def _usage_line(msg_id, n):
    import json

    return json.dumps({"type": "assistant", "message": {
        "id": msg_id, "model": "claude-sonnet-5",
        "usage": {"input_tokens": n, "output_tokens": n,
                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}}) + "\n"


def test_repeated_reports_and_merges_do_not_double_count(db, repo, tmp_path, monkeypatch):
    from brindle import git

    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    boss_t, w_t = tmp_path / "boss.jsonl", tmp_path / "w1.jsonl"
    boss_t.write_text(_usage_line("b1", 100))
    w_t.write_text(_usage_line("w1", 1000))

    root_ws = workspaces.adopt_root(db, str(repo))
    add(db, root_ws, "boss")
    db.update_agent("boss", transcript_path=str(boss_t))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    ws = workspaces.create(db, str(repo), "feature").workspace
    add(db, ws, "w1", mode="assign", parent="boss")
    db.update_agent("w1", transcript_path=str(w_t))

    agents.report_result(db, "w1", "first")
    agents.report_result(db, "w1", "again, no new work")
    with w_t.open("a") as f:
        f.write(_usage_line("w2", 500))
    agents.report_result(db, "w1", "more work")

    for i, branch in enumerate(("feature", "feature2")):
        w = ws if branch == "feature" else workspaces.create(db, str(repo), branch).workspace
        with open(f"{w.path}/f{i}.py", "w") as f:
            f.write("x = 1\n")
        git.commit_all(w.path, "work")
        with boss_t.open("a") as f:
            f.write(_usage_line(f"b{i + 2}", 10))
        assert asyncio.run(mcp_server.merge_workspace(w.id)).startswith("Merged")

    rows = db.list_history(ws.repo_root)
    assert sum(history.tokens_total(r.tokens) for r in rows) == 2 * (1500 + 120)
    worker_rows = [r for r in reversed(rows) if r.kind == "worker_result"]
    assert [history.tokens_total(r.tokens) for r in worker_rows] == [2000, 0, 1000]
    merge_rows = [r for r in reversed(rows) if r.kind == "merge"]
    assert [history.tokens_total(r.tokens) for r in merge_rows] == [2 * 110, 2 * 10]


# -- /clear and other transcript resets ---------------------------------------


def test_clear_with_a_larger_new_transcript_starts_a_fresh_baseline(db, repo, tmp_path, monkeypatch):
    """After /clear, Claude Code writes a brand-new transcript file. Even
    though it can grow past the size of the old one, its usage has nothing
    to do with the agent's previous mark: the whole thing is new."""
    monkeypatch.setattr(agents, "is_alive", lambda a: True)
    root_ws = workspaces.adopt_root(db, str(repo))
    add(db, root_ws, "boss")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    ws = workspaces.create(db, str(repo), "feature").workspace
    add(db, ws, "w1", mode="assign", parent="boss")

    old_t = tmp_path / "old.jsonl"
    old_t.write_text(_usage_line("m1", 1000))
    db.update_agent("w1", transcript_path=str(old_t))
    agents.report_result(db, "w1", "first")

    new_t = tmp_path / "new.jsonl"  # /clear: a new, already-larger transcript
    new_t.write_text(_usage_line("n1", 5000))
    db.update_agent("w1", transcript_path=str(new_t))
    agents.report_result(db, "w1", "after /clear")

    rows = [r for r in db.list_history(ws.repo_root) if r.kind == "worker_result"]
    assert history.tokens_total(rows[0].tokens) == 10000  # not 10000 - 2000
    assert history.tokens_total(rows[1].tokens) == 2000




def test_same_transcript_usage_drop_falls_back_to_a_fresh_baseline(db):
    """If usage on the *same* transcript somehow drops (a component going
    down that isn't the transcript changing), that's still not a negative
    delta to record: start fresh, same as a stale-mark mismatch."""
    history.record(db, "/r", "worker_result", agent_id="a1", transcript_path="/t/x.jsonl",
                   usage=Usage(1000, 500, 0, 0))
    history.record(db, "/r", "worker_result", agent_id="a1", transcript_path="/t/x.jsonl",
                   usage=Usage(100, 50, 0, 0))

    rows = db.list_history("/r")
    assert history.tokens_total(rows[0].tokens) == 150


def test_history_cli_outside_a_repo_says_it_shows_all(db, tmp_path, monkeypatch):
    history.record(db, "/elsewhere", "check", task="t")
    monkeypatch.chdir(tmp_path)
    res = CliRunner().invoke(app, ["history"])
    assert res.exit_code == 0, res.output
    assert "showing all repos" in res.output
    assert "check" in res.output

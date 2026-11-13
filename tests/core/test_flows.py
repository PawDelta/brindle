"""Connected user flows: real dispatch, persistence and git; no agent processes."""

import asyncio
import json
from pathlib import Path
from subprocess import CompletedProcess
from urllib.parse import urlencode

import pytest
from typer.testing import CliRunner

from brindle import agents, autopilot, git, mcp_server, tasks, tmux, workspaces
from brindle.cli import app
from brindle.config import set_local
from brindle.db import Agent
from fast_repo import repo, repo_template  # noqa: F401
from git_snapshots import fast_host_tools  # noqa: F401


@pytest.fixture
def processes(monkeypatch):
    """Provider launch boundary; everything above it creates real agent rows."""
    live = {}

    def launch(db, agent, ws, **kw):
        pane = f"%{agent.id}"
        live[agent.id] = tmux.AgentPane(pane, agent.id, True, True)
        db.update_agent(agent.id, tmux_window=pane, tmux_server=tmux.current_server())
        db.set_status(agent.id, "processing")

    monkeypatch.setattr(agents, "_launch", launch)
    monkeypatch.setattr(agents, "model_problem", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "_detach", lambda args: None)
    monkeypatch.setattr(agents, "warm_checks", lambda ws: None)
    monkeypatch.setattr(agents, "close_later", lambda *a, **kw: None)
    monkeypatch.setattr(agents, "_pane_state", lambda agent: None)
    monkeypatch.setattr(agents, "screen_status", lambda *a, **kw: None)
    monkeypatch.setattr(tmux, "_tmux", lambda *a, **kw: CompletedProcess(a, 1, "", ""))
    monkeypatch.setattr(tmux, "agent_panes", lambda: list(live.values()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id in live)
    return live


@pytest.fixture
def boss(db, repo, processes, monkeypatch):
    for key, value in {"review": True, "auto_merge_default_branch": True,
                       "overlap": "warn", "goal_audit": False}.items():
        set_local(str(repo), key, value)
    root = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", root.id, "supervisor", "claude", None, "interactive",
                       "processing", "%boss", None, 1000.0))
    processes["boss"] = tmux.AgentPane("%boss", "boss", True, True)
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return root


def assign(db, branch, **kw):
    reply = asyncio.run(mcp_server.assign("developer", f"Implement {branch}", branch=branch, **kw))
    matches = [t for t in db.list_tasks() if t.branch == branch]
    assert len(matches) == 1, reply
    return matches[0]


def commit_file(ws, name):
    (Path(ws.path) / name).write_text("implemented\n")
    git.commit_all(ws.path, f"Add {name}")
    return git.out(["rev-parse", "HEAD"], ws.path)


@pytest.mark.variant("Core integration sentinel: a reported worker must reach review and land its committed artifact")
def test_assign_report_review_merge(db, repo, boss, monkeypatch):
    task = assign(db, "core-feature")
    worker = db.get_agent(task.agent_id)
    ws = db.get_workspace(worker.workspace_id)
    assert task.state == "started" and worker.parent_id == "boss"
    assert ws.kind == "worktree" and ws.path != boss.path
    assert git.out(["branch", "--show-current"], ws.path) == "core-feature"
    sha = commit_file(ws, "feature.txt")

    monkeypatch.setenv("BRINDLE_AGENT_ID", worker.id)
    mcp_server.report_result("Implemented feature")
    assert db.get_agent(worker.id).result == "Implemented feature"
    assert db.get_agent(worker.id).pipeline == "reviewing"
    [reviewer] = [a for a in db.list_agents(ws.id) if a.mode == "review"]
    assert reviewer.review_sha == sha
    assert not (repo / "feature.txt").exists()

    monkeypatch.setenv("BRINDLE_AGENT_ID", reviewer.id)
    mcp_server.submit_review(True, "Verified behavior")
    assert (repo / "feature.txt").read_text() == "implemented\n"
    assert git.is_ancestor(str(repo), sha, "main")
    assert db.get_task(task.id).state == "merged"


@pytest.mark.variant("Core integration sentinel: merge notification must release a dependency through actual task dispatch once")
def test_dependency_starts_after_merge(db, repo, boss):
    set_local(str(repo), "pipeline", False)
    set_local(str(repo), "review", False)
    upstream = assign(db, "core-upstream")
    ws = db.get_workspace(db.get_agent(upstream.agent_id).workspace_id)
    commit_file(ws, "upstream.txt")
    downstream = assign(db, "core-downstream", depends_on=[upstream.agent_id])
    assert downstream.state == "pending" and downstream.agent_id is None
    assert not git.branch_exists(str(repo), "core-downstream")

    asyncio.run(mcp_server.merge_workspace(ws.id))
    started = db.get_task(downstream.id)
    assert started.state == "started" and started.agent_id
    child = db.get_agent(started.agent_id)
    child_ws = db.get_workspace(child.workspace_id)
    assert child.parent_id == "boss" and child_ws.branch == "core-downstream"
    assert (Path(child_ws.path) / "upstream.txt").read_text() == "implemented\n"
    tasks.on_merged(db, ws)
    assert db.get_task(downstream.id).agent_id == child.id
    assert [a.id for a in db.list_agents(child_ws.id)] == [child.id]


@pytest.mark.variant("Core integration sentinel: implicit and explicit CLI resolution must commit in the worker checkout only")
def test_cli_resolves_workspace(db, repo, boss, monkeypatch):
    ws = workspaces.create(db, str(repo), "core-cli", run_setup=False).workspace
    before = git.out(["rev-parse", "main"], str(repo))
    (Path(ws.path) / "cli.txt").write_text("cli change\n")
    nested = Path(ws.path) / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    runner = CliRunner()
    result = runner.invoke(app, ["commit", "-m", "Commit from nested directory"])
    assert result.exit_code == 0, result.output
    assert git.out(["show", "HEAD:cli.txt"], ws.path) == "cli change"
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["cd", ws.name])
    assert result.exit_code == 0 and Path(result.stdout.strip()) == Path(ws.path)
    assert git.out(["rev-parse", "main"], str(repo)) == before
    assert not (repo / "cli.txt").exists()


def request(method, path, body=None, caller=None):
    """HTTP transport in process, including authentication and response serialization."""
    from brindle.daemon import peer
    from brindle.daemon.app import build_app

    token, sent = "core-test-token", []
    path, _, query = path.partition("?")

    async def receive():
        return {"type": "http.request", "body": json.dumps(body or {}).encode(), "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
             "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query.encode(),
             "root_path": "", "server": ("127.0.0.1", 8123), "client": ("127.0.0.1", 50000),
             "headers": [(b"content-type", b"application/json")]}
    if caller:
        scope[peer.PEER_PID] = caller
    else:
        scope["headers"].append((b"authorization", f"Bearer {token}".encode()))
    asyncio.run(build_app(token)(scope, receive, send))
    return sent[0]["status"], json.loads(b"".join(m.get("body", b"") for m in sent))


@pytest.mark.variant("Core integration sentinel: a supervisor escalation must reach the inbox and return the person's decision to the hook")
def test_permission_escalates_to_person(db, boss, monkeypatch):
    from brindle import permission_routing, permissions
    from brindle.daemon import events, holds, peer
    from brindle.service import inbox

    worker = Agent("worker", boss.id, "developer", "claude", "boss", "assign", "processing",
                   "%worker", None, 1000.0)
    db.add_agent(worker)
    set_local(boss.repo_root, "permission_policy", "on")
    permissions.add_rule("bash", "make deploy", "ask", "exact")
    monkeypatch.setattr(events, "connected", 1)
    monkeypatch.setattr(holds.HOLDS, "_holds", {})
    monkeypatch.setattr(holds.HOLDS, "_outcomes", {})
    monkeypatch.setattr(holds.HOLDS, "clock", lambda: 1000.0)
    monkeypatch.setattr(peer, "caller_agents", lambda pid: {pid})
    caller = ["worker"]
    monkeypatch.setattr(holds, "post", lambda path, body, seconds: request("POST", path, body, caller[0]))
    answers = []

    def wait(holder, seconds):
        assert inbox.pending_permissions(db, worker) == []
        caller[0] = "boss"
        permission_routing.answer_via_daemon(worker.id, holder.request_id, "escalate", "Needs person")
        caller[0] = "worker"
        [pending] = inbox.pending_permissions(db, worker)
        assert pending.dto.request_id == holder.request_id
        # The hook consumes the released answer while the HTTP action awaits its receipt.
        def consume(predicate, **kw):
            answers.append(holds.HOLDS.take(holder.request_id, holder.nonce))
            assert predicate()
        monkeypatch.setattr(inbox, "_until", consume)
        status, reply = request("POST", "/v1/inbox/worker/permission", {
            "request": holder.request_id, "digest": pending.dto.digest, "choice": "deny"})
        assert status == 200 and reply["cleared"], reply
        return answers[-1]

    monkeypatch.setattr(holds.Holder, "wait", wait)
    result = agents.permission_request_decision(db, worker, {
        "tool_name": "Bash", "tool_input": {"command": "make deploy"}, "hook_event_name": "PermissionRequest"})
    assert result["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert [tuple(r) for r in db.conn.execute("SELECT resolved_by, outcome FROM permission_asks")] == [("person", "deny")]
    assert inbox.pending_permissions(db, worker) == []


@pytest.mark.variant("Core integration sentinel: daemon shutdown must persist its session tree and startup must restore and settle it")
def test_daemon_restores_paused_session(db, boss, processes, monkeypatch):
    from brindle import procs
    from brindle.daemon import lifecycle
    from brindle.service import sessions

    db.add_agent(Agent("worker", boss.id, "developer", "claude", "boss", "assign", "processing",
                       "%worker", None, 1000.0, task="Unfinished work"))
    processes["worker"] = tmux.AgentPane("%worker", "worker", True, True)
    db.add_agent(Agent("finished", boss.id, "developer", "claude", "boss", "assign", "done",
                       "", "Reported", 1000.0))

    def panes():
        snapshot = tmux.PaneSnapshot(tmux.current_server())
        for pane in processes.values():
            snapshot[pane.pane] = True
            snapshot.tags[pane.pane] = {tmux.AGENT_TAG: pane.agent}
        return snapshot

    def places():
        return {p.pane: tmux.Place(p.pane, boss.tmux_session, p.agent) for p in processes.values()}

    def close(panes=(), windows=(), sessions=()):
        for aid, pane in list(processes.items()):
            if pane.pane in (*panes, *windows) or boss.tmux_session in sessions:
                del processes[aid]

    monkeypatch.setattr(tmux, "list_panes", panes)
    monkeypatch.setattr(tmux, "pane_places", places)
    monkeypatch.setattr(tmux, "close", close)
    monkeypatch.setattr(tmux, "capture", lambda *a, **kw: "❯ \n? for shortcuts")
    monkeypatch.setattr(procs, "stop", lambda *a, **kw: 0)
    monkeypatch.setattr(sessions, "cull_detached", lambda *a: None)

    assert lifecycle.pause_for_exit() == ["boss"]
    assert {a.id: a.status for a in db.list_agents()} == {
        "boss": "paused", "worker": "paused", "finished": "done"}
    assert processes == {}
    assert [e["id"] for e in lifecycle._read_resume()] == ["boss"]
    assert lifecycle.resume_after_start() == ["boss"]
    assert set(processes) == {"boss", "worker"}
    assert db.get_agent("boss").status == db.get_agent("worker").status == "processing"
    assert db.get_agent("finished").status == "done"
    assert lifecycle._read_resume() is None


@pytest.mark.variant("Core integration sentinel: milestone execution must persist failure then completion and expose it through the daemon")
def test_milestone_progress_reaches_goal(db, repo, boss, monkeypatch):
    db.add_autopilot("boss")
    mcp_server.set_goal("Deliver feature", [{"title": "Feature works", "check": "test -f delivered.txt"}])
    monkeypatch.setattr(autopilot, "run_check", lambda cmd, cwd, env, seconds, **kw:
                        ((Path(cwd) / "delivered.txt").exists(), "check output"))
    autopilot.check_milestones(db, "boss", boss)
    assert [m.status for m in db.milestones("boss")] == ["failed"]
    assert db.get_autopilot("boss").state != "done"
    commit_file(boss, "delivered.txt")
    autopilot.check_milestones(db, "boss", boss)
    assert [m.status for m in db.milestones("boss")] == ["passed"]
    assert db.get_autopilot("boss").state == "done"
    status, goal = request("GET", "/v1/goal?" + urlencode({"repo": str(repo), "root": "boss"}))
    assert status == 200 and goal["state"] == "done", goal
    assert [m["status"] for m in goal["milestones"]] == ["passed"]

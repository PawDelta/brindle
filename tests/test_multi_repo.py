"""Several repos in one session (brindle Pro `multi_repo`): `brindle repo
add/ls/rm`, workers assigned into an attached repo (worktree, checks and
merge there, never in the session's own repo), cross-repo `depends_on`,
milestone checks in another repo, and the Pro gate."""

import asyncio
import json
import re
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import sh
from brindle import agents, autopilot, cli, mcp_server, repos, tasks, workspaces
from brindle.db import Agent
from brindle.pro import license


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    """Records a worker without launching a CLI; delegate()'s real worktree
    creation still runs."""
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)


@pytest.fixture(autouse=True)
def entitled(monkeypatch):
    monkeypatch.setattr(license, "has", lambda feature: feature == "multi_repo")


def git_repo(path: Path, name: str, config: dict) -> Path:
    work = path / name
    work.mkdir()
    sh("git init -q -b main", work)
    (work / "app.py").write_text(f"print('{name}')\n")
    (work / ".brindle").mkdir()
    (work / ".brindle" / "config.json").write_text(json.dumps(config))
    sh("git add -A && git commit -qm init", work)
    return work


@pytest.fixture
def web(tmp_path):
    """A second repo with its own config: its check leaves a marker under
    the brindle home, so a test can tell whose checks ran."""
    return git_repo(tmp_path, "web", {
        "pipeline": False, "overlap": "warn",
        "checks": ['test -f app.py && touch "$BRINDLE_HOME/web-checked"'],
    })


@pytest.fixture
def boss(db, repo, monkeypatch):
    """A supervisor adopted on the main checkout, whose repo's check leaves
    its own marker."""
    (repo / ".brindle").mkdir(exist_ok=True)
    (repo / ".brindle" / "config.json").write_text(json.dumps({
        "pipeline": False, "overlap": "warn",
        "checks": ['touch "$BRINDLE_HOME/main-checked"'],
    }))
    sh("git add -A && git commit -qm config", repo)   # a clean checkout records check shas
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                        "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    return ws


def started_worker_id(reply: str) -> str:
    m = re.search(r"Started worker (\S+)", reply)
    assert m, reply
    return m.group(1)


def commit_file(path: Path, name: str, content: str = "x\n") -> None:
    (path / name).write_text(content)
    sh(f"git add -A && git commit -qm {name}", path)


def run_cli(monkeypatch, cwd: Path, *args: str):
    monkeypatch.chdir(cwd)
    result = CliRunner().invoke(cli.app, list(args))
    text = result.output + (getattr(result, "stderr", "") or "" if result.exit_code else "")
    return result.exit_code, text


# -- repo add / ls / rm -----------------------------------------------------------


def test_attach_list_and_detach(db, repo, web, boss):
    assert repos.listing_text(db, "boss").startswith("No repos attached")
    a = repos.attach(db, "boss", str(web), "web")
    assert (a.alias, a.repo_root) == ("web", str(web))
    assert [x.alias for x in repos.attached(db, "boss")] == ["web"]
    assert "web" in repos.listing_text(db, "boss") and str(web) in repos.listing_text(db, "boss")
    assert repos.resolve(db, "boss", str(repo), "web") == str(web)
    assert repos.resolve(db, "boss", str(repo), str(web)) == str(web)   # by path too
    assert repos.resolve(db, "boss", str(repo), None) == str(repo)      # default: own repo
    assert repos.detach(db, "boss", "web").repo_root == str(web)
    assert repos.attached(db, "boss") == []


def test_attach_refuses_bad_paths_duplicates_and_the_own_repo(db, repo, web, boss, tmp_path):
    with pytest.raises(repos.RepoError, match="not a git repository"):
        repos.attach(db, "boss", str(tmp_path), "x")
    with pytest.raises(repos.RepoError, match="not a directory"):
        repos.attach(db, "boss", str(tmp_path / "nope"), "x")
    with pytest.raises(repos.RepoError, match="own repo"):
        repos.attach(db, "boss", str(repo))
    repos.attach(db, "boss", str(web), "web")
    with pytest.raises(repos.RepoError, match="already attached"):
        repos.attach(db, "boss", str(web), "web2")
    other = git_repo(tmp_path, "other", {})
    with pytest.raises(repos.RepoError, match="already"):
        repos.attach(db, "boss", str(other), "web")
    with pytest.raises(repos.RepoError, match="no repo 'nope'"):
        repos.resolve(db, "boss", str(repo), "nope")


def test_attach_a_repo_whose_folder_name_matches_the_session_repo(db, repo, boss, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    twin = git_repo(elsewhere, repo.name, {"pipeline": False})
    assert twin != repo and twin.name == repo.name
    attached = repos.attach(db, "boss", str(twin))
    ows = workspaces.adopt_root(db, str(twin))
    assert attached.repo_root == str(twin)
    assert boss.id == f"{repo.name}/root"          # existing ids are unchanged
    assert ows.id != boss.id and ows.tmux_session != boss.tmux_session
    assert workspaces.resolve(db, boss.id).repo_root == str(repo)
    assert workspaces.resolve(db, ows.id).repo_root == str(twin)
    # A worktree in the twin gets an id under the twin's prefix, not the other repo's.
    wt = workspaces.create(db, str(twin), "feat/x", fetch=False).workspace
    assert wt.id.startswith(ows.id.rsplit("/", 1)[0] + "/")
    assert workspaces.resolve(db, wt.id).repo_root == str(twin)


def test_attach_refuses_a_repo_attached_to_another_live_session(db, repo, web, boss, tmp_path, monkeypatch):
    other_repo = git_repo(tmp_path, "other", {})
    ows = workspaces.adopt_root(db, str(other_repo))
    db.add_agent(Agent("other", ows.id, "supervisor", "claude", None, "interactive", "processing",
                        "@1", None, time.time()))
    repos.attach(db, "other", str(web), "web")
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "other")
    with pytest.raises(repos.RepoError, match="another running session"):
        repos.attach(db, "boss", str(web), "web")
    # Once that session is gone (not live), the repo can move here.
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: False)
    assert repos.attach(db, "boss", str(web), "web").alias == "web"


def test_cli_repo_add_ls_rm(db, repo, web, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    code, out = run_cli(monkeypatch, repo, "repo", "add", str(web), "--name", "web")
    assert code == 0 and "attached" in out and "web" in out
    assert [x.alias for x in db.session_repos("boss")] == ["web"]
    code, out = run_cli(monkeypatch, repo, "repo", "ls")
    assert code == 0 and "web" in out and str(web) in out
    code, out = run_cli(monkeypatch, repo, "repo", "rm", "web")
    assert code == 0 and "detached" in out
    assert db.session_repos("boss") == []
    code, out = run_cli(monkeypatch, repo, "repo", "ls")
    assert code == 0 and "No repos attached" in out


# -- a worker in an attached repo ---------------------------------------------------


def test_assign_into_an_attached_repo_works_checks_and_merges_there(db, repo, web, boss, brindle_home):
    repos.attach(db, "boss", str(web), "web")
    out = asyncio.run(mcp_server.assign("developer", "do web", branch="feat-web", repo="web"))
    worker_id = started_worker_id(out)
    assert "(repo web:" in out
    worker = db.get_agent(worker_id)
    wws = db.get_workspace(worker.workspace_id)
    assert wws.repo_root == str(web) and wws.base_branch == "main"
    assert Path(wws.path).is_dir() and "/web/" in wws.path
    assert (Path(wws.path) / "app.py").read_text() == "print('web')\n"
    assert not [w for w in db.find_workspaces(str(repo)) if w.kind == "worktree"]
    [t] = db.list_tasks(str(web), state="started")
    assert t.agent_id == worker_id and t.caller_ws_id == workspaces.adopt_root(db, str(web)).id

    commit_file(Path(wws.path), "web_output.txt", "from the web worker\n")
    # The workspace tools find it by name within the repo given.
    assert "web_output.txt" in mcp_server.workspace_diff("feat-web", repo="web")
    merged = asyncio.run(mcp_server.merge_workspace(wws.id))
    assert merged.startswith("Merged feat-web into main"), merged
    assert (web / "web_output.txt").read_text() == "from the web worker\n"
    assert not (repo / "web_output.txt").exists()
    assert (brindle_home / "web-checked").exists()          # web's own checks ran
    assert not (brindle_home / "main-checked").exists()     # the session's repo's didn't


def test_assign_without_repo_is_unchanged(db, repo, web, boss):
    repos.attach(db, "boss", str(web), "web")
    out = asyncio.run(mcp_server.assign("developer", "do main", branch="feat-main"))
    wws = db.get_workspace(db.get_agent(started_worker_id(out)).workspace_id)
    assert wws.repo_root == str(repo)


def test_cross_repo_depends_on_queues_then_starts_in_the_other_repo(db, repo, web, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    repos.attach(db, "boss", str(web), "web")
    out_a = asyncio.run(mcp_server.assign("developer", "do A", branch="feat-a"))
    worker_a = started_worker_id(out_a)
    ws_a = next(w for w in db.find_workspaces(str(repo)) if w.branch == "feat-a")
    commit_file(Path(ws_a.path), "a_output.txt")

    out_b = asyncio.run(mcp_server.assign("developer", "do B", branch="feat-b", repo="web",
                                          depends_on=[worker_a]))
    assert "Queued task" in out_b and worker_a in out_b
    [b] = db.list_tasks(str(web), state="pending")
    assert "feat-b" in mcp_server.list_tasks()

    assert asyncio.run(mcp_server.merge_workspace(ws_a.id)).startswith("Merged")
    b = db.get_task(b.id)
    assert b.state == "started" and b.agent_id
    ws_b = db.get_workspace(db.get_agent(b.agent_id).workspace_id)
    assert ws_b.repo_root == str(web) and ws_b.branch == "feat-b"
    assert not (Path(ws_b.path) / "a_output.txt").exists()   # another repo: A's file isn't there
    msg = db.pop_pending("boss")
    assert msg is not None and "Started" in msg.body and worker_a in msg.body


def test_a_branch_name_dependency_only_matches_within_its_own_repo(db, repo, web, boss, monkeypatch):
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    repos.attach(db, "boss", str(web), "web")
    worker_main = started_worker_id(asyncio.run(mcp_server.assign("developer", "A", branch="feat-a")))
    worker_web = started_worker_id(asyncio.run(mcp_server.assign("developer", "W", branch="feat-w", repo="web")))
    # Both repos queue a task on a branch called "shared".
    assert "Queued" in asyncio.run(mcp_server.assign("developer", "S", branch="shared", depends_on=[worker_main]))
    assert "Queued" in asyncio.run(mcp_server.assign("developer", "S", branch="shared", repo="web",
                                                     depends_on=[worker_web]))
    # A task in the main repo waiting on "shared" by name means the main repo's.
    assert "Queued" in asyncio.run(mcp_server.assign("developer", "D", branch="dep", depends_on=["shared"]))
    [p_main] = [t for t in db.list_tasks(str(repo), state="pending") if t.branch == "shared"]
    [p_web] = [t for t in db.list_tasks(str(web), state="pending") if t.branch == "shared"]
    [dep] = [t for t in db.list_tasks(str(repo), state="pending") if t.branch == "dep"]

    assert tasks.cancel(db, db.get_agent("boss"), p_web.id) == f"Cancelled task {p_web.id}."
    assert db.get_task(p_web.id).state == "cancelled"
    assert db.get_task(dep.id).state == "pending"       # the other repo's "shared" is unrelated
    assert db.get_task(p_main.id).state == "pending"

    assert dep.id in tasks.cancel(db, db.get_agent("boss"), p_main.id)   # its own repo's: cascades
    assert db.get_task(dep.id).state == "cancelled"


def test_handover_repoints_a_queued_task_whose_caller_workspace_is_gone(db, repo, web, boss, monkeypatch):
    from brindle import sessions

    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    repos.attach(db, "boss", str(web), "web")
    worker_a = started_worker_id(asyncio.run(mcp_server.assign("developer", "A", branch="feat-a")))
    assert "Queued" in asyncio.run(mcp_server.assign("developer", "B", branch="feat-b", repo="web",
                                                     depends_on=[worker_a]))
    assert "Queued" in asyncio.run(mcp_server.assign("developer", "C", branch="feat-c", depends_on=[worker_a]))
    [b] = db.list_tasks(str(web), state="pending")
    web_root = db.get_workspace(b.caller_ws_id)
    assert web_root.repo_root == str(web)
    db.delete_workspace(web_root.id)                 # its checkout record is gone

    new = sessions.handover(db, "boss", boss, "take over", pause_old=False)
    b = db.get_task(b.id)
    assert b.caller_id == new.id
    ws = db.get_workspace(b.caller_ws_id)
    assert ws is not None and ws.repo_root == str(web) and ws.kind == "main"
    [c] = [t for t in db.list_tasks(str(repo), state="pending") if t.branch == "feat-c"]
    assert c.caller_id == new.id and c.caller_ws_id == boss.id
    assert [x.alias for x in repos.attached(db, new.id)] == ["web"] and repos.attached(db, "boss") == []


def test_list_agents_groups_by_repo(db, repo, web, boss):
    repos.attach(db, "boss", str(web), "web")
    main_worker = started_worker_id(asyncio.run(mcp_server.assign("developer", "m", branch="m")))
    web_worker = started_worker_id(asyncio.run(mcp_server.assign("developer", "w", branch="w", repo="web")))
    everything = mcp_server.list_agents()
    assert "## this repo (" in everything and "## web (" in everything
    assert main_worker in everything and web_worker in everything
    only_web = mcp_server.list_agents(repo="web")
    assert web_worker in only_web and main_worker not in only_web
    listed = mcp_server.list_repos()
    assert str(web) in listed and str(repo) in listed


def test_sidebar_and_ls_group_workers_by_repo(db, repo, web, boss, monkeypatch):
    from brindle import view, watch

    repos.attach(db, "boss", str(web), "web")
    asyncio.run(mcp_server.assign("developer", "m", branch="feat-m"))
    asyncio.run(mcp_server.assign("developer", "w", branch="feat-w", repo="web"))
    snap = view.snapshot(db, str(repo), panes={})
    assert {ws["id"].partition("/")[0] for ws in snap} == {"proj", "web"}
    titles = [ln.text for ln in watch.render(snap, time.time(), width=80) if ln.group]
    assert any("web · feat-w" in t for t in titles) and any("proj · feat-m" in t for t in titles)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: False)
    code, out = run_cli(monkeypatch, repo, "ls")
    assert code == 0 and f"web  ({web})" in out and "feat-w" in out and "feat-m" in out
    # One repo only: no repo labels on the headers, as before.
    headers = [ln.text for ln in watch.render(snap[:1], time.time(), width=80) if ln.group]
    assert headers and all(" · " not in t for t in headers)


# -- milestone checks in another repo ------------------------------------------------


def test_parse_goals_reads_a_repo_qualified_check():
    plan = autopilot.parse_goals("# G\n\n## A\ncheck@web: test -f web.txt\n\n## B\nrepo: api\ncheck: make test\n\n## C\ncheck: true\n")
    assert plan.milestones == [
        ("A", "test -f web.txt", None, None, "web"),
        ("B", "make test", None, None, "api"),
        ("C", "true", None),
    ]
    assert repos.split_check("@web: test -f x") == ("web", "test -f x")
    assert repos.split_check("test -f x") == (None, "test -f x")


def test_milestone_check_runs_in_the_attached_repo(db, repo, web, boss):
    db.add_autopilot("boss")
    repos.attach(db, "boss", str(web), "web")
    reply = mcp_server.set_goal("Two repos", [
        {"title": "Main", "check": "test -f app.py"},
        {"title": "Web", "check": "@web: test -f web.txt"},
    ])
    assert "@web: test -f web.txt" in reply
    [m_main, m_web] = db.milestones("boss")
    assert m_web.repo == "web" and m_web.check_cmd == "test -f web.txt"

    autopilot.check_milestones(db, "boss", boss)
    [m_main, m_web] = db.milestones("boss")
    assert m_main.status == "passed" and m_web.status == "failed"

    commit_file(web, "web.txt")
    autopilot.check_milestones(db, "boss", boss, position=2)
    [m_main, m_web] = db.milestones("boss")
    assert m_web.status == "passed"
    assert m_web.checked_sha == sh("git rev-parse HEAD", web)
    assert m_main.checked_sha == sh("git rev-parse HEAD", repo)

    assert "isn't attached" in mcp_server.set_goal("G", [{"title": "X", "check": "@nope: true"}])


# -- the Pro gate ------------------------------------------------------------------------


def test_without_the_feature_everything_cross_repo_is_refused(db, repo, web, boss, monkeypatch):
    repos.attach(db, "boss", str(web), "web")     # attached while entitled
    monkeypatch.setattr(license, "has", lambda feature: False)
    with pytest.raises(repos.RepoError, match=re.escape(repos.PRO_MESSAGE)):
        repos.attach(db, "boss", str(web), "web2")
    out = asyncio.run(mcp_server.assign("developer", "do web", branch="feat-web", repo="web"))
    assert out == f"Not started: {repos.PRO_MESSAGE}"
    assert db.list_tasks(str(web)) == []
    assert repos.PRO_MESSAGE in mcp_server.workspace_diff("x", repo="web")
    # The session's own repo still works, with or without an alias for it.
    out = asyncio.run(mcp_server.assign("developer", "do main", branch="feat-main"))
    assert "Started worker" in out
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "boss")
    code, text = run_cli(monkeypatch, repo, "repo", "add", str(web), "--name", "web3")
    assert code == 1 and repos.PRO_MESSAGE in text


def test_license_errors_fail_closed(monkeypatch):
    def boom(feature):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(license, "has", boom)
    assert repos.entitled() is False

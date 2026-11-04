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
from git_snapshots import fast_host_tools, repo, restore_or_build  # noqa: F401  (fixtures)
from brindle import agents, autopilot, cli, git, mcp_server, repos, tasks, workspaces
from brindle.db import Agent
from brindle.pro import license
from test_task_deps import dependency_rows


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
    (work / "app.py").write_text(f"print('{name}')\n")
    (work / ".brindle").mkdir()
    (work / ".brindle" / "config.json").write_text(json.dumps(config))
    sh("git init -q -b main && git add -A && git commit -qm init", work)
    return work


@pytest.fixture
def web(tmp_path, tmp_path_factory):
    """A second repo with its own config: its check leaves a marker under
    the brindle home, so a test can tell whose checks ran. Built once and copied."""
    return restore_or_build("multi:web", tmp_path, tmp_path_factory, ("web",), lambda: git_repo(tmp_path, "web", {
        "pipeline": False, "overlap": "warn", "review": False,
        "checks": ['test -f app.py && touch "$BRINDLE_HOME/web-checked"'],
    }))


@pytest.fixture
def boss(db, repo, tmp_path, tmp_path_factory, monkeypatch):
    """A supervisor adopted on the main checkout, whose repo's check leaves
    its own marker."""
    def commit_config():
        (repo / ".brindle").mkdir(exist_ok=True)
        (repo / ".brindle" / "config.json").write_text(json.dumps({
            "pipeline": False, "overlap": "warn", "review": False,
            "checks": ['touch "$BRINDLE_HOME/main-checked"'],
        }))
        sh("git add -A && git commit -qm config", repo)   # a clean checkout records check shas

    restore_or_build("multi:boss", tmp_path, tmp_path_factory, ("origin.git", "proj"), commit_config)
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
    # The same branch in both repos: separate ids, sessions and directories.
    wt1 = workspaces.create(db, str(repo), "feat/x", fetch=False).workspace
    wt2 = workspaces.create(db, str(twin), "feat/x", fetch=False).workspace
    assert wt2.id.startswith(ows.id.rsplit("/", 1)[0] + "/")
    assert wt1.id != wt2.id and wt1.path != wt2.path and wt1.tmux_session != wt2.tmux_session
    assert workspaces.resolve(db, wt1.id).repo_root == str(repo)
    assert workspaces.resolve(db, wt2.id).repo_root == str(twin)
    # Pool entries of the twin don't share the first repo's pool directory either.
    from brindle import pool
    e1, e2 = pool.fill_one(db, str(repo)), pool.fill_one(db, str(twin))
    assert e1 and e2 and Path(e1.path).parent != Path(e2.path).parent
    assert pool.pool_dir(str(twin)) == Path(e2.path).parent


@pytest.mark.slow("the end-to-end run: two sessions each attach, start a worker, commit and merge in real git (~150 calls)")
def test_two_live_sessions_attach_one_repo_and_each_merges_into_its_own_branch(
        db, repo, web, boss, tmp_path, monkeypatch):
    from brindle import view

    other_repo = git_repo(tmp_path, "other", {"pipeline": False, "review": False})
    ows = workspaces.adopt_root(db, str(other_repo))
    db.add_agent(Agent("other", ows.id, "supervisor", "claude", None, "interactive", "processing",
                        "@1", None, time.time()))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id in ("boss", "other"))
    # The repo's merge_into must not pull either session's workers onto its main.
    cfg = web / ".brindle" / "config.json"
    cfg.write_text(json.dumps({**json.loads(cfg.read_text()), "merge_into": "main"}))
    sh("git add -A && git commit -qm merge-into", web)
    theirs = repos.attach(db, "other", str(web), "web")
    mine = repos.attach(db, "boss", str(web), "web")
    assert {mine.branch, theirs.branch} == {"brindle/session-2", "brindle/session-3"}

    merged = {}
    for session in ("other", "boss"):
        monkeypatch.setenv("BRINDLE_AGENT_ID", session)
        wws = db.get_workspace(db.get_agent(started_worker_id(asyncio.run(
            mcp_server.assign("developer", "w", branch=f"feat-{session}", repo="web")))).workspace_id)
        assert wws.base_branch == repos.session_branch(db, session, str(web))
        commit_file(Path(wws.path), f"{session}.txt")
        merged[session] = asyncio.run(mcp_server.merge_workspace(wws.id))
        assert merged[session].startswith(f"Merged feat-{session} into {wws.base_branch}"), merged[session]
    target = {s: repos.target_workspace(db, s, str(web)) for s in ("boss", "other")}
    assert (Path(target["boss"].path) / "boss.txt").exists() and not (Path(target["boss"].path) / "other.txt").exists()
    assert (Path(target["other"].path) / "other.txt").exists() and not (Path(target["other"].path) / "boss.txt").exists()
    assert not (web / "boss.txt").exists() and not (web / "other.txt").exists()   # the shared checkout's branch is untouched
    # Another session's merge target is reserved, even though no worker ever ran in it.
    refused = asyncio.run(mcp_server.assign("developer", "w", branch=theirs.branch, repo="web"))
    assert "belongs to session other" in refused, refused
    assert not db.list_agents(target["other"].id)
    # The cleanup sweep keeps a session's merge target while it's attached, with
    # nothing on it yet and after a finished non-isolated worker ran there.
    monkeypatch.setattr(view, "UNUSED_WORKTREE_GRACE", -1)
    sh(f"git reset -q --hard {sh('git rev-parse main', web)}", Path(target["boss"].path))
    assert not view.retired(db, target["boss"], set())
    shared = started_worker_id(asyncio.run(mcp_server.assign("developer", "w", isolate=False, repo="web")))
    assert db.get_agent(shared).workspace_id == target["boss"].id
    db.set_status(shared, "done")
    assert not view.retired(db, target["boss"], set())
    assert repos.detach(db, "boss", "web").branch == mine.branch
    assert git.branch_exists(str(web), mine.branch)              # detach keeps the branch


# -- a worker in an attached repo ---------------------------------------------------


def test_assign_into_an_attached_repo_works_checks_and_merges_there(db, repo, web, boss, brindle_home):
    attached = repos.attach(db, "boss", str(web), "web")
    out = asyncio.run(mcp_server.assign("developer", "do web", branch="feat-web", repo="web"))
    worker_id = started_worker_id(out)
    assert "(repo web:" in out
    worker = db.get_agent(worker_id)
    wws = db.get_workspace(worker.workspace_id)
    assert wws.repo_root == str(web) and wws.base_branch == attached.branch
    assert Path(wws.path).is_dir() and "/web/" in wws.path
    assert (Path(wws.path) / "app.py").read_text() == "print('web')\n"
    assert not [w for w in db.find_workspaces(str(repo)) if w.kind == "worktree"]
    [t] = db.list_tasks(str(web), state="started")
    target = repos.target_workspace(db, "boss", str(web))
    assert t.agent_id == worker_id and t.caller_ws_id == target.id and target.branch == attached.branch

    commit_file(Path(wws.path), "web_output.txt", "from the web worker\n")
    # The workspace tools find it by name within the repo given.
    assert "web_output.txt" in mcp_server.workspace_diff("feat-web", repo="web")
    merged = asyncio.run(mcp_server.merge_workspace(wws.id))
    assert merged.startswith(f"Merged feat-web into {attached.branch}"), merged
    assert (Path(target.path) / "web_output.txt").read_text() == "from the web worker\n"
    assert not (repo / "web_output.txt").exists()
    assert (brindle_home / "web-checked").exists()          # web's own checks ran
    assert not (brindle_home / "main-checked").exists()     # the session's repo's didn't




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


@pytest.mark.parametrize("candidate", [
    "attached",
    pytest.param("alien", marks=pytest.mark.variant(
        "A same-named branch in an unrelated identity must not block the selected upstream")),
    pytest.param("local", marks=pytest.mark.variant(
        "A local workspace without a task must still shadow a merged task in an attached repo")),
])
def test_cross_repo_branch_dependency_refuses_when_ambiguous(db, dependency_rows, monkeypatch, candidate):
    from brindle import identity_lock

    caller, boss, workspace, _ = dependency_rows
    selected = workspace("web", "web", "feature", tracked=True)
    db.add_session_repo(caller.id, "web", selected.repo_root)
    if candidate == "local":
        tasks.on_merged(db, selected)
        workspace("local", branch="feature")
    else:
        other = workspace(candidate, candidate, "feature", tracked=True)
        worker = agents.workspace_worker(db, other)
        if candidate == "attached":
            db.add_session_repo(caller.id, candidate, other.repo_root)
            with pytest.raises(agents.AgentError):
                tasks.unmet_dependencies(db, boss, ["feature"], caller.id)
            assert tasks.unmet_dependencies(db, boss, [worker.id], caller.id) == [worker.id]
            return
        monkeypatch.setattr(identity_lock, "locked_by", lambda aid: "corp-id" if aid == worker.id else None)
    assert tasks.unmet_dependencies(db, boss, ["feature"], caller.id) == ["feature"]


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
    assert ws is not None and ws.repo_root == str(web) and ws.branch == repos.session_branch(db, new.id, str(web))
    [c] = [t for t in db.list_tasks(str(repo), state="pending") if t.branch == "feat-c"]
    assert c.caller_id == new.id and c.caller_ws_id == boss.id
    assert [x.alias for x in repos.attached(db, new.id)] == ["web"] and repos.attached(db, "boss") == []


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

    commit_file(web, "web.txt")       # on the shared checkout's branch: not this session's
    autopilot.check_milestones(db, "boss", boss, position=2)
    assert db.milestones("boss")[1].status == "failed"
    mine = Path(repos.target_workspace(db, "boss", str(web)).path)
    commit_file(mine, "web.txt")
    autopilot.check_milestones(db, "boss", boss, position=2)
    [m_main, m_web] = db.milestones("boss")
    assert m_web.status == "passed"
    assert m_web.checked_sha == sh("git rev-parse HEAD", mine)
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


def test_two_sessions_do_not_share_attached_repos(db, dependency_rows, monkeypatch):
    caller, boss, workspace, launched = dependency_rows
    other = fake_spawn(db, boss, "supervisor", mode="interactive")
    alien = workspace("alien", "other", "feature", tracked=True)
    db.update_agent(agents.workspace_worker(db, alien).id, parent_id=other.id)
    db.add_session_repo(other.id, "other", alien.repo_root)
    pending = tasks.enqueue(db, caller, boss, "developer", "app", "assign", isolate=True,
                            branch="dep", done_when=None, files=None, depends_on=["feature"])
    monkeypatch.setenv("BRINDLE_AGENT_ID", other.id)
    tasks.on_merged(db, alien)
    assert tasks.sweep(db) == 0
    assert db.get_task(pending.id).state == "pending" and launched == []

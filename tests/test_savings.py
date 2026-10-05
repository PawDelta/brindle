"""Routing decisions are recorded locally, and `copse account savings` turns
them into an honest, clearly labelled estimate."""
import asyncio
import io
import json
import time

import pytest

from copse import agents, learning, mcp_server, pipeline, plugins, quota, savings, tasks, workspaces
from copse.db import Agent
from copse.pro import account, credentials, license
from pro_fixtures import BASE, backend, claims, pro_env, sign, signing_key  # noqa: F401 - fixtures

COSTS = {"developer": 3, "developer-codex": 1, "developer-heavy": 3}
NOW = time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1))
LAST_MONTH = time.mktime((2026, 9, 10, 12, 0, 0, 0, 0, -1))


def cost(name):
    return COSTS.get(name, 2)


class Picker(learning.LearningPlugin):
    def __init__(self, prefer=None):
        self.prefer = prefer

    def record(self, task, outcome):
        pass

    def suggest(self, task, candidates, default=None):
        return self.prefer if self.prefer in candidates else None


@pytest.fixture(autouse=True)
def clear_cache():
    plugins.reset()
    yield
    plugins.reset()


@pytest.fixture(autouse=True)
def everything_available(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cli: f"/usr/bin/{cli}")
    monkeypatch.setattr(quota, "headroom", lambda provider, cfg=None, repo_root=None: 100.0)


@pytest.fixture
def boss(db, repo, monkeypatch):
    (repo / ".copse").mkdir(exist_ok=True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("COPSE_AGENT_ID", "boss")
    return ws


def config(repo, **kw):
    (repo / ".copse" / "config.json").write_text(json.dumps(kw))


def install(monkeypatch, plugin):
    from copse.pro import learning as pro_learning

    plugins.reset()
    monkeypatch.setattr(pro_learning, "CloudLearner", lambda repo_root: plugin)


def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="handoff", done_when=None, **kw):
    a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "",
              None, time.time(), task=prompt, done_when=done_when)
    db.add_agent(a)
    return a


def decide(db, repo, n, *, ts=NOW, weight="medium", baseline="developer", profile="developer",
           learned=False, tokens=0, outcome="merged", reviews=0, escalated=False):
    """One recorded decision for worker ``n``, with its usage and outcome."""
    agent = f"w{n}"
    db.add_routing_decision(str(repo), task_id=f"t{n}", agent_id=agent, weight=weight,
                            baseline_profile=baseline, profile=profile, learned=learned, ts=ts)
    for _ in range(reviews):
        db.note_routing_outcome(agent, review=True)
    db.note_routing_outcome(agent, escalated=escalated, outcome=outcome)
    if tokens:
        db.add_history(str(repo), "worker_result", agent_id=agent, profile=profile,
                       tokens=json.dumps({"input": tokens, "output": 0}))


def report(db, repo):
    return savings.report(db, str(repo), now=NOW, cost=cost)


# -- recording ------------------------------------------------------------------------------------


def test_a_learned_pick_is_recorded_with_its_baseline(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    install(monkeypatch, Picker(prefer="developer-codex"))
    config(repo, pipeline=False, learning="cloud")
    asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="medium"))
    [t] = db.list_tasks(str(repo), state="started")
    [d] = db.list_routing_decisions(str(repo))
    assert (d.task_id, d.agent_id, d.weight) == (t.id, t.agent_id, "medium")
    assert (d.baseline_profile, d.profile, d.learned) == ("developer", "developer-codex", 1)


def test_a_baseline_pick_is_recorded_too(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    config(repo, pipeline=False)
    asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="medium"))
    asyncio.run(mcp_server.assign(agent_profile="reviewer", task="do B", branch="feat-b"))
    a, b = db.list_routing_decisions(str(repo))
    assert (a.baseline_profile, a.profile, a.learned, a.weight) == ("developer", "developer", 0, "medium")
    assert (b.baseline_profile, b.profile, b.learned, b.weight) == ("reviewer", "reviewer", 0, None)


def test_a_queued_task_gets_its_worker_when_it_starts(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    config(repo, pipeline=False)
    asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="light"))
    out = asyncio.run(mcp_server.assign(task="do B", branch="feat-b", weight="light",
                                        depends_on=["feat-a"]))
    assert "Queued task" in out
    queued = db.list_routing_decisions(str(repo))[1]
    assert queued.agent_id is None
    worker = tasks.start_queued(db, db.get_task(queued.task_id))
    assert db.list_routing_decisions(str(repo))[1].agent_id == worker.id


def test_outcomes_are_counted_on_the_decision(db, repo, boss, monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    config(repo, pipeline=False)
    asyncio.run(mcp_server.assign(task="do A", branch="feat-a", weight="medium"))
    [t] = db.list_tasks(str(repo), state="started")
    worker = db.get_agent(t.agent_id)
    ws = db.get_workspace(worker.workspace_id)
    pipeline.note_review(db, ws, False)
    pipeline.note_review(db, ws, True)
    pipeline._escalate(db, ws, worker)
    pipeline.note_removed_unmerged(db, ws)
    [d] = db.list_routing_decisions(str(repo))
    assert (d.review_rounds, d.escalations, d.outcome) == (2, 1, "removed_unmerged")


def test_recording_never_raises(db, repo):
    db.conn.execute("DROP TABLE routing_decisions")
    savings.record(db, str(repo), {"profile": "developer"}, task_id="t")
    savings.attach_agent(db, "t", "w")
    savings.note_outcome(db, Agent("w", "ws", "developer", "claude", None, "assign", "idle", "",
                                   None, time.time()), merged=True)


# -- the estimate ---------------------------------------------------------------------------------


def test_too_little_data_says_so(db, repo):
    for n in range(savings.MIN_TASKS - 1):
        decide(db, repo, n, profile="developer-codex", learned=True, tokens=1000)
    this, last, total = report(db, repo)
    assert this.learned.tasks == savings.MIN_TASKS - 1 and this.saved_fraction is None
    text = savings.describe([this, last, total], str(repo))
    assert f"not enough data yet ({savings.MIN_TASKS - 1} of the {savings.MIN_TASKS}" in text
    assert "%" not in text
    assert "not enough data yet" in savings.summary_line([this, last, total])


def test_no_decisions_at_all(db, repo):
    text = savings.describe(report(db, repo), str(repo))
    assert "Not enough data yet" in text and "%" not in text


def test_estimate_from_the_relative_cost_ratio(db, repo):
    # no finished baseline tasks: the same tokens at the baseline's relative cost (3 vs 1)
    for n in range(5):
        decide(db, repo, n, profile="developer-codex", learned=True, tokens=1000)
    this, _, _ = report(db, repo)
    assert (this.compared, this.from_average) == (5, 0)
    assert this.saved_fraction == pytest.approx(2 / 3)
    text = savings.describe(report(db, repo), str(repo))
    assert "estimated ~67% lower cost than the baseline profiles, over 5 tasks" in text
    assert savings.ESTIMATE_NOTE in text and "not money" in text


def test_estimate_prefers_the_repos_own_average(db, repo):
    # the baseline profile used 4000 tokens a task at this weight here, not 1000
    for n in range(savings.MIN_BASELINE):
        decide(db, repo, f"b{n}", tokens=4000)
    decide(db, repo, "other", weight="heavy", tokens=90000)   # another weight: not averaged in
    for n in range(5):
        decide(db, repo, n, profile="developer-codex", learned=True, tokens=1000)
    this, _, _ = report(db, repo)
    assert (this.compared, this.from_average) == (5, 5)
    assert this.baseline_cost == 5 * 4000 * 3 and this.actual_cost == 5 * 1000 * 1
    assert this.saved_fraction == pytest.approx(1 - 5000 / 60000)


def test_a_costlier_pick_is_reported_as_costlier(db, repo):
    for n in range(5):
        decide(db, repo, n, baseline="developer-codex", profile="developer", learned=True,
               tokens=1000)
    this, last, total = report(db, repo)
    assert this.saved_fraction == pytest.approx(-2.0)
    assert "~200% higher cost" in savings.describe([this, last, total], str(repo))
    assert "higher cost" in savings.summary_line([this, last, total])


def test_unfinished_and_unmeasured_tasks_are_not_estimated(db, repo):
    for n in range(4):
        decide(db, repo, n, profile="developer-codex", learned=True, tokens=1000)
    decide(db, repo, "running", profile="developer-codex", learned=True, tokens=1000, outcome=None)
    decide(db, repo, "no-usage", profile="developer-codex", learned=True)
    this, _, _ = report(db, repo)
    assert this.learned.tasks == 6 and this.compared == 4 and this.saved_fraction is None


def test_months_and_counts(db, repo):
    for n in range(5):
        decide(db, repo, n, ts=LAST_MONTH, profile="developer-codex", learned=True, tokens=1000,
               reviews=1)
    decide(db, repo, "x", profile="developer-codex", learned=True, tokens=1000,
           outcome="removed_unmerged")
    decide(db, repo, "y", reviews=3, escalated=True)
    decide(db, repo, "z", reviews=1)
    this, last, total = report(db, repo)
    assert (this.learned.tasks, this.baseline.tasks) == (1, 2)
    assert (this.learned.troubled, this.baseline.troubled, this.baseline.review_rounds) == (1, 1, 4)
    assert (last.learned.tasks, last.learned.review_rounds, last.baseline.tasks) == (5, 5, 0)
    assert (total.learned.tasks, total.baseline.tasks, total.compared) == (6, 2, 6)
    assert this.saved_fraction is None and last.saved_fraction == pytest.approx(2 / 3)
    text = savings.describe([this, last, total], str(repo))
    assert "This month (2026-10)" in text and "Last month (2026-09)" in text and "All time" in text
    assert "picks                1 by learning · 2 baseline" in text
    assert "review rounds/task   0.0 learning · 2.0 baseline" in text
    assert "failed or escalated  1 of 1 learning · 1 of 2 baseline" in text
    # this month has too little: the summary falls back to last month's, and says which
    line = savings.summary_line([this, last, total])
    assert line.startswith("Learning last month: 5 picks, estimated ~67% lower cost")
    assert "an estimate" in line


def test_other_repos_are_not_counted(db, repo, tmp_path):
    for n in range(5):
        decide(db, tmp_path / "elsewhere", n, profile="developer-codex", learned=True, tokens=1000)
    assert report(db, repo)[2].learned.tasks == 0


# -- copse account --------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path, backend):  # noqa: F811
    s = credentials.FileStore(tmp_path / "pro")
    login_as(backend, s)
    return s


def login_as(backend, store, features=("learning",)):  # noqa: F811
    t = backend.issue()
    store.save({"access_token": t["access_token"], "refresh_token": t["refresh_token"],
                "access_expires_at": time.time() + 900, "base_url": BASE,
                "entitlement": sign(backend.key, claims(features=list(features)))})


def run_account(repo, store, backend, *args):  # noqa: F811
    out, err = io.StringIO(), io.StringIO()
    code = account.ProAccount(str(repo), store=store, transport=backend, out=out, err=err).run(
        list(args))
    return code, out.getvalue() + err.getvalue()


def test_account_savings_command(db, repo, store, backend, pro_env):  # noqa: F811
    for n in range(5):
        decide(db, repo, n, ts=time.time(), profile="developer-codex", baseline="reviewer",
               learned=True, tokens=1000)
    code, text = run_account(repo, store, backend, "savings")
    assert code == 0
    assert "This month" in text and "Last month" in text and "All time" in text
    assert "5 by learning · 0 baseline" in text and savings.ESTIMATE_NOTE in text
    assert run_account(repo, store, backend, "savings", "extra")[0] == 2


def test_bare_account_has_one_savings_line_when_learning_is_active(db, repo, store, backend,  # noqa: F811
                                                                   pro_env):  # noqa: F811
    code, text = run_account(repo, store, backend)
    assert code == 0
    assert len([line for line in text.splitlines() if line.startswith("Learning")]) == 1
    assert "not enough data yet" in text and "`copse account savings`" in text
    license.clear_cache()
    login_as(backend, store, features=())
    code, text = run_account(repo, store, backend)
    assert code == 0 and "Learning" not in text

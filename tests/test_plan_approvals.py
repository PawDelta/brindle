"""plan approvals: Auto / Always / Never -- when a worker must get its plan approved first."""

import json
import time

import pytest

from brindle import agents, config, tasks
from brindle.config import load_repo_config
from brindle.db import Agent, Workspace
from brindle.plan_policy import PlanTask, needs_plan

LIGHT = PlanTask(weight="light", files=["src/app/util.py"])


@pytest.mark.parametrize("layers, task, expected", [
    ({"local": {"plan": "always"}}, PlanTask(), True),
    ({"local": {"plan": "never"}}, PlanTask(weight="heavy", cross_repo=True), False),
    ({}, PlanTask(weight="heavy"), True),
    ({}, PlanTask(cross_repo=True), True),
    ({}, PlanTask(files=[".github/workflows/ci.yml"]), True),
    ({}, PlanTask(files=["src/app/migrations/0002_x.py"]), True),
    ({}, PlanTask(files=["src/app/auth/session.py"]), True),
    ({}, PlanTask(files=["deploy/aws/stack.yaml"]), True),
    ({}, LIGHT, False),
    ({"local": {"plan_first": True}}, PlanTask(), True),
    ({"local": {"plan_first": False}}, PlanTask(weight="heavy"), False),
    # a broad glob declaration overlaps a sensitive path
    ({}, PlanTask(files=[".github/**"]), True),
    ({}, PlanTask(files=["src/pro/*.py"]), True),
    ({}, PlanTask(files=[".github/**/*.yml"]), True),
    ({}, PlanTask(files=["src/**/session.py"]), True),
    # plan_first and plan resolve together, local > shared > user
    ({"shared": {"plan_first": True}, "user": {"plan": "never"}}, LIGHT, True),
    ({"local": {"plan_first": False}, "shared": {"plan": "always"}}, PlanTask(weight="heavy"), False),
])
def test_plan_approvals_auto_always_never(tmp_path, layers, task, expected):
    (tmp_path / ".brindle").mkdir()
    for layer, name in (("shared", config.CONFIG_FILE), ("local", config.LOCAL_CONFIG_FILE)):
        (tmp_path / ".brindle" / name).write_text(json.dumps(layers.get(layer, {})))
    for key, value in layers.get("user", {}).items():
        config.set_user(key, value)
    assert needs_plan(task, config=load_repo_config(tmp_path)) is expected


@pytest.mark.parametrize("how", ["immediate", "queued", "queued_same_branch"])
def test_plan_approvals_dependency_in_another_repo_plans_first(db, tmp_path, monkeypatch, how):
    """A light task depending on a task in an attached repo gets a plan, via real dispatch."""
    def workspace(name):
        root = str(tmp_path / name)
        ws = Workspace(name, root, name, "main", "main", "main", root, None, "", 1)
        db.add_workspace(ws)
        return ws

    planned = []

    def fake_spawn(db, ws, profile, *, prompt=None, parent_id=None, mode="assign", plan_first=False, **kw):
        planned.append(plan_first)
        a = Agent(agents.new_id(), ws.id, profile, "claude", parent_id, mode, "processing", "", None,
                  time.time(), task=prompt)
        db.add_agent(a)
        return a

    app, engine = workspace("app"), workspace("engine")
    boss = fake_spawn(db, app, "supervisor", mode="interactive")
    planned.clear()
    db.add_session_repo(boss.id, "engine", engine.repo_root)
    upstream = fake_spawn(db, engine, "developer", parent_id=boss.id)
    tasks.record_started(db, engine, upstream, "developer", "dep", "assign", isolate=True,
                         branch="dep", done_when=None, files=None, depends_on=None)
    planned.clear()
    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "model_problem", lambda *a: None)
    monkeypatch.setattr("brindle.identity_lock.ensure_workspace", lambda *a: None)
    monkeypatch.setattr("brindle.events.emit", lambda *a, **kw: None)

    if how != "immediate":   # the same-branch task must not shadow its own upstream
        t = tasks.enqueue(db, boss, app, "developer", "light job", "assign", isolate=False,
                          branch="dep" if how == "queued_same_branch" else None, done_when=None,
                          files=["src/app/util.py"], weight="light",
                          depends_on=["dep" if how == "queued_same_branch" else upstream.id])
        tasks.start_queued(db, t)
    else:
        agents.delegate(db, boss, app, "developer", "light job", "assign", isolate=False,
                        files=["src/app/util.py"], weight="light", depends_on=[upstream.id])
    assert planned == [True]

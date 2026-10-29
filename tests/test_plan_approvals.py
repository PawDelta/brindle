"""plan approvals: Auto / Always / Never -- when a worker must get its plan approved first."""

import json
import time

import pytest

from brindle import agents, config, tasks
from brindle.config import load_repo_config
from brindle.db import Agent, Workspace
from brindle.plan_approvals import Approver, may_approve
from brindle.plan_policy import OrgPlanRules, PlanTask, needs_plan

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
    # plan_first and plan resolve together, local > shared > user
    ({"shared": {"plan_first": True}, "user": {"plan": "never"}}, LIGHT, True),
    ({"local": {"plan_first": False}, "shared": {"plan": "always"}}, PlanTask(weight="heavy"), False),
    # the org's rules: always / never replace the local auto heuristics, plan_paths always plan
    ({"org": {"plan": "always"}, "local": {"plan": "never"}}, LIGHT, True),
    ({"org": {"plan": "never"}}, PlanTask(weight="heavy"), False),
    ({"org": {"plan": "never"}, "local": {"plan": "always"}}, LIGHT, True),
    ({"org": {"plan_paths": ["src/app/**"]}, "local": {"plan": "never"}}, LIGHT, True),
    ({"org": {"plan": "auto"}}, PlanTask(cross_repo=True), True),
])
def test_plan_approvals_auto_always_never(tmp_path, layers, task, expected):
    (tmp_path / ".brindle").mkdir()
    for layer, name in (("shared", config.CONFIG_FILE), ("local", config.LOCAL_CONFIG_FILE)):
        (tmp_path / ".brindle" / name).write_text(json.dumps(layers.get(layer, {})))
    for key, value in layers.get("user", {}).items():
        config.set_user(key, value)
    org = OrgPlanRules.from_policy(layers["org"]) if "org" in layers else None
    assert needs_plan(task, config=load_repo_config(tmp_path), org_rules=org) is expected


def _rules(**kw):
    return OrgPlanRules.from_policy({"plan_" + k: v for k, v in kw.items()})


@pytest.mark.parametrize("rules, approver, plan, so_far, allowed", [
    (_rules(approvers=["alice"]), Approver("alice"), LIGHT, [], True),
    (_rules(approvers=["alice"]), Approver("bob"), LIGHT, [], False),
    (_rules(two_person=True), Approver("alice"), LIGHT, [], False),
    (_rules(two_person=True), Approver("bob"), LIGHT, ["alice"], True),
    (_rules(two_person=True), Approver("alice"), LIGHT, ["alice"], False),
    (_rules(reviewer_may_approve_low_risk=True), Approver("r1", reviewer=True), LIGHT, [], True),
    (_rules(reviewer_may_approve_low_risk=True), Approver("r1", reviewer=True),
     PlanTask(weight="light", files=["deploy/x.yaml"]), [], False),
    (_rules(reviewer_may_approve_low_risk=True), Approver("r1", reviewer=True), PlanTask(weight="heavy"), [], False),
    (None, Approver("anyone"), PlanTask(weight="heavy"), [], True),
])
def test_plan_approvals_may_approve(rules, approver, plan, so_far, allowed):
    d = may_approve(rules, approver, plan, so_far)
    assert d.allowed is allowed
    if not allowed and rules.approvers:
        assert "alice" in d.reason


@pytest.mark.parametrize("queued", [False, True])
def test_plan_approvals_dependency_in_another_repo_plans_first(db, tmp_path, monkeypatch, queued):
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

    if queued:
        t = tasks.enqueue(db, boss, app, "developer", "light job", "assign", isolate=False, branch=None,
                          done_when=None, files=["src/app/util.py"], depends_on=[upstream.id], weight="light")
        tasks.start_queued(db, t)
    else:
        agents.delegate(db, boss, app, "developer", "light job", "assign", isolate=False,
                        files=["src/app/util.py"], weight="light", depends_on=[upstream.id])
    assert planned == [True]

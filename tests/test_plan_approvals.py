"""plan approvals: Auto / Always / Never -- when a worker must get its plan approved first."""

import json

import pytest

from brindle.config import load_repo_config
from brindle.plan_policy import PlanTask, needs_plan


@pytest.mark.parametrize("conf, task, expected", [
    ({"plan": "always"}, PlanTask(), True),
    ({"plan": "never"}, PlanTask(weight="heavy", cross_repo=True), False),
    ({}, PlanTask(weight="heavy"), True),
    ({}, PlanTask(cross_repo=True), True),
    ({}, PlanTask(files=[".github/workflows/ci.yml"]), True),
    ({}, PlanTask(files=["src/app/migrations/0002_x.py"]), True),
    ({}, PlanTask(files=["src/app/auth/session.py"]), True),
    ({}, PlanTask(files=["deploy/aws/stack.yaml"]), True),
    ({}, PlanTask(weight="light", files=["src/app/util.py"]), False),
    ({"plan_first": True}, PlanTask(), True),
    ({"plan_first": False}, PlanTask(weight="heavy"), False),
])
def test_plan_approvals_auto_always_never(tmp_path, conf, task, expected):
    (tmp_path / ".brindle").mkdir()
    (tmp_path / ".brindle" / "config.json").write_text(json.dumps(conf))
    assert needs_plan(task, config=load_repo_config(tmp_path)) is expected

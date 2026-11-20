import subprocess
import time

import pytest

from brindle import agents, intent, mcp_server, plans, visual_offers
from brindle.daemon import holds
from brindle.db import Agent, Workspace

PLAN = "# Fixes\n\nOne page.\n\n## Goal\n\nFix them all.\n\n## Split\n\nTwo workers."


def model(answer):
    """A fake model client that answers ``answer`` (or raises it) and records each prompt."""
    calls = []

    def ask(prompt, budget):
        calls.append(prompt)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    ask.calls = calls
    return ask


@pytest.mark.parametrize("text,earlier,answer,expected", [
    # The owner's refused asks. The model reads the person's earlier words: "one" is the plan they just named.
    pytest.param("ok so lets test the plan, please make a test one", [], "plan_request", intent.PLAN,
                 marks=pytest.mark.variant("refused ask the owner reported: the regexes miss it, the model finds it")),
    pytest.param("please make a test one", ["ok so lets test the plan"], "plan_request", intent.PLAN,
                 marks=pytest.mark.variant("the plan is named only in an earlier message: context must reach the call")),
    pytest.param("ok then build me a fucking planning doc", [], "none", intent.PLAN,
                 marks=pytest.mark.variant("refused ask the owner reported: the regex must win over a model saying none")),
    pytest.param("now lets make a plan to execute all these fixes", [], "none", intent.PLAN,
                 marks=pytest.mark.variant("refused ask the owner reported, with a purpose clause after the plan")),
    pytest.param("the plan looks good", [], "none", intent.NONE,
                 marks=pytest.mark.variant("a comment on the plan is not an ask")),
    # A cancel or negation wins, whatever the model says.
    ("don't write a plan", [], "plan_request", intent.CANCEL),
    ("we really need no planning doc for this one", [], "plan_request", intent.NONE),
    pytest.param("ignore", ["make a plan for the fixes"], "cancel", intent.CANCEL,
                 marks=pytest.mark.variant("a bare cancel word only the model recognizes, after a plan was named")),
    pytest.param("ignore", [], "plan_request", intent.NONE,
                 marks=pytest.mark.variant("nothing named in the message or its context: no call, so no wrong ask")),
])
def test_plan_ask_intent(text, earlier, answer, expected):
    assert intent.classify(text, earlier, ask=model(answer)) == expected


@pytest.mark.parametrize("failure", [
    subprocess.TimeoutExpired(["claude"], 1),
    pytest.param("the plan, I think", marks=pytest.mark.variant("a reply that isn't one of the four answers")),
])
def test_a_failed_model_call_falls_back_to_the_regex_result_and_is_asked_again(failure):
    text, earlier = "please make a test one", ["ok so lets test the plan"]
    assert intent.classify(text, earlier, ask=model(failure)) == intent.regex_intent(text) == intent.NONE
    working = model("plan_request")
    assert intent.classify(text, earlier, ask=working) == intent.PLAN
    assert intent.classify(text, earlier, ask=working) == intent.PLAN
    assert len(working.calls) == 1                          # one call per message


@pytest.fixture
def session(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")
    monkeypatch.setattr(holds, "app_connected", lambda: True)
    monkeypatch.setattr(agents, "is_alive", lambda agent, panes=None: True)
    ws = Workspace("ws1", str(tmp_path), "main", "main", "main", None, str(tmp_path), None, "brindle-ws1", time.time())
    db.add_workspace(ws)
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "%1", None, time.time(),
                 headless=1)
    db.add_agent(boss)
    return boss


@pytest.mark.variant("end to end: a model-found ask opens the plan, and non-person text never reaches the model")
def test_follow_up_ask_opens_the_plan_and_deliveries_never_reach_the_model(db, session, monkeypatch):
    fake = model("plan_request")
    monkeypatch.setattr(intent, "_ask", fake)
    # A prompt holding a brindle delivery: words that aren't the person's are never sent, nor kept as context.
    visual_offers.person_spoke(db, session, "[worker] the plan doc is ready to build", cli_prompt=True,
                               carries_delivery=True)
    assert fake.calls == [] and plans.holding(db, "boss") is None
    visual_offers.person_spoke(db, session, "ok so lets test the plan")
    visual_offers.person_spoke(db, session, "please make a test one")
    assert plans.holding(db, "boss") is not None            # the ask opened its plan
    assert "ok so lets test the plan" in fake.calls[-1] and "worker" not in fake.calls[-1]


def test_supervisor_revises_its_open_plan_without_a_new_ask(db, session):
    visual_offers.person_spoke(db, session, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    # r1 spent the person's ask; filling the template's empty sections is a revision, not a new doc.
    assert mcp_server.propose_plan(PLAN + "\n\n## Risks\n\nNone.", plan_id=pid).split()[2] == pid
    assert plans.get(db, pid)["rev"] == 2

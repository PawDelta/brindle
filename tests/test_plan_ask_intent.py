import subprocess
import time

import pytest

from brindle import agents, intent, mcp_server, plans, visual_offers
from brindle.daemon import holds
from brindle.db import Agent, Workspace
from brindle.mcp_server import ToolError

PLAN = "# Fixes\n\nOne page.\n\n## Goal\n\nFix them all.\n\n## Split\n\nTwo workers."


def model(answer, meanwhile=None):
    """A fake model client that answers ``answer`` (or raises it) and records each prompt.
    ``meanwhile()`` runs while the call is out, before it answers."""
    calls = []

    def ask(prompt, budget):
        calls.append(prompt)
        if meanwhile:
            meanwhile()
        if isinstance(answer, BaseException):
            raise answer
        return answer

    ask.calls = calls
    return ask


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


def say(db, boss, *texts):
    for text in texts:
        visual_offers.person_spoke(db, boss, text, typed=True)


def proposed(plan=PLAN) -> bool:
    try:
        mcp_server.propose_plan(plan)
    except ToolError as e:
        assert "ask for one" in str(e)
        return False
    return True


@pytest.mark.parametrize("said,answer,granted", [
    # The owner's refused asks: the regexes miss the first, so the model is asked when the plan is proposed.
    pytest.param(["ok so lets test the plan, please make a test one"], "1", True,
                 marks=pytest.mark.variant("refused ask the owner reported: the regexes miss it, the model grants it")),
    pytest.param(["ok so lets test the plan", "please make a test one"], "2", True,
                 marks=pytest.mark.variant("the plan is named only in an earlier message")),
    pytest.param(["ok then build me a fucking planning doc"], "0", True,
                 marks=pytest.mark.variant("refused ask the owner reported: the regexes grant it, no model needed")),
    pytest.param(["now lets make a plan to execute all these fixes"], "0", True,
                 marks=pytest.mark.variant("refused ask the owner reported, with a purpose clause after the plan")),
    ("the plan looks good", "0", False),
    # A cancel or a negation wins, whatever the model says.
    pytest.param(["don't write a plan"], "1", False, marks=pytest.mark.variant("a regex cancel keeps no words to grant")),
    (["we really need no planning doc for this one"], "1", False),
    pytest.param(["ok so lets test the plan, please make a test one with no external dependencies"], "1", True,
                 marks=pytest.mark.variant("a constraint in the ask is not a negation of it")),
    pytest.param(["ignore"], "1", False, marks=pytest.mark.variant("nothing named: no call, so no wrong grant")),
])
def test_plan_ask_intent(db, session, monkeypatch, said, answer, granted):
    monkeypatch.setattr(intent, "_ask", model(answer))
    say(db, session, *([said] if isinstance(said, str) else said))
    assert proposed() is granted


@pytest.mark.parametrize("failure,takes", [
    (subprocess.TimeoutExpired(["claude"], 3), 0.0),
    pytest.param("the plan, I think", 0.0, marks=pytest.mark.variant("a reply that isn't a message number")),
    pytest.param("1", intent.BUDGET_S + 0.5, marks=pytest.mark.variant("a right answer past the deadline")),
])
def test_a_failed_model_call_refuses_and_the_next_publish_asks_again(db, session, monkeypatch, failure, takes):
    clock = [100.0]
    monkeypatch.setattr(intent, "_clock", lambda: clock[0])
    say(db, session, "ok so lets test the plan, please make a test one")
    monkeypatch.setattr(intent, "_ask", model(failure, meanwhile=lambda: clock.__setitem__(0, clock[0] + takes)))
    assert not proposed()                                   # refused as without the model
    working = model("1")
    monkeypatch.setattr(intent, "_ask", working)
    assert proposed() and len(working.calls) == 1


@pytest.mark.variant("a message the model granted from must not grant a second plan")
def test_one_message_grants_one_plan(db, session, monkeypatch):
    gullible = model("1")
    monkeypatch.setattr(intent, "_ask", gullible)
    say(db, session, "ok so lets test the plan, please make a test one")
    assert proposed()
    pid = plans.holding(db, "boss")["id"]
    plans.decide(db, pid, 1, True, person=True)
    assert not proposed()                                   # the message was spent on the first plan
    assert len(gullible.calls) == 1


@pytest.mark.parametrize("newer", [
    pytest.param("never mind, drop the plan",
                 marks=pytest.mark.variant("a cancel while the call is out: the asking message is gone")),
    pytest.param("we really need no planning doc for this one",
                 marks=pytest.mark.variant("the asking message stays: only the turn check refuses the old answer")),
])
def test_a_message_sent_while_the_model_is_out_voids_its_answer(db, session, monkeypatch, newer):
    say(db, session, "ok so lets test the plan, please make a test one")
    # The person writes again while the model is still reading their ask.
    monkeypatch.setattr(intent, "_ask", model("1", meanwhile=lambda: say(db, session, newer)))
    assert not proposed()


class _AfterCandidates:
    """The DB connection, with ``hook(conn)`` run once right after the candidate messages are read."""

    def __init__(self, conn, hook):
        self._conn, self._hook = conn, hook

    def execute(self, sql, *args):
        cursor = self._conn.execute(sql, *args)
        if self._hook and sql.lstrip().startswith("SELECT id, words FROM visual_person_words"):
            rows = cursor.fetchall()
            hook, self._hook = self._hook, None
            hook(self._conn)
            return _Rows(rows)
        return cursor

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _Rows(list):
    def fetchall(self):
        return list(self)


@pytest.mark.variant("a race: a message between reading the candidates and the turn (fails on the pre-fix order)")
def test_a_message_recorded_after_the_candidates_are_read_voids_the_answer(db, session, monkeypatch):
    say(db, session, "ok so lets test the plan, please make a test one")

    def newer_message(conn):
        # Recorded the way person_spoke records one, after the call's messages were taken but before
        # a turn read that follows them: the model never sees it, so its turn must still count.
        visual_offers.take_turn(conn, "boss")
        visual_offers._keep_words(conn, "boss", "we really need no planning doc for this one")

    monkeypatch.setattr(intent, "_ask", model("1"))
    monkeypatch.setattr(db, "conn", _AfterCandidates(db.conn, newer_message))
    assert not intent.grant_from_recent(db, "boss", "plan")


@pytest.mark.parametrize("cancel,closes", [
    pytest.param("ignore", True, marks=pytest.mark.variant(
        "a bare cancel must close the standing plan draft, not only drop its grant")),
    pytest.param("drop the chart", False, marks=pytest.mark.variant("calling off a chart must spare the plan")),
])
def test_a_cancel_calls_off_the_plan_it_finds_standing(db, session, cancel, closes):
    say(db, session, "Make a planning doc.")
    assert plans.holding(db, "boss")["state"] == "writing"
    say(db, session, cancel)
    assert (plans.holding(db, "boss") is None) is closes    # its draft no longer holds work
    assert proposed() is not closes                         # and a proposal needs a new ask


@pytest.mark.variant("non-person text must never reach the model")
def test_only_typed_person_words_reach_the_model(db, session, monkeypatch):
    fake = model("1")
    monkeypatch.setattr(intent, "_ask", fake)
    visual_offers.person_spoke(db, session, "[worker] the plan doc is ready to build", cli_prompt=True,
                               carries_delivery=True)
    say(db, session, "ok so lets test the plan, please make a test one")
    assert proposed() and "worker" not in fake.calls[0] and "test one" in fake.calls[0]


def test_supervisor_revises_its_open_plan_without_a_new_ask(db, session):
    say(db, session, "Make a planning doc.")
    pid = mcp_server.propose_plan(PLAN).split()[2]
    # r1 spent the person's ask; filling the template's empty sections is a revision, not a new doc.
    assert mcp_server.propose_plan(PLAN + "\n\n## Risks\n\nNone.", plan_id=pid).split()[2] == pid
    assert plans.get(db, pid)["rev"] == 2

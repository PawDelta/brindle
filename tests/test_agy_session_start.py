"""A new Antigravity session's start-up prompts (its warm-up, then its task)
are typed in once agy draws its input box (agents.ready), through the real
delivery code, into one fake agy pane."""

import time
from types import SimpleNamespace

import pytest

from brindle import agents, antigravity, tmux, workspaces
from brindle.db import Agent

_REAL_PANE_INPUT = tmux.pane_input   # before conftest's nobody_typing
MODE_HINT = "Accept-edits mode: file edits auto-approved (shift+tab to cycle)"


@pytest.fixture
def agy(db, repo, monkeypatch):
    """Agent g1, a new agy supervisor with its warm-up and task queued, in
    pane %9 drawn as agy 1.5.9 draws it. Keys typed into it (brindle's or the
    person's, ``pane["undrawn"]``) show from the screen read after next.
    ``pane["clients"]``: (activity, created) of each tmux client on it."""
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("g1", ws.id, "supervisor", "antigravity", None, "interactive", "starting", "%9",
                       None, time.time()))
    warmup = antigravity.warmup("You are a supervisor.")
    db.enqueue("g1", warmup, None)
    db.enqueue("g1", "THE TASK", None)
    pane = {"composer": "", "undrawn": "", "buffer": "", "clients": [], "submitted": [], "detached": [],
            "warmup": warmup}

    def screen():
        shown, footer = (pane["composer"], "") if pane["composer"] else (MODE_HINT, "? for shortcuts")
        return f"{'─' * 60}\n> {shown}\n{'─' * 60}\n{footer}   accept-edits · Gemini 4 Carbon · medium\n"

    def tmux_call(*args, input=None, authorize=None, **k):
        if authorize is not None:
            authorize()
        out = ""
        if args[0] == "list-clients":   # -F <format>, as real tmux fills it in
            out = "".join(args[2].replace("#{pane_id}", "%9").replace("#{client_activity}", str(int(a)))
                          .replace("#{client_created}", str(int(c))) + "\n" for a, c in pane["clients"])
        elif args[0] == "capture-pane":
            out = screen()
            pane["composer"] += pane["undrawn"]
            pane["undrawn"] = ""
        elif args[0] == "load-buffer":
            pane["buffer"] = input
        elif args[0] == "send-keys" and "-l" in args:
            pane["undrawn"] += args[-1]
        elif args[0] == "paste-buffer":
            pane["composer"] += pane["undrawn"] + pane["buffer"]
            pane["undrawn"] = ""
        elif args[0] == "send-keys" and args[-1] == "Enter":
            pane["submitted"].append(pane["composer"])
            pane["composer"] = ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(tmux, "pane_input", _REAL_PANE_INPUT)
    monkeypatch.setattr(tmux, "_tmux", tmux_call)
    monkeypatch.setattr(agents.processes, "detach", lambda argv, **k: pane["detached"].append(argv))
    return pane


@pytest.mark.parametrize("typed", [False, True])
def test_new_agy_session_gets_its_warmup_once_a_just_attached_viewer_is_quiet(db, agy, typed):
    """The app opens a viewer on the new pane as agy becomes ready: tmux
    starts its activity at its creation, the same second a key pressed at once
    would show, so the start-up delivery is held either way. It once stayed
    queued with nothing to deliver it, the session idle without its task."""
    now = time.time()
    agy["clients"] = [(now, now)]
    if typed:
        agy["undrawn"] = "MY REPLY"   # pressed as it attached, not drawn yet

    agents.ready(db, "g1")   # after_launch saw agy's input box

    assert agy["submitted"] == [] and db.pending_count("g1") == 2
    flush = agy["detached"][-1]
    assert flush[-4:-2] == ["_flush", "g1"] and float(flush[-1]) > agents.TYPED_WITHIN

    agy["clients"] = [(now - float(flush[-1]), now - float(flush[-1]))]   # that flush runs, input quiet since
    agents.flush(db, "g1")

    if typed:   # their reply holds the queue; nothing lands on it
        assert agy["submitted"] == [] and agy["composer"] == "MY REPLY" and db.pending_count("g1") == 2
    else:
        lead = agents.message_lead(db, db.get_agent("g1"), None)
        assert agy["submitted"] == [f"{lead} {agy['warmup']}"] and db.pending_count("g1") == 1


def test_new_agy_session_gets_its_warmup_when_agy_draws_the_lead_late(db, agy):
    """agy draws a burst of typed keys a moment after tmux sends them: the
    screen read right after brindle types its lead still shows the empty
    composer. That once stopped the delivery with the lead stranded in the
    composer, where every later flush took it for the person's draft."""
    lead = agents.message_lead(db, db.get_agent("g1"), None)

    agents.ready(db, "g1")

    assert agy["submitted"] == [f"{lead} {agy['warmup']}"] and db.pending_count("g1") == 1

"""A new Antigravity session's start-up prompts (its warm-up, then its task)
are typed in once agy draws its input box. A viewer the app opens on the new
pane at that moment is a tmux client with no input yet; tmux starts its
activity at its creation, which once read as the person typing and left the
prompts queued, the session sitting idle without its task."""

import time
from types import SimpleNamespace

import pytest

from brindle import agents, antigravity, tmux, workspaces
from brindle.db import Agent

_REAL_PANE_INPUT = tmux.pane_input   # before conftest's nobody_typing

# agy 1.5.9's ready screen, as tmux capture-pane -e shows it (accept-edits mode).
AGY_READY = (
    "  \x1b[1m\x1b[38;5;111mAntigravity CLI 1.5.9\x1b[0m\n"
    "  \x1b[38;5;110mGemini 4 Carbon (Medium)\x1b[39m\n\n"
    "\x1b[38;5;239m" + "─" * 80 + "\n"
    "\x1b[38;5;111m>\x1b[39m \x1b[38;5;110mAccept-edits mode: file edits auto-approved (shift+tab to cycle)\x1b[39m\n"
    "\x1b[38;5;239m" + "─" * 80 + "\n"
    "\x1b[38;5;110m? for shortcuts\x1b[39m" + " " * 40
    + "\x1b[38;5;149maccept-edits\x1b[38;5;110m · Gemini 4 Carbon · medium\n"
)


@pytest.mark.parametrize("viewer_typed", [False, True])
def test_new_agy_session_gets_its_warmup_when_a_viewer_just_attached(db, repo, monkeypatch, viewer_typed):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("g1", ws.id, "supervisor", "antigravity", None, "interactive", "starting", "%9",
                       None, time.time()))
    warmup = antigravity.warmup("You are a supervisor.")
    db.enqueue("g1", warmup, None)
    db.enqueue("g1", "THE TASK", None)

    now = int(time.time())
    # The app's viewer, created as agy became ready; or one that attached
    # earlier, where the person has just pressed a key.
    attached = now - 3 if viewer_typed else now
    activity = now

    def tmux_call(*args, **k):   # tmux list-clients -F <format>, for that one client
        assert args[:2] == ("list-clients", "-F")
        row = (args[2].replace("#{pane_id}", "%9").replace("#{client_activity}", str(activity))
               .replace("#{client_created}", str(attached)))
        return SimpleNamespace(returncode=0, stdout=row + "\n", stderr="")

    pasted = []
    monkeypatch.setattr(tmux, "pane_input", _REAL_PANE_INPUT)
    monkeypatch.setattr(tmux, "_tmux", tmux_call)
    monkeypatch.setattr(tmux, "capture", lambda *a, **k: AGY_READY)
    monkeypatch.setattr(tmux, "paste", lambda target, text, **k: pasted.append(text))

    agents.ready(db, "g1")   # after_launch saw agy's input box

    if viewer_typed:   # their keys still hold the queue back
        assert pasted == [] and db.pending_count("g1") == 2
    else:
        assert pasted == [warmup] and db.pending_count("g1") == 1   # the task follows the warm-up's turn


def test_new_agy_session_gets_its_warmup_when_agy_draws_the_lead_late(db, repo, monkeypatch):
    """agy draws a burst of typed keys a moment after tmux sends them: the
    screen read right after brindle types its lead still shows the empty
    composer. That once stopped the delivery with the lead stranded in the
    composer, where every later flush took it for the person's draft."""
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("g1", ws.id, "supervisor", "antigravity", None, "interactive", "starting", "%9",
                       None, time.time()))
    warmup = antigravity.warmup("You are a supervisor.")
    db.enqueue("g1", warmup, None)
    db.enqueue("g1", "THE TASK", None)
    pane = {"composer": "", "undrawn": "", "buffer": ""}
    submitted = []

    def screen():
        shown = pane["composer"] or "Accept-edits mode: file edits auto-approved (shift+tab to cycle)"
        footer = "" if pane["composer"] else "? for shortcuts"
        return f"{'─' * 60}\n> {shown}\n{'─' * 60}\n{footer}   accept-edits · Gemini 4 Carbon · medium\n"

    def tmux_call(*args, input=None, authorize=None, **k):   # one fake agy pane, %9
        if authorize is not None:
            authorize()
        out = ""
        if args[0] == "capture-pane":
            out = screen()
            pane["composer"] += pane["undrawn"]   # typed keys show from the next read on
            pane["undrawn"] = ""
        elif args[0] == "load-buffer":
            pane["buffer"] = input
        elif args[0] == "send-keys" and "-l" in args:
            pane["undrawn"] += args[-1]
        elif args[0] == "paste-buffer":
            pane["composer"] += pane["undrawn"] + pane["buffer"]
            pane["undrawn"] = ""
        elif args[0] == "send-keys" and args[-1] == "Enter":
            submitted.append(pane["composer"])
            pane["composer"] = ""
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(tmux, "pane_input", _REAL_PANE_INPUT)
    monkeypatch.setattr(tmux, "_tmux", tmux_call)
    lead = agents.message_lead(db, db.get_agent("g1"), None)

    agents.ready(db, "g1")   # after_launch saw agy's input box

    assert submitted == [f"{lead} {warmup}"] and db.pending_count("g1") == 1

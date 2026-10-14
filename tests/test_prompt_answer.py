"""A startup prompt no provider regex knows is answered by what it asks
(brindle.prompt_answer): folder trust is, a sign-in never is (its supervisor
is told once), and a screen that isn't a prompt gets no keypress."""

from __future__ import annotations

import json
import time

import pytest

from brindle import agents, cull, prompt_answer, tmux, workspaces
from brindle.config import worktrees_dir
from brindle.db import Agent

from test_reliability import CLAUDE_BUSY


class Pane:
    """An agy pane showing one menu; Enter picks the option under the cursor."""

    def __init__(self, question: str, options: list[str]):
        self.question, self.options = question, options
        self.cursor, self.keys, self.chosen = 0, [], None

    def render(self) -> str:
        if self.chosen is not None:
            return "? for shortcuts\n"
        menu = [f"{'>' if i == self.cursor else ' '} {o}" for i, o in enumerate(self.options)]
        return "\n".join([self.question, "", *menu]) + "\n"

    def press(self, target, *keys, **_):
        for key in keys:
            self.keys.append(key)
            if key == "Down":
                self.cursor = min(self.cursor + 1, len(self.options) - 1)
            elif key == "Up":
                self.cursor = max(self.cursor - 1, 0)
            elif key == "Enter":
                self.chosen = self.options[self.cursor]


@pytest.mark.parametrize("question,options,reply,keys,chosen,told", [
    # trust, in wording agy's TRUST_DIALOG doesn't match: the cursor starts on
    # "Not now", so it moves down and presses Enter only once "Allow" is selected
    ("Antigravity would like to open this workspace:\n{path}",
     ["Not now", "Allow this folder"], {"kind": "trust", "option": "Allow this folder"},
     ["Down", "Enter"], "Allow this folder", 0),
    # a sign-in is the person's, whatever the model says
    ("Sign in to Antigravity to continue",
     ["Continue with Google", "Use a key file"], {"kind": "trust", "option": "Continue with Google"},
     [], None, 1),
    ("Connecting MCP servers", ["brindle", "github"], {"kind": "not_a_prompt", "option": ""},
     [], None, 0),
])
def test_unrecognized_startup_prompt(db, repo, monkeypatch, question, options, reply, keys, chosen, told):
    folder = str(worktrees_dir() / "proj" / "feature")
    pane = Pane(question.format(path=folder), options)
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(tmux, "capture", lambda target, **k: pane.render() if target == "%w1" else CLAUDE_BUSY)
    monkeypatch.setattr(tmux, "send_keys", pane.press)
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    asked = []
    monkeypatch.setattr(prompt_answer, "_ask", lambda prompt: asked.append(prompt) or json.dumps(reply))

    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "%boss",
                       None, clock[0]))
    db.add_agent(Agent("w1", ws.id, "developer", "antigravity", "boss", "assign", "starting", "%w1",
                       None, clock[0] - 600, status_since=clock[0] - 600))

    cull.note_stuck(db, clock[0], {})
    cull.note_stuck(db, clock[0] + cull.STUCK_AFTER + 1, {})

    assert pane.keys == keys and pane.chosen == chosen
    assert db.pending_count("boss") == told
    assert all(folder not in p for p in asked)  # folder paths never leave the machine

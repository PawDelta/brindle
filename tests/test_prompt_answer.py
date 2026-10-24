"""A startup prompt no provider regex knows is answered by what it asks
(brindle.prompt_answer): folder trust is, a sign-in never is (its supervisor
is told once), a screen that isn't a prompt gets no keypress, and a tool
permission prompt is left to the permission policy."""

from __future__ import annotations

import json
import time

import pytest

from brindle import agents, cull, prompt_answer, tmux, workspaces
from brindle.config import worktrees_dir
from brindle.db import Agent

from test_reliability import BOX, CLAUDE_BUSY

TRUST = "Antigravity would like to open this workspace:\n{path}"


class Pane:
    """A pane showing one menu; Enter picks the option under the cursor.
    ``question`` may take {n}, the number of captures so far."""

    def __init__(self, question: str, options: list[str]):
        self.question, self.options = question, options
        self.cursor, self.keys, self.chosen, self.captures = 0, [], None, 0

    def render(self) -> str:
        if self.chosen is not None:
            return "? for shortcuts\n"
        self.captures += 1
        menu = [f"{'>' if i == self.cursor else ' '} {o}" for i, o in enumerate(self.options)]
        return "\n".join([self.question.format(n=self.captures), "", *menu]) + "\n"

    def press(self, target, *keys, **_):
        for key in keys:
            self.keys.append(key)
            if key == "Down":
                self.cursor = min(self.cursor + 1, len(self.options) - 1)
            elif key == "Up":
                self.cursor = max(self.cursor - 1, 0)
            elif key == "Enter":
                self.chosen = self.options[self.cursor]


@pytest.fixture
def launch(db, repo, monkeypatch):
    """launch(question, options, reply, ...) starts worker w1 on a fake pane
    and a fake model; returns (pane, the prompts the model was sent)."""
    clock = [1000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(tmux, "paste", lambda *a, **k: None)
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing", "%boss",
                       None, clock[0]))

    def start(question, options, reply, *, provider="antigravity", mode="assign", parent="boss",
              status="starting", during=None):
        pane = Pane(question.format(path=worktrees_dir() / "proj" / "feature", n="{n}"), options)
        monkeypatch.setattr(tmux, "capture", lambda target, **k: pane.render() if target == "%w1" else CLAUDE_BUSY)
        monkeypatch.setattr(tmux, "send_keys", pane.press)
        asked = []

        def ask(prompt):
            asked.append(prompt)
            if during:
                during(pane)
            return json.dumps(reply)

        monkeypatch.setattr(prompt_answer, "_ask", ask)
        db.add_agent(Agent("w1", ws.id, "developer", provider, parent, mode, status, "%w1", None, clock[0],
                           status_since=clock[0]))
        prompt_answer.launched(db, db.get_agent("w1"))
        return pane, asked

    start.clock = clock
    return start


def sweeps(db, clock, n=2):
    """``n`` sweeps' startup and stuck passes, each past the startup grace."""
    for i in range(n):
        now = clock[0] + cull.STARTUP_GRACE + cull.STUCK_AFTER * i + 1
        cull.settle_startups(db, now, {})
        cull.note_stuck(db, now, {})


@pytest.mark.parametrize("question,options,mode,status,screen,provider", [
    ("Which storage should I use?", ["1. SQLite", "2. Postgres"], "assign", "idle",
     "• Which storage should I use?\n\n  1. SQLite\n  2. Postgres\n\n› \n  97% context left", "codex"),
    pytest.param("Which storage should I use?", ["1. SQLite", "2. Postgres"], "assign", "idle",
                 "Which storage should I use?\n\n  1. SQLite\n  2. Postgres", "codex",
                 marks=pytest.mark.variant("numbered question without a composer footer")),
    ("Which storage should I use?", ["1. SQLite", "2. Postgres"], "assign", "idle",
     "⏺ Which storage should I use?\n\n  1. SQLite\n  2. Postgres\n\n────\n❯ \n────\n"
     "  ⏵⏵ accept edits on (shift+tab to cycle) · ← for agents\n", "claude"),
    pytest.param("Which storage should I use?", ["1. SQLite", "2. Postgres"], "assign", "idle",
                 "⏺ Read(schema.sql)\n  ⎿ Read 12 lines\n\n⏺ Which storage should I use?\n\n  1. SQLite\n  2. Postgres",
                 "claude", marks=pytest.mark.variant("completed Claude tool transcript before a plain question")),
    pytest.param("Hooks need review", ["1. Enable hooks", "2. Skip hooks"], "review", "starting", None, "codex",
                 marks=pytest.mark.variant("#250: reviewer startup menu")),
    pytest.param("Would you like to run the following command?\n$ make probe", ["1. Yes", "2. No"], "review", "processing", None, "codex",
                 marks=pytest.mark.variant("#250: reviewer command approval")),
])
def test_active_worker_menu_is_forwarded_immediately(db, launch, monkeypatch, question, options, mode, status, screen, provider):
    pane, asked = launch(question, options, {}, provider=provider, mode=mode, status=status)
    if screen is not None:
        monkeypatch.setattr(pane, "render", lambda: screen)
    monkeypatch.setattr(agents, "pane_owners", lambda *a: {})
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    assert cull.note_stuck(db, launch.clock[0], {})
    assert db.get_agent("w1").status == "waiting"
    [message] = db.conn.execute("SELECT body FROM inbox WHERE agent_id='boss' AND held=1").fetchall()
    assert question in message["body"]
    assert all(option in message["body"] for option in options)
    assert not cull.note_stuck(db, launch.clock[0], {})
    assert db.pending_count("boss") == 1
    assert not pane.keys and not asked


@pytest.mark.variant("busy, answered, and echoed transcript bypass attempts must not become pending questions")
@pytest.mark.parametrize("suffix,echo,provider", [
    ("\n• Working (esc to interrupt)\n› \n97% context left", False, "codex"),
    ("\n› Use SQLite\n• Implemented SQLite.\n› \n97% context left", False, "codex"),
    ("\n› \n97% context left", True, "codex"),
    ("\n" + CLAUDE_BUSY, False, "claude"),
    ("\n❯ Use SQLite\n⏺ Implemented SQLite." + BOX, False, "claude"),
    (BOX, True, "claude"),
    (BOX.replace("❯ ", "❯ Use SQLite"), False, "claude"),
])
def test_numbered_transcript_without_pending_question_is_not_forwarded(db, launch, monkeypatch, suffix, echo, provider):
    question = "Which storage should I use?\n\n1. SQLite\n2. Postgres"
    pane, _ = launch("", [], {}, provider=provider, status="idle")
    monkeypatch.setattr(pane, "render", lambda: question + suffix)
    if echo:
        db.update_agent("w1", task=question)
    assert not cull.note_stuck(db, launch.clock[0], {})
    assert db.get_agent("w1").status == "idle"
    assert db.pending_count("boss") == 0


@pytest.mark.parametrize("question,options,reply,keys,chosen,told", [
    # trust, in wording agy's TRUST_DIALOG doesn't match: the cursor starts on
    # "Not now", so it moves down and presses Enter only once "Allow" is selected
    (TRUST, ["Not now", "Allow this folder"], {"kind": "trust", "option": "Allow this folder"},
     ["Down", "Enter"], "Allow this folder", 0),
    # a sign-in is the person's, whatever the model says
    ("Sign in to Antigravity to continue",
     ["Continue with Google", "Use a key file"], {"kind": "trust", "option": "Continue with Google"},
     [], None, 1),
    # terms or a licence are the person's to accept: never a plain "Continue"
    ("By continuing you agree to the Terms of Service and license agreement",
     ["Continue", "Exit"], {"kind": "acknowledge", "option": "Continue"}, [], None, 1),
    pytest.param("Terms of Service\n" + "\n".join(f"Notice {i}." for i in range(26)),
                 ["Continue", "Exit"], {"kind": "acknowledge", "option": "Continue"}, [], None, 1,
                 marks=pytest.mark.variant("terms above the 25 lines the model sees: a review finding")),
    ("Starting up", ["brindle", "github"], {"kind": "not_a_prompt", "option": ""}, [], None, 0),
    # a tool permission is the permission policy's (note_stuck reports it), never answered here
    ("Requesting permission for read_file\n{path}/README.md", ["Allow", "Deny"],
     {"kind": "trust", "option": "Allow"}, [], None, 1),
    # the model only points at an option: one granting more is left for the person
    ("Some actions in this mode can't be undone.",
     ["No, exit", "Yes, I accept"], {"kind": "acknowledge", "option": "Yes, I accept"}, [], None, 1),
    # a trust "Yes" with words beyond trusting the folder (the screen can
    # carry repo text that steers the model to it)
    (TRUST, ["No", "Yes, and trust this project's servers"],
     {"kind": "trust", "option": "Yes, and trust this project's servers"}, [], None, 1),
    # a settings picker is only ever answered with its default
    ("Choose a colour scheme", ["Dark", "Light"], {"kind": "setting_choice", "option": "Light"}, [], None, 1),
    pytest.param("Choose how edits are handled", ["Always allow edits", "Ask each time"],
                 {"kind": "setting_choice", "option": "Always allow edits"}, [], None, 1,
                 marks=pytest.mark.variant("a settings default that grants permissions: a review finding")),
])
def test_unrecognized_startup_prompt(db, launch, question, options, reply, keys, chosen, told):
    pane, asked = launch(question, options, reply)
    prompt_answer.finish_launch(db, db.get_agent("w1"))
    sweeps(db, launch.clock)
    assert pane.keys == keys and pane.chosen == chosen
    assert db.pending_count("boss") == told
    assert all(str(worktrees_dir()) not in p for p in asked)  # folder paths never leave the machine


@pytest.mark.parametrize("provider,mode,parent,status", [
    ("antigravity", "assign", "boss", "processing"),   # an argv task starts 'processing'
    ("antigravity", "interactive", None, "idle"),      # a supervisor
    ("claude", "assign", "boss", "waiting"),           # a startup menu reads as 'waiting'
])
def test_sweep_answers_a_prompt_after_after_launch_gave_up(db, launch, provider, mode, parent, status):
    pane, _ = launch(TRUST, ["Not now", "Allow this folder"], {"kind": "trust", "option": "Allow this folder"},
                     provider=provider, mode=mode, parent=parent, status=status)
    sweeps(db, launch.clock, n=1)
    assert pane.chosen == "Allow this folder" and pane.keys == ["Down", "Enter"]
    assert prompt_answer.pending(db) == []


def test_sweep_leaves_codex_to_its_inflight_startup_helper(db, launch, monkeypatch):
    from test_codex_notify import CODEX_EMPTY_PANE
    pane, _ = launch("loading", [], {}, provider="codex")
    monkeypatch.setattr(pane, "render", lambda: CODEX_EMPTY_PANE)
    assert cull.settle_startups(db, launch.clock[0] + 41, {}) == []
    assert prompt_answer.pending(db)
    assert len(cull.settle_startups(db, launch.clock[0] + 161, {})) == 1
    assert prompt_answer.pending(db) == []


@pytest.mark.variant("bypass attempt: a menu the agent raises mid-task; the only test of the hook ending startup")
def test_a_menu_after_the_session_started_is_never_answered(db, launch):
    # A hook ran, so the session is going: a menu now is the agent's own
    # question, even though after_launch never saw the input box.
    pane, asked = launch(TRUST, ["Not now", "Allow this folder"],
                         {"kind": "trust", "option": "Allow this folder"}, status="processing")
    agents.handle_hook(db, "w1", "prompt-submit", {})
    sweeps(db, launch.clock)
    assert pane.keys == [] and asked == []


def test_model_calls_are_capped_per_launch_across_sweeps(db, launch):
    _, asked = launch("Loading {n}", ["step", "next"], {"kind": "not_a_prompt", "option": ""})
    prompt_answer.finish_launch(db, db.get_agent("w1"))
    sweeps(db, launch.clock, n=5)
    assert len(asked) == prompt_answer.MAX_CALLS


def test_the_delivered_brief_is_never_the_prompt(db, launch, monkeypatch):
    # The brief the CLI echoes as "❯ <brief>" with indented wrapped lines reads
    # as a menu, and its words (sign-in, API key) as a personal prompt.
    from brindle import launch_health

    brief = ("Review the changes on branch `fix/133-goal-hooks-off` and check that the sign-in flow "
             "and the API key handling are untouched, then report back.")
    launch(TRUST, ["Not now", "Allow this folder"], {"kind": "trust", "option": "Allow this folder"},
           provider="claude", status="processing")
    db.update_agent("w1", task="review")
    launch_health.prepare(db, db.get_agent("w1"), "fp", submitted_prompt=brief)
    screen = ("❯ Review the changes on branch `fix/133-goal-hooks-off` and check that the\n"
              "  sign-in flow and the API key handling are untouched, then report\n"
              "  back.\n\n"
              "· Percolating… (42s · ↓ 2.0k tokens)\n")
    monkeypatch.setattr(tmux, "capture", lambda target, **k: screen)
    prompt_answer.finish_launch(db, db.get_agent("w1"))
    assert db.pending_count("boss") == 0
    assert db.get_agent("w1").status != "waiting"


@pytest.mark.parametrize("spinner", [
    "",   # echoed, the turn not yet showing
    pytest.param("✻ Pondering…\n", marks=pytest.mark.variant("the captured screen from #203")),
])
def test_a_goal_brief_rendered_as_markdown_is_never_the_prompt(db, launch, monkeypatch, spinner):
    # Claude Code echoes a worker's /goal brief rendered as markdown (no
    # backticks), wrapped anywhere, and its footer names the MCP server.
    from brindle import launch_health

    brief = ("/goal Finish line: `uv run pytest tests/test_x.py -q` passes. The work is committed on branch "
             "`fix/x` and reported with the brindle report_result tool.\n\nTask:\nFix the login bug in "
             "`src/app.py`.\n\n---\nYou are running as a brindle worker (agent id w1) on branch `fix/x`.\n"
             "When you have finished:\n1. Commit your work to this branch with a clear message.\n"
             "2. Call the `report_result` tool from the `brindle` MCP server.")
    launch(TRUST, ["Not now", "Allow this folder"], {"kind": "trust", "option": "Allow this folder"},
           provider="claude", status="processing")
    db.update_agent("w1", task="fix")
    launch_health.prepare(db, db.get_agent("w1"), "fp", submitted_prompt=brief)
    screen = ("❯ /goal Finish line: uv run pytest tests/test_x.py -q passes. The work is committed on bran\n"
              "  ch fix/x and reported with the brindle report_result tool.\n\n"
              "  Task:\n  Fix the login bug in src/app.py.\n\n  ───\n"
              "  You are running as a brindle worker (agent id w1) on branch fix/x.\n"
              "  When you have finished:\n"
              "  1. Commit your work to this branch with a clear message.\n"
              "  2. Call the report_result tool from the brindle MCP server.\n\n"
              f"{spinner}\n────\n❯ \n────\n  /goal active\n")
    monkeypatch.setattr(tmux, "capture", lambda target, **k: screen)
    prompt_answer.finish_launch(db, db.get_agent("w1"))
    assert db.pending_count("boss") == 0
    assert db.get_agent("w1").status != "waiting"


def test_brief_not_prompt_when_its_start_has_scrolled_off(db, launch, monkeypatch):
    # Only the tail of a long brief is on screen: a numbered list, "press
    # enter" and key/budget words, with no first line to match it by.
    from brindle import launch_health

    brief = ("Intro paragraph that is long enough to scroll off the capture. "
             "Steps: 1. Run the unit tests first 2. Fix any failures you find 3. Press Enter to confirm each "
             "fix, mind the API key and token budget. Before you finish, check your work against the task.")
    launch(TRUST, ["Not now", "Allow this folder"], {"kind": "trust", "option": "Allow this folder"},
           provider="claude", status="processing")
    db.update_agent("w1", task="review")
    launch_health.prepare(db, db.get_agent("w1"), "fp", submitted_prompt=brief)
    screen = ("  Steps:\n"
              "  1. Run the unit tests first\n"
              "❯ 2. Fix any failures you find\n"
              "  3. Press Enter to confirm each fix, mind the API key and token budget.\n"
              "  Before you finish, check your work against the task.\n")
    monkeypatch.setattr(tmux, "capture", lambda target, **k: screen)
    prompt_answer.finish_launch(db, db.get_agent("w1"))
    cull.note_stuck(db, launch.clock[0], {})
    assert db.pending_count("boss") == 0
    assert db.get_agent("w1").status != "waiting"
    # The same menu-like lines above a busy spinner (timed or not yet) or a
    # tool call are a working agent, not a prompt.
    for working in ("✳ Clauding… (1m 37s)", "✻ Pondering…", "⏺ Read(src/app.py)"):
        assert not prompt_answer.looks_like_prompt(
            f"  1. Run the unit tests first\n❯ 2. Fix any failures you find\n\n{working}\n\n❯ \n")


@pytest.mark.variant("race: the selection moves during the model call; the only test of the re-capture")
def test_selection_is_checked_on_the_pane_as_it_is_after_the_model_call(db, launch):
    # The selection moved from Dark to Light while the model was deciding Dark.
    pane, _ = launch("Choose a colour scheme", ["Dark", "Light"], {"kind": "setting_choice", "option": "Dark"},
                     during=lambda p: setattr(p, "cursor", 1))
    prompt_answer.finish_launch(db, db.get_agent("w1"))
    assert "Enter" not in pane.keys and pane.chosen is None

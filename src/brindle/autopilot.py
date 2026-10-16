"""Autopilot: the supervisor as a project manager that keeps going.

A session with autopilot on has a goal broken into milestones. Each milestone
has a check command that brindle runs itself (exit 0 means done), so progress
is verified rather than claimed. The supervisor splits milestones into tasks,
hands them to workers, gets their branches reviewed and merged (through the
merge gates in ``gates``), and re-runs the checks.

When the supervisor stops while milestones are still unverified and no worker
is running, its Stop hook tells it to keep going. It stops pushing when:
- every milestone's check passes (the goal is done),
- the supervisor calls ``need_user`` (blocked on a decision only the user can
  make), until the user next types something,
- it has nudged MAX_NUDGES times in a row with no progress (stalled), or
- Claude usage is near its limit.

Every session has its own goal, never shared with another session. It comes
from the chat (the supervisor calls ``set_goal`` as soon as it delegates) and
lives in ``.brindle/goals/<session id>.md``, which ``set_goal`` writes and
``brindle continue`` reloads, so the person can read and edit it there. A
hand-written ``.brindle/goals.md`` is a one-time seed: the first new session
claims it (moves it to its own file), so no second session picks it up:

    # Settings page

    Users can change their name and email.

    ## Settings API
    check: uv run pytest tests/test_settings_api.py -q

    ## Settings UI
    check: npm test -- settings
    The form saves and shows errors inline.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from brindle.config import CONFIG_DIR, WEIGHTS, RepoConfig, config_root, load_repo_config
from brindle import escapes
from brindle.db import DB, Agent, Autopilot, Milestone, Workspace
from brindle.profiles import load_profile

log = logging.getLogger(__name__)

GOALS_FILE = "goals.md"
GOALS_DIR = "goals"          # .brindle/goals/<session id>.md: each session's own goal
MAX_NUDGES = 3
NUDGE_BACKOFF = 900.0        # seconds before the same reminder is repeated when nothing has changed
MAX_GOAL_CHARS = 4000        # Claude Code's limit for a /goal condition
OUTPUT_TAIL_LINES = 30

GUIDE = """

## Autopilot is on

You are this project's manager. Drive the goal to completion without waiting
to be asked each step.

- Size first, by the delegation rule. A request that rule has you do
  yourself needs no goal: do it directly, run the targeted tests, and report.
- The goal: as soon as you delegate work (before your first `assign` or
  `handoff`), record a goal derived from the user's request with `set_goal`,
  without asking: the request is the goal, its parts are 1-6 milestones. Ask
  the user what we're building only when there is no request at all. Each
  milestone has a `check` command brindle can run from the root of this
  checkout that exits 0 only when that milestone is done (e.g.
  `uv run pytest tests/test_settings.py -q`). A check must test what the
  milestone promises, not just that the suite passes: name the tests that
  cover its acceptance criteria. Tell the user the plan in a few lines, then
  start. If the session started with a goal, call `get_progress`.
- A later request in this session becomes new milestones on this session's
  goal: call `set_goal` with the existing milestones (unchanged, so they keep
  their results) plus the new ones. The goal is this session's alone; brindle
  keeps it in `.brindle/goals/<session id>.md`.
- Within a goal, the delegation rule decides which of a milestone's tasks you
  do yourself and which you `assign`.
- Work on the first unverified milestone. Split it into independent tasks and
  `assign` them to workers in parallel (at most {max_agents} at once). Give
  every task a `done_when` finish line the worker can verify itself. Pass
  `files` (the paths/globs each task will touch) so brindle can warn about
  overlaps, and `depends_on` (an earlier task's agent id or branch) when one
  task's work must merge before another starts; brindle queues it until then.
- Unless you need a specific profile, pass `weight` on every `assign`:
  "light" (small, well-specified, mechanical: docs, renames, simple tests),
  "medium" (normal feature work or bugfixes in one area),
  "heavy" (architecture design, broad cross-cutting refactors, or subtle bugs that failed earlier fix rounds), or
  "advanced" (complex, long-running system-design and implementation tasks). brindle picks an available
  profile for that tier.
- Keep task briefs short: the goal in a sentence or two, the files, and the
  test that proves it done. Workers read the tests and code themselves;
  never paste them. Writing is the slowest thing you do.
- When a worker reports, brindle has its branch reviewed and, once approved and
  the checks pass, merges it and removes the worktree; you get one message
  per branch: merged, or "needs you" with the details. Don't request_review
  or merge_workspace a reported branch yourself unless brindle says so.
- Messages reach you as one line each; read_messages(message_id=N) gives the
  full text only when you need it. Relay worker results to the user in one line.
  Reply to an agent with send_message(to_agent_id=<sender id>).
- After merging, call `check_milestone`. Only brindle's check marks a milestone
  done: never claim one is done yourself. When every milestone passes, brindle
  audits the work against the goal; the goal is reached only when that audit
  approves. Treat any gaps it finds as unfinished work, and tighten (with
  `set_goal`) the check that let each gap through.
- Keep going until every milestone passes. If you stop early, brindle will ask
  you to continue. A denied action, a stalled external job (a CI run no runner
  picks up) or a question only the user can answer blocks only the steps that
  depend on it: call `need_user` with the question, ask it, keep working the
  rest, and report the blocked item with it. Stop only when nothing else is
  left; once the user answers, call `need_user(answered=true)`.
- When a milestone passes, tell the user in one line.
- If a message says autopilot is off, stop driving: wait for the user's
  instructions.
"""

DELEGATIONS = ("conservative", "balanced", "fast")

# The repo's "delegation": how readily a supervisor hands work to workers.
# Delegating buys wall-clock time with tokens: a worker plus its review costs
# several times what doing the change in the supervisor's own chat does.
DELEGATION = {
    "conservative": """## Delegation rule: conservative (save tokens)

Delegating costs far more than doing: a worker plus its review takes about ten
times the tokens of making the same change yourself. Do directly anything you
can finish in one sitting within one area (a fix, a small feature, a doc
change), and never hand off code you've already read. Delegate only work that
is genuinely parallel (independent parts that touch different files) or long
(more than about 15 minutes of work).""",
    "balanced": """## Delegation rule: balanced

Delegating buys time with tokens: a worker plus its review costs several times
what the same change costs in your own chat. Do small things directly: a fix,
a change of a few files in one area, anything you'd finish in about five
minutes, or work whose code you've already read. Delegate when a request has
two or more independent parts that can run side by side, or a piece that
would take more than about ten minutes; give each part its own worker.""",
    "fast": """## Delegation rule: fast (save time)

The person prefers speed to token cost. Split any request with more than one
independent part into parallel workers straight away, up to the worker limit,
and delegate any single piece of more than a few minutes, so your own chat
stays free to plan, answer and merge. Do only trivial edits yourself (a typo,
a one-line config change), or what you must do before workers can start.""",
}


SUPERVISOR_RULES = """\
Acting on what you find:
- Before you offer the user options, a plan, a split or a sequence, validate
  each one: build it, run the tests, or read the code path, so it's known to
  work, and say how you checked. Don't offer an option you haven't validated
  next to one you have; validate it first, or say plainly you can't and ask.
- When a review, a check or a test turns up something that needs fixing,
  verify each finding (a reviewer can be wrong), fix the real ones, and report
  what you fixed and which findings weren't real. Don't ask first. Ask only
  before outward-facing or hard-to-reverse actions (posting, merging into a
  shared branch, deleting) and for decisions that are the user's to make.
- Once you've laid out a plan, start it in the same turn. Don't end with
  "shall I start?" or wait for a "yes" or "go": if a step needs a decision
  only the user can make, ask that question and carry on with the rest.
- To wait for workers, checks or anything else brindle runs, wait for
  brindle's messages (they wake you) or use the Monitor tool; don't poll with
  short cron loops, which spend turns and keep running while brindle has
  paused the session (an expired cloud login, say).
- Format your own verbose outputs or message transcripts in collapsible
  `<details><summary>Title</summary>...</details>` blocks so they are not
  sprawling in the chat UI."""


def supervisor_rules(cfg: RepoConfig) -> str:
    """The built-in rules for acting on findings, plus the person's standing
    ``rules`` from their brindle config (user, repo, local)."""
    text = SUPERVISOR_RULES
    if cfg.rules:
        text += ("\n\nStanding rules from the user (brindle config `rules`); follow them every time:\n"
                 + "\n".join(f"- {r}" for r in cfg.rules))
    return text


def delegation_rule(cfg: RepoConfig) -> str:
    level = cfg.delegation if cfg.delegation in DELEGATION else "balanced"
    return DELEGATION[level]


KICKOFF = (
    "[brindle autopilot] The goal in .brindle/goals.md is now this session's (kept in "
    ".brindle/goals/{root_id}.md): {goal} ({n} milestones). "
    "Call get_progress, tell me your plan in a few lines, then start working toward it."
)


class AutopilotError(RuntimeError):
    pass


# -- goals.md ---------------------------------------------------------------


@dataclass
class Plan:
    goal: str
    detail: str | None
    milestones: list[tuple]   # (title, check, detail[, profile[, repo]])


PROFILE_RE = re.compile(r"^\s*profile:\s*`?([\w.-]+)`?\s*$", re.I)
REPO_RE = re.compile(r"^\s*repo:\s*`?([\w.-]+)`?\s*$", re.I)
CHECK_RE = re.compile(r"^\s*check(?:@([\w.-]+))?:\s*`?(.+?)`?\s*$", re.I)
FENCED_CHECK_RE = re.compile(r"^\s*check(?:@([\w.-]+))?:\s*$", re.I)   # the command is the fenced block below
FENCE_RE = re.compile(r"^(`{3,})")


def _fences(lines: list[str]) -> dict[int, tuple[int, int]]:
    """Fenced blocks: opening line index -> (closing line index, index after
    the block). Nothing inside a block is a heading, field or status line."""
    out, i = {}, 0
    while i < len(lines):
        if m := FENCE_RE.match(lines[i]):
            close = re.compile(rf"^`{{{len(m.group(1))},}}\s*$")
            j = next((k for k in range(i + 1, len(lines)) if close.match(lines[k])), len(lines))
            out[i] = (j, min(j + 1, len(lines)))
            i = j + 1
        else:
            i += 1
    return out


def parse_goals(text: str) -> Plan | None:
    """``# Goal`` then ``## Milestone`` sections, each with an optional
    ``check: <command>`` line (``check@<alias>: <command>`` runs it in a repo
    attached to the session; so does a ``repo: <alias>`` line) and a
    ``profile: <name>`` line. A bare ``check:`` line followed by a fenced
    block takes the block as the command (any number of lines). A detail
    line starting with a backslash is that line without it, so detail can
    hold text that would otherwise read as a heading or a field. Returns None
    when there's no goal heading."""
    goal, detail_lines = None, []
    milestones: list[list] = []
    lines = text.splitlines()
    fences = _fences(lines)
    i = 0
    while i < len(lines):
        line = lines[i]
        detail = milestones[-1][2] if milestones else detail_lines
        if i in fences:   # a code block in the detail: kept verbatim
            detail.extend(lines[i:fences[i][1]])
            i = fences[i][1]
            continue
        i += 1
        if m := re.match(r"^#\s+(.+?)\s*$", line):
            if goal is None:
                goal = m.group(1)
            continue
        if m := re.match(r"^##\s+(.+?)\s*$", line):
            milestones.append([m.group(1), None, [], None, None])
            continue
        if milestones and STATUS_RE.match(line):
            continue   # written by sync_goals_file: information only, never read back
        if milestones and milestones[-1][3] is None and (m := PROFILE_RE.match(line)):
            milestones[-1][3] = m.group(1)
            continue
        if milestones and milestones[-1][4] is None and (m := REPO_RE.match(line)):
            milestones[-1][4] = m.group(1)
            continue
        if milestones and milestones[-1][1] is None and i in fences and (m := FENCED_CHECK_RE.match(line)):
            close, after = fences[i]
            milestones[-1][1] = "\n".join(lines[i + 1:close])
            i = after
        elif milestones and milestones[-1][1] is None and (m := CHECK_RE.match(line)):
            milestones[-1][1] = m.group(2)
        else:
            detail.append(line[1:] if line.startswith("\\") else line)
            continue
        if m.group(1):
            milestones[-1][4] = m.group(1)
    if not goal:
        return None
    clean = lambda lines: "\n".join(lines).strip() or None  # noqa: E731
    return Plan(goal, clean(detail_lines), [_item(t, c, clean(d), p, r) for t, c, d, p, r in milestones])


def _item(title: str, check: str | None, detail: str | None,
          profile: str | None, repo: str | None) -> tuple:
    item: tuple = (title, check or None, detail)
    if profile or repo:
        item += (profile or None,)
    if repo:
        item += (repo,)
    return item


def _escaped(detail: str) -> list[str]:
    """``detail``'s lines, each that ``parse_goals`` would read as something
    other than detail text behind a backslash."""
    def special(line: str) -> bool:
        return (line.startswith(("\\", "#")) or any(r.match(line) for r in (
            FENCE_RE, STATUS_RE, PROFILE_RE, REPO_RE, CHECK_RE, FENCED_CHECK_RE)))
    return ["\\" + line if special(line) else line for line in detail.splitlines()]


def _check_lines(check: str, repo: str | None) -> list[str]:
    key = f"check@{repo}:" if repo else "check:"
    m = CHECK_RE.match(f"{key} {check}")
    if m and m.group(2) == check:
        return [f"{key} {check}"]
    # Anything a one-line field can't hold exactly (several lines, edge
    # backticks or spaces) goes in a fenced block longer than any run inside.
    fence = "`" * max(3, 1 + max((len(r) for r in re.findall(r"`+", check)), default=0))
    return [key, fence, *check.split("\n"), fence]


def render_goals(plan: Plan, statuses: list[str]) -> str:
    """``plan`` as goals.md text that ``parse_goals`` reads back as ``plan``,
    with ``statuses`` (one ``status_line`` per milestone)."""
    out = [f"# {plan.goal}", ""]
    if plan.detail:
        out += [*_escaped(plan.detail), ""]
    for item, status in zip(plan.milestones, statuses):
        title, check, detail = item[:3]
        profile = item[3] if len(item) > 3 else None
        repo = item[4] if len(item) > 4 else None
        out += [f"## {title}", status]
        if check:
            out += _check_lines(check, repo)
        elif repo:
            out.append(f"repo: {repo}")
        if profile:
            out.append(f"profile: {profile}")
        if detail:
            out += _escaped(detail)
        out.append("")
    return "\n".join(out)


def goals_path(root: str) -> Path:
    """The hand-written seed, ``.brindle/goals.md``. config_root: in a linked
    worktree the git-ignored .brindle lives in the main checkout."""
    return config_root(root) / CONFIG_DIR / GOALS_FILE


def session_goals_path(db: DB, root_id: str) -> Path | None:
    """Session ``root_id``'s own goal file, ``.brindle/goals/<root_id>.md``:
    derived from the session, so no two sessions ever write the same file."""
    root = db.get_agent(root_id)
    ws = db.get_workspace(root.workspace_id) if root else None
    if ws is None:
        return None
    base = config_root(ws.path)
    if not (base / CONFIG_DIR).is_dir():
        base = Path(ws.repo_root)   # never create a .brindle that would hide the main checkout's
    return base / CONFIG_DIR / GOALS_DIR / f"{root_id}.md"


def _goals_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")   # session files never dirty the checkout


def _replace(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(text.encode("utf-8"))
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _db_plan(db: DB, root_id: str) -> Plan | None:
    ap = db.get_autopilot(root_id)
    if not ap or not ap.goal:
        return None
    return Plan(ap.goal, (ap.detail or "").strip() or None,
                [_item(m.title, m.check_cmd, m.detail, m.profile, m.repo) for m in db.milestones(root_id)])


def write_goals_file(db: DB, root_id: str) -> None:
    """Write the session's goal to its own file. A file that already says this
    goal is left byte for byte (a check only updates its status lines; see
    ``sync_goals_file``). Never raises."""
    try:
        path, plan = session_goals_path(db, root_id), _db_plan(db, root_id)
        if path is None or plan is None:
            return
        try:
            if parse_goals(path.read_bytes().decode("utf-8")) == plan:
                return
        except OSError:
            pass
        text = render_goals(plan, [status_line(m) for m in db.milestones(root_id)])
        if parse_goals(text) != plan:
            # The file can't say this goal exactly (a title over several
            # lines, say): no file rather than one a resume would misread.
            log.warning("brindle: session %s's goal can't be written to its goal file", root_id)
            path.unlink(missing_ok=True)
            return
        _goals_dir(path)
        _replace(path, text)
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't write the session's goal file")


def _adopt_legacy(db: DB, root_id: str) -> None:
    """A session from before each session had its own file kept its goal in
    the shared goals.md it was loaded from (``goals_file``). Give it its own
    file, taking the shared one along when it still says this session's goal
    (so no new session imports it), and forget the shared path."""
    ap = db.get_autopilot(root_id)
    if not ap or not ap.goals_file:
        return
    own, legacy = session_goals_path(db, root_id), Path(ap.goals_file)
    try:
        plan = parse_goals(legacy.read_bytes().decode("utf-8"))
        if own is not None and ap.goal and plan is not None and plan.goal == ap.goal:
            _goals_dir(own)
            os.replace(legacy, own)
    except OSError:
        pass   # gone already (adopted by an older owner of the same file), or unreadable
    db.update_autopilot(root_id, goals_file=None)
    write_goals_file(db, root_id)   # the file says exactly the session's goal


def remove_goals_file(db: DB, root_id: str) -> None:
    """The session's goal is done, handed over or dropped: remove its file."""
    _adopt_legacy(db, root_id)
    path = session_goals_path(db, root_id)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.exception("brindle: couldn't remove the session's goal file")


def reload_goals_file(db: DB, root_id: str) -> None:
    """A session resumes: take up edits the person made to its goal file.
    Milestones that keep their title and check keep their results. Never raises."""
    try:
        _adopt_legacy(db, root_id)
        path = session_goals_path(db, root_id)
        if path is None or not path.is_file() or not db.get_autopilot(root_id):
            return
        plan = parse_goals(path.read_bytes().decode("utf-8"))
        if plan is not None and plan != _db_plan(db, root_id):
            set_goal(db, root_id, plan.goal, plan.milestones, plan.detail)
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't reload the session's goal file")


def _claim_seed(db: DB, root_id: str, ws: Workspace) -> Plan | None:
    """Move a hand-written ``.brindle/goals.md`` to this session's own file.
    The move is atomic, so of sessions starting together only one gets it.
    A goals.md an existing session loaded its goal from (before each session
    had its own file) is that session's: it moves there instead."""
    seed, own = goals_path(ws.path), session_goals_path(db, root_id)
    for other in db.legacy_goal_owners():   # newest first: the newest owner takes the file
        ap = db.get_autopilot(other)
        if other != root_id and ap and ap.goals_file and Path(ap.goals_file).resolve() == seed.resolve():
            _adopt_legacy(db, other)
    if own is None or not seed.is_file():
        return None
    try:
        if parse_goals(seed.read_bytes().decode("utf-8")) is None:
            return None
        _goals_dir(own)
        os.replace(seed, own)
    except FileNotFoundError:
        return None   # another session claimed it first
    return parse_goals(own.read_bytes().decode("utf-8"))


def load_goals_file(root: str) -> Plan | None:
    path = goals_path(root)
    if not path.is_file():
        return None
    return parse_goals(path.read_text(encoding="utf-8"))


# A status line is what sync_goals_file writes under each milestone. The
# format is strict so user prose is never mistaken for one.
STATUS_RE = re.compile(r"^status: (passed|failed|pending)( at [0-9a-f]{7,40})?( \(\d{4}-\d{2}-\d{2}\))?\s*$")


def status_line(m: Milestone) -> str:
    """``status: passed at abc1234 (2026-09-29)``: only the state, a short sha
    and a date, nothing else about the session."""
    if m.status not in ("passed", "failed"):
        return "status: pending"
    sha = (m.passed_sha if m.status == "passed" else None) or m.checked_sha
    out = f"status: {m.status}"
    if sha and re.fullmatch(r"[0-9a-f]{7,40}", sha):
        out += f" at {sha[:7]}"
    if m.checked_at:
        out += time.strftime(" (%Y-%m-%d)", time.localtime(m.checked_at))
    return out


def rewrite_goals(text: str, lines_for: list[str]) -> str | None:
    """``text`` with one status line per milestone (``lines_for``, in order),
    the rest byte for byte. None when the file's milestones don't number
    ``len(lines_for)``."""
    lines = text.splitlines(keepends=True)
    fences = _fences([l.rstrip("\r\n") for l in lines])
    inside = {k for start, (_, after) in fences.items() for k in range(start, after)}
    sections: list[list[int]] = []   # line indexes per milestone, heading first
    for i, line in enumerate(lines):
        if i not in inside and re.match(r"^##\s+(.+?)\s*$", line.rstrip("\r\n")):
            sections.append([i])
        elif sections:
            sections[-1].append(i)
    if len(sections) != len(lines_for):
        return None
    out = lines[:sections[0][0]] if sections else lines
    for idx, new in zip(sections, lines_for):
        body = [lines[i] for i in idx]
        eol = "\r\n" if body[0].endswith("\r\n") else "\n"
        placed, anchor, have_profile, have_check = False, 0, False, False
        check_end = None   # the last line of a fenced check's block: the status goes after it
        kept: list[str] = []
        for j, line in enumerate(body):
            bare = line.rstrip("\r\n")
            if idx[j] in inside:
                kept.append(line)
                if idx[j] == check_end:
                    anchor = len(kept)
                continue
            if j and STATUS_RE.match(bare):
                if not placed:
                    kept.append(new + (line[len(bare):] or eol))
                    placed = True
                continue
            kept.append(line)
            if j and not have_profile and PROFILE_RE.match(bare):
                have_profile, anchor = True, len(kept)
            elif j and not have_check and idx[j] + 1 in fences and FENCED_CHECK_RE.match(bare):
                have_check, check_end = True, fences[idx[j] + 1][1] - 1
            elif j and not have_check and CHECK_RE.match(bare):
                have_check, anchor = True, len(kept)
        if not placed:
            anchor = anchor or 1
            if not kept[anchor - 1].endswith("\n"):
                kept[anchor - 1] += eol
            kept.insert(anchor, new + eol)
        out += kept
    return "".join(out)


def sync_goals_file(db: DB, root_id: str) -> None:
    """Write each milestone's status back to the session's own goal file. What
    goes in it is only passed/failed/pending, a short sha and a date; loading
    it never trusts a status line. Does nothing for a session with no file,
    with autopilot off (a handover switches the old session's off), paused,
    or whose milestones no longer match the file (the person edited it: the
    edit is taken up when the session resumes). Never raises: a file we
    can't write must not fail a check."""
    try:
        _adopt_legacy(db, root_id)
        ap = db.get_autopilot(root_id)
        root = db.get_agent(root_id)
        if not ap or not ap.enabled or not ap.goal or root is None \
                or root.status in ("paused", "done"):
            return
        path = session_goals_path(db, root_id)
        if path is None or not path.is_file():
            return
        text = path.read_bytes().decode("utf-8")
        plan = parse_goals(text)
        ms = db.milestones(root_id)
        if plan is None or plan.goal != ap.goal or [m[0] for m in plan.milestones] != [m.title for m in ms]:
            return   # the person edited the file: it's taken up on resume
        new = rewrite_goals(text, [status_line(m) for m in ms])
        if new is None or new == text:
            return
        _replace(path, new)
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't sync milestone status to the session's goal file")


def clear_finished_goal(db: DB, root_id: str) -> None:
    """The goal is reached: remove the session's goal file and clear its
    goal. Never raises: a file we can't write must not fail the verdict."""
    try:
        ap = db.get_autopilot(root_id)
        if not ap or not ap.goal:
            return
        remove_goals_file(db, root_id)
        db.set_milestones(root_id, [])
        db.update_autopilot(root_id, goal=None, detail=None, goals_file=None, goal_sha=None,
                            audit_sha=None, audit_ok=None, auditor_id=None, note=None,
                            audit_history=None)
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't clear the finished goal")


# -- sessions ----------------------------------------------------------------


def root_of(db: DB, agent_id: str) -> str:
    """The session an agent belongs to: its topmost ancestor."""
    seen = set()
    agent = db.get_agent(agent_id)
    while agent and agent.parent_id and agent.parent_id not in seen:
        seen.add(agent.id)
        parent = db.get_agent(agent.parent_id)
        if parent is None:
            break
        agent = parent
    return agent.id if agent else agent_id


def for_agent(db: DB, agent_id: str) -> Autopilot | None:
    """The autopilot of the session ``agent_id`` belongs to, if it has one."""
    return db.get_autopilot(root_of(db, agent_id))


def enable(db: DB, root_id: str, ws: Workspace, *, claim: bool = True) -> Plan | None:
    """Turn autopilot on for a new session. With ``claim``, a hand-written
    ``.brindle/goals.md`` becomes this session's goal (and file), and is returned."""
    db.add_autopilot(root_id)
    plan = _claim_seed(db, root_id, ws) if claim else None
    if plan:
        set_goal(db, root_id, plan.goal, plan.milestones, plan.detail)
    return plan


def set_enabled(db: DB, root_id: str, on: bool) -> None:
    db.add_autopilot(root_id, enabled=on)
    fields: dict[str, object] = {"enabled": int(on), "nudges": 0}
    ap = db.get_autopilot(root_id)
    if on and ap and ap.state in ("blocked", "stalled"):
        fields.update(state="running", note=None, question=None)
    db.update_autopilot(root_id, **fields)


def set_goal(db: DB, root_id: str, goal: str,
             milestones: list[tuple], detail: str | None = None) -> None:
    """Record the goal and its milestones, ``(title, check, detail[, profile[, repo]])``.
    A milestone that keeps its title and check keeps its last result."""
    if not goal.strip():
        raise AutopilotError("the goal needs a title")
    if not milestones:
        raise AutopilotError("give at least one milestone")
    # Before the goal changes: a shared goals.md still saying the old goal
    # would otherwise look unowned and seed the next session with it.
    _adopt_legacy(db, root_id)
    old = {(m.title, m.check_cmd): m for m in db.milestones(root_id)}
    ap = db.get_autopilot(root_id)
    fields: dict[str, object] = {}
    if ap is None or ap.goal != goal.strip():
        fields["audit_history"] = None
    if ap is None or ap.goal != goal.strip() or not ap.goal_sha:
        root = db.get_agent(root_id)
        ws = db.get_workspace(root.workspace_id) if root else None
        fields["goal_sha"] = _commit(ws, tracked_only=True)
    # Changed milestones need a fresh audit.
    db.update_autopilot(root_id, goal=goal.strip(), detail=detail, state="running", note=None,
                        auditor_id=None, audit_sha=None, audit_ok=None, **fields)
    db.set_milestones(root_id, milestones)
    for m in db.milestones(root_id):
        prev = old.get((m.title, m.check_cmd))
        if prev and prev.status != "pending":
            db.record_check(m.id, prev.status == "passed", prev.output or "", prev.checked_sha,
                            passed_sha=prev.passed_sha)
    db.bump_progress(root_id)
    write_goals_file(db, root_id)


CLI_FOR_PROVIDER = {"claude": "claude", "codex": "codex", "antigravity": "agy"}


def _unavailable(name: str, cfg: RepoConfig, repo_root: str, gate=None,
                 weight: str | None = None) -> str | None:
    """Why routing skips profile ``name`` (its CLI isn't installed or
    signed in, its provider is out of headroom, it would break a budget
    (``gate``: a ``brindle.budget.Gate``), it doesn't exist), or None if it's usable."""
    from brindle import quota
    from brindle.providers import signed_out, unusable

    try:
        p = load_profile(name, repo_root)
    except KeyError:
        source = cfg.routing_sources.get(weight or "", "DEFAULT_ROUTING")
        return f"no profile {name} in {source}, skipped {name}"
    over = gate.why_not(name, weight) if gate is not None and gate.active else None
    if over:
        return f"over budget, skipped {name} ({over})"
    from brindle import agents
    from brindle.db import Workspace
    # Read-only preview: no adopted workspace, branch, setup or model turn.
    ws = Workspace("preflight", repo_root, "preflight", "root", "", None,
                   repo_root, None, "", 0.0)
    try:
        problem = agents.model_problem(name, ws)
    except agents.AgentError as e:
        problem = str(e)
    if problem:
        return f"{problem}, skipped {name}"
    from brindle import model_access

    denied = model_access.refused(name, repo_root)
    if denied:
        return f"model refused for this account ({denied.cause}; {denied.fix}), skipped {name}"
    cli = CLI_FOR_PROVIDER.get(p.provider)
    if cli and unusable(p.provider, p.env):
        return (f"{cli} isn't signed in, skipped {name}" if signed_out(p.provider, p.env)
                else f"{cli} isn't installed, skipped {name}")
    if p.provider in quota.PROVIDERS:
        room = quota.headroom(p.provider, cfg, repo_root)
        if room <= 100 - cfg.usage_limit:
            label = quota.NAMES.get(p.provider, p.provider)
            if p.provider == "native":
                return f"local model server not answering, skipped {name}"
            if room <= 0:
                return f"{label} limit reached, skipped {name}"
            return f"{label} at {100 - room:.0f}%, skipped {name}"
    return None


def _route_by_weight(db: DB, cfg: RepoConfig, repo_root: str, weight: str, task: str | None,
                     files: list[str] | None,
                     decision: dict | None = None, gate=None,
                     ceiling: str | None = None) -> tuple[str | None, bool, str]:
    """(profile, learned, why) for a task of ``weight``; profile is None when
    every candidate for the tier is out. ``decision`` gets the ``baseline``
    (the pick without learning), and ``over_budget`` (why each candidate was
    skipped) when a budget was what ruled every one out, or ``demoted_from``
    when it moved the task to a cheaper candidate."""
    from brindle import learning

    skipped: list[str] = []
    remaining: list[str] = []
    over: dict[str, str] = {}
    weight_order = {"light": 0, "medium": 1, "heavy": 2, "advanced": 3, "reasoning": 3, "complex": 3}
    effective_ceiling = ceiling or (cfg.max_weight if (cfg.max_weight and cfg.max_weight in weight_order) else None)
    order = cfg.routing.get(weight, [])
    if effective_ceiling:
        order = [n for n in order if weight_order[profile_weight(n, cfg, repo_root)] <= weight_order[effective_ceiling]]
    for name in order:
        why = _unavailable(name, cfg, repo_root, gate, weight)
        if why:
            skipped.append(why)
            if why.startswith("over budget"):
                over[name] = why
        else:
            remaining.append(name)
    if over and decision is not None:
        if not remaining:
            decision["over_budget"] = list(over.values())
        else:
            first = next(iter(over))
            if order.index(first) < order.index(remaining[0]):
                decision["demoted_from"] = first
    if not remaining:
        return None, False, f"every {weight} candidate was out ({'; '.join(skipped) or 'none configured'})"
    pick, reason = learning.choose_why(db, cfg, repo_root, task, files, candidates=remaining,
                                       weight=weight, default=remaining[0])
    name = pick or remaining[0]
    if decision is not None:
        decision["baseline"] = remaining[0]

    info_str = model_cost_info(name, cfg, repo_root)

    why = f"weight {weight} -> {name}{info_str}"
    picked = f"learning picked it: {reason}" if reason else "learning picked it"
    kept = learning.kept_note(cfg, repo_root)
    detail = [picked if pick else (f"learning kept it: {kept}" if kept else ""), *skipped]
    detail = [d for d in detail if d]
    if detail:
        why += f" ({', '.join(detail)})"
    return name, bool(pick), why


def effective_profile_model(p, repo_root: str | None = None) -> str:
    """Resolve the effective model for a profile, checking model, provider-specific env variables, and managed overrides."""
    from brindle import profiles

    prof = p
    if isinstance(p, str):
        try:
            prof = profiles.load_profile(p, repo_root)
        except Exception:
            prof = p

    m = getattr(prof, "model", None) or ""
    provider = getattr(prof, "provider", None) or "claude"

    # Provider-specific model environment variables in order of provider precedence
    provider_keys = {
        "claude": ("ANTHROPIC_MODEL", "CLAUDE_MODEL", "MODEL"),
        "codex": ("CODEX_MODEL", "OPENAI_MODEL", "MODEL"),
        "antigravity": ("GEMINI_MODEL", "MODEL"),
    }.get(provider, ("MODEL",))

    if not m and getattr(prof, "env", None):
        for k in provider_keys:
            if prof.env.get(k):
                m = prof.env[k]
                break
        if not m:
            for k, v in prof.env.items():
                if "MODEL" in k and v:
                    m = v
                    break
    if not m:
        for k in provider_keys:
            val = os.environ.get(k)
            if val and val.strip():
                m = val.strip()
                break

    try:
        from brindle.pro import managed_models
        from brindle.agents import managed
        mg = managed(repo_root)
        if mg and not isinstance(prof, str):
            applied = managed_models.apply_profile(mg, prof)
            if applied and applied.model:
                m = applied.model
    except Exception:
        pass
    return m.strip()



def model_cost_info(name: str, cfg: RepoConfig | None = None, repo_root: str | None = None) -> str:
    """Format model name, effort, and cost tier as a cost signal string."""
    try:
        from brindle.profiles import load_profile
        p = load_profile(name, repo_root)
        eff = effective_profile_model(p, repo_root)
        model_name = eff or p.model or "inherited CLI default"
        model_info = [model_name]
        if p.effort:
            model_info.append(f"{p.effort} effort")
        pw = profile_weight(name, cfg, repo_root)
        if pw in ("heavy", "advanced", "reasoning"):
            cost_tier = "most expensive tier"
        elif pw == "light":
            cost_tier = "lowest cost tier"
        else:
            cost_tier = "medium cost tier"
        cost_tag = f"; {cost_tier}"
        return f" ({', '.join(model_info)}{cost_tag})"
    except Exception:
        return ""


def profile_weight(name: str, cfg: RepoConfig | None = None, repo_root: str | None = None) -> str:
    """The tier/weight of a profile ('light', 'medium', 'heavy', 'advanced')."""
    if "-fable" in name or "-advanced" in name or "-reasoning" in name:
        return "advanced"
    try:
        p = load_profile(name, repo_root)
        m = effective_profile_model(p, repo_root).lower()
        if any(h in m for h in ("fable", "opus", "o3", "deepseek-r1", "astra")):
            return "heavy"
        if getattr(p, "effort", None) in ("high", "xhigh", "max"):
            return "heavy"
        if "-heavy" in name:
            return "heavy"
        if getattr(p, "effort", None) in ("low",):
            return "light"
        if any(l in m for l in ("haiku", "flash", "mini", "4o-mini", "luna")):
            return "light"
        if "-light" in name:
            return "light"
    except Exception:
        pass
    if "-heavy" in name:
        return "heavy"
    if "-light" in name:
        return "light"
    if cfg is not None and hasattr(cfg, "routing") and cfg.routing:
        in_light = name in cfg.routing.get("light", [])
        in_med = name in cfg.routing.get("medium", [])
        in_heavy = name in cfg.routing.get("heavy", [])
        in_advanced = name in cfg.routing.get("advanced", []) or name in cfg.routing.get("reasoning", [])
        if in_advanced and not in_heavy and not in_med and not in_light:
            return "advanced"
        if in_heavy and not in_med and not in_light:
            return "heavy"
        if in_light and not in_med and not in_heavy:
            return "light"
        if in_med:
            return "medium"
    return "medium"


def choose_profile(db: DB, caller_id: str, repo_root: str, requested: str | None = None,
                   task: str | None = None, files: list[str] | None = None,
                   weight: str | None = None, why: list[str] | None = None,
                   decision: dict | None = None) -> tuple[str, bool]:
    """The worker profile for a delegation, and whether learning chose it:
    ``requested`` if given, else the first unverified milestone's profile in
    the caller's session, else the repo's routing for ``weight`` (available
    candidates only, the hosted learner choosing among them), else the hosted
    learner's pick among ``learning_candidates``, else the repo's
    ``default_agent``. Raises AutopilotError if it doesn't exist. An
    explanation of a routed pick is appended to ``why``. ``decision`` is
    filled with what ``brindle.savings`` records: the ``profile``, the
    ``baseline`` (the pick without learning), ``learned`` and the ``weight``."""
    cfg = load_repo_config(repo_root)
    weight_order = {"light": 0, "medium": 1, "heavy": 2, "advanced": 3, "reasoning": 3, "complex": 3}
    ceiling = cfg.max_weight if (cfg.max_weight and cfg.max_weight in weight_order) else None

    if requested:
        req = requested.strip()
        if ceiling and weight_order[profile_weight(req, cfg, repo_root)] > weight_order[ceiling]:
            raise AutopilotError(f"profile {req!r} exceeds configured max_weight {ceiling!r}")
        name = req
        if why is not None:
            why.append(f"requested profile -> {name}{model_cost_info(name, cfg, repo_root)}")
    else:
        name = ""

    learned = False
    routed: dict = {}
    if not name:
        pending = next((m for m in db.milestones(root_of(db, caller_id)) if m.status != "passed"), None)
        if pending and pending.profile:
            m_profile = pending.profile.strip()
            if ceiling and weight_order[profile_weight(m_profile, cfg, repo_root)] > weight_order[ceiling]:
                candidates = cfg.routing.get(ceiling, [cfg.default_agent])
                name = candidates[0] if candidates else cfg.default_agent
                if why is not None:
                    why.append(f"milestone profile {m_profile} capped to {name}{model_cost_info(name, cfg, repo_root)} by max_weight {ceiling}")
            else:
                name = m_profile
                if why is not None:
                    why.append(f"milestone profile -> {name}{model_cost_info(name, cfg, repo_root)}")
    if not name:
        from brindle import learning
        from brindle.config import normalize_weight

        if weight:
            norm_weight = normalize_weight(weight)
            if norm_weight not in WEIGHTS:
                raise AutopilotError(f"weight must be one of {', '.join(WEIGHTS)}, not {weight!r}")
            weight = norm_weight
            if ceiling and weight_order[weight] > weight_order[ceiling]:
                orig_weight = weight
                weight = ceiling
                if why is not None:
                    why.append(f"weight {orig_weight} capped to {weight} by max_weight")
            if files and all(f.startswith("docs/") or f.endswith(".md") for f in files):
                if weight in ("advanced", "reasoning", "heavy"):
                    orig_demote = weight
                    weight = "medium"
                    if why is not None:
                        why.append(f"weight demoted from {orig_demote} to medium (docs only)")
                elif weight == "medium":
                    weight = "light"
                    if why is not None:
                        why.append("weight demoted from medium to light (docs only)")
                docs_cap = weight
                if ceiling is None or weight_order[docs_cap] < weight_order[ceiling]:
                    ceiling = docs_cap
            from brindle import budget

            gate = budget.Gate(db, cfg, repo_root, caller_id)
            picked, learned, note = _route_by_weight(db, cfg, repo_root, weight, task, files, routed, gate, ceiling=ceiling)
            if routed.get("over_budget"):
                message = budget.refusal(routed["over_budget"])
                try:
                    need_user(db, root_of(db, caller_id), message)
                except Exception:  # noqa: BLE001 - no autopilot session: the refusal is the answer
                    pass
                raise AutopilotError(message)
            if picked:
                name = picked
            else:
                fallback = cfg.default_agent
                if ceiling and weight_order[profile_weight(fallback, cfg, repo_root)] > weight_order[ceiling]:
                    candidates = [n for n in cfg.routing.get(ceiling, []) if weight_order[profile_weight(n, cfg, repo_root)] <= weight_order[ceiling]]
                    fallback = candidates[0] if candidates else None
                if not fallback:
                    raise AutopilotError(f"no permissible agent profile under max_weight ceiling {ceiling!r}")
                name = fallback
                note += f"; using {name}"
            if why is not None:
                why.append(note)
        if not name:
            name = learning.choose(db, cfg, repo_root, task, files, weight=weight,
                                   default=cfg.default_agent) or ""
            learned = bool(name)
            name = name or cfg.default_agent
            if ceiling and weight_order[profile_weight(name, cfg, repo_root)] > weight_order[ceiling]:
                candidates = cfg.routing.get(ceiling, [cfg.default_agent])
                orig_name = name
                name = candidates[0] if candidates else cfg.default_agent
                if why is not None:
                    why.append(f"fallback profile {orig_name} capped to {name} by max_weight {ceiling}")
            routed["baseline"] = cfg.default_agent
        routed["prior"] = learned and learning.used_prior(cfg, repo_root)
    if decision is not None:
        decision.update(profile=name, learned=learned, weight=weight,
                        prior=bool(routed.get("prior")),
                        baseline=routed.get("baseline", name) if learned else name,
                        demoted_from=routed.get("demoted_from"))
    if ceiling and weight_order[profile_weight(name, cfg, repo_root)] > weight_order[ceiling]:
        permissible = [
            n for n in cfg.routing.get(ceiling, [])
            if weight_order[profile_weight(n, cfg, repo_root)] <= weight_order[ceiling]
        ]
        if not permissible:
            for tier in ("medium", "light"):
                if weight_order[tier] <= weight_order[ceiling]:
                    permissible.extend([
                        n for n in cfg.routing.get(tier, [])
                        if weight_order[profile_weight(n, cfg, repo_root)] <= weight_order[ceiling]
                    ])
        if permissible:
            orig_name = name
            name = permissible[0]
            if why is not None:
                why.append(f"profile {orig_name} capped to {name} by max_weight {ceiling}")
        else:
            raise AutopilotError(f"no permissible agent profile under max_weight {ceiling!r}")
    try:
        load_profile(name, repo_root)
    except KeyError:
        raise AutopilotError(
            f"no agent profile named {name!r}; see list_agent_profiles, "
            "or pass agent_profile explicitly"
        ) from None
    if why is not None:
        info_str = model_cost_info(name, cfg, repo_root)
        if info_str and not any("cost tier" in w for w in why):
            if why:
                why[-1] += info_str
            else:
                why.append(f"{name}{info_str}")
    return name, learned


def resolve_profile(db: DB, caller_id: str, repo_root: str, requested: str | None = None,
                    task: str | None = None, files: list[str] | None = None) -> str:
    """``choose_profile`` without the learned flag."""
    return choose_profile(db, caller_id, repo_root, requested, task, files)[0]


def need_user(db: DB, root_id: str, question: str, ask: bool = False) -> None:
    """Block autopilot on ``question``. brindle's own blocks (credentials,
    budget) stop the whole session until the user replies; a supervisor's
    question (``ask``) holds only the work that depends on it, until the
    supervisor says it's answered."""
    note = question.strip()[:500]
    fields: dict[str, object] = {"state": "blocked", "note": note, "nudges": 0,
                                 "question": note if ask else None}
    if ask:
        ap = db.get_autopilot(root_id)
        fields["nudged_at"] = ap.progress if ap else None
    db.update_autopilot(root_id, **fields)


def held(ap: Autopilot) -> bool:
    """Autopilot waits on the supervisor's open question, and nothing has
    blocked it since (that would have replaced the note)."""
    return ap.state == "blocked" and bool(ap.question) and ap.question == ap.note


def answered(db: DB, root_id: str) -> None:
    db.update_autopilot(root_id, state="running", note=None, question=None, nudges=0)


def user_spoke(db: DB, agent: Agent) -> None:
    """The user typed into the supervisor's chat: whatever blocked autopilot
    is theirs to have answered, so it may drive again. An open question stays
    open (the message may be about something else), but the supervisor gets
    one more reminder of it."""
    ap = db.get_autopilot(agent.id)
    if ap and held(ap):
        db.update_autopilot(agent.id, nudges=0, nudged_at=None)
    elif ap and ap.state in ("blocked", "stalled"):
        db.update_autopilot(agent.id, state="running", note=None, question=None, nudges=0)
    elif ap:
        db.update_autopilot(agent.id, nudges=0)


# -- checks ------------------------------------------------------------------


def tail(text: str, lines: int = OUTPUT_TAIL_LINES) -> str:
    out = text.rstrip().splitlines()
    return "\n".join(out[-lines:])


class CheckCancelled(Exception):
    """``cancel()`` turned true while a check command was running."""


KILL_WAIT = 5.0     # seconds to wait for a killed check's pipes to close
CANCEL_POLL = 1.0  # seconds between looks at ``cancel`` while a check runs


def run_check(cmd: str, cwd: str, env: dict[str, str], timeout: int,
              cancel=None) -> tuple[bool, str]:
    """Run one check command. Returns (passed, the tail of its output).
    ``cancel``, if given, is polled while the command runs; when it turns
    true the command is killed and ``CheckCancelled`` is raised."""
    if cancel is None:
        try:
            proc = subprocess.run(
                cmd, shell=True, cwd=cwd, env={**os.environ, **env}, capture_output=True,
                text=True, timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return False, f"$ {cmd}\n(timed out after {timeout}s)"
        stdout, stderr, returncode = proc.stdout, proc.stderr, proc.returncode
    else:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=cwd, env={**os.environ, **env}, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL, start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            try:
                stdout, stderr = proc.communicate(timeout=CANCEL_POLL)
                break
            except subprocess.TimeoutExpired:
                pass
            timed_out = time.monotonic() >= deadline
            if timed_out or cancel():
                _kill_group(proc)
                try:
                    proc.communicate(timeout=KILL_WAIT)
                except subprocess.TimeoutExpired:
                    pass   # a grandchild still holds the pipes; don't hang on it
                if timed_out:
                    return False, f"$ {cmd}\n(timed out after {timeout}s)"
                raise CheckCancelled(cmd)
        returncode = proc.returncode
    output = tail((stdout or "") + (stderr or ""))
    status = "" if returncode == 0 else f"(exit {returncode})"
    return returncode == 0, "\n".join(p for p in (f"$ {cmd}", output, status) if p)


def _kill_group(proc: subprocess.Popen) -> None:
    import signal

    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


def checking(ap: Autopilot, timeout: float = 900.0) -> bool:
    """Whether a background milestone check is still running."""
    return bool(ap.checking_since) and time.time() - ap.checking_since < timeout


def _detach(args: list[str]) -> None:
    """Start a brindle helper that outlives this process."""
    subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check_in_background(db: DB, root_id: str, ws: Workspace, position: int | None) -> str:
    """Start the milestone checks detached and return at once. The result is
    delivered to the session root's inbox (see cli._check_milestones). A full
    suite can outrun an MCP call's time limit, and nothing should wait on it."""
    from brindle.providers import brindle_invocation

    ap = db.get_autopilot(root_id)
    if ap and checking(ap):
        return ("A milestone check is already running; its result arrives as a message. "
                "Carry on with other work meanwhile.")
    db.update_autopilot(root_id, checking_since=time.time())
    args = [*brindle_invocation(), "_check-milestones", root_id, ws.id]
    if position is not None:
        args += ["--position", str(position)]
    _detach(args)
    which = f"milestone {position}" if position else "every milestone"
    return (f"Checking {which} in the background (the checks run in your checkout; results "
            "already recorded for this commit are reused). The result arrives as a message; "
            "carry on with other work meanwhile, and don't claim a milestone done until it does.")


def check_milestones(db: DB, root_id: str, ws: Workspace, position: int | None = None,
                     cfg: RepoConfig | None = None) -> str:
    """Run milestone checks in the supervisor's checkout (where merges land)
    and record the results. With ``position``, just that one milestone, then
    the other milestones currently marked passed are re-run too, to catch a
    merge that broke one of them while another is still in progress; those
    that passed at the checkout's current HEAD are skipped. Only a milestone
    newly passing, and not already passed at this commit, counts as progress,
    so a flaky check can't keep resetting the nudge limit."""
    from brindle import git, repos, workspaces

    cfg = cfg or load_repo_config(ws.repo_root)
    ms = db.milestones(root_id)
    if not ms:
        raise AutopilotError("no milestones yet: record the goal with set_goal first")
    chosen = [m for m in ms if position is None or m.position == position]
    if not chosen:
        raise AutopilotError(f"no milestone {position}; they're numbered 1-{len(ms)}")

    def commit_of(checkout: Workspace) -> str | None:
        # The commit the checks ran against. Uncommitted changes could break a
        # check without moving HEAD, so a dirty checkout records no sha.
        try:
            return None if git.dirty_files(checkout.path) else git.out(["rev-parse", "HEAD"], checkout.path)
        except git.GitError:
            return None

    head = commit_of(ws)
    # A milestone for a repo attached to the session runs in that repo's own
    # checkout, with that repo's config; its sha bookkeeping is that checkout's.
    sites: dict[str | None, tuple[Workspace, RepoConfig, str | None]] = {None: (ws, cfg, head)}

    def site(alias: str | None) -> tuple[Workspace, RepoConfig, str | None] | str:
        if alias not in sites:
            found = repos.resolve_attached(db, root_id, alias)
            if found is None:
                return f"repo {alias!r} isn't attached to this session (`brindle repo ls`)"
            other = repos.target_workspace(db, found.repo_root)
            sites[alias] = (other, load_repo_config(other.repo_root), commit_of(other))
        return sites[alias]

    newly_passed = False

    def run(batch: list[Milestone]) -> None:
        nonlocal newly_passed
        from brindle import gates

        for m in batch:
            if not m.check_cmd:
                continue
            at = site(m.repo)
            if isinstance(at, str):
                db.record_check(m.id, False, f"$ @{m.repo}: {m.check_cmd}\n{at}", None)
                continue
            checkout, repo_cfg, sha = at
            # Cached by commit when the checkout is clean, so a check that
            # already passed at this HEAD (as a merge gate, say) isn't re-run.
            ok, out = gates.run_checked(db, checkout, m.check_cmd, workspaces.workspace_env(checkout),
                                        repo_cfg.check_timeout)
            # Not if it already passed at this very commit: a flaky check
            # flipping back isn't progress.
            newly_passed |= ok and m.status != "passed" and (sha is None or m.passed_sha != sha)
            if not ok and m.status == "passed" and sha and m.checked_sha:
                # Passed at the commit before, fails now: whatever merged in
                # between got past its reviewer.
                escapes.milestone_regressed(db, checkout.repo_root, checkout.path, m.title,
                                            m.checked_sha, sha)
            db.record_check(m.id, ok, out, sha)

    run(chosen)
    ms = db.milestones(root_id)
    regressed: list[Milestone] = []
    if position is not None and len(ms) > 1:
        def moved(m: Milestone) -> bool:
            at = site(m.repo)
            sha = at[2] if not isinstance(at, str) else None
            return sha is None or m.checked_sha != sha

        recheck = [m for m in ms if m.position != position and m.status == "passed" and moved(m)]
        if recheck:
            was_passed = {m.id for m in recheck}
            run(recheck)
            ms = db.milestones(root_id)
            regressed = [m for m in ms if m.id in was_passed and m.status != "passed"]
    if newly_passed:
        db.bump_progress(root_id)
    done = all(m.status == "passed" for m in ms)
    ap = db.get_autopilot(root_id)
    audit = audit_due(ap, cfg, head) if done and ap else None
    if done and audit is None:
        db.update_autopilot(root_id, state="done", note=None)
    elif done and audit == "start":
        db.update_autopilot(root_id, state="auditing")
    elif done:
        db.update_autopilot(root_id, state="running")
    elif ap and ap.state in ("done", "auditing"):
        db.update_autopilot(root_id, state="running")
    sync_goals_file(db, root_id)
    regressed_ids = {m.id for m in regressed}
    shown = [m for m in ms if position is None or m.position == position or done or m.id in regressed_ids]
    lines = []
    if regressed:
        positions = ", ".join(str(m.position) for m in regressed)
        verb = "was passing but now fails" if len(regressed) == 1 else "were passing but now fail"
        lines.append(f"REGRESSED: milestone {positions} {verb}.")
    lines.append(progress(db, root_id))
    for m in shown:
        if m.output and m.status == "failed":
            lines.append(f"\nMilestone {m.position} check output:\n{m.output}")
    if done and audit is None:
        lines.append("\n" + GOAL_REACHED)
        clear_finished_goal(db, root_id)
    elif done:
        lines.append("\n" + AUDIT_HOLD[audit].format(note=(ap.note if ap else None) or ""))
    return "\n".join(lines)


# -- completion audit ---------------------------------------------------------
#
# Passing milestone checks don't prove the goal is done: a check can be too
# weak, or test the wrong thing. So once every milestone passes, an
# adversarial reviewer compares the work with the original goal before the
# goal counts as reached (``goal_audit`` in .brindle/config.json turns it off).

GOAL_REACHED = "Every milestone's check passes: the goal is reached. Tell the user."
AUDIT_HOLD = {
    "start": ("Every milestone's check passes. Before the goal counts as reached, an independent "
              "auditor is checking the work against the original goal; its verdict arrives as a "
              "message. Don't tell the user the goal is reached yet."),
    "gaps": ("Every milestone's check passes, but the completion audit of this commit found "
             "gaps. If a gap is an intentional scope choice, trade-off, or awaiting user decision, "
             "record it with record_audit_decision so subsequent audits respect the decision. "
             "Otherwise close them, commit, and call check_milestone again to re-audit.\n\n{note}"),
    "dirty": ("Every milestone's check passes, but your checkout has uncommitted changes, so "
              "there's no commit to audit. Commit them and call check_milestone again."),
}
MAX_AUDIT_PASSES = 2

AUDIT = """You are conducting a fast, final audit to verify: did the updates do what the milestones say they did?
Every milestone's check command has already passed. Your job is a quick delivery verification: check that the deliverables described in the milestones are genuinely implemented in the code.

The goal: {goal}{detail}

Milestones, each with the check that passed:
{milestones}

{work} No check summary will arrive for this audit; don't wait for one.

Verification instructions:
- Fast single pass: Inspect the changes (using the git commands above) to confirm that the requested features, fixes, or deliverables are present.
- Trust passing checks unless a deliverable is visibly missing, stubbed out (`TODO`, `pass`, or dummy returns), or unhandled.
- Do NOT debate test assertion depth, testing style, code architecture, or aesthetic choices. If a milestone's check passes and the deliverable is implemented in the diff, consider that milestone delivered.
- Do NOT create scratch files, write temporary probe scripts, or run extensive test suites.
- Separate findings into:
  1. "Work not done" (missing implementation for deliverables).
  2. "Disclosed and awaiting owner decision" (deliberate scope choices, proposals or options awaiting user decision).
- Do not re-raise any gap or choice that earlier audits mentioned and the supervisor or owner has accepted.
{prior_decisions}
Call submit_review once and immediately.
- approved=true if the updates delivered what the milestones asked for.
- approved=false only if a concrete milestone deliverable is missing or incomplete, listing each missing item: the milestone it concerns, file:line where it applies, what is missing, and what would close it."""

RE_AUDIT = """You are conducting a fast, bounded re-audit of a finished autopilot goal.
Prior audits identified specific gaps and recorded decisions.
DO NOT move the goalposts or introduce new requirements that were not flagged in earlier audits.
Your job is a quick delivery verification: did the latest updates resolve the previously identified gaps?

The goal: {goal}{detail}

Milestones, each with the check that passed:
{milestones}

{work} No check summary will arrive for this audit; don't wait for one.

Prior audit findings and accepted decisions:
{prior_decisions}

Re-audit rules:
- Strictly bounded scope: Only verify whether the previously flagged gaps have been addressed in the diff or resolved by an accepted decision.
- Do NOT introduce new criteria, debate testing style, or re-open closed topics.
- Fast single pass: Inspect the diff, verify whether the fixes were implemented, and immediately call submit_review. Do not create scratch files or run extra test suites.
- Separate findings into:
  1. "Work not done" (missing implementation for previously flagged gaps).
  2. "Disclosed and awaiting owner decision" (deliberate scope choices, proposals or options awaiting user decision).
- If a prior gap has been addressed by code or tests, consider it closed.
- If an item was accepted as a decision or scope exclusion, do NOT flag it as a gap.
- If and only if previously flagged gaps remain unresolved without an accepted decision, report them.

Call submit_review once and immediately. approved=true if all previously identified gaps are resolved (or accepted).
Otherwise approved=false, listing only the remaining unresolved gaps: what is still missing and what would close it."""


def _commit(ws: Workspace | None, *, tracked_only: bool = False) -> str | None:
    """``ws``'s HEAD, or None when it has uncommitted changes (or isn't a checkout)."""
    from brindle import git

    if ws is None:
        return None
    try:
        return None if git.dirty_files(ws.path, tracked_only=tracked_only) else git.out(["rev-parse", "HEAD"], ws.path)
    except git.GitError:
        return None


def audit_due(ap: Autopilot, cfg: RepoConfig, head: str | None) -> str | None:
    """With every milestone passing: None if the goal is reached (the audit is
    off, or approved ``head``), else which AUDIT_HOLD applies."""
    if not cfg.goal_audit:
        return None
    if head is None:
        return "dirty"
    if ap.audit_sha == head:
        if ap.audit_ok:
            return None
        if ap.audit_ok == 0:
            return "gaps"
    return "start"


def audit_prompt(db: DB, ap: Autopilot, head: str) -> str:
    ms = db.milestones(ap.root_id)
    lines = [f"{m.position}. {m.title} (check: `{m.check_cmd or 'none'}`)"
             + (f"\n   {m.detail}" if m.detail else "") for m in ms]
    history_text = (getattr(ap, "audit_history", None) or ap.note or "").strip()
    template = RE_AUDIT if history_text else AUDIT
    prior = f"\nPrior audit notes / accepted decisions:\n{history_text}\n" if history_text else ""
    return template.format(
        goal=ap.goal, detail=f"\n\n{ap.detail.strip()}" if ap.detail and ap.detail.strip() else "",
        milestones="\n".join(lines), work=(
            f"The work since the goal was set: run `git log --oneline {ap.goal_sha}..{head}` and "
            f"`git diff {ap.goal_sha}..{head}` in this checkout." if ap.goal_sha else
            f"The goal's starting commit wasn't recorded: use `git log` to find where this "
            f"work began, and audit up to {head}."),
        prior_decisions=prior,
    )


def start_audit(db: DB, root_id: str, ws: Workspace, cfg: RepoConfig | None = None) -> Agent | None:
    """Start the completion audit of ``ws``'s HEAD: a read-only reviewer whose
    verdict ``record_audit`` takes. None if one is already running or the
    session isn't waiting on one."""
    from brindle import agents

    ap = db.get_autopilot(root_id)
    head = _commit(ws, tracked_only=True)
    if ap is None or ap.state != "auditing" or head is None:
        return None
    running = db.get_agent(ap.auditor_id) if ap.auditor_id else None
    if running and running.result is None and running.status not in ("paused", "done") \
            and agents.is_alive(running):
        if ap.audit_sha == head:
            return None
        agents.close(db, running.id)
    cfg = cfg or load_repo_config(ws.repo_root)
    profile = agents.default_review_profile(cfg, db.get_agent(root_id), db, ws.repo_root, ap.goal)
    auditor = agents.spawn(db, ws, profile, prompt=audit_prompt(db, ap, head), parent_id=root_id,
                           mode="review", background_setup=True)
    db.update_autopilot(root_id, auditor_id=auditor.id, audit_sha=head, audit_ok=None)
    return auditor


def record_accepted_decision(db: DB, root_id: str, decision: str) -> None:
    """Record a supervisor/user-accepted scope exclusion or decision into persistent audit history."""
    ap = db.get_autopilot(root_id)
    entry = f"Accepted decision / scope exclusion:\n{decision.strip()}"
    new_history = ((ap.audit_history + "\n\n" + entry) if (ap and ap.audit_history) else entry)
    db.update_autopilot(root_id, audit_history=new_history, audit_sha=None, audit_ok=None, auditor_id=None, state="running")


def record_audit(db: DB, root_id: str, approved: bool, summary: str) -> str:
    """The auditor's verdict. Returns the message for the supervisor."""
    ap = db.get_autopilot(root_id)
    sha = (ap.audit_sha or "")[:8] if ap else ""
    root = db.get_agent(root_id)
    now = _commit(db.get_workspace(root.workspace_id) if root else None, tracked_only=True)
    entry = f"Audit of {sha} ({'approved' if approved else 'gaps found'}):\n{summary.strip()}"
    new_history = ((ap.audit_history + "\n\n" + entry) if (ap and ap.audit_history) else entry)
    if approved and (now is None or now != (ap.audit_sha if ap else None)):
        # Work landed (or is uncommitted) since the audit began: an approval of
        # the old commit says nothing about it, so the goal stays open.
        db.update_autopilot(root_id, state="running", note=None, audit_ok=1, auditor_id=None,
                            audit_history=new_history)
        return (f"[brindle] Completion audit of {sha}: approved, but your checkout has changed since "
                "it started, so the goal isn't reached yet. Commit any changes and call "
                f"check_milestone to audit the current commit.\n\n{summary}")
    if approved:
        db.update_autopilot(root_id, state="done", note=None, audit_ok=1, auditor_id=None,
                            audit_history=new_history)
        db.bump_progress(root_id)
        clear_finished_goal(db, root_id)
        return (f"[brindle] Completion audit of {sha}: APPROVED. Every milestone passes and the "
                f"auditor found the goal fully delivered: the goal is reached. Tell the user."
                f"\n\n{summary}")
    audit_fails = new_history.count("(gaps found)")
    if audit_fails >= MAX_AUDIT_PASSES:
        db.update_autopilot(root_id, state="blocked", note=summary.strip()[:4000], audit_ok=0,
                            auditor_id=None, audit_history=new_history, nudges=0)
        audit_ws = db.get_workspace(root.workspace_id) if root else None
        if audit_ws is not None:
            escapes.audit_gaps(db, audit_ws.repo_root, summary)
        return (f"[brindle] Completion audit of {sha}: GAPS FOUND (persistent after {audit_fails} passes). "
                "Autopilot paused to avoid an unbounded audit loop. Review the findings below with the "
                "user: either address the remaining gaps, or record them as accepted scope decisions "
                "with `record_audit_decision`, then call check_milestone."
                f"\n\n{summary}")

    db.update_autopilot(root_id, state="running", note=summary.strip()[:4000], audit_ok=0,
                        auditor_id=None, audit_history=new_history)
    audit_ws = db.get_workspace(root.workspace_id) if root else None
    if audit_ws is not None:
        escapes.audit_gaps(db, audit_ws.repo_root, summary)
    return (f"[brindle] Completion audit of {sha}: GAPS FOUND. The goal isn't reached yet, even "
            "though every milestone's check passes. If a finding is an accepted scope choice or "
            "trade-off, record it with `record_audit_decision(finding, rationale)` so subsequent audits "
            "do not re-raise it. Otherwise close each gap below (and where a check let a gap through, "
            "tighten it with set_goal), commit, and call check_milestone to re-audit."
            f"\n\n{summary}")


# -- progress -----------------------------------------------------------------

MARK = {"passed": "✓", "failed": "✗", "pending": "○", "working": "◐", "waiting": "◌"}


def counts(db: DB, root_id: str) -> tuple[int, int]:
    ms = db.milestones(root_id)
    return sum(m.status == "passed" for m in ms), len(ms)


def work_state(db: DB, root_id: str) -> str | None:
    """Display-only: "working" or "waiting" while the session has work in
    flight, else None. A failed milestone's last check is stale then, so it
    shouldn't read as a plain failure. Never stored on the milestone.

    shortcut: work isn't tied to a milestone, so any in-flight work marks every
    failed milestone; upgrade if tasks get a milestone link."""
    from brindle import login_pause

    ap = db.get_autopilot(root_id)
    if (ap is not None and ap.state == "usage_paused") or login_pause.paused(db, root_id):
        return "waiting"
    live = active_workers(db, root_id)
    if any(a.status != "waiting" for a in live):
        return "working"
    if live or any(root_of(db, t.caller_id) == root_id
                   for t in db.list_tasks(state="pending") if t.caller_id):
        return "waiting"
    return None


def progress(db: DB, root_id: str) -> str:
    ap = db.get_autopilot(root_id)
    if ap is None:
        return "Autopilot is not set up for this session."
    head = f"Autopilot {'on' if ap.enabled else 'off'}"
    if not ap.goal:
        return (f"{head}. No goal yet: call set_goal with a goal and milestones derived from the "
                "user's request (ask what we're building only if there is no request).")
    done, total = counts(db, root_id)
    lines = [f"{head}. Goal: {ap.goal}", f"{done} of {total} milestones verified."]
    ms = db.milestones(root_id)
    w = work_state(db, root_id) if any(m.status == "failed" for m in ms) else None
    for m in ms:
        where = f"@{m.repo}: " if m.repo else ""
        how = f"check: `{where}{m.check_cmd}`" if m.check_cmd else "NO CHECK: propose one with set_goal"
        when = f", last checked {time.strftime('%H:%M', time.localtime(m.checked_at))}" if m.checked_at else ""
        who = f", profile: {m.profile}" if m.profile else ""
        shown = w if m.status == "failed" and w else m.status
        stale = ", last check failed" if shown != m.status else ""
        lines.append(f"  {MARK.get(shown, '·')} {m.position}. {m.title} ({how}{who}{when}{stale})")
    if ap.state in ("blocked", "stalled") and ap.note:
        lines.append(f"{ap.state.capitalize()}: {ap.note}")
    elif ap.state == "auditing":
        lines.append("Completion audit running: the goal isn't reached until it approves.")
    elif ap.audit_ok == 0 and ap.note:
        lines.append(f"Completion audit found gaps:\n{ap.note}")
    return "\n".join(lines)


def check_summary(db: DB, root_id: str, position: int | None, text: str) -> str:
    """The one line pushed after a milestone check: the result, how many
    milestones are verified, and a REGRESSED note. ``text`` is what
    check_milestones returned; the full progress stays in get_progress."""
    from brindle.agents import first_line

    ms = db.milestones(root_id)
    done, total = counts(db, root_id)
    if not ms:
        return first_line(text)
    chosen = [m for m in ms if position is None or m.position == position]
    parts = []
    for m in chosen if position is not None else [m for m in ms if m.status == "failed"]:
        if m.status == "failed":
            parts.append(f"Milestone {m.position} ✗ failed: {first_line(m.output or 'no output')}")
        else:
            parts.append(f"Milestone {m.position} {MARK.get(m.status, '·')} {m.status}")
    head = "; ".join(parts) or "Milestone check"
    head += f" ({done} of {total} verified)"
    first = text.splitlines()[0] if text else ""
    if first.startswith("REGRESSED:"):
        head += f". {first}"
    if GOAL_REACHED in text.splitlines():
        head += ". Goal reached: tell the user"
    elif ms and done == total:
        head += ". Not done yet: completion audit pending, has gaps, or needs a commit"
    return head


# -- workers and the cap ----------------------------------------------------------


BUSY = ("starting", "processing", "waiting")
IDLE_GRACE_SECONDS = 60  # how long a worker may sit idle and unreported before it's called out


def active_workers(db: DB, root_id: str, *, reviewers: bool = True) -> list[Agent]:
    """Agents in the session still working: not yet reported, or back at work
    after reporting (e.g. on review feedback). Includes workers stalled idle
    and unreported past the grace period; see ``stalled_workers`` and
    ``working_workers`` to tell those apart."""
    from brindle import agents

    return [a for a in agents.tree(db, root_id)[1:]
            if a.mode in agents.REPORTING_MODES and (reviewers or a.mode != "review")
            and (a.result is None or a.status in BUSY or a.pipeline)
            and a.status not in ("paused", "done") and agents.is_alive(a)
            and not agents.review_moot(db, a)]


def split_workers(db: DB, root_id: str, *, screen: bool = False) -> tuple[list[Agent], list[Agent]]:
    """``active_workers`` split into (working, stalled). Stalled: idle without
    reporting a result for more than ``IDLE_GRACE_SECONDS``. Claude Code
    already reminded them once to call report_result (see agents.handle_hook's
    "stop" case); past the grace period they won't be reminded again on their
    own, so the supervisor has to check on them itself. Working: the rest,
    still expected to report on their own.

    Only workers whose provider reports idle through hooks can stall this way
    (Codex and shell workers never leave 'unknown'). A stale 'idle' can also
    outlive the turn after it, so with ``screen`` a worker with a screen to
    read is only stalled when the screen shows it idle too (its status is
    reconciled otherwise). That sleeps between samples, so only the Stop hook
    asks for it, and only for idle, unreported workers past the grace period.
    Without it (the dashboard), the status is taken as it stands."""
    from brindle import agents
    from brindle.providers import get_provider

    now = time.time()
    working, stalled = [], []
    for a in active_workers(db, root_id):
        maybe = (a.result is None and a.status == "idle" and a.plan_state != "proposed"
                 and get_provider(a.provider).uses_hooks
                 and now - (a.status_since or a.created_at) >= IDLE_GRACE_SECONDS)
        if maybe and (not screen or a.headless or agents.screen_status(db, a, samples=2) == "idle"):
            stalled.append(a)
        else:
            working.append(a)
    return working, stalled


def stalled_workers(db: DB, root_id: str) -> list[Agent]:
    """Active workers the supervisor has to check on; see ``split_workers``."""
    return split_workers(db, root_id)[1]


def working_workers(db: DB, root_id: str) -> list[Agent]:
    """Active workers still expected to report on their own; see ``split_workers``."""
    return split_workers(db, root_id)[0]


def check_capacity(db: DB, caller_id: str | None, cfg: RepoConfig) -> None:
    """Refuse a new worker beyond ``max_agents``. Reviewers don't count: they
    are short, and holding up a review would hold up every merge."""
    if not caller_id or cfg.max_agents <= 0:
        return
    busy = active_workers(db, root_of(db, caller_id), reviewers=False)
    if len(busy) >= cfg.max_agents:
        raise AutopilotError(
            f"{len(busy)} workers are already running, the most allowed at once "
            f"(max_agents in .brindle/config.json). Wait for one to report, then try again."
        )


# -- Claude usage -------------------------------------------------------------------


def record_usage(status: dict) -> None:
    """Keep the plan usage Claude Code gives its status line (Claude.ai
    subscriptions only)."""
    from brindle import quota

    quota.record_claude(status)


def usage() -> dict | None:
    """The fullest recent usage window: {"window", "used", "resets_at"}, or None."""
    from brindle import quota

    q = quota.get("claude")
    w = quota.fullest(q) if q else None
    if w is None:
        return None
    name = "five_hour" if w.minutes == 300 else "seven_day"
    return {"window": name, "used": w.used, "resets_at": w.resets_at}


def usage_note(u: dict) -> str:
    window = "5-hour" if u["window"] == "five_hour" else "weekly"
    resets = ""
    if isinstance(u.get("resets_at"), (int, float)):
        resets = f", resets {time.strftime('%-I:%M%p', time.localtime(u['resets_at'])).lower()}"
    return f"Claude usage at {u['used']:.0f}% of the {window} limit{resets}"


def limit_reached(db: DB, agent: Agent) -> None:
    """A turn failed on its provider's usage limit: stop pushing until the
    user is back. Only Claude's window is tracked (see ``usage``), so only a
    Claude limit, with a reset time known, stops that provider's workers and
    resumes on its own; any other provider's just blocks and names it."""
    ap = db.get_autopilot(root_of(db, agent.id))
    if not ap or not ap.enabled:
        return
    u = usage() if agent.provider == "claude" else None
    if u and isinstance(u.get("resets_at"), (int, float)):
        _pause_provider(db, ap.root_id, agent.provider, u["resets_at"],
                        "Claude's usage limit was reached")
        return
    db.update_autopilot(ap.root_id, state="blocked", nudges=0,
                        note=f"The {agent.provider} provider's usage limit was reached. "
                        "Continue when it resets.")


def _pause_provider(db: DB, root_id: str, provider: str, resets_at: float, why: str) -> None:
    from brindle import agents

    stopped = [a for a in active_workers(db, root_id)
               if agents.runs_process(a) and a.provider == provider]
    for a in stopped:
        agents.pause_worker(db, a)
    note = f"{why}. Autopilot paused so work doesn't stall halfway"
    if stopped:
        note += f"; {len(stopped)} {provider} worker(s) stopped until then"
    db.update_autopilot(root_id, state="usage_paused", usage_resets_at=float(resets_at),
                        usage_paused_ids=json.dumps([a.id for a in stopped]),
                        note=note + ".", nudges=0)


def over_limit(db: DB, root_id: str, cfg: RepoConfig) -> bool:
    """Usage is at the limit: stop the session's workers until the window
    resets (autopilot state "usage_paused"). Without a reset time to wait for,
    autopilot is just blocked, as the user has to say when to go on."""
    u = usage()
    if not u or u["used"] < cfg.usage_limit:
        return False
    resets_at = u.get("resets_at")
    if not isinstance(resets_at, (int, float)):
        db.update_autopilot(root_id, state="blocked", note=usage_note(u)
                            + ". Autopilot paused so work doesn't stall halfway.")
        return True
    _pause_provider(db, root_id, "claude", resets_at, usage_note(u))
    return True


def usage_resume(db: DB, root_id: str, now: float | None = None) -> str | None:
    """Once the usage window has reset, bring back the workers stopped for it
    and tell the supervisor. Returns what was done, or None if still waiting."""
    ap = db.get_autopilot(root_id)
    if ap is None or ap.state != "usage_paused" or ap.usage_resets_at is None:
        return None
    now = time.time() if now is None else now
    if now <= ap.usage_resets_at:
        return None
    from brindle import agents

    root = db.get_agent(root_id)
    if root is None or root.status in ("paused", "done") or not agents.is_alive(root):
        return None  # the session itself is stopped: `brindle continue` brings it back
    ws = db.get_workspace(root.workspace_id)
    cfg = load_repo_config(ws.repo_root) if ws else RepoConfig()
    u = usage()
    if u and u["used"] >= cfg.usage_limit:
        return None  # the fresh reading says the limit still holds
    try:
        ids = set(json.loads(ap.usage_paused_ids or "[]"))
    except ValueError:
        ids = set()
    resumed = agents.resume(db, root_id, only=ids) if ids else []
    db.update_autopilot(root_id, state="running", note=None, usage_resets_at=None,
                        usage_paused_ids=None, nudges=0)
    what = (f"resumed {', '.join(a.id for a in resumed)}" if resumed
            else "no workers needed resuming")
    agents.send_message(db, root_id, "[brindle autopilot] Claude's usage window has reset, "
                        f"so autopilot is running again: {what}.")
    return f"usage reset for {root_id}: {what}"


def usage_sweep(db: DB, now: float | None = None) -> list[str]:
    """One pass over every autopilot session: stop workers at the limit,
    resume them after the reset. Called from the cull sweep."""
    done = []
    for root in db.list_agents():
        if root.mode != "interactive" or root.status in ("paused", "done"):
            continue
        ap = db.get_autopilot(root.id)
        if ap is None or not ap.enabled:
            continue
        if ap.state == "running" and ap.goal:
            ws = db.get_workspace(root.workspace_id)
            cfg = load_repo_config(ws.repo_root) if ws else RepoConfig()
            if over_limit(db, root.id, cfg):
                done.append(f"usage limit reached: {root.id} is {db.get_autopilot(root.id).state}")
        elif ap.state == "usage_paused":
            line = usage_resume(db, root.id, now)
            if line:
                done.append(line)
    return done


# -- the Stop hook -------------------------------------------------------------------


def on_stop(db: DB, agent: Agent, payload: dict) -> dict | None:
    """The supervisor is about to stop. Returns a "block" decision telling it
    to keep going, or None to let it stop."""
    ap = db.get_autopilot(agent.id)
    if ap is None or not ap.enabled or not ap.goal:
        return None
    from brindle import agents, login_pause

    if login_pause.paused(db, agent.id):
        return None  # only the person can renew the login; a nudge would just fail
    if ap.state == "auditing":
        if ap.auditor_id:
            auditor = db.get_agent(ap.auditor_id)
            if auditor is None or auditor.status in ("paused", "done") or not agents.is_alive(auditor) or auditor.result is not None:
                # Auditor was spawned but is no longer running (dead or crashed without verdict).
                # Reset state to running so autopilot can recover.
                db.update_autopilot(agent.id, state="running", auditor_id=None)
                ap = db.get_autopilot(agent.id)
            else:
                return None
        else:
            return None
    elif held(ap):
        if ap.nudged_at == ap.progress:
            return None  # reminded of the open question since the last step or user message
    elif ap.state != "running":
        return None

    ws = db.get_workspace(agent.workspace_id)
    cfg = load_repo_config(ws.repo_root) if ws else RepoConfig()
    ms = db.milestones(agent.id)
    if ms and all(m.status == "passed" for m in ms) and audit_due(ap, cfg, _commit(ws, tracked_only=True)) is None:
        db.update_autopilot(agent.id, state="done")
        return None

    if over_limit(db, agent.id, cfg):
        return None  # workers included: they'd keep burning usage
    if checking(ap):
        return None  # the check's result arrives as a message and wakes it up
    working, stalled = split_workers(db, agent.id, screen=True)
    # A worker waiting on plan approval waits on the supervisor, so the supervisor
    # can't stop for it (see nudge).
    if any(agents.runs_process(a) for a in working
            if getattr(a, "plan_state", None) != "proposed"):
        return None  # their results arrive as messages and wake it up
    if held(ap):
        # One reminder per step: never counts toward stalling.
        db.update_autopilot(agent.id, nudges=0, nudged_at=ap.progress)
        return {"decision": "block", "reason": nudge(db, ap, cfg, working, stalled)}
    now = time.time()
    if (ap.nudged_at == ap.progress and ap.nudged_time is not None
            and now - ap.nudged_time < NUDGE_BACKOFF):
        return None  # (#177) already reminded since the last step: it's legitimately waiting
    nudges = ap.nudges if ap.nudged_at == ap.progress else 0
    if nudges >= MAX_NUDGES:
        db.update_autopilot(agent.id, state="stalled", nudges=0,
                            note=f"no progress after {MAX_NUDGES} reminders to keep going")
        return None
    db.update_autopilot(agent.id, nudges=nudges + 1, nudged_at=ap.progress, nudged_time=now)
    return {"decision": "block", "reason": nudge(db, ap, cfg, working, stalled)}


def nudge(db: DB, ap: Autopilot, cfg: RepoConfig,
          working: list[Agent], stalled: list[Agent]) -> str:
    from brindle import agents

    # Subagent workers send no message when done; the supervisor records them.
    open_subagents = [a.id for a in working if not agents.runs_process(a)]
    pending = ""
    if open_subagents:
        pending = ("Subagent work not yet recorded: when each of your subagents finishes, call "
                   f"complete_subagent for {', '.join(open_subagents)}.\n\n")
    planning = [a.id for a in working if getattr(a, "plan_state", None) == "proposed"]
    if planning:
        pending += (f"Plans awaiting your approval: {', '.join(planning)}. Read each plan "
                    "(it arrived as a message) and call approve_plan, with feedback and "
                    "approved=false to ask for changes; the worker is waiting on you.\n\n")
    stuck = ""
    if stalled:
        seen = "\n".join(f"- {a.id}: {agents.liveness(db, a, db.get_workspace(a.workspace_id))}"
                         for a in stalled)
        stuck = (
            "These went idle without ever calling report_result and won't be reminded "
            f"again on their own. What brindle sees of each:\n{seen}\nDecide from that: "
            "send_message asking them to report or finish, workspace_diff for the full change, "
            "or remove_workspace if the work is abandoned.\n\n"
        )
    ms = db.milestones(ap.root_id)
    if ms and all(m.status == "passed" for m in ms):
        pending += ("Every milestone's check passes, but the completion audit hasn't approved "
                    "the current commit. Close any gaps it found, commit, and call "
                    "check_milestone: that starts a fresh audit.\n\n")
    if held(ap):
        pending += (f"Waiting on the user for: {ap.question}. Work only on what doesn't depend "
                    "on it; if nothing is left, stop. When they've answered, call "
                    "need_user(answered=true).\n\n")
    return (
        "[brindle autopilot] The goal isn't reached yet, and no workers are running.\n"
        f"{progress(db, ap.root_id)}\n\n{pending}{stuck}"
        "Keep going without waiting for the user: plan the next tasks for the first "
        f"unverified milestone and assign workers (at most {cfg.max_agents or 'any number'} "
        "at once), get finished branches reviewed and merged, and call check_milestone. "
        "If you need a decision only the user can make, call need_user with the question, "
        "then ask it."
    )


NO_CHECKS = """- This repo has no `checks` in .brindle/config.json, so nothing verifies a
  branch before it merges. Before the first `assign`, propose a check command
  to the user{suggestion}. In that same question offer to allow its individual
  commands in .claude/settings.local.json, so they answer once. When they
  agree, call `add_checks` (allow=true if they accepted the permissions) and
  commit .brindle/config.json.
"""


def add_checks(repo_root: str, checks: list[str], allow: bool = False) -> str:
    """Add approved ``checks`` to .brindle/config.json and, with ``allow``, one
    narrow Bash allow rule per command in them to .claude/settings.local.json."""
    from brindle.config import CONFIG_FILE, _read_json

    root = Path(config_root(repo_root))
    path = root / CONFIG_DIR / CONFIG_FILE
    data = _read_json(path)
    data["checks"] = [*(c for c in data.get("checks") or [] if c not in checks), *checks]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    text = f"Added {len(checks)} check(s) to {path}."
    if not allow:
        return text
    settings_path = root / ".claude" / "settings.local.json"
    settings = _read_json(settings_path)
    perms = settings.setdefault("permissions", {})
    allowed = perms.setdefault("allow", [])
    commands = [c.strip() for check in checks for c in re.split(r"&&|\|\||;|\|", check)]
    for rule in (f"Bash({c})" for c in commands if c):
        if rule not in allowed:
            allowed.append(rule)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    return f"{text} Allowed their commands in {settings_path}."


def guide(cfg: RepoConfig, repo_root: str | None = None) -> str:
    text = GUIDE.format(max_agents=cfg.max_agents or "any number of")
    if repo_root and not cfg.checks:
        from brindle import detect

        found = detect.detect(repo_root).checks
        suggestion = (" (this repo looks like it runs: "
                      + "; ".join(f"`{c}`" for c in found) + ")") if found else ""
        text += NO_CHECKS.format(suggestion=suggestion)
    return text


def kickoff(plan: Plan, root_id: str) -> str:
    return KICKOFF.format(goal=plan.goal, n=len(plan.milestones), root_id=root_id)


GOAL_REMINDER = (
    "[brindle autopilot] This session has no goal yet. Call set_goal now, without asking: "
    "the user's request is the goal, its parts are the milestones, each with a check. "
    "Later requests in this session become new milestones on it."
)


def goal_reminder(db: DB, caller_id: str) -> str | None:
    """What a delegating supervisor is told when its autopilot session has no
    goal yet. Only a session root is reminded: a nested supervisor delegating
    isn't the one that owns the goal."""
    if root_of(db, caller_id) != caller_id:
        return None
    ap = db.get_autopilot(caller_id)
    return GOAL_REMINDER if ap and ap.enabled and not ap.goal else None


def worker_goal(prompt: str, done_when: str, branch: str) -> str | None:
    """A worker's first message as a Claude Code ``/goal``: Claude keeps working
    until a small model judges the finish line met. None if too long for one."""
    text = (f"/goal Finish line: {done_when.strip()} The work is committed on branch "
            f"`{branch}` and reported with the brindle report_result tool.\n\nTask:\n{prompt}")
    return text if len(text) - len("/goal ") <= MAX_GOAL_CHARS else None

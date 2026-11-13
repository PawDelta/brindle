#!/usr/bin/env python3
"""Benchmark: brindle vs. a single Claude Code session, on one task.

    python3 scripts/bench_solo_vs_brindle.py run --arm solo --run-id 1
    python3 scripts/bench_solo_vs_brindle.py run --arm brindle --run-id 1
    python3 scripts/bench_solo_vs_brindle.py report --require-runs 5

The task kit lives in bench/solo_vs_brindle/task/: ``repo.json`` ({"url",
"commit", "setup": [cmds], "test_cmd"}), ``spec.md`` (the prompt) and
``grade.sh <checkout>``, which prints {"passed", "failed", "per_part"}.

``run`` clones the repo at the pinned commit into a scratch directory outside
this repo, with its origin a local bare copy there and every GitHub URL sent
nowhere (no arm may push to, fork or open a PR on the task's public repo),
runs the setup commands, starts the clock, then runs one arm:

- solo: ``claude -p <spec> --model claude-fable-5-2 --output-format
  stream-json --verbose`` in the clone, in ``--permission-mode`` (default
  "default"), with ``--permission-prompt-tool`` pointing at a small stdio MCP
  server in this script that logs and approves each request (an
  AskUserQuestion gets its recommended, else first, option). A turn that ends
  by asking the person something is answered by resuming the same session.
  Tokens per model (subagents included) are each turn's result totals.
- brindle: the INSTALLED ``brindle`` (never this checkout's CLI: it would
  migrate the real database). ``brindle init`` in the clone, the supervisor
  pinned to ``--supervisor-model`` (a repo profile named in
  ``supervisor_profile``), then ``brindle start --no-attach -p <spec>``. Done
  is the goal reached with nothing actionable left in the Inbox, or the whole
  session quiet (supervisor idle, no agent working, nothing waiting on the
  person) for ``--idle-grace`` seconds; the clock stops then. A permission
  ask only the person can answer, in the app, is never answered here: the
  run waits for them (``needs_person``). Tokens per model for the supervisor and every worker
  and reviewer of that session come from their transcripts (Codex and others
  without one: brindle's own usage reader), read through the installed
  brindle's Python.

Person-facing interruptions are counted in both arms. Solo: each request the
permission tool logged, each AskUserQuestion, and each turn that ends in a
question. brindle: permission asks its checker or the worker's supervisor did
not answer (``permission_asks``), the supervisor stopping on a question or a
need_user call, and anything else that lands in the session's Inbox. Each but
a held permission ask is answered automatically ("go ahead with your
recommendation", or the Inbox item's own answer) so the run keeps going.

Then ``grade.sh`` runs on the final checkout (brindle: the session branch's
worktree) and one JSON record is appended to
``.brindle/bench/solo_vs_brindle.jsonl``.

A run that hits an interruption the runner can't automate (an expired login,
an Inbox item the supervisor doesn't resolve), or that gives up, is recorded
with ``"valid": false`` and why.

``report`` prints, per arm, the median and min-max of wall time, total tokens,
cost, hidden-test pass rate and interruptions over the valid runs; with
``--require-runs N`` it exits non-zero when an arm has fewer than N of them.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = Path(__file__).resolve()
TASK_DIR = REPO / "bench" / "solo_vs_brindle" / "task"
RESULTS = REPO / ".brindle" / "bench" / "solo_vs_brindle.jsonl"
ARMS = ("solo", "brindle")
SOLO_MODEL = "claude-fable-5-2"
SUPERVISOR_MODEL = "claude-opus-5-5"
ANSWER = "go ahead with your recommendation"
APPROVER_SERVER = "benchperm"
APPROVER_TOOL = "approve"
PROFILE_NAME = "bench-supervisor"

# cache_creation is every cache write; cache_creation_1h the part of it written
# with the 1-hour TTL (priced higher), so it isn't added again in totals.
BASE_KINDS = ("input", "output", "cache_creation", "cache_read")
KINDS = (*BASE_KINDS, "cache_creation_1h")
MTOK = 1_000_000
CACHE_WRITE_FACTOR = 1.25      # a 5-minute cache write costs 1.25x the input rate
CACHE_WRITE_1H_FACTOR = 2.0    # a 1-hour one, 2x
# List prices, $ per MTok: input, output, cache read.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.0, 40.0, 0.20),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),   # cache read: Anthropic's usual 0.1x input
}
SYNTHETIC = "<synthetic>"
ASK_TOOL = "AskUserQuestion"

# permission_asks.resolved_by values that mean nobody had to be asked.
AUTO_RESOLVED = {"policy", "saved_action", "supervisor"}
# Inbox kinds that put a question to the person; the rest are "other".
QUESTION_KINDS = {"question", "waiting", "plan", "plan_offer", "proposed_checks", "visual"}
# How the runner handles each Inbox kind, as the person would:
#   answer    the item's own operation (the probe's "answer"); blocks done while listed
#   delegate  told to the supervisor to sort out; blocks done while listed, and the
#             run ends unresolved if it is still listed after --unresolved-after
#   note      nothing to do (a PR opened, or ready to open: publishing is not the task)
#   stuck     nothing the runner can do: the run ends there, marked invalid
# A kind not listed here is delegated.
HANDLING = {"plan": "answer", "plan_offer": "answer", "proposed_checks": "answer", "visual": "answer",
            "doc": "answer", "pr_failed": "delegate", "sync_conflict": "delegate", "failed": "delegate",
            "pr_ready": "note", "pr_opened": "note", "login": "stuck", "idle_stopped": "stuck"}
# Providers whose permission ask reaches their own prompt once the app's hold
# ends (service.inbox.PROMPT_AFTER_HOLD); any other's is refused then.
PROMPT_AFTER_HOLD = {"claude", "codex"}
VALID_FINISHES = {"goal", "idle", "exit"}
BUSY = ("processing", "starting")
TAIL_BYTES = 256 * 1024


class BenchError(Exception):
    pass


class UnknownModel(BenchError):
    pass


# -- tokens and dollars -----------------------------------------------------------------------------


def normalize_model(model: str) -> str:
    """``model`` without a context tag (``[1m]``), provider prefix or date suffix."""
    m = model.strip().lower()
    if m.endswith("]") and "[" in m:
        m = m[:m.rindex("[")]
    for prefix in ("anthropic/", "anthropic.", "us.anthropic.", "eu.anthropic."):
        m = m.removeprefix(prefix)
    head, _, tail = m.rpartition("-")
    if head and len(tail) == 8 and tail.isdigit():
        m = head
    return m


def empty() -> dict[str, int]:
    return dict.fromkeys(KINDS, 0)


def add_usage(into: dict[str, dict[str, int]], model: str, kinds: dict[str, int]) -> None:
    bucket = into.setdefault(model, empty())
    for k in KINDS:
        bucket[k] += int(kinds.get(k) or 0)


def merge(into: dict[str, dict[str, int]], other: dict[str, dict[str, int]]) -> None:
    for model, kinds in other.items():
        add_usage(into, model, kinds)


def iter_json(lines) -> list[dict]:
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def message_usage(entries: list[dict]) -> dict[str, dict[str, int]]:
    """Tokens per model of the assistant messages in ``entries`` (a stream-json
    stream or a transcript). A message streamed as several entries (one per
    content block) repeats one usage under one id: it counts once."""
    seen: dict[str, tuple[str, dict]] = {}
    anonymous: list[tuple[str, dict]] = []
    for entry in entries:
        if entry.get("type") != "assistant" or not isinstance(entry.get("message"), dict):
            continue
        msg = entry["message"]
        usage, model = msg.get("usage"), msg.get("model")
        if not isinstance(usage, dict) or not isinstance(model, str) or model == SYNTHETIC:
            continue
        if msg.get("id"):
            seen[msg["id"]] = (model, usage)
        else:
            anonymous.append((model, usage))
    by_model: dict[str, dict[str, int]] = {}
    for model, u in [*seen.values(), *anonymous]:
        ttl = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
        add_usage(by_model, normalize_model(model), {
            "input": u.get("input_tokens"), "output": u.get("output_tokens"),
            "cache_creation": u.get("cache_creation_input_tokens"),
            "cache_creation_1h": ttl.get("ephemeral_1h_input_tokens"),
            "cache_read": u.get("cache_read_input_tokens")})
    return by_model


def cli_usage(model_usage: dict) -> dict[str, dict[str, int]]:
    """A result event's ``modelUsage``: the CLI's own per-model totals."""
    by_model: dict[str, dict[str, int]] = {}
    for model, u in model_usage.items():
        if isinstance(u, dict):
            add_usage(by_model, normalize_model(model), {
                "input": u.get("inputTokens"), "output": u.get("outputTokens"),
                "cache_creation": u.get("cacheCreationInputTokens"),
                "cache_creation_1h": u.get("cacheCreationEphemeral1hInputTokens"),
                "cache_read": u.get("cacheReadInputTokens")})
    return by_model


def stream_usage(entries: list[dict]) -> dict[str, dict[str, int]]:
    """Tokens per model over a ``-p`` stream (one or more turns). Each turn's
    come from its result event's per-model totals: the stream's assistant
    messages carry the usage from the start of each message, so their output
    counts stop short. A turn with no such totals (killed before its result)
    falls back to its assistant messages."""
    by_model: dict[str, dict[str, int]] = {}
    turn: list[dict] = []
    for entry in entries:
        turn.append(entry)
        if entry.get("type") != "result":
            continue
        totals = entry.get("model_usage_since_last_result") or entry.get("modelUsage")
        merge(by_model, cli_usage(totals) if isinstance(totals, dict) and totals else message_usage(turn))
        turn = []
    merge(by_model, message_usage(turn))
    return by_model


def permission_mode(entries: list[dict]) -> str | None:
    """The permission mode the CLI says it ran in (its init event)."""
    return next((e.get("permissionMode") for e in entries
                 if e.get("type") == "system" and e.get("subtype") == "init"), None)


def transcript_usage(path: str) -> dict[str, dict[str, int]]:
    """A Claude Code transcript's tokens per model, with its subagents' (stored
    under ``<session>/subagents/``)."""
    main = Path(path)
    by_model: dict[str, dict[str, int]] = {}
    files = [main, *sorted((main.parent / main.stem / "subagents").glob("*.jsonl"))]
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except OSError:
            continue
        merge(by_model, message_usage(iter_json(lines)))
    return by_model


def tokens_total(by_model: dict[str, dict[str, int]]) -> int:
    return sum(k.get(kind, 0) for k in by_model.values() for kind in BASE_KINDS)


def price_usage(by_model: dict[str, dict[str, int]], rates: dict[str, list[float]] | None = None,
                free: set[str] | frozenset = frozenset()) -> tuple[float, dict[str, float], list[str]]:
    """Dollars for ``by_model``: the total, per model, and the models left
    unpriced. A Claude model this script has no price for raises UnknownModel
    (never priced at 0). Another provider's model is priced at brindle's own
    rate (``rates``: input, output, cache write, cache read per MTok), costs
    nothing when it ran on this machine (``free``), else is listed as unpriced."""
    rates = rates or {}
    total, per, unpriced = 0.0, {}, []
    for model, k in sorted(by_model.items()):
        hour = k.get("cache_creation_1h", 0)
        if model in PRICES:
            inp, out, read = PRICES[model]
            write, write_1h = inp * CACHE_WRITE_FACTOR, inp * CACHE_WRITE_1H_FACTOR
        elif model.startswith("claude"):
            raise UnknownModel(f"no price for model {model!r}: add it to PRICES in {SCRIPT.name}")
        elif model in free:
            inp = out = write = write_1h = read = 0.0
        elif model in rates:
            inp, out, write, read = rates[model]
            write_1h = write   # brindle's table has one cache-write rate
        else:
            unpriced.append(model)
            continue
        dollars = (k["input"] * inp + k["output"] * out + (k["cache_creation"] - hour) * write
                   + hour * write_1h + k["cache_read"] * read) / MTOK
        per[model] = round(dollars, 6)
        total += dollars
    return round(total, 6), per, unpriced


# -- interruptions ----------------------------------------------------------------------------------


def ends_with_question(text: str | None) -> bool:
    """Whether a reply ends by asking the person something."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return bool(lines) and lines[-1].rstrip("*_`) \"'").endswith("?")


def ask_tool_uses(entries: list[dict]) -> int:
    ids = set()
    for entry in entries:
        msg = entry.get("message") if entry.get("type") == "assistant" else None
        content = msg.get("content") if isinstance(msg, dict) else None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == ASK_TOOL:
                ids.add(block.get("id") or len(ids))
    return len(ids)


def results(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("type") == "result"]


def solo_prompts(entries: list[dict], approvals: list[dict]) -> dict[str, int]:
    """The solo session's interruptions: each permission request its prompt
    tool logged, each AskUserQuestion, and each turn ending in a question."""
    closing = sum(ends_with_question(r.get("result") if isinstance(r.get("result"), str) else None)
                  for r in results(entries))
    permission = sum(1 for a in approvals if a.get("tool_name") != ASK_TOOL)
    return {"permission": permission, "question": ask_tool_uses(entries) + closing, "other": 0}


def brindle_prompts(asks: list[dict], events: list[dict]) -> dict[str, int]:
    """The brindle session's interruptions that reached the person: permission
    asks neither the policy, a saved action nor the worker's supervisor
    answered (a pending one counts: it was waiting on the person), and the
    questions and other Inbox items seen while it ran (``events``)."""
    permission = sum(1 for r in asks if r.get("asked") and r.get("resolved_by") not in AUTO_RESOLVED)
    question = sum(1 for e in events if e["kind"] in QUESTION_KINDS)
    return {"permission": permission, "question": question, "other": len(events) - question}


# -- the solo arm's permission prompt tool ----------------------------------------------------------


def recommended(question: dict) -> str | None:
    """The option an AskUserQuestion recommends (its label says so), else its first."""
    labels = [o.get("label") for o in question.get("options") or () if isinstance(o, dict)]
    return next((label for label in labels if label and "recommended" in label.lower()),
                labels[0] if labels else None)


def approval(tool_name: str | None, tool_input: dict) -> dict:
    """The permission tool's answer: allow, unchanged; an AskUserQuestion is
    allowed with each question answered by its recommended option."""
    if tool_name == ASK_TOOL:
        questions = [q for q in tool_input.get("questions") or () if isinstance(q, dict)]
        tool_input = {**tool_input, "answers": {q.get("question"): recommended(q) or ANSWER for q in questions}}
    return {"behavior": "allow", "updatedInput": tool_input}


def approver_reply(msg: dict, log_path: Path) -> dict | None:
    """One JSON-RPC message to the approver MCP server: its reply, or None for
    a notification. A tools/call is logged, then allowed."""
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        result = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": APPROVER_SERVER, "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{
            "name": APPROVER_TOOL,
            "description": "Approves a permission request (benchmark harness).",
            "inputSchema": {"type": "object", "properties": {
                "tool_name": {"type": "string"}, "input": {"type": "object"},
                "tool_use_id": {"type": "string"}}}}]}
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        with log_path.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "tool_name": args.get("tool_name"),
                                "input": args.get("input")}) + "\n")
        decision = approval(args.get("tool_name"), args.get("input") or {})
        result = {"content": [{"type": "text", "text": json.dumps(decision)}]}
    elif method == "ping":
        result = {}
    elif mid is None:
        return None
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"no method {method}"}}
    return None if mid is None else {"jsonrpc": "2.0", "id": mid, "result": result}


def serve_approver(log_path: Path) -> int:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        reply = approver_reply(msg, log_path) if isinstance(msg, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


# -- report -----------------------------------------------------------------------------------------


def load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return iter_json(path.read_text().splitlines())


def pass_rate(grade: dict | None) -> float | None:
    if not isinstance(grade, dict):
        return None
    try:
        passed, failed = int(grade.get("passed") or 0), int(grade.get("failed") or 0)
    except (TypeError, ValueError):
        return None
    return passed / (passed + failed) if passed + failed else None


METRICS = ("wall_seconds", "tokens_total", "cost_usd", "pass_rate", "prompts_total")


def summarize(records: list[dict]) -> dict[str, dict]:
    """Per arm: the valid run count, the invalid ones (an interruption the
    runner couldn't automate, or it gave up), and each metric's median, min
    and max over the valid runs."""
    out: dict[str, dict] = {}
    for arm in sorted({r.get("arm") for r in records} | set(ARMS)):
        mine = [r for r in records if r.get("arm") == arm]
        runs = [r for r in mine if r.get("valid", True)]
        s: dict = {"runs": len(runs), "invalid": len(mine) - len(runs)}
        for metric in METRICS:
            values = [r.get(metric) if metric != "pass_rate" or "pass_rate" in r else pass_rate(r.get("grade"))
                      for r in runs]
            values = [v for v in values if isinstance(v, (int, float))]
            s[metric] = ({"median": statistics.median(values), "min": min(values), "max": max(values)}
                         if values else None)
        out[arm] = s
    return out


def _fmt(metric: str, v: float) -> str:
    if metric == "cost_usd":
        return f"${v:,.2f}"
    if metric == "pass_rate":
        return f"{v:.0%}"
    if metric == "wall_seconds":
        return f"{v / 60:.1f}m"
    return f"{v:,.0f}"


def cmd_report(args) -> int:
    summary = summarize(load_records(Path(args.results)))
    short = []
    for arm, s in summary.items():
        print(f"{arm} ({s['runs']} runs" + (f", {s['invalid']} invalid left out)" if s["invalid"] else ")"))
        for metric in METRICS:
            m = s[metric]
            shown = "-" if m is None else (f"{_fmt(metric, m['median'])} "
                                           f"({_fmt(metric, m['min'])} - {_fmt(metric, m['max'])})")
            print(f"  {metric:<14} {shown}")
        if s["runs"] < args.require_runs:
            short.append(arm)
    if short:
        print(f"fewer than {args.require_runs} runs: {', '.join(short)}", file=sys.stderr)
        return 1
    return 0


# -- running an arm ---------------------------------------------------------------------------------


def wait(seconds: float) -> None:
    threading.Event().wait(seconds)


def clean_env() -> dict[str, str]:
    """This process's environment without what makes a child think it runs
    inside a brindle agent, a tmux pane or a Claude Code session."""
    drop = {"TMUX", "TMUX_PANE", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CONFIG_DIR", "VIRTUAL_ENV"}
    return {k: v for k, v in os.environ.items()
            if k not in drop and not (k.startswith("BRINDLE_") and k != "BRINDLE_HOME")}


def sh(cmd, cwd: Path, env: dict, check: bool = True, **kw) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, check=False, cwd=cwd, env=env, text=True, stdin=subprocess.DEVNULL, **kw)
    if check and res.returncode != 0:
        raise BenchError(f"{cmd if isinstance(cmd, str) else ' '.join(map(str, cmd[:3]))} "
                         f"failed with exit code {res.returncode}")
    return res


def load_kit(task_dir: Path) -> tuple[dict, str, Path]:
    task_dir = task_dir.resolve()   # grade.sh runs from inside the clone
    for name in ("repo.json", "spec.md", "grade.sh"):
        if not (task_dir / name).is_file():
            raise BenchError(f"task kit incomplete: no {task_dir / name}")
    return json.loads((task_dir / "repo.json").read_text()), (task_dir / "spec.md").read_text(), task_dir / "grade.sh"


# Where a GitHub URL used in the clone goes instead: a path that doesn't exist,
# so a push, fetch or PR against the task's real (someone else's) repo fails.
BLOCKED_URL = "file:///nonexistent/bench-solo-vs-brindle-no-github/"
GITHUB_PREFIXES = ("https://github.com/", "http://github.com/", "git@github.com:", "ssh://git@github.com/")


def is_local(url: str) -> bool:
    return url.startswith(("/", "file://"))


def remote_urls(path: Path, env: dict) -> list[str]:
    res = sh(["git", "config", "--get-regexp", r"^remote\..*url$"], path, env, check=False, capture_output=True)
    return [line.split(" ", 1)[1] for line in res.stdout.splitlines() if " " in line]


def github_rewrites(path: Path, env: dict) -> list[str]:
    """The URL prefixes git in ``path`` sends to BLOCKED_URL instead."""
    res = sh(["git", "config", "--get-all", f"url.{BLOCKED_URL}.insteadOf"], path, env,
             check=False, capture_output=True)
    return res.stdout.split()


def prepare_clone(repo: dict, scratch: Path, env: dict) -> Path:
    """The task repo at its pinned commit, cut off from where it came from:
    its origin is a local bare copy in ``scratch`` (no remote of its own), and
    any GitHub URL git is given there goes nowhere. Neither arm can then push
    to, fork or open a PR on the task's public repo."""
    origin, clone = scratch / "origin.git", scratch / "repo"
    sh(["git", "clone", "-q", "--bare", repo["url"], str(origin)], scratch, env)
    sh(["git", "config", "--remove-section", "remote.origin"], origin, env, check=False)
    sh(["git", "clone", "-q", str(origin), str(clone)], scratch, env)
    for path in (origin, clone):
        for prefix in GITHUB_PREFIXES:
            sh(["git", "config", "--add", f"url.{BLOCKED_URL}.insteadOf", prefix], path, env)
    for path in (origin, clone):
        foreign = [u for u in remote_urls(path, env) if not is_local(u)]
        if foreign:
            raise BenchError(f"{path} still has a remote off this machine: {foreign}")
    sh(["git", "checkout", "-q", "-B", "bench", repo["commit"]], clone, env)
    for cmd in repo.get("setup") or []:
        sh(cmd, clone, env, shell=True)
    return clone


def grade(grade_sh: Path, checkout: Path, env: dict) -> dict:
    res = sh(["bash", str(grade_sh), str(checkout)], checkout, env, check=False, capture_output=True)
    for text in (res.stdout, *reversed(res.stdout.strip().splitlines())):
        try:
            found = json.loads(text)
        except ValueError:
            continue
        if isinstance(found, dict):
            return found
    return {"error": f"grade.sh exit {res.returncode}: no JSON on stdout", "passed": 0, "failed": 0}


def main_model(by_model: dict[str, dict[str, int]]) -> str | None:
    return max(by_model, key=lambda m: by_model[m]["output"], default=None)


def solo_session(run_turn, spec: str, max_answers: int) -> list[dict]:
    """The solo session's stream-json entries over all its turns:
    ``run_turn(prompt, session_id)`` runs one (``session_id`` None: a new
    session) and returns its entries. A turn that ends by asking the person
    something is answered in the same session, up to ``max_answers`` times."""
    entries = run_turn(spec, None)
    for _ in range(max_answers):
        last = (results(entries) or [{}])[-1]
        if last.get("gave_up") or not ends_with_question(last.get("result") if isinstance(
                last.get("result"), str) else None):
            break
        session = last.get("session_id") or next(
            (e["session_id"] for e in reversed(entries) if isinstance(e.get("session_id"), str)), None)
        if not session:
            break
        entries += run_turn(ANSWER, session)
    return entries


def run_solo(args, clone: Path, spec: str, scratch: Path, env: dict) -> dict:
    claude = shutil.which("claude", path=env.get("PATH"))
    if not claude:
        raise BenchError("no `claude` on PATH")
    approvals, stream = scratch / "approvals.jsonl", scratch / "solo_stream.jsonl"
    approvals.touch()
    mcp = scratch / "mcp.json"
    mcp.write_text(json.dumps({"mcpServers": {APPROVER_SERVER: {
        "command": sys.executable, "args": [str(SCRIPT), "approver", "--log", str(approvals)]}}}))
    flags = ["--model", args.model, "--output-format", "stream-json", "--verbose",
             "--mcp-config", str(mcp), "--strict-mcp-config",
             "--permission-prompt-tool", f"mcp__{APPROVER_SERVER}__{APPROVER_TOOL}",
             *(["--permission-mode", args.permission_mode] if args.permission_mode else [])]
    t0 = time.time()
    deadline = t0 + args.max_hours * 3600
    state = {"finished": "exit"}

    def run_turn(prompt: str, session: str | None) -> list[dict]:
        turn = scratch / f"solo_turn_{len(list(scratch.glob('solo_turn_*')))}.jsonl"
        cmd = [claude, "-p", prompt, *flags, *(["--resume", session] if session else [])]
        with turn.open("w") as out:
            proc = subprocess.Popen(cmd, cwd=clone, env=env, stdout=out, stdin=subprocess.DEVNULL, text=True)
            try:
                code = proc.wait(max(1.0, deadline - time.time()))
                state["finished"] = "exit" if code == 0 else f"exit:{code}"
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                state["finished"] = "gave_up"
        text = turn.read_text(errors="replace")
        with stream.open("a") as f:
            f.write(text)
        entries = iter_json(text.splitlines())
        return entries + ([{"type": "result", "gave_up": True}] if state["finished"] == "gave_up" else [])

    entries = solo_session(run_turn, spec, args.max_answers)
    stop = time.time()
    by_model = stream_usage(entries)
    return {"finished": state["finished"], "valid": state["finished"] in VALID_FINISHES, "why": None,
            "permission_mode": permission_mode(entries),
            "wall_seconds": round(stop - t0, 1), "by_model": by_model,
            "by_role": {"solo": by_model}, "rates": {}, "free": set(),
            "prompts": solo_prompts(entries, iter_json(approvals.read_text().splitlines())),
            "checkout": clone, "supervisor_model": args.model,
            "supervisor_model_observed": main_model(by_model),
            "turns": len(results(entries)),
            "cli_model_usage": [r.get("modelUsage") for r in results(entries)]}


# -- the brindle arm --------------------------------------------------------------------------------

# Runs under the INSTALLED brindle's Python (``python -I``: nothing from this
# checkout or the current directory is importable), so it reads the database
# with the code that owns its schema. It never calls service.inbox.items: that
# would mark requests the daemon holds (in its own memory) as gone to the CLI's
# prompt; nor anything that asks GitHub.
PROBE = r'''
import json, os, sys, time


def main(args):
    import brindle
    from brindle import autopilot
    from brindle.db import DB

    db = DB()
    op = args["op"]
    out = {"errors": [], "brindle_file": brindle.__file__}

    def real(p):
        return os.path.realpath(p) if p else p

    def describe(a):
        ws = db.get_workspace(a.workspace_id)
        d = {k: getattr(a, k, None) for k in ("id", "profile", "provider", "parent_id", "mode", "status",
                                              "status_since", "created_at", "transcript_path", "session_ref")}
        d.update(workspace_path=ws.path if ws else None, branch=ws.branch if ws else None,
                 repo_root=ws.repo_root if ws else None, local=False)
        try:
            from brindle import pricing
            from brindle.profiles import load_profile
            d["local"] = pricing.is_local(load_profile(a.profile, ws.repo_root if ws else None).base_url)
        except Exception:
            pass
        return d

    def in_repo(a):
        ws = db.get_workspace(a.workspace_id)
        return ws is not None and real(ws.repo_root) == real(args["clone"])

    def guarded(name, fn):
        try:
            return fn()
        except Exception as e:
            out["errors"].append("%s: %r" % (name, e))
            return None

    if op == "find_root":
        found = [a for a in db.list_agents() if a.profile == "supervisor" and a.mode == "interactive"
                 and not a.parent_id and a.created_at >= args["since"] and in_repo(a)]
        found.sort(key=lambda a: a.created_at)
        out["root"] = describe(found[-1]) if found else None
    elif op == "snapshot":
        rid, repo = args["root_id"], args["repo_root"]
        agents = [a for a in db.list_agents() if in_repo(a) and autopilot.root_of(db, a.id) == rid]
        out["agents"] = [describe(a) for a in agents]
        ap = db.get_autopilot(rid)
        out["autopilot"] = {"state": ap.state, "question": ap.question, "goal": ap.goal} if ap else None
        from brindle import config
        out["permission_mode"] = guarded("permission mode", config.claude_permission_mode)
        now, items = time.time(), []
        for a in agents:
            rows = [dict(r) for r in db.permission_requests_of(a.id)]
            holds = {r["channel"] for r in rows if r["channel"] in ("hook", "supervisor")
                     and (r["hold_until"] or 0) > now}
            if a.status == "waiting" or holds:
                # held_by: the person's (the app's hold), else still its supervisor's to answer first.
                items.append({"kind": "waiting" if a.status == "waiting" else "permission", "agent_id": a.id,
                              "held": bool(holds), "held_by": "person" if "hook" in holds else "supervisor",
                              "provider": a.provider, "permission": bool(rows)})
            if a.status == "failed":
                items.append({"kind": "failed", "agent_id": a.id, "detail": getattr(a, "failed_why", None)})
            if getattr(a, "plan_state", None) == "proposed" and getattr(a, "plan_first", None) and a.parent_id:
                items.append({"kind": "plan", "agent_id": a.id, "detail": (a.plan_text or "")[:200]})
        if ap is not None and autopilot.proposing(ap):
            items.append({"kind": "proposed_checks", "agent_id": rid, "detail": ap.proposal})
        root = db.get_agent(rid)
        home = db.get_workspace(root.workspace_id) if root else None
        if home is not None:
            # The Inbox's own PR card (its precedence, dismissals and merges), on what
            # brindle recorded: in this short-lived process GitHub is never asked.
            from brindle.daemon import session_review
            from brindle.service import inbox
            kept = session_review.open_pr_for_head, session_review.head_pr_known_none
            session_review.open_pr_for_head = lambda *a, **k: None
            session_review.head_pr_known_none = lambda *a, **k: False
            link = {"agent_id": rid, "workspace_id": home.id, "root_id": rid}
            try:
                prs = guarded("pr", lambda: inbox._pr_items(db, rid, home.branch, home, link)) or []
            finally:   # put back: the probe may run inside a longer-lived process (tests)
                session_review.open_pr_for_head, session_review.head_pr_known_none = kept
            for i in prs:
                items.append({"kind": i.kind, "agent_id": rid, "detail": i.detail or i.url})
            from brindle import session_sync
            stuck = guarded("sync", lambda: session_sync.needs_you(db, home))
            if stuck:
                items.append({"kind": "sync_conflict", "agent_id": rid, "detail": stuck})
        login = guarded("login", lambda: db.get_login_state(rid))
        if login is not None and login.state == "auth_paused":
            items.append({"kind": "login", "agent_id": rid, "detail": login.refresh_cmd})
        from brindle.service import inbox
        ids = {a.id for a in agents}
        for name, fn in (("visuals", lambda: inbox._visual_items(db, repo)),
                         ("plan offers", lambda: inbox._plan_offer_items(db, repo)),
                         ("docs", lambda: inbox._doc_items(db, repo, ids)),
                         ("idle", lambda: inbox._idle_items(db, repo))):
            for i in guarded(name, fn) or []:
                if i.root_id == rid:
                    item = {"kind": i.kind, "agent_id": i.agent_id, "detail": i.detail,
                            "visual_id": i.visual_id, "request_id": i.request_id}
                    if i.visual_id:   # each revision is its own question
                        from brindle import visuals
                        item["rev"] = guarded("visual rev", lambda: visuals.get(db, i.visual_id)["rev"])
                    items.append(item)
        out["inbox"] = items
        out["outside_usage"] = {}
        out["rates"] = {}
        try:
            from brindle import pricing
            from brindle.usage import agent_usage
            extra = pricing.repo_overrides(repo)
            for model, p in {**pricing.PRICES, **extra}.items():
                out["rates"][model] = [p.input, p.output, p.cache_write, p.cache_read]
            for a in agents:
                if a.provider == "claude" or a.transcript_path:
                    continue   # its transcript is read directly: one source per agent
                u = agent_usage(db, a)
                if u is not None:
                    out["outside_usage"][a.id] = {
                        "model": u.model or "unknown", "input": u.input_tokens, "output": u.output_tokens,
                        "cache_read": u.cache_read_tokens, "cache_creation": u.cache_creation_tokens}
        except Exception as e:
            out["errors"].append("usage: %r" % (e,))
        out["permission_asks"] = [dict(r) for r in db.conn.execute(
            "SELECT * FROM permission_asks WHERE repo_root=?", (repo,)).fetchall()]
        out["history"] = [{"agent_id": h.agent_id, "kind": h.kind, "profile": h.profile, "tokens": h.tokens}
                          for h in db.list_history(repo, None, 10_000_000) if h.tokens]
    elif op == "press_enter":
        from brindle import agents as ag, tmux
        a = db.get_agent(args["agent_id"])
        if a is not None and a.tmux_window:
            tmux.send_keys(a.tmux_window, "Enter", server=ag.server_of(a))
    elif op == "answer":
        # The person's answer, through the same operation the app's Inbox action runs.
        item, rid = args["item"], args["root_id"]
        kind = item["kind"]
        if kind == "plan":
            from brindle import agents as ag
            a = db.get_agent(item["agent_id"])
            ag.approve_plan(db, a.parent_id, a.id, "", True)
        elif kind == "proposed_checks":
            ap = db.get_autopilot(rid)
            if ap is not None and autopilot.proposing(ap):
                checks = json.loads(ap.proposal).get("checks") or []
                autopilot.add_checks(args["repo_root"], checks, False)
                with db.tx() as c:
                    c.execute("UPDATE autopilot SET proposal = NULL WHERE root_id = ? AND proposal = ?",
                              (rid, ap.proposal))
        elif kind == "visual":
            from brindle import visuals
            v = visuals.get(db, item["visual_id"])
            rev = item.get("rev") or v["rev"]   # the revision that was on screen
            if v["options"]:
                visuals.answer(db, v["id"], rev, option=v["options"][0]["id"], note=args["answer"])
            else:
                visuals.mark_read(db, v["id"], rev)
        elif kind == "plan_offer":
            from brindle import plans
            plans.answer_offer(db, rid, item["request_id"], False)
        elif kind == "doc":
            from brindle.service import inbox
            inbox.dismiss_doc_read(db, item["request_id"])
        out["answered"] = kind
    return out


if __name__ == "__main__":
    print(json.dumps(main(json.loads(sys.argv[1])), default=str))
'''


def installed_brindle(explicit: str | None, env: dict) -> Path:
    found = explicit or shutil.which("brindle", path=env.get("PATH"))
    if not found:
        raise BenchError("no installed `brindle` on PATH: pass --brindle")
    path = Path(found).resolve()
    if path.is_relative_to(REPO):
        raise BenchError(f"{found} is this checkout's brindle; use the installed one (--brindle)")
    return Path(found)


def brindle_python(brindle: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    # brindle.app's launcher (engine/bin/brindle) runs the engine's own Python.
    engine_python = brindle.resolve().parent.parent / "python" / "bin" / "python3"
    if engine_python.is_file():
        return str(engine_python)
    try:
        first = brindle.resolve().read_bytes().split(b"\n", 1)[0].decode()
    except (OSError, UnicodeDecodeError):
        first = ""
    if not first.startswith("#!") or "python" not in first:
        raise BenchError(f"can't tell which Python runs {brindle}: pass --brindle-python")
    return first[2:].strip().split()[0]


class Brindle:
    """The installed brindle, driven from outside."""

    def __init__(self, exe: Path, python: str, clone: Path, env: dict):
        self.exe, self.python, self.clone, self.env = exe, python, clone, env

    def cli(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
        return sh([str(self.exe), *argv], self.clone, self.env, check=check, capture_output=True)

    def probe(self, op: str, **kw) -> dict:
        # -B: nothing written into an app bundle; its bin first, as its launcher does (tmux).
        env = {**self.env, "PATH": f"{self.exe.resolve().parent}{os.pathsep}{self.env.get('PATH', '')}"}
        res = sh([self.python, "-I", "-B", "-c", PROBE, json.dumps({"op": op, "clone": str(self.clone), **kw})],
                 self.clone, env, check=False, capture_output=True)
        if res.returncode != 0:
            raise BenchError(f"brindle probe {op} failed: {res.stderr.strip()[-2000:]}")
        out = json.loads(res.stdout)
        if Path(out["brindle_file"]).resolve().is_relative_to(REPO):
            raise BenchError("the probe imported this checkout's brindle, not the installed one")
        for err in out["errors"]:
            print(f"brindle probe {op}: {err}", file=sys.stderr)
        return out


def configure_clone(clone: Path, model: str, hold_seconds: int | None) -> None:
    """A repo profile on ``model`` and ``supervisor_profile`` naming it (a new
    supervisor chat takes its provider, model and effort from there), and,
    when ``hold_seconds`` is given, the app's hold on a permission ask cut to
    it: a Claude or Codex agent's ask then reaches its own prompt, where this
    runner answers it, but any other provider's is refused when the hold ends."""
    agents_dir = clone / ".brindle" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{PROFILE_NAME}.md").write_text(
        f"---\nname: {PROFILE_NAME}\ndescription: Benchmark supervisor model pin\n"
        f"provider: claude\nmodel: {model}\n---\nBenchmark pin: only provider and model are used.\n")
    local = clone / ".brindle" / "config.local.json"
    cfg = json.loads(local.read_text()) if local.exists() else {}
    cfg["supervisor_profile"] = PROFILE_NAME
    if hold_seconds is not None:
        cfg["permission_hold_seconds"] = hold_seconds
    local.write_text(json.dumps(cfg, indent=2) + "\n")


def last_text(path: str | None) -> str | None:
    """The text of the latest assistant message in a transcript."""
    if not path:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - TAIL_BYTES))
            lines = f.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for entry in reversed(iter_json(lines)):
        msg = entry.get("message") if entry.get("type") == "assistant" else None
        content = msg.get("content") if isinstance(msg, dict) else None
        texts = [b.get("text") for b in content or () if isinstance(b, dict) and b.get("type") == "text"]
        if texts:
            return texts[-1]
    return None


def role(agent: dict, root_id: str) -> str:
    if agent["id"] == root_id:
        return "supervisor"
    return "reviewer" if agent.get("mode") == "review" else "worker"


def watch_session(b, rid: str, root: dict, t0: float, args, clock=time.time, pause=wait) -> dict:
    """Look at the session every ``args.poll`` seconds until it is done,
    answering what waits on the person, and counting each such interruption.
    Done: the goal reached, or the whole session quiet (supervisor idle, no
    agent working, nothing waiting on the person) for ``args.idle_grace``
    seconds; ``stop`` is when that quiet began. A reached goal finishes only
    once nothing actionable is left in the Inbox. A permission ask only the
    person can answer (held for the app, with no prompt of the agent's own
    the runner could answer after the hold) is never answered here: the run
    waits for the person, the clock running, and is marked ``needs_person``.
    An interruption the runner can't automate ends the run there
    (``stuck:<kind>``), and one handed to the supervisor that is still listed
    after ``args.unresolved_after`` seconds ends it too
    (``unresolved:<kind>``): both are recorded as invalid runs."""
    seen: dict[str, dict] = {rid: root}            # every session agent ever seen (workers get removed)
    transcripts: dict[str, set[str]] = {}
    outside: dict[str, dict] = {}
    rates: dict[str, list[float]] = {}
    events: list[dict] = []
    counted: set[tuple] = set()
    answered: set[tuple] = set()
    delegated: dict[tuple, float] = {}
    pressed: dict[str, float] = {}
    told: set[str] = set()
    quiet_since = None
    snap: dict = {}
    short_hold = getattr(args, "hold_seconds", None) is not None
    needs_person = False

    def finish(how: str, at: float, why: str | None = None) -> dict:
        return {"finished": how, "stop": at, "valid": how in VALID_FINISHES, "why": why,
                "needs_person": needs_person, "seen": seen, "transcripts": transcripts, "outside": outside,
                "rates": rates, "events": events, "snap": snap}

    while True:
        snap = b.probe("snapshot", root_id=rid, repo_root=root["repo_root"])
        now = clock()
        for a in snap["agents"]:
            seen[a["id"]] = {**seen.get(a["id"], {}), **a}
            if a.get("transcript_path"):
                transcripts.setdefault(a["id"], set()).add(a["transcript_path"])
        outside.update(snap.get("outside_usage") or {})
        rates.update(snap.get("rates") or {})
        live = {a["id"]: a for a in snap["agents"]}
        sup = live.get(rid, seen[rid])
        ap = snap.get("autopilot") or {}
        waiting = False   # something is waiting on the person (or on its answer to land)
        for item in snap["inbox"]:
            kind, aid = item["kind"], item.get("agent_id")
            if item.get("held"):
                waiting = True
                if item.get("held_by") == "person" and not (short_hold and item.get("provider") in PROMPT_AFTER_HOLD):
                    # Only the person can answer it, in the app: never as them from here. It is
                    # counted from permission_asks; the run waits for them, the clock running.
                    needs_person = True
                    if aid not in told:
                        told.add(aid)
                        print(f"waiting for the person: answer {item.get('provider')} agent {aid}'s "
                              "permission ask in the brindle app", file=sys.stderr)
                continue
            if kind == "waiting":
                # A prompt or dialog still on an agent's screen after --answer-after: take its
                # default (first) choice. A permission prompt is counted from permission_asks,
                # any other dialog here.
                waiting = True
                since = float(live.get(aid, {}).get("status_since") or now)
                if now - since < args.answer_after or now - pressed.get(aid, -1e18) < args.answer_after:
                    continue
                key = (kind, aid, since)
                if not item.get("permission") and key not in counted:
                    events.append({"kind": kind, "agent_id": aid, "at": now})
                counted.add(key)
                b.probe("press_enter", agent_id=aid)
                pressed[aid] = now
                continue
            key = (kind, aid, item.get("visual_id") or item.get("request_id") or (item.get("detail") or "")[:200],
                   item.get("rev"))
            if key not in counted:
                counted.add(key)
                events.append({"kind": kind, "agent_id": aid, "at": now})
            how = HANDLING.get(kind, "delegate")
            if how == "stuck":
                return finish(f"stuck:{kind}", now, item.get("detail"))
            if how == "answer":
                waiting = True   # until it leaves the Inbox
                if key not in answered:
                    try:
                        b.probe("answer", root_id=rid, repo_root=root["repo_root"], item=item, answer=ANSWER)
                        answered.add(key)
                    except BenchError as e:
                        print(f"couldn't answer {kind} (again at the next look): {e}", file=sys.stderr)
            elif how == "delegate":
                waiting = True   # until it leaves the Inbox
                if key not in delegated:
                    delegated[key] = now
                    b.cli("send", rid, f"{kind}: {item.get('detail') or ''}\n{ANSWER}", check=False)
                elif now - delegated[key] >= args.unresolved_after:
                    return finish(f"unresolved:{kind}", now, item.get("detail"))
        stopped = sup.get("status") == "idle"
        asking = stopped and (ap.get("state") == "blocked" or ends_with_question(
            last_text(sup.get("transcript_path"))))
        if ap.get("state") == "done":
            spell = ("question", rid, sup.get("status_since"))
            if asking and spell not in counted:   # a closing question: counted, not answered
                counted.add(spell)
                events.append({"kind": "question", "agent_id": rid, "at": now})
            if not waiting:
                return finish("goal", now)
            asking = False   # the goal is reached: only the Inbox's open items are left to settle
        if asking:
            waiting = True
            spell = ("question", rid, sup.get("status_since"))
            if spell not in counted and now - float(sup.get("status_since") or now) >= args.answer_after:
                counted.add(spell)
                events.append({"kind": "question", "agent_id": rid, "at": now})
                b.cli("send", rid, ANSWER, check=False)
        busy = any(a.get("status") in BUSY for a in snap["agents"])
        if stopped and not busy and not waiting:
            quiet_since = now if quiet_since is None else quiet_since
            if now - quiet_since >= args.idle_grace:
                return finish("idle", quiet_since)
        else:
            quiet_since = None
        if now - t0 > args.max_hours * 3600:
            return finish("gave_up", now)
        pause(args.poll)


def run_brindle(args, clone: Path, spec: str, env: dict) -> dict:
    b = Brindle(installed_brindle(args.brindle, env), "", clone, env)
    b.python = brindle_python(b.exe, args.brindle_python)
    t0 = time.time()
    init = b.cli("init", "--yes", check=False)
    if init.returncode != 0:
        print(f"brindle init exited {init.returncode}: {init.stderr.strip()[-500:]}", file=sys.stderr)
    configure_clone(clone, args.supervisor_model, args.hold_seconds)
    b.cli("start", "--no-attach", "--no-watch", "--yes", "-p", spec)
    root = None
    while root is None:
        root = b.probe("find_root", since=t0 - 5)["root"]
        if root is None:
            if time.time() - t0 > args.start_grace:
                raise BenchError("brindle start launched no supervisor in this clone")
            wait(args.poll)
    rid = root["id"]
    w = watch_session(b, rid, root, t0, args)
    usage = brindle_usage(rid, w["seen"], w["transcripts"], w["outside"], w["snap"].get("history", []))
    if not args.leave_running:
        for aid, a in w["seen"].items():
            if a.get("status") not in ("done", "paused"):
                b.cli("close", aid, check=False)
    checkout = Path(w["seen"][rid].get("workspace_path") or clone)
    return {"finished": w["finished"], "valid": w["valid"], "why": w["why"], "needs_person": w["needs_person"],
            "permission_mode": w["snap"].get("permission_mode"),   # brindle's Claude agents' mode
            "wall_seconds": round(w["stop"] - t0, 1), **usage, "rates": w["rates"],
            "prompts": brindle_prompts(w["snap"].get("permission_asks", []), w["events"]),
            "checkout": checkout, "supervisor_model": args.supervisor_model,
            "session_root": rid, "agents": len(w["seen"])}


def brindle_usage(rid: str, seen: dict[str, dict], transcripts: dict[str, set[str]],
                  outside: dict[str, dict], history: list[dict]) -> dict:
    """Tokens per model and per role over the session's agents, one source
    each: its transcripts when it has any (Claude, and native workers, which
    write the same format), else brindle's usage reader (Codex). An agent never
    seen live (started and removed between two looks) counts from its history
    rows. ``free``: the models agents ran on this machine."""
    by_model: dict[str, dict[str, int]] = {}
    by_role: dict[str, dict[str, dict[str, int]]] = {}
    free: set[str] = set()
    supervisor: dict[str, dict[str, int]] = {}
    for aid, a in seen.items():
        mine: dict[str, dict[str, int]] = {}
        for path in sorted(transcripts.get(aid, ())):
            merge(mine, transcript_usage(path))
        if not transcripts.get(aid) and aid in outside:
            add_usage(mine, normalize_model(outside[aid]["model"]), outside[aid])
        if a.get("local"):
            free.update(mine)
        if aid == rid:
            supervisor = mine
        merge(by_model, mine)
        merge(by_role.setdefault(role(a, rid), {}), mine)
    for h in history:
        if h["agent_id"] in seen:
            continue
        try:
            d = json.loads(h["tokens"])
        except (TypeError, ValueError):
            continue
        model = normalize_model(d.get("model") or "unknown")
        add_usage(by_model, model, d)
        add_usage(by_role.setdefault("unseen", {}), model, d)
    return {"by_model": by_model, "by_role": by_role, "free": free,
            "supervisor_model_observed": main_model(supervisor)}


# -- run --------------------------------------------------------------------------------------------


def cmd_run(args) -> int:
    kit, spec, grade_sh = load_kit(Path(args.task_dir))
    env = clean_env()
    base = Path(args.scratch).resolve()
    scratch = base / f"{args.arm}-{args.run_id}-{time.strftime('%Y%m%d-%H%M%S')}"
    if scratch.is_relative_to(REPO):
        raise BenchError(f"the scratch directory must be outside this repo: {scratch}")
    scratch.mkdir(parents=True)
    print(f"scratch: {scratch}", file=sys.stderr)
    clone = prepare_clone(kit, scratch, env)
    started = time.time()
    out = run_solo(args, clone, spec, scratch, env) if args.arm == "solo" else run_brindle(args, clone, spec, env)
    graded = grade(grade_sh, out["checkout"], env)
    prompts = out["prompts"]
    record = {"arm": args.arm, "run_id": args.run_id, "started_at": started, "finished": out["finished"],
              "valid": out["valid"], "why": out["why"], "needs_person": out.get("needs_person", False),
              "permission_mode": out.get("permission_mode"),
              "wall_seconds": out["wall_seconds"], "supervisor_model": out["supervisor_model"],
              "supervisor_model_observed": out["supervisor_model_observed"],
              "tokens": {"by_model": out["by_model"], "by_role": out["by_role"]},
              "tokens_total": tokens_total(out["by_model"]),
              "prompts_total": sum(prompts.values()), "prompts": prompts,
              "grade": graded, "pass_rate": pass_rate(graded), "checkout": str(out["checkout"])}
    for extra in ("turns", "cli_model_usage", "session_root", "agents"):
        if extra in out:
            record[extra] = out[extra]
    results_path = Path(args.results)
    results_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cost, per_model, unpriced = price_usage(out["by_model"], out["rates"], out["free"])
    except UnknownModel as e:
        side = results_path.with_name(results_path.stem + ".unpriced.jsonl")
        with side.open("a") as f:
            f.write(json.dumps({**record, "error": str(e)}) + "\n")
        print(f"error: {e} (record kept in {side})", file=sys.stderr)
        return 2
    record.update(cost_usd=cost, cost_by_model=per_model, unpriced=unpriced)
    with results_path.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(json.dumps({k: record[k] for k in ("arm", "run_id", "finished", "wall_seconds", "tokens_total",
                                             "cost_usd", "prompts_total", "pass_rate")}))
    return 0


ARMS_HELP = """Both arms work on a clone whose origin is a local bare copy, with GitHub URLs
sent nowhere: nothing reaches the task's public repo.

arms:
  solo     claude -p <spec> --model claude-fable-5-2 --output-format stream-json --verbose,
           in --permission-mode; --permission-prompt-tool points at this script's
           approver MCP server, which logs and allows each request (AskUserQuestion: its
           recommended option). A turn ending in a question is answered with --resume.
  brindle  the installed brindle: `brindle init`, the supervisor pinned to
           --supervisor-model, `brindle start --no-attach -p <spec>`; done when the goal
           is reached and the Inbox is settled, or the whole session is quiet for
           --idle-grace seconds. Questions are answered "go ahead with your
           recommendation", Inbox items with their own answer, prompts on an agent's
           screen with their default choice. A permission ask only the person can answer
           in the app waits for them (the record says needs_person).
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="run one arm once and record it", epilog=ARMS_HELP,
                         formatter_class=argparse.RawDescriptionHelpFormatter,
                         description="Clone the task repo, run setup, time one arm, grade it and "
                                     "append a record to the results file.")
    run.add_argument("--arm", choices=ARMS, required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--task-dir", default=str(TASK_DIR))
    run.add_argument("--results", default=str(RESULTS))
    run.add_argument("--scratch", default=str(Path(tempfile.gettempdir()) / "bench_solo_vs_brindle"),
                     help="where clones go (outside this repo)")
    run.add_argument("--model", default=SOLO_MODEL, help="solo: the session's model")
    run.add_argument("--permission-mode", default="default",
                     help="solo: the CLI's --permission-mode ('' for the person's own setting); "
                          "the mode it ran in is recorded either way")
    run.add_argument("--max-answers", type=int, default=5,
                     help="solo: how many closing questions to answer by resuming the session")
    run.add_argument("--supervisor-model", default=SUPERVISOR_MODEL,
                     help="brindle: the supervisor's model (workers keep default routing)")
    run.add_argument("--brindle", help="brindle: the installed brindle (default: `brindle` on PATH)")
    run.add_argument("--brindle-python", help="brindle: the Python the installed brindle runs on")
    run.add_argument("--hold-seconds", type=int, default=None,
                     help="brindle: cut the app's hold on a permission ask to this (default: brindle's own). "
                          "Claude and Codex asks then reach the agent's prompt, which the runner answers; "
                          "other providers' asks are refused when it ends")
    run.add_argument("--poll", type=float, default=20.0, help="brindle: seconds between looks")
    run.add_argument("--idle-grace", type=float, default=300.0,
                     help="brindle: seconds the session must stay quiet to count as done")
    run.add_argument("--answer-after", type=float, default=60.0,
                     help="brindle: seconds a question or prompt waits before it is answered")
    run.add_argument("--unresolved-after", type=float, default=1800.0,
                     help="brindle: seconds an item handed to the supervisor may stay in the Inbox "
                          "before the run ends as invalid")
    run.add_argument("--start-grace", type=float, default=120.0,
                     help="brindle: seconds to wait for the supervisor to appear")
    run.add_argument("--max-hours", type=float, default=6.0, help="give up after this long")
    run.add_argument("--leave-running", action="store_true",
                     help="brindle: don't close the session's agents afterwards")
    rep = sub.add_parser("report", help="per arm: median and min-max of each metric")
    rep.add_argument("--results", default=str(RESULTS))
    rep.add_argument("--require-runs", type=int, default=0,
                     help="exit non-zero if any arm has fewer runs than this")
    appr = sub.add_parser("approver", help="(internal) the solo arm's permission prompt MCP server")
    appr.add_argument("--log", required=True)
    args = ap.parse_args(argv)
    try:
        if args.cmd == "report":
            return cmd_report(args)
        if args.cmd == "approver":
            return serve_approver(Path(args.log))
        return cmd_run(args)
    except BenchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

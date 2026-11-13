import importlib.util
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle.db import Agent, Workspace

spec = importlib.util.spec_from_file_location(
    "bench_solo_vs_brindle", Path(__file__).resolve().parent.parent / "scripts/bench_solo_vs_brindle.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

STREAM = Path(__file__).resolve().parent / "fixtures/bench/solo_stream.jsonl"
FABLE = {"input": 105, "output": 80, "cache_creation": 1000, "cache_read": 5000}
HAIKU = {"input": 10, "output": 20, "cache_creation": 0, "cache_read": 400}


def entries():
    return bench.iter_json(STREAM.read_text().splitlines())


def test_solo_cost_from_a_recorded_stream():
    # msg_1 streams as two entries (counted once), the subagent's haiku counts, <synthetic> doesn't.
    by_model = bench.message_usage(entries())
    assert by_model == {"claude-fable-5-2": FABLE, "claude-haiku-5-5": HAIKU}
    total, per_model, unpriced = bench.price_usage(by_model)
    # fable: 105*8 + 80*40 + 5000*0.20 + 1000*8*1.25; haiku: 10*0.10 + 20*0.50 + 400*0.01 ($/MTok)
    assert per_model == {"claude-fable-5-2": pytest.approx(0.01504), "claude-haiku-5-5": pytest.approx(0.000015)}
    assert total == pytest.approx(0.015055)
    assert unpriced == []


def test_unknown_claude_model_raises_and_other_providers_take_brindles_rate_or_are_flagged():
    with pytest.raises(bench.UnknownModel):
        bench.price_usage({"claude-opus-4-8": bench.empty()})
    one_m = {"input": 1_000_000, "output": 0, "cache_creation": 0, "cache_read": 0}
    usage = {"gpt-6-sol": one_m, "gpt-5.6-luna": one_m, "qwen-local": one_m}
    total, per_model, unpriced = bench.price_usage(usage, {"gpt-6-sol": [2, 10, 2.5, 0.2]}, {"qwen-local"})
    assert (total, per_model, unpriced) == (2.0, {"gpt-6-sol": 2.0, "qwen-local": 0.0}, ["gpt-5.6-luna"])


def test_solo_prompts_count_logged_approvals_questions_and_a_closing_question(tmp_path):
    log = tmp_path / "approvals.jsonl"
    ask = {"questions": [{"question": "Which part first?",
                          "options": [{"label": "Part A"}, {"label": "Part B (Recommended)"}]}]}
    replies = {}
    for tool, tool_input in (("Bash", {"command": "ls"}), ("AskUserQuestion", ask)):
        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "approve", "arguments": {"tool_name": tool, "input": tool_input}}}
        replies[tool] = json.loads(bench.approver_reply(call, log)["result"]["content"][0]["text"])
    assert replies["Bash"] == {"behavior": "allow", "updatedInput": {"command": "ls"}}
    assert replies["AskUserQuestion"]["updatedInput"]["answers"] == {"Which part first?": "Part B (Recommended)"}
    approvals = bench.iter_json(log.read_text().splitlines())
    # Bash's request is a permission prompt; the AskUserQuestion (in the stream) and the
    # closing "Shall I also add docs?" are questions.
    assert bench.solo_prompts(entries(), approvals) == {"permission": 1, "question": 2, "other": 0}


def test_solo_session_answers_a_closing_question_by_resuming_it():
    def turn(text, out_tokens):
        return [{"type": "assistant", "session_id": "s1",
                 "message": {"id": f"m{out_tokens}", "model": "claude-fable-5-2",
                             "usage": {"output_tokens": out_tokens}}},
                {"type": "result", "session_id": "s1", "result": text}]

    calls, turns = [], [turn("Should I proceed?", 5), turn("Done.", 7)]

    def run_turn(prompt, session):
        calls.append((prompt, session))
        return turns.pop(0)

    got = bench.solo_session(run_turn, "the spec", max_answers=3)
    assert calls == [("the spec", None), (bench.ANSWER, "s1")]
    assert bench.message_usage(got)["claude-fable-5-2"]["output"] == 12
    assert bench.solo_prompts(got, []) == {"permission": 0, "question": 1, "other": 0}


def test_brindle_prompts_count_only_what_reached_the_person():
    asks = [
        {"asked": 0, "resolved_by": "policy"},            # the checker decided
        {"asked": 1, "resolved_by": "saved_action"},      # a saved action settled it
        {"asked": 1, "resolved_by": "supervisor"},        # the worker's supervisor answered
        {"asked": 1, "resolved_by": "person"},
        {"asked": 1, "resolved_by": "unheld"},            # straight to the CLI's prompt
        {"asked": 1, "resolved_by": None},                # still pending
    ]
    events = [{"kind": "question"}, {"kind": "plan"}, {"kind": "pr_failed"}]
    assert bench.brindle_prompts(asks, events) == {"permission": 3, "question": 2, "other": 1}


def test_brindle_usage_takes_one_source_per_agent(tmp_path):
    transcript = tmp_path / "root.jsonl"
    transcript.write_text(STREAM.read_text())
    native = tmp_path / "native.jsonl"
    native.write_text(json.dumps({"type": "assistant", "message": {
        "id": "n1", "model": "claude-sonnet-5-5", "usage": {"input_tokens": 1, "output_tokens": 2}}}) + "\n")
    seen = {"root": {"id": "root", "mode": "interactive"}, "w1": {"id": "w1", "mode": "assign"},
            "n1": {"id": "n1", "mode": "assign", "provider": "native"}}
    sonnet = {"input": 1, "output": 2, "cache_creation": 0, "cache_read": 0}
    outside = {"w1": {"model": "gpt-6-sol", "input": 7, "output": 3, "cache_read": 0, "cache_creation": 0},
               "n1": {"model": "claude-sonnet-5-5", **sonnet}}   # the same transcript, read by brindle
    history = [
        {"agent_id": "root", "tokens": json.dumps({"input": 500, "model": "claude-fable-5-2"})},  # seen: no double count
        {"agent_id": "gone", "tokens": json.dumps({"input": 4, "output": 6, "model": "claude-opus-5-5"})},
    ]
    out = bench.brindle_usage("root", seen, {"root": {str(transcript)}, "n1": {str(native)}}, outside, history)
    assert out["by_model"] == {"claude-fable-5-2": FABLE, "claude-haiku-5-5": HAIKU, "claude-sonnet-5-5": sonnet,
                               "gpt-6-sol": {"input": 7, "output": 3, "cache_creation": 0, "cache_read": 0},
                               "claude-opus-5-5": {"input": 4, "output": 6, "cache_creation": 0, "cache_read": 0}}
    assert set(out["by_role"]) == {"supervisor", "worker", "unseen"}
    assert out["supervisor_model_observed"] == "claude-fable-5-2"


class FakeSession:
    """brindle as watch_session sees it, on a fake clock: ``state(fake)`` gives
    the snapshot at ``fake.t``; every probe and CLI call is kept."""

    def __init__(self, state):
        self.t, self.calls, self.state = 0.0, [], state

    def clock(self):
        return self.t

    def pause(self, seconds):
        self.t += seconds

    def probe(self, op, **kw):
        self.calls.append((op, kw))
        if op != "snapshot":
            return {}
        agents, inbox = self.state(self)
        return {"agents": agents, "inbox": inbox, "autopilot": {"state": "running"}}

    def cli(self, *argv, check=True):
        self.calls.append(("cli", argv))

    def ops(self, op):
        return [kw for o, kw in self.calls if o == op]


ARGS = SimpleNamespace(poll=50, idle_grace=100, answer_after=60, unresolved_after=300, max_hours=1)
ROOT = {"id": "root", "repo_root": "/r", "status": "idle", "status_since": 10}


def test_watch_waits_for_the_whole_session_to_go_quiet():
    # The supervisor has been idle since t=10, but its worker works until t=400.
    def state(s):
        worker = {"id": "w1", "status": "processing" if s.t < 400 else "idle"}
        return [ROOT, worker], []

    s = FakeSession(state)
    got = bench.watch_session(s, "root", ROOT, 0, ARGS, s.clock, s.pause)
    assert (got["finished"], got["stop"]) == ("idle", 400)


def test_watch_answers_inbox_items_and_held_asks_before_counting_the_session_quiet():
    def answered(s, kind, rev=None):
        return any(kw["item"]["kind"] == kind and kw["item"].get("rev") == rev for kw in s.ops("answer"))

    def state(s):
        inbox = []
        if not answered(s, "plan_offer"):     # the plan offer stays until its own answer runs
            inbox.append({"kind": "plan_offer", "agent_id": "root", "request_id": "o1", "detail": "goal"})
        for rev in (1, 2):                    # a visual's second revision is a new question
            if not answered(s, "visual", rev):
                inbox.append({"kind": "visual", "agent_id": "root", "visual_id": "v1", "rev": rev})
                break
        worker = {"id": "w1", "status": "idle"}
        if s.t < 100:               # held for the app, then at the worker's own prompt until answered
            inbox.append({"kind": "permission", "agent_id": "w1", "held": True, "permission": True})
        elif not s.ops("press_enter"):
            worker = {"id": "w1", "status": "waiting", "status_since": 100}
            inbox.append({"kind": "waiting", "agent_id": "w1", "held": False, "permission": True})
        return [ROOT, worker], inbox

    s = FakeSession(state)
    got = bench.watch_session(s, "root", ROOT, 0, ARGS, s.clock, s.pause)
    assert [(kw["item"]["kind"], kw["item"].get("rev")) for kw in s.ops("answer")] == [
        ("plan_offer", None), ("visual", 1), ("visual", 2)]
    assert s.ops("press_enter") == [{"agent_id": "w1"}]
    # Pressed at t=200 (60s after its prompt appeared); quiet from the next look.
    assert (got["finished"], got["valid"], got["stop"]) == ("idle", True, 250)
    # The permission counts from permission_asks, not here.
    assert [e["kind"] for e in got["events"]] == ["plan_offer", "visual", "visual"]


@pytest.mark.parametrize("item, finished, sent", [
    # Handed to the supervisor once; still listed after --unresolved-after.
    ({"kind": "pr_failed", "agent_id": "root", "detail": "checks failed"}, "unresolved:pr_failed", 1),
    # A native worker has no prompt to fall back to: its ask can't be answered here.
    ({"kind": "permission", "agent_id": "w1", "held": True, "held_by": "person", "provider": "native"},
     "stuck:permission", 0),
])
def test_watch_ends_a_run_it_cannot_complete_as_invalid(item, finished, sent):
    s = FakeSession(lambda s: ([ROOT, {"id": "w1", "status": "idle"}], [item]))
    got = bench.watch_session(s, "root", ROOT, 0, ARGS, s.clock, s.pause)
    assert (got["finished"], got["valid"]) == (finished, False)
    assert len(s.ops("cli")) == sent


@pytest.mark.parametrize("opened, kinds", [(False, ["pr_ready"]), (True, ["pr_opened"])])
def test_probe_lists_the_sessions_pr_in_its_inbox(db, tmp_path, opened, kinds):
    repo = str(tmp_path)
    ws = Workspace("w", repo, "s", "worktree", "brindle/session-1", "main", repo, None, "t", time.time())
    db.add_workspace(ws)
    db.add_agent(Agent("r1", ws.id, "supervisor", "claude", None, "interactive", "idle", "@1", None, time.time()))
    with db.tx() as c:
        c.execute("INSERT INTO session_pr_prep (root_id, branch, sha, state, title, prepared_at)"
                  " VALUES ('r1', 'brindle/session-1', 'abc', 'passed', 'Add parts', ?)", (time.time(),))
        if opened:   # opened from that preparation: its row stays, the card is the open PR's
            c.execute("INSERT INTO session_prs (root_id, branch, url, opened_at)"
                      " VALUES ('r1', 'brindle/session-1', 'https://github.com/o/r/pull/7', ?)", (time.time(),))
    ns = {"__name__": "bench_probe"}
    exec(bench.PROBE, ns)
    out = ns["main"]({"op": "snapshot", "clone": repo, "root_id": "r1", "repo_root": repo})
    assert out["errors"] == []
    assert [i["kind"] for i in out["inbox"]] == kinds


def test_report_medians_and_require_runs(tmp_path):
    results = tmp_path / "results.jsonl"
    rows = [{"arm": "solo", "wall_seconds": w, "tokens_total": t, "cost_usd": c, "prompts_total": p,
             "grade": {"passed": g, "failed": 4 - g}}
            for w, t, c, p, g in [(100, 10, 1.0, 0, 4), (300, 30, 3.0, 2, 2), (200, 20, 2.0, 1, 3)]]
    rows.append({"arm": "brindle", "wall_seconds": 50, "tokens_total": 5, "cost_usd": 0.5,
                 "prompts_total": 4, "pass_rate": 1.0})
    rows.append({"arm": "solo", "valid": False, "wall_seconds": 9999, "pass_rate": 0.0})   # left out
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    solo = bench.summarize(bench.load_records(results))["solo"]
    assert (solo["runs"], solo["invalid"]) == (3, 1)
    assert solo["wall_seconds"] == {"median": 200, "min": 100, "max": 300}
    assert solo["pass_rate"] == {"median": 0.75, "min": 0.5, "max": 1.0}
    assert solo["prompts_total"] == {"median": 1, "min": 0, "max": 2}
    assert bench.main(["report", "--results", str(results), "--require-runs", "1"]) == 0
    assert bench.main(["report", "--results", str(results), "--require-runs", "2"]) == 1

import importlib.util
import json
import os
import socket
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from brindle import tmux
from brindle.db import Agent, Workspace

spec = importlib.util.spec_from_file_location(
    "bench_solo_vs_brindle", Path(__file__).resolve().parent.parent / "scripts/bench_solo_vs_brindle.py")
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)

STREAM = Path(__file__).resolve().parent / "fixtures/bench/solo_stream.jsonl"
RUN1 = Path(__file__).resolve().parent / "fixtures/bench/solo_run1_stream.jsonl"   # trimmed from a real run
FABLE = {"input": 105, "output": 80, "cache_creation": 1000, "cache_read": 5000, "cache_creation_1h": 0}
HAIKU = {"input": 10, "output": 20, "cache_creation": 0, "cache_read": 400, "cache_creation_1h": 0}


def entries(path=STREAM):
    return bench.iter_json(path.read_text().splitlines())


def test_solo_usage_takes_the_result_events_totals():
    # The stream's assistant messages carry output counts from the start of each
    # message (16 + 16 + 8 here); the result event has the turn's real totals.
    run = entries(RUN1)
    assert bench.stream_usage(run) == {"claude-fable-5-2": {
        "input": 22, "output": 33250, "cache_creation": 87019, "cache_creation_1h": 87019, "cache_read": 672173}}
    assert bench.permission_mode(run) == "auto"


def test_solo_cost_prices_one_hour_cache_writes_at_twice_input():
    u = entries(RUN1)[-1]["modelUsage"]["claude-fable-5-2"]   # every cache write here was a 1-hour one
    usage = {"input": u["inputTokens"], "output": u["outputTokens"], "cache_read": u["cacheReadInputTokens"],
             "cache_creation": u["cacheCreationInputTokens"],
             "cache_creation_1h": u["cacheCreationEphemeral1hInputTokens"]}
    total, _, _ = bench.price_usage({"claude-fable-5-2": usage})
    assert abs(total - u["costUSD"]) < 0.01


@pytest.mark.parametrize("name", [
    pytest.param(None, marks=pytest.mark.variant("Unnamed kits must still produce tinydb records")),
    "large",
])
def test_load_kit_resolves_a_relative_task_dir(tmp_path, monkeypatch, name):
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "repo.json").write_text(json.dumps({"name": name} if name is not None else {}))
    (kit / "spec.md").write_text("spec")
    (kit / "grade.sh").write_text("")
    monkeypatch.chdir(tmp_path)
    assert bench.load_kit(Path("kit"))[2] == kit.resolve() / "grade.sh"   # grade.sh runs from the clone
    monkeypatch.setattr(bench, "prepare_clone", lambda kit, scratch, env: scratch)

    def fake_run(args, clone, spec, *unused):
        return {
            "finished": "exit", "valid": True, "why": "", "wall_seconds": 1,
            "supervisor_model": None, "supervisor_model_observed": None,
            "by_model": {}, "by_role": {}, "prompts": {}, "checkout": clone, "rates": {}, "free": set()}

    monkeypatch.setattr(bench, "run_solo", fake_run)
    monkeypatch.setattr(bench, "run_brindle", fake_run)
    monkeypatch.setattr(bench, "grade", lambda path, checkout, env: {"passed": 1, "failed": 0})
    phase = {"after": False}

    def machine(op, **kw):
        after = phase["after"]
        phase["after"] = True
        return {"limits": {"claude": {"used": 34.5 if after else 31, "resets_at": 1000},
                           "codex": None, "antigravity": None},
                "other_agents": [{"id": "other", "provider": "claude", "status": "processing"}]}

    monkeypatch.setattr(bench, "installed_client", lambda *a: SimpleNamespace(probe=machine))
    results = tmp_path / "results.jsonl"
    arm = "solo" if name is None else "brindle"
    assert bench.main(["run", "--arm", arm, "--run-id", "1", "--task-dir", "kit",
                       "--scratch", str(tmp_path / "scratch"), "--results", str(results),
                       *(["--only-workers", "developer-codex"] if arm == "brindle" else [])]) == 0
    record = bench.load_records(results)[0]
    assert record["task"] == (name if name is not None else "tinydb")
    assert record["limits_before"]["claude"]["used"] == 31
    assert record["limits_after"]["claude"]["used"] == 34.5
    assert record["limits_used"] == {"claude": 3.5, "codex": None, "antigravity": None}
    assert [a["id"] for a in record["other_agents"]] == ["other"]
    assert record["only_workers"] == (["developer-codex"] if arm == "brindle" else None)


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
    # Neither result has the CLI's totals: each turn falls back to its messages.
    assert bench.stream_usage(got)["claude-fable-5-2"]["output"] == 12
    assert bench.solo_prompts(got, []) == {"permission": 0, "question": 1, "other": 0}


@pytest.mark.parametrize("decisions,flags,rounds,cap_hit,failure", [
    ([True, True, False], [], 2, False, None),
    ([True, True, True], ["--max-fix-rounds", "2"], 2, True, None),
    ([], ["--no-review-followup"], 0, False, None),
    pytest.param([True], [], 1, False, "classifier-expired", marks=pytest.mark.variant(
        "An expired classifier process must preserve the completed benchmark and earlier usage")),
    pytest.param([True], [], 1, False, "classifier-json", marks=pytest.mark.variant(
        "Malformed classifier JSON must preserve a record rather than escape the run lifecycle")),
    pytest.param([True], [], 1, False, "classifier-ambiguous", marks=pytest.mark.variant(
        "An ambiguous classifier answer must preserve its billable usage as well as earlier usage")),
    ([], [], 0, False, "missing-result"),
    pytest.param([], [], 0, False, "missing-result-success", marks=pytest.mark.variant(
        "A zero process exit without a result must still invalidate the run")),
    ([], [], 0, False, "failed-result"),
])
def test_solo_review_followup_records_first_and_final_passes(
        tmp_path, monkeypatch, decisions, flags, rounds, cap_hit, failure):
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "repo.json").write_text('{}')
    (kit / "spec.md").write_text("Implement the feature.")
    (kit / "grade.sh").touch()
    clock = [100.0]
    monkeypatch.setattr(bench.time, "time", lambda: clock[0])
    monkeypatch.setattr(bench.shutil, "which", lambda *a, **kw: "claude")

    def prepare(repo, scratch, env):
        clone = scratch / "repo"
        clone.mkdir()
        return clone

    monkeypatch.setattr(bench, "prepare_clone", prepare)
    monkeypatch.setattr(bench, "installed_client", lambda *a: SimpleNamespace(
        probe=lambda *a, **kw: {"limits": {}, "other_agents": []}))
    calls, classified, grades = [], [], []
    answers = iter(decisions)

    def run_turn(cmd, clone, scratch, env, deadline):
        calls.append(cmd)
        clock[0] += 10
        (clone / "implementation").write_text(str(len(calls)))
        if len(calls) == 2 and failure in ("missing-result", "missing-result-success"):
            return [], "exit" if failure == "missing-result-success" else "exit:1"
        return [{"type": "result", "session_id": "same-session", "result": f"Reply {len(calls)}",
                 "modelUsage": {"claude-fable-5-2": {"inputTokens": 1000, "outputTokens": 100}}}], (
                     "exit:1" if len(calls) == 2 and failure == "failed-result" else "exit")

    def classify(claude, text, cwd, env, remaining):
        classified.append(text)
        return next(answers, False), {"claude-haiku-5-5": HAIKU}

    def grade(path, checkout, env):
        state = int((checkout / "implementation").read_text())
        grades.append((checkout, state))
        clock[0] += 50  # grading and snapshots aren't agent wall time
        return {"passed": state, "failed": 0}

    monkeypatch.setattr(bench, "run_solo_turn", run_turn)
    if failure and failure.startswith("classifier-"):
        def classifier_cli(cmd, cwd, env, **kw):
            classified.append(f"Reply {len(calls)}")
            if len(classified) == 2:
                if failure == "classifier-expired":
                    raise bench.subprocess.TimeoutExpired("claude", kw["timeout"])
                if failure == "classifier-json":
                    return SimpleNamespace(stdout="invalid JSON")
            answer = "unclear" if len(classified) == 2 else "yes"
            return SimpleNamespace(stdout=json.dumps({"result": answer, "modelUsage": {
                "claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 20, "cacheReadInputTokens": 400}}}))

        monkeypatch.setattr(bench, "sh", classifier_cli)
    else:
        monkeypatch.setattr(bench, "classify_fixes", classify)
    monkeypatch.setattr(bench, "grade", grade)
    output = tmp_path / "results.jsonl"
    assert bench.main(["run", "--arm", "solo", "--run-id", "1", "--task-dir", str(kit),
                       "--scratch", str(tmp_path / "scratch"), "--results", str(output),
                       "--solo-note", "", *flags]) == 0
    record = bench.load_records(output)[0]
    messages = (["Did you run a code review on this?"] + ["Please fix those."] * rounds
                if "--no-review-followup" not in flags else [])
    assert [cmd[2] for cmd in calls] == ["Implement the feature.", *messages]
    assert "--resume" not in calls[0]
    assert all(cmd[-2:] == ["--resume", "same-session"] for cmd in calls[1:])
    assert record["followup_messages"] == messages
    assert record["fix_rounds"] == rounds
    assert record["fix_rounds_cap_hit"] is cap_hit
    classifier_failed = bool(failure and failure.startswith("classifier-"))
    expected_checks = [{"text": text, "fixes_needed": needed} for text, needed in zip(classified, decisions)]
    if classifier_failed:
        error_check = record["review_checks"][-1]
        assert error_check["error"]
        assert error_check["fixes_needed"] is None
        assert error_check["text"] == f"Reply {len(calls)}"
        expected_checks.append(error_check)
    assert record["review_checks"] == expected_checks
    assert len(classified) == len(decisions) + int(classifier_failed)
    assert record["valid"] is (failure is None)
    assert bool(record["why"]) is (failure is not None)
    if failure and not classifier_failed:
        assert record["finished"] == ("incomplete" if failure == "missing-result-success" else "exit:1")
    assert record["prompts"]["followup"] == record["prompts_total"] == len(messages)
    assert record["grade_first_pass"] == {"passed": 1, "failed": 0}
    assert record["grade"] == {"passed": len(calls), "failed": 0}
    snapshot = Path(record["first_pass_checkout"])
    assert snapshot == grades[0][0]
    assert (snapshot / "implementation").read_text() == "1"
    assert record["wall_seconds_first_pass"] == 10
    assert record["wall_seconds"] == len(calls) * 10
    assert record["cost_first_pass"] == pytest.approx(0.012)
    completed = len(calls) - int(failure in ("missing-result", "missing-result-success"))
    assert record["cost_usd"] == pytest.approx(0.012 * completed)
    assert record["tokens_total"] == 1100 * completed
    harness_calls = len(decisions) + int(failure == "classifier-ambiguous")
    assert record["harness_tokens"] == ({"claude-haiku-5-5": {
        k: v * harness_calls for k, v in HAIKU.items()}} if harness_calls else {})
    assert record["harness_cost_usd"] == pytest.approx(0.000015 * harness_calls)


@pytest.mark.parametrize("answer", [
    "yes",
    pytest.param("no", marks=pytest.mark.variant("A negative classification must stop fix requests")),
    "unclear",
])
def test_fix_classifier_reads_cli_result_and_rejects_ambiguous_answers(tmp_path, monkeypatch, answer):
    monkeypatch.setattr(bench, "sh", lambda *a, **kw: SimpleNamespace(stdout=json.dumps({
        "result": answer, "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 20}}})))
    if answer == "unclear":
        with pytest.raises(bench.BenchError):
            bench.classify_fixes("claude", "developer reply", tmp_path, {}, 60)
    else:
        needed, usage = bench.classify_fixes("claude", "developer reply", tmp_path, {}, 60)
        assert needed is (answer == "yes")
        assert bench.tokens_total(usage) == 30


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
    sonnet = {"input": 1, "output": 2, "cache_creation": 0, "cache_read": 0, "cache_creation_1h": 0}
    outside = {"w1": {"model": "gpt-6-sol", "input": 7, "output": 3, "cache_read": 0, "cache_creation": 0},
               "n1": {"model": "claude-sonnet-5-5", **sonnet}}   # the same transcript, read by brindle
    history = [
        {"agent_id": "root", "tokens": json.dumps({"input": 500, "model": "claude-fable-5-2"})},  # seen: no double count
        {"agent_id": "gone", "tokens": json.dumps({"input": 4, "output": 6, "model": "claude-opus-5-5"})},
    ]
    out = bench.brindle_usage("root", seen, {"root": {str(transcript)}, "n1": {str(native)}}, outside, history)
    assert out["by_model"] == {"claude-fable-5-2": FABLE, "claude-haiku-5-5": HAIKU, "claude-sonnet-5-5": sonnet,
                               "gpt-6-sol": {**bench.empty(), "input": 7, "output": 3},
                               "claude-opus-5-5": {**bench.empty(), "input": 4, "output": 6}}
    assert set(out["by_role"]) == {"supervisor", "worker", "unseen"}
    assert out["supervisor_model_observed"] == "claude-fable-5-2"


def test_transcript_usage_counts_workflow_agents(tmp_path):
    main = tmp_path / "s.jsonl"
    main.write_text("")
    wf = tmp_path / "s" / "subagents" / "workflows" / "wf_1"
    wf.mkdir(parents=True)
    (wf / "agent-a.jsonl").write_text(json.dumps({"type": "assistant", "message": {
        "id": "a1", "model": "claude-haiku-5-5", "usage": {"input_tokens": 2, "output_tokens": 4}}}) + "\n")
    assert bench.transcript_usage(str(main))["claude-haiku-5-5"]["output"] == 4


class FakeSession:
    """brindle as watch_session sees it, on a fake clock: ``state(fake)`` gives
    the snapshot at ``fake.t``; every probe and CLI call is kept."""

    def __init__(self, state, autopilot="running"):
        self.t, self.calls, self.state, self.autopilot = 0.0, [], state, autopilot

    def clock(self):
        return self.t

    def pause(self, seconds):
        self.t += seconds

    def probe(self, op, **kw):
        self.calls.append((op, kw))
        if op != "snapshot":
            return {}
        agents, inbox = self.state(self)
        return {"agents": agents, "inbox": inbox, "autopilot": {"state": self.autopilot}}

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


def test_watch_finishes_a_reached_goal_only_once_its_inbox_is_settled():
    # The goal is reached from t=0, but a plan waits on the person until it is answered.
    s = FakeSession(lambda s: ([ROOT], [] if s.ops("answer") else [{"kind": "plan", "agent_id": "w1"}]), "done")
    got = bench.watch_session(s, "root", ROOT, 0, ARGS, s.clock, s.pause)
    assert (got["finished"], got["stop"]) == ("goal", 50)
    assert len(s.ops("answer")) == 1


def test_watch_ends_a_run_whose_delegated_item_stays_unresolved_as_invalid():
    item = {"kind": "pr_failed", "agent_id": "root", "detail": "checks failed"}
    s = FakeSession(lambda s: ([ROOT], [item]))
    got = bench.watch_session(s, "root", ROOT, 0, ARGS, s.clock, s.pause)
    assert (got["finished"], got["valid"]) == ("unresolved:pr_failed", False)
    assert len(s.ops("cli")) == 1   # handed to the supervisor once


def test_watch_waits_for_the_person_on_an_ask_only_they_can_answer():
    # A native worker's ask, held for the app: never answered from here; the person answers at t=200.
    def state(s):
        held = [{"kind": "permission", "agent_id": "w1", "held": True, "held_by": "person", "provider": "native"}]
        return [ROOT, {"id": "w1", "status": "idle"}], held if s.t < 200 else []

    s = FakeSession(state)
    got = bench.watch_session(s, "root", ROOT, 0, ARGS, s.clock, s.pause)
    assert (got["finished"], got["valid"], got["needs_person"], got["stop"]) == ("idle", True, True, 200)
    assert s.ops("press_enter") == s.ops("answer") == s.ops("cli") == []


def test_prepare_clone_leaves_only_local_remotes(repo, tmp_path):
    # The task repo is on GitHub: this test's git finds it locally under that URL.
    url = "https://github.com/o/task"
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text(f'[url "{repo}"]\n\tinsteadOf = {url}\n')
    env = {**bench.clean_env(), "GIT_CONFIG_GLOBAL": str(gitconfig), "GIT_CONFIG_NOSYSTEM": "1"}
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    clone = bench.prepare_clone({"url": url, "commit": "main", "setup": []}, scratch, env)
    for path in (clone, scratch / "origin.git"):
        assert all(bench.is_local(u) for u in bench.remote_urls(path, env))
        assert set(bench.github_rewrites(path, env)) == set(bench.GITHUB_PREFIXES)
    assert bench.remote_urls(clone, env) == [str(scratch / "origin.git")]


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
    from brindle.daemon import session_review
    lookup = session_review.open_pr_for_head
    ns = {"__name__": "bench_probe"}
    exec(bench.PROBE, ns)
    out = ns["main"]({"op": "snapshot", "clone": repo, "root_id": "r1", "repo_root": repo})
    assert out["errors"] == []
    assert [i["kind"] for i in out["inbox"]] == kinds
    assert session_review.open_pr_for_head is lookup   # its GitHub stub doesn't outlive the probe


def test_report_medians_and_require_runs(tmp_path, capsys):
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
    rows[0]["task"] = "tinydb"   # explicit and legacy records belong to the same task
    rows.extend({"task": "large", "arm": "solo", "wall_seconds": 900} for _ in range(2))
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    capsys.readouterr()
    assert bench.main(["report", "--results", str(results), "--task", "tinydb", "--require-runs", "1"]) == 0
    tinydb = capsys.readouterr().out
    assert "solo (3 runs, 1 invalid" in tinydb
    assert "3.3m (1.7m - 5.0m)" in tinydb
    assert "large" not in tinydb
    assert bench.main(["report", "--results", str(results), "--task", "tinydb", "--require-runs", "2"]) == 1
    assert bench.main(["report", "--results", str(results), "--task", "large", "--require-runs", "1"]) == 1
    large = capsys.readouterr().out.split("task: large\n")[-1]
    assert "solo (2 runs)" in large
    assert "brindle (0 runs)" in large
    assert bench.main(["report", "--results", str(results), "--require-runs", "1"]) == 1
    grouped = capsys.readouterr().out
    assert "task: tinydb\n" + tinydb.split("task: tinydb\n")[-1] in grouped
    assert "task: large\n" + large in grouped
    rows = [{"arm": "brindle", "only_workers": ["developer-codex"], "limits_used": {"codex": 3.5}},
            {"arm": "brindle", "only_workers": ["developer-fable"], "limits_used": {"codex": None}},
            {"arm": "brindle", "excluded_workers": ["developer-fable"], "limits_used": {"codex": 1}}]
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    summary = bench.summarize(rows)
    assert summary["brindle [only=developer-codex]"]["limits_used.codex"] == {
        "median": 3.5, "min": 3.5, "max": 3.5}
    assert summary["brindle [only=developer-fable]"]["limits_used.codex"] is None
    assert summary["brindle [exclude=developer-fable]"]["runs"] == 1
    assert bench.main(["report", "--results", str(results)]) == 0
    assert "3.5%" in capsys.readouterr().out


def test_capped_routing_drops_excluded_workers_from_every_tier(tmp_path):
    routing = bench.capped_routing({"heavy": ["developer-heavy"], "advanced": ["developer-fable", "developer-heavy"]},
                                   ["developer-fable"])
    bench.configure_clone(tmp_path, "claude-opus-5-5", None, routing)
    cfg = json.loads((tmp_path / ".brindle" / "config.local.json").read_text())
    assert cfg["routing"] == {"heavy": ["developer-heavy"], "advanced": ["developer-heavy"]}


def test_only_workers_cli_writes_every_tier(tmp_path, monkeypatch):
    client = SimpleNamespace(
        python="python", env={}, cli=lambda *a, **kw: SimpleNamespace(returncode=0),
        probe=lambda *a, **kw: {"root": {"id": "root"}})
    monkeypatch.setattr(bench, "installed_client", lambda *a: client)
    monkeypatch.setattr(bench, "sh", lambda *a, **kw: SimpleNamespace(stdout=json.dumps({
        "heavy": ["developer-heavy"], "advanced": ["developer-fable"]})))
    monkeypatch.setattr(bench, "watch_session", lambda *a: {
        "seen": {"root": {}}, "transcripts": {}, "outside": {}, "snap": {}, "finished": "goal",
        "valid": True, "why": None, "needs_person": False, "stop": time.time(), "rates": {}, "events": []})
    monkeypatch.setattr(bench, "brindle_usage", lambda *a: {})
    runs = []

    def run(args):
        runs.append(bench.run_brindle(args, tmp_path, "spec", {}))
        return 0

    monkeypatch.setattr(bench, "cmd_run", run)
    assert bench.main(["run", "--arm", "brindle", "--run-id", "1",
                       "--only-workers", "developer-codex,developer-fable",
                       "--brindle-setting", "fast_path=true", "--brindle-setting", "review_sizing=small"]) == 0
    cfg = json.loads((tmp_path / ".brindle/config.local.json").read_text())
    assert cfg["routing"] == {tier: ["developer-codex", "developer-fable"] for tier in ("heavy", "advanced")}
    assert (cfg["fast_path"], cfg["review_sizing"]) == (True, "small")
    assert runs[0]["only_workers"] == ["developer-codex", "developer-fable"]
    assert runs[0]["brindle_settings"] == {"fast_path": True, "review_sizing": "small"}
    assert bench.main(["run", "--arm", "brindle", "--run-id", "2",
                       "--only-workers", "developer-codex,developer-fable",
                       "--exclude-worker", "developer-fable"]) == 0
    cfg = json.loads((tmp_path / ".brindle/config.local.json").read_text())
    assert cfg["routing"] == {tier: ["developer-codex"] for tier in ("heavy", "advanced")}


def snapshot(folder: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p.relative_to(folder)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_checkout_env_uses_a_temp_home_and_leaves_provider_homes_unchanged(tmp_path, monkeypatch):
    person = tmp_path / "person"
    monkeypatch.setenv("HOME", str(person))
    claude, codex = person / "claude-home", person / "codex-home"
    for folder, files in ((claude, (".claude.json", "settings.json", "projects/p/s.jsonl")),
                          (codex, ("auth.json", "config.toml", "sessions/r.jsonl"))):
        for name in files:
            (folder / name).parent.mkdir(parents=True, exist_ok=True)
            (folder / name).write_text(name)
    before = {f: snapshot(f) for f in (claude, codex)}
    root = tmp_path / "bench"
    root.mkdir()
    homes = {"claude": {"path": str(claude), "isolated": True}, "codex": {"path": str(codex), "isolated": True}}
    env = bench.checkout_env({"BRINDLE_HOME": str(person / ".brindle"), "PATH": "/usr/bin",
                              "CLAUDE_CONFIG_DIR": str(person / "inherited")},   # brindle's own home wins
                             root, homes, bench.bench_socket(root))
    real = root.resolve()
    assert env["BRINDLE_HOME"] == str(real / "home")
    # Its tmux server is out of reach of every brindle's leftover-server sweep.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    tmux.socket_dir().mkdir()
    (tmux.socket_dir() / env["BRINDLE_TMUX_SOCKET"]).touch()
    assert tmux.other_servers() == []
    assert env["PATH"].startswith(str(real / "bin"))
    assert env["BRINDLE_CLAUDE_CONFIG_HOME"] == str(claude)   # the Keychain login's path
    linked = Path(env["BRINDLE_CODEX_CONFIG_HOME"]) / "auth.json"
    assert linked.is_symlink() and linked.resolve() == (codex / "auth.json").resolve()
    # The only two writes allowed outside the temp home are the ones the record names.
    assert [(w["provider"], w["path"]) for w in bench.provider_writes(env)] == [
        ("claude", str(claude)), ("codex", str(codex / "auth.json"))]
    for overlapping in (person / ".brindle", person):   # the real home itself, or a folder holding it
        with pytest.raises(bench.BenchError):
            bench.checkout_env({"PATH": ""}, overlapping, homes, "s")
    bench.stop_checkout(None, root)
    assert not linked.is_symlink()
    assert {f: snapshot(f) for f in (claude, codex)} == before   # setup and teardown wrote nothing there


@pytest.mark.parametrize("fails", [
    False,
    pytest.param(True, marks=pytest.mark.variant("A run that fails must still stop the temp home's daemon")),
])
def test_checkout_run_records_its_commit_and_stops_its_daemon(tmp_path, monkeypatch, fails):
    """A fake machine: daemons per BRINDLE_HOME and tmux servers per socket,
    which the run starts and only the real cleanup path can stop."""
    monkeypatch.setenv("HOME", str(tmp_path / "person"))
    monkeypatch.chdir(tmp_path)   # relative socket paths: a Unix socket path must stay short
    sockets = Path(f"tmux-{os.getuid()}")
    person_home = str(tmp_path / "person" / ".brindle")
    daemons, servers = {person_home: "running"}, {"default": "running"}
    root = tmp_path / "bench"
    root.mkdir()

    def sh(cmd, cwd, env, check=True, **kw):
        if cmd[-2:] == ["daemon", "stop"]:
            daemons[env["BRINDLE_HOME"]] = "stopped"
        elif cmd[:2] == ["tmux", "-L"] and cmd[3:] == ["kill-server"]:
            servers[cmd[2]] = "stopped"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def client(args, clone, env, root):
        return bench.Brindle(Path("brindle"), "python", clone, bench.checkout_env(env, root, {}, "brindle-bench-x"),
                             argv=["uv", "run", "brindle"], source=tmp_path)

    def drive(args, b, *rest):
        daemons[b.env["BRINDLE_HOME"]] = servers[b.env["BRINDLE_TMUX_SOCKET"]] = "running"
        sockets.mkdir()
        with socket.socket(socket.AF_UNIX) as s:   # what a killed server leaves: its socket file
            s.bind(str(sockets / b.env["BRINDLE_TMUX_SOCKET"]))
        if fails:
            raise bench.BenchError("supervisor never started")
        return {"finished": "goal"}

    monkeypatch.setattr(bench, "sh", sh)
    monkeypatch.setattr(bench, "checkout_info", lambda path, env: {"path": str(path), "commit": "abc", "dirty": False})
    monkeypatch.setattr(bench, "checkout_client", client)
    monkeypatch.setattr(bench, "drive_brindle", drive)
    monkeypatch.setattr(bench.tempfile, "mkdtemp", lambda prefix: str(root))
    args = SimpleNamespace(brindle_checkout=str(tmp_path), brindle_setting=[("fast_path", True)], leave_running=False)
    if fails:
        with pytest.raises(bench.BenchError):
            bench.run_brindle(args, tmp_path, "spec", {"BRINDLE_HOME": person_home, "TMUX_TMPDIR": "."})
    else:
        out = bench.run_brindle(args, tmp_path, "spec", {"BRINDLE_HOME": person_home, "TMUX_TMPDIR": "."})
        assert out["brindle_checkout"]["commit"] == "abc"
        assert out["brindle_settings"] == {"fast_path": True}
    temp_home = str(root.resolve() / "home")
    assert daemons == {person_home: "running", temp_home: "stopped"}   # the person's daemon untouched
    assert servers == {"default": "running", "brindle-bench-x": "stopped"}
    assert list(sockets.iterdir()) == []   # nor is its socket file left behind


def test_machine_probe_reads_weekly_plan_limits_and_other_sessions(db, tmp_path, monkeypatch):
    from brindle import quota, subscription_usage

    repo = str(tmp_path)
    for wid, path in (("own", repo), ("other", "/other")):
        db.add_workspace(Workspace(wid, path, "s", "worktree", "branch", "main", path, None, "t", time.time()))
        db.add_agent(Agent(wid, wid, "supervisor", "claude", None, "interactive", "processing",
                           "@1", None, time.time()))
    db.add_agent(Agent("done", "other", "developer", "codex", None, "interactive", "done",
                       "@2", None, time.time()))
    monkeypatch.setattr(quota, "get", lambda provider, **kw: quota.Quota(provider, [
        quota.Window(80, 1000, 300), quota.Window(31, 2000, 10080),
        quota.Window(90, 2000, 10080, "Fable")]) if provider == "claude" else None)
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    monkeypatch.setattr(subscription_usage, "cache_key", lambda provider: provider)
    ns = {"__name__": "bench_probe"}
    exec(bench.PROBE, ns)
    out = ns["main"]({"op": "machine", "clone": repo})
    assert out["limits"]["claude"]["used"] == 31  # neither the short window nor the model cap
    assert out["limits"]["codex"] is None
    assert [a["id"] for a in out["other_agents"]] == ["other"]
    before = out["limits"]
    after = {**before, "claude": {**before["claude"], "used": 35}}
    assert bench.limits_used(before, after)["claude"] == 4
    assert bench.limits_used(before, {**after, "codex": {"used": 10}})["codex"] is None
    after["claude"]["resets_at"] += 1
    assert bench.limits_used(before, after)["claude"] is None
    after["claude"] = {**before["claude"], "identity": "new-account", "used": 40}
    assert bench.limits_used(before, after)["claude"] is None


def test_monitor_keeps_agents_seen_only_during_the_run():
    samples = iter([[], [{"id": "midrun", "status": "processing"}], []])
    b = SimpleNamespace(probe=lambda op, **kw: {"limits": {}, "other_agents": next(samples)})
    monitor = bench.RunMonitor(b, 20)
    ticks = iter([False, True])
    monitor.stopped = SimpleNamespace(wait=lambda interval: next(ticks))
    monitor.before = monitor.sample()
    monitor.watch()
    monitor.after = monitor.sample()
    assert [a["id"] for a in monitor.record()["other_agents"]] == ["midrun"]


def test_machine_boundary_reads_codex_rollout_past_a_fresh_cache(tmp_path, monkeypatch):
    from brindle import quota

    monkeypatch.setattr(quota.time, "time", lambda: 1000)
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    home = tmp_path / "codex"
    sessions = home / "sessions"
    sessions.mkdir(parents=True)
    rollout = sessions / "rollout-latest.jsonl"
    monkeypatch.setattr(quota, "codex_home", lambda: home)
    quota.record("codex", [quota.Window(31, 2000, 10080)])
    event = {"payload": {"rate_limits": {"secondary": {
        "used_percent": 31, "resets_at": 2000, "window_minutes": 10080}}}}
    rollout.write_text(json.dumps(event) + "\n")
    ns = {"__name__": "bench_probe"}
    exec(bench.PROBE, ns)
    b = SimpleNamespace(probe=lambda op, **kw: ns["main"]({"op": op, "clone": str(tmp_path), **kw}))
    with bench.RunMonitor(b, 20) as monitor:
        event["payload"]["rate_limits"]["secondary"]["used_percent"] = 35
        rollout.write_text(json.dumps(event) + "\n")
        assert quota.get("codex").windows[0].used == 31
    assert monitor.record()["limits_used"]["codex"] == 4
    args = {"op": "machine", "clone": str(tmp_path), "boundary": True}
    rollout.unlink()
    assert ns["main"](args)["limits"]["codex"] is None


@pytest.mark.parametrize("custom_home", [
    False,
    pytest.param(True, marks=pytest.mark.variant("Solo must use CLAUDE_CONFIG_DIR rather than the default account")),
])
def test_solo_limits_do_not_use_another_claude_home(tmp_path, monkeypatch, custom_home):
    from brindle import providers, quota, subscription_usage

    monkeypatch.setenv("HOME", str(tmp_path))
    for name in providers._ENV_AUTH["claude"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(providers, "claude_org_managed", lambda: None)
    monkeypatch.setattr(providers, "_stored_key", lambda name: name == "ANTHROPIC_API_KEY")
    brindle_config = tmp_path / "brindle-claude.json"
    account = {"oauthAccount": {"accountUuid": "brindle-account", "organizationUuid": "org"}}
    brindle_config.write_text(json.dumps(account))
    solo_home = tmp_path / "solo" if custom_home else tmp_path
    solo_home.mkdir(exist_ok=True)
    if custom_home:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(solo_home))
    else:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    solo_config = solo_home / ".claude.json"
    solo_account = {"oauthAccount": {"accountUuid": "solo-account", "organizationUuid": "org"}}
    solo_config.write_text(json.dumps(solo_account))
    monkeypatch.setattr(providers, "claude_global_config", lambda: str(brindle_config))
    quota.record("claude", [quota.Window(31, time.time() + 1000, 10080)])
    ns = {"__name__": "bench_probe"}
    exec(bench.PROBE, ns)
    b = SimpleNamespace(probe=lambda op, **kw: ns["main"]({"op": op, "clone": str(tmp_path), **kw}))
    with bench.RunMonitor(b, 20, "solo") as monitor:
        pass
    assert monitor.record()["limits_used"]["claude"] is None
    args = {"op": "machine", "clone": str(tmp_path), "arm": "solo"}
    missing = ns["main"](args)
    assert missing["limits"]["claude"] is None
    solo_key = subscription_usage.cache_key("claude", who=json.dumps(["solo-account", "org"]))
    reset = time.time() + 1000
    quota.record("claude", [quota.Window(42, reset, 10080)], identity_key=solo_key)
    own = ns["main"](args)
    assert own["limits"]["claude"]["used"] == 42
    assert own["limits"]["claude"]["identity"] == solo_key
    assert "claude" not in own["limits_reason"]
    assert missing["limits_reason"]["claude"]
    assert monitor.record()["limits_reason"]["claude"]
    with bench.RunMonitor(b, 20, "solo") as monitor:
        quota.record("claude", [quota.Window(46, reset, 10080)], identity_key=solo_key)
    assert monitor.record()["limits_used"]["claude"] == 4
    solo_config.write_text(json.dumps(account))
    assert ns["main"](args)["limits"]["claude"]["used"] == 31
    billed = ns["main"]({**args, "arm": "brindle"})
    assert billed["limits"]["claude"] is None
    assert billed["limits_reason"]["claude"]
    monkeypatch.setenv("ANTHROPIC_API_KEY", "solo-test-key")
    billed = ns["main"](args)
    assert billed["limits"]["claude"] is None
    assert billed["limits_reason"]["claude"]


def test_installed_client_prefers_desktop_and_records_its_engine(tmp_path, monkeypatch):
    desktop = tmp_path / "engine/bin/brindle"
    desktop.parent.mkdir(parents=True)
    desktop.touch()
    python = tmp_path / "engine/python/bin/python3"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(bench, "DESKTOP_BRINDLE", desktop, raising=False)
    monkeypatch.setattr(bench.shutil, "which", lambda *a, **kw: "/old/bin/brindle")
    args = SimpleNamespace(brindle=None, brindle_python=None)
    client = bench.installed_client(args, tmp_path, {})
    assert client.exe == desktop
    assert client.python == str(python)
    assert bench.installed_brindle("/explicit/bin/brindle", {}) == Path("/explicit/bin/brindle")
    python.unlink()
    assert bench.installed_brindle(None, {}) == Path("/old/bin/brindle")
    samples = SimpleNamespace(probe=lambda *a, **kw: {
        "limits": {}, "other_agents": [], "brindle_file": "/engine/brindle/__init__.py",
        "brindle_python": str(python), "brindle_executable": str(desktop)})
    with bench.RunMonitor(samples, 20) as monitor:
        pass
    assert monitor.record()["limits_reader"] == {
        "module": "/engine/brindle/__init__.py", "python": str(python), "executable": str(desktop)}

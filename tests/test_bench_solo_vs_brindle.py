import importlib.util
import json
from pathlib import Path

import pytest

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


def test_unknown_claude_model_raises_and_another_providers_is_flagged():
    with pytest.raises(bench.UnknownModel):
        bench.price_usage({"claude-opus-4-8": bench.empty()})
    usage = {"gpt-6-sol": bench.empty(), "gpt-5.6-luna": bench.empty()}
    total, per_model, unpriced = bench.price_usage(usage, {"gpt-6-sol": 1.5, "gpt-5.6-luna": None})
    assert (total, per_model, unpriced) == (1.5, {"gpt-6-sol": 1.5}, ["gpt-5.6-luna"])


def test_solo_prompts_count_logged_approvals_questions_and_a_closing_question(tmp_path):
    log = tmp_path / "approvals.jsonl"
    for tool in ("Bash", "AskUserQuestion"):
        call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                "params": {"name": "approve", "arguments": {"tool_name": tool, "input": {"command": "ls"}}}}
        reply = bench.approver_reply(call, log)
        assert json.loads(reply["result"]["content"][0]["text"]) == {
            "behavior": "allow", "updatedInput": {"command": "ls"}}
    approvals = bench.iter_json(log.read_text().splitlines())
    # Bash's request is a permission prompt; the AskUserQuestion (in the stream) and the
    # closing "Shall I also add docs?" are questions.
    assert bench.solo_prompts(entries(), approvals) == {"permission": 1, "question": 2, "other": 0}


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


def test_brindle_usage_sums_transcripts_outside_providers_and_unseen_agents(tmp_path):
    transcript = tmp_path / "root.jsonl"
    transcript.write_text(STREAM.read_text())
    seen = {"root": {"id": "root", "mode": "interactive"}, "w1": {"id": "w1", "mode": "assign"}}
    outside = {"w1": {"model": "gpt-6-sol", "input": 7, "output": 3, "cache_read": 0,
                      "cache_creation": 0, "cost": 0.25}}
    history = [
        {"agent_id": "root", "tokens": json.dumps({"input": 500, "model": "claude-fable-5-2"})},  # seen: no double count
        {"agent_id": "gone", "tokens": json.dumps({"input": 4, "output": 6, "model": "claude-opus-5-5"})},
    ]
    out = bench.brindle_usage("root", seen, {"root": {str(transcript)}}, outside, history)
    assert out["by_model"] == {"claude-fable-5-2": FABLE, "claude-haiku-5-5": HAIKU,
                               "gpt-6-sol": {"input": 7, "output": 3, "cache_creation": 0, "cache_read": 0},
                               "claude-opus-5-5": {"input": 4, "output": 6, "cache_creation": 0, "cache_read": 0}}
    assert set(out["by_role"]) == {"supervisor", "worker", "unseen"}
    assert out["outside"] == {"gpt-6-sol": 0.25}
    assert out["supervisor_model_observed"] == "claude-fable-5-2"


def test_report_medians_and_require_runs(tmp_path):
    results = tmp_path / "results.jsonl"
    rows = [{"arm": "solo", "wall_seconds": w, "tokens_total": t, "cost_usd": c, "prompts_total": p,
             "grade": {"passed": g, "failed": 4 - g}}
            for w, t, c, p, g in [(100, 10, 1.0, 0, 4), (300, 30, 3.0, 2, 2), (200, 20, 2.0, 1, 3)]]
    rows.append({"arm": "brindle", "wall_seconds": 50, "tokens_total": 5, "cost_usd": 0.5,
                 "prompts_total": 4, "pass_rate": 1.0})
    results.write_text("".join(json.dumps(r) + "\n" for r in rows))
    solo = bench.summarize(bench.load_records(results))["solo"]
    assert solo["runs"] == 3
    assert solo["wall_seconds"] == {"median": 200, "min": 100, "max": 300}
    assert solo["pass_rate"] == {"median": 0.75, "min": 0.5, "max": 1.0}
    assert solo["prompts_total"] == {"median": 1, "min": 0, "max": 2}
    assert bench.main(["report", "--results", str(results), "--require-runs", "1"]) == 0
    assert bench.main(["report", "--results", str(results), "--require-runs", "2"]) == 1

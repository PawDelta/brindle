import importlib.util
import json
from pathlib import Path

from conftest import sh

spec = importlib.util.spec_from_file_location(
    "bench_blind_review", Path(__file__).resolve().parent.parent / "scripts/bench_blind_review.py")
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)

GIT_ID = "git -c user.name=t -c user.email=t@localhost -c commit.gpgsign=false"


def finding(title, severity, code, location="lib.py:upsert"):
    return {"title": title, "severity": severity, "location": location, "description": "", "reproduction": code}


def test_review_keeps_reproduced_bugs_merges_them_and_checks_each_in_both_arms(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    (base / "lib.py").write_text("def upsert(rows):\n    return rows\n")
    sh(f"git init -q -b main && git add -A && {GIT_ID} commit -qm init", base)
    commit = sh("git rev-parse HEAD", base)
    checkouts = {}
    for arm, marker in (("solo", "SOLO"), ("brindle", "OTHER")):
        co = tmp_path / arm
        sh(f"git clone -q {base} {co}", tmp_path)
        (co / "lib.py").write_text(f"def upsert(rows):\n    return rows[1:]  # {marker}\n")
        (co / ".venv" / "bin").mkdir(parents=True)
        (co / ".venv" / "bin" / "python").write_text("")
        checkouts[arm] = co
    co = checkouts["brindle"]   # everything here that names the arm must not reach a reviewer
    (co / ".brindle").mkdir()
    (co / ".brindle" / "config.json").write_text("{}")
    (co / "HISTORY_SPEC.md").write_text("plan")
    (co / "helper.py").write_text("# written by Brindle's worker\n")
    sh(f"git checkout -q -b brindle/session-7 && git add -A && {GIT_ID} commit -qm 'brindle: worker merge'", co)
    kit = tmp_path / "kit"
    kit.mkdir()
    (kit / "repo.json").write_text(json.dumps({"name": "t", "commit": commit}))
    (kit / "spec.md").write_text("Make upsert keep every row.")
    results, out = tmp_path / "results.jsonl", tmp_path / "review.jsonl"
    results.write_text("".join(json.dumps({"task": "t", "arm": arm, "run_id": "1", "checkout": str(path)}) + "\n"
                               for arm, path in checkouts.items()))

    answers = {   # (reviewer, arm's marker) -> findings
        ("opus", "SOLO"): [finding("Upsert drops existing rows", "high", "A"),
                           finding("Slow on large inputs", "low", "NOPE")],
        ("codex", "SOLO"): [finding("upsert drops rows that already exist", "critical", "A2")],
        ("codex", "OTHER"): [finding("Crash on empty input", "medium", "B", "lib.py:parse")],
    }
    outcomes = {("SOLO", "A"): "bug", ("SOLO", "A2"): "bug", ("SOLO", "NOPE"): "ok", ("SOLO", "B"): "inconclusive",
                ("OTHER", "A"): "bug", ("OTHER", "A2"): "ok", ("OTHER", "B"): "bug"}
    seen = []

    def fake_reviewer(name, model, prompt, workdir, scratch, env, seconds):
        seen.append((prompt, workdir, sh("git log --format=%s%d", workdir)))
        usage = {"input": 1000, "output": 200, "cache_creation": 0, "cache_read": 0, "cache_creation_1h": 0}
        return {"findings": answers.get((name, "SOLO" if "SOLO" in prompt else "OTHER"), []),
                "by_model": {"claude-opus-5-5" if name == "opus" else "gpt-5.5": usage}, "error": None}

    def fake_repro(tree, python, code, scratch, seconds, sandbox):
        assert python.name == "python" and seconds == review.REPRO_SECONDS
        return {"status": outcomes[("SOLO" if "SOLO" in (tree / "lib.py").read_text() else "OTHER", code)],
                "exit": None, "output": ""}

    monkeypatch.setattr(review, "call_reviewer", fake_reviewer)
    monkeypatch.setattr(review, "run_repro", fake_repro)
    assert review.main(["review", "--task", "t", "--run-id", "1", "--task-dir", str(kit), "--results", str(results),
                        "--out", str(out), "--scratch", str(tmp_path / "scratch")]) == 0

    # blind: no brindle file, commit message, branch name or the word itself reaches a reviewer
    assert len(seen) == 4
    for prompt, workdir, log in seen:
        assert "brindle" not in prompt.lower() and "session-7" not in prompt and "HISTORY_SPEC" not in prompt
        assert log == "base (HEAD -> review)"
    assert any("helper.py" in p and "[redacted]" in p for p, _, _ in seen)

    rec = json.loads(out.read_text())
    solo, other = rec["arms"]["solo"], rec["arms"]["brindle"]
    # the two reviewers' reports of one bug merge; the one that didn't reproduce is dropped
    assert [(b["title"], b["severity"], b["found_by"], b["how"]) for b in solo["bugs"]] == [
        ("upsert drops rows that already exist", "critical", ["codex", "opus"], "found")]
    assert [u["title"] for u in solo["unconfirmed"]] == ["Slow on large inputs"]
    # found in solo, reproduced in the other arm by one of its reproductions: it counts there too
    assert [(b["severity"], b["how"], b["reproduction"]) for b in other["bugs"]] == [
        ("critical", "cross-applied", "A"), ("medium", "found", "B")]
    assert other["by_severity"] == {"critical": 1, "high": 0, "medium": 1, "low": 0}
    # found in the other arm, inconclusive in solo: not counted there
    assert solo["by_severity"] == {"critical": 1, "high": 0, "medium": 0, "low": 0}
    assert solo["not_in_this_arm"][0]["status"] == ["inconclusive"]
    # the review's own cost, per reviewer, apart from the arms
    assert rec["reviewers"]["opus"]["tokens_total"] == rec["reviewers"]["codex"]["tokens_total"] == 2400
    assert rec["review_cost_usd"] > 0 and "cost_usd" not in solo

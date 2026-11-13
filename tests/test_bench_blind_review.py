import importlib.util
import json
import os
import tempfile
from pathlib import Path

import pytest
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
    (tmp_path / "note.txt").write_text("brindle base note")
    (base / "note_link").symlink_to(tmp_path / "note.txt")   # the base snapshot's own link out of the tree
    (base / "bridge").symlink_to(".")
    (base / "base_chain.txt").symlink_to("bridge/../note.txt")   # out only through another link
    sh(f"git init -q -b main && git add -A && {GIT_ID} commit -qm init", base)
    commit = sh("git rev-parse HEAD", base)
    checkouts = {}
    for arm, marker in (("solo", "SOLO"), ("brindle", "OTHER")):
        co = tmp_path / f"checkout-{marker.lower()}"   # a neutral path, so a link into it leaks only content
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
    (co / "alias.py").symlink_to(co / "helper.py")   # absolute, into the original checkout
    (tmp_path / "outside.txt").write_text("Brindle outside")
    (co / "up.txt").symlink_to("../outside.txt")   # relative, climbing out of the checkout
    (co / "chain.py").symlink_to("bridge/../outside.txt")   # out through the base's bridge link
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
    seen, ran_on, aliases = [], [], []

    def walk(root):   # every path under root, links not followed, .git left out
        return [Path(h, n) for h, ds, fs in os.walk(root) for n in ds + fs if ".git" not in Path(h, n).parts]

    def escaping(root):   # links that resolve (through any other links) outside root
        top = os.path.realpath(root)
        return [p for p in walk(root) if p.is_symlink()
                and os.path.commonpath([top, os.path.realpath(p)]) != top]

    def readable(workdir):   # what a reviewer can read there: files, the index and the history
        files = [p.read_text(errors="ignore") + str(p) for p in walk(workdir) if p.is_file()]
        return "\n".join([*files, sh("git grep -i brindle HEAD; git grep -i --cached brindle; true", workdir),
                          sh("git log --format=%s%d", workdir)])

    def fake_reviewer(name, model, prompt, workdir, scratch, env, seconds):
        snapshot = Path(tempfile.mkdtemp(prefix="base-snapshot-", dir=tmp_path))   # the committed base
        sh(f"git archive HEAD | tar -x -C {snapshot}", workdir)
        assert escaping(workdir) == [] and escaping(snapshot) == []
        seen.append((prompt, readable(workdir), sh("git log --format=%s%d", workdir)))
        if (workdir / "alias.py").is_symlink():
            aliases.append((workdir / "alias.py").read_text())
        usage = {"input": 1000, "output": 200, "cache_creation": 0, "cache_read": 0, "cache_creation_1h": 0}
        return {"findings": answers.get((name, "SOLO" if "SOLO" in prompt else "OTHER"), []),
                "by_model": {"claude-opus-5-5" if name == "opus" else "gpt-5.5": usage}, "error": None}

    def fake_repro(tree, python, code, scratch, seconds):
        assert python.name == "python" and seconds == review.REPRO_SECONDS
        ran_on.append((tree / "helper.py").read_text() if (tree / "helper.py").exists() else "")
        return {"status": outcomes[("SOLO" if "SOLO" in (tree / "lib.py").read_text() else "OTHER", code)],
                "exit": None, "output": ""}

    monkeypatch.setattr(review, "can_sandbox", lambda: True)
    monkeypatch.setattr(review, "call_reviewer", fake_reviewer)
    monkeypatch.setattr(review, "run_repro", fake_repro)
    assert review.main(["review", "--task", "t", "--run-id", "1", "--task-dir", str(kit), "--results", str(results),
                        "--out", str(out), "--scratch", str(tmp_path / "scratch")]) == 0

    # blind: no brindle file, commit message, branch name or the word itself reaches a reviewer,
    # in the prompt or in any file, index entry or commit of the tree it reads
    assert len(seen) == 4
    for prompt, files, log in seen:
        for shown in (prompt, files):
            assert "brindle" not in shown.lower() and "session-7" not in shown and "HISTORY_SPEC" not in shown
        assert log == "base (HEAD -> review)"
    assert any("helper.py" in p and "[redacted]" in p for p, _, _ in seen)
    # a link into the original checkout reads the redacted copy instead
    assert aliases == ["# written by [redacted]'s worker\n"] * 2
    # reproductions run on the arm's files as written, not on the reviewers' redacted copy
    assert any("Brindle" in helper for helper in ran_on)

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


@pytest.mark.parametrize("files, links", [
    # Every link resolves inside on its own, but redacting "zbrindle" turns
    # "[redacted] -> zbrindle/../OUT" into a link out of the tree (through
    # "z[redacted] -> ."), and "brindle/data" redacts onto that same path.
    ({"OUT/keep": "in", "deep/keep": "in", "brindle/data": "brindle payload"},
     {"zbrindle": "deep", "z[redacted]": ".", "[redacted]": "zbrindle/../OUT"}),
    # "Xbrindle" redacts to "X[redacted]", a link out of the tree (through
    # "Bridge -> ."), which a case-insensitive filesystem takes for the
    # directory "x[redacted]" another link is installed under.
    ({}, {"Bridge": ".", "Xbrindle": "Bridge/../OUT", "x[redacted]/created/payload": "inside"}),
])
def test_redacting_a_base_snapshot_never_writes_through_a_link(tmp_path, files, links):
    base, dest, outside = tmp_path / "base", tmp_path / "tree", tmp_path / "OUT"
    for rel, text in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(text)
    for rel, target in links.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).symlink_to(target)
    sh(f"git init -q -b main && git add -A && {GIT_ID} commit -qm init", base)
    outside.mkdir()   # the tree's sibling, where an escape would land
    (outside / "data").write_text("sentinel")
    dest.mkdir()
    with pytest.raises(review.BenchError):
        review.blind_tree(base, sh("git rev-parse HEAD", base), dest, dict(os.environ), ["brindle"])
    assert os.listdir(outside) == ["data"] and (outside / "data").read_text() == "sentinel"
    assert os.listdir(dest) == []   # refused before anything was written


@pytest.mark.parametrize("sandboxed", [True, False])
def test_a_reproduction_never_writes_or_runs_outside_its_copy(tmp_path, monkeypatch, sandboxed):
    tree, scratch, outside = tmp_path / "tree", tmp_path / "scratch", tmp_path / "outside.txt"
    tree.mkdir()
    scratch.mkdir()
    outside.write_text("keep")
    (tree / review.REPRO_NAME).symlink_to(outside)   # a checkout's own repro.py pointing out of the copy
    started = []

    class FakeProc:
        pid, returncode = 0, 0

        def __init__(self, cmd, cwd, **kw):
            started.append((cmd, (Path(cwd) / review.REPRO_NAME).read_text()))

        def communicate(self, timeout=None):
            return "", None

    monkeypatch.setattr(review, "can_sandbox", lambda: sandboxed)
    monkeypatch.setattr(review.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(review, "_kill_group", lambda pid: None)
    if sandboxed:
        assert review.run_repro(tree, Path("py"), "print(1)", scratch, 1.0)["status"] == "ok"
        assert started[0][0][0] == "sandbox-exec" and started[0][1] == "print(1)"
    else:   # no enforcing sandbox: fail closed, before writing or running anything
        with pytest.raises(review.BenchError):
            review.run_repro(tree, Path("py"), "print(1)", scratch, 1.0)
        assert started == [] and list(scratch.iterdir()) == []
    assert outside.read_text() == "keep"


def test_reports_on_different_functions_stay_apart_and_each_is_checked_in_both_arms():
    found = {"solo": [{**finding("Insert loses rows after a failed write", "high", "I", "db.py:insert"),
                       "reviewer": "opus"},
                      # names only the file: it may join either one, but must not bridge the two
                      {**finding("Loses rows after a failed write", "high", "F", "db.py"), "reviewer": "codex"},
                      {**finding("Update loses rows after a failed write", "high", "U", "db.py:update"),
                       "reviewer": "codex"}],
             "brindle": []}
    ran = []

    def reproduce(arm, code):
        ran.append((arm, code))
        return {"status": "bug" if arm == "solo" or code == "U" else "ok", "exit": None, "output": ""}

    out = review.assess(found, reproduce)
    assert [b["location"] for b in out["solo"]["bugs"]] == ["db.py:insert", "db.py:update"]
    assert [(b["location"], b["how"]) for b in out["brindle"]["bugs"]] == [("db.py:update", "cross-applied")]
    assert sorted(ran) == [("brindle", c) for c in "FIU"] + [("solo", c) for c in "FIU"]

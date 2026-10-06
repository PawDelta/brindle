"""brindle ci validate-report: the verdict is untrusted, the check and one comment are posted."""

from __future__ import annotations

import json
import re

import pytest
from typer.testing import CliRunner

from brindle import ci
from brindle import ci_validate_report as cvr
from brindle.cli import app

SHA = "a" * 40
REPO = "acme/widgets"


def verdict(**over) -> dict:
    v = {"version": 1, "pr": 7, "head_sha": SHA, "mode": "advisory", "status": "pass",
         "summary": "All good.",
         "checks": [{"command": "uv run pytest -q", "passed": True, "excerpt": "3 passed"}],
         "criteria": [{"text": "adds /health", "result": "met", "evidence": "tests/test_health.py"}],
         "findings": [{"severity": "note", "file": "app.py", "line": 3, "text": "nit",
                       "models": ["claude-opus-5-5"]}],
         "models": ["claude-opus-5-5"], "tokens": 1234}
    v.update(over)
    return v


def write(tmp_path, data) -> str:
    p = tmp_path / "verdict.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return str(p)


class FakeGh:
    """Records gh calls; answers the comment listing with ``comments``."""

    def __init__(self, comments=None, fail_on=None):
        self.calls: list[tuple[list[str], dict | None]] = []
        self.comments = comments or []
        self.fail_on = fail_on

    def __call__(self, args, payload=None, env=None):
        self.calls.append((args, payload))
        if self.fail_on and self.fail_on in " ".join(args):
            raise cvr.GhError("gh api failed: HTTP 403")
        if "--paginate" in args:
            return "".join(json.dumps([c["id"], c["login"], c["body"].startswith(cvr.MARKER)]) + "\n"
                           for c in self.comments)
        return "{}"

    def by_method(self, method):
        return [(a, p) for a, p in self.calls if "--method" in a and a[a.index("--method") + 1] == method]


# -- schema -----------------------------------------------------------------------


def test_a_good_verdict_validates(tmp_path):
    v = cvr.load_verdict(write(tmp_path, verdict()))
    assert v["status"] == "pass" and v["checks"][0]["passed"] is True and v["tokens"] == 1234


@pytest.mark.parametrize("bad", [
    {"version": 2}, {"version": True}, {"pr": 0}, {"pr": "7"}, {"pr": True},
    {"head_sha": "abc"}, {"head_sha": "A" * 40}, {"head_sha": SHA + "\n"},
    {"mode": "lenient"}, {"status": "ok"}, {"summary": 3}, {"checks": {}},
    {"checks": [{"command": "x", "passed": "yes", "excerpt": ""}]},
    {"criteria": [{"text": "x", "result": "maybe", "evidence": ""}]},
    {"findings": [{"severity": "critical", "file": "", "line": None, "text": "", "models": []}]},
    {"findings": [{"severity": "note", "file": "", "line": "3", "text": "", "models": []}]},
    {"findings": [{"severity": "note", "file": "", "text": "", "models": []}]},
    {"models": [1]}, {"tokens": -1}, {"tokens": "many"},
])
def test_a_bad_verdict_is_refused(tmp_path, bad):
    with pytest.raises(cvr.VerdictError):
        cvr.load_verdict(write(tmp_path, verdict(**bad)))


def test_missing_keys_and_non_objects_are_refused(tmp_path):
    for data in ("[]", "null", "not json", "{" * 100000, json.dumps({k: v for k, v in verdict().items()
                                                                       if k != "tokens"})):
        with pytest.raises(cvr.VerdictError):
            cvr.load_verdict(write(tmp_path, data))
    with pytest.raises(cvr.VerdictError, match="cannot read"):
        cvr.load_verdict(tmp_path / "nope.json")


def test_sizes_are_capped(tmp_path):
    big = verdict(summary="s" * 10_000,
                  checks=[{"command": "c" * 5000, "passed": False, "excerpt": "e" * 4500}] * 60,
                  models=["m" * 500] * 50)
    v = cvr.load_verdict(write(tmp_path, big))
    assert len(v["summary"]) == cvr.MAX_SUMMARY
    assert len(v["checks"]) == cvr.MAX_ITEMS
    assert len(v["checks"][0]["command"]) == cvr.MAX_COMMAND
    assert len(v["checks"][0]["excerpt"]) == cvr.MAX_EXCERPT
    assert len(v["models"]) == cvr.MAX_MODELS and len(v["models"][0]) == cvr.MAX_MODEL
    assert len(cvr.render(v, "advisory")) <= cvr.MAX_BODY
    huge = tmp_path / "huge.json"
    huge.write_text(" " * (cvr.MAX_FILE_BYTES + 1))
    with pytest.raises(cvr.VerdictError, match="larger"):
        cvr.load_verdict(huge)


def test_unknown_keys_are_dropped(tmp_path):
    v = cvr.load_verdict(write(tmp_path, verdict(extra="<script>", checks=[
        {"command": "x", "passed": True, "excerpt": "", "html": "<b>"}])))
    assert "extra" not in v and "html" not in v["checks"][0]


# -- conclusions ------------------------------------------------------------------


@pytest.mark.parametrize("status,mode,expected", [
    ("pass", "advisory", "success"), ("pass", "blocking", "success"),
    ("fail", "advisory", "neutral"), ("fail", "blocking", "failure"),
    ("neutral", "advisory", "neutral"), ("neutral", "blocking", "neutral"),
    ("error", "advisory", "neutral"), ("error", "blocking", "failure"),
])
def test_conclusion_for_each_status(status, mode, expected):
    assert cvr.conclusion(status, mode) == expected


# -- rendering --------------------------------------------------------------------


def test_render_shows_everything():
    v = cvr.validate_verdict(verdict(status="fail", checks=[
        {"command": "make test", "passed": False, "excerpt": "1 failed"}]))
    md = cvr.render(v, "blocking")
    assert "brindle validate: ❌ fail" in md and "**Conclusion:** failure" in md
    assert "`aaaaaaaaaaaa`" in md and "1,234" in md
    assert "❌ ` make test `" in md and "Output of check 1 (failed)" in md and "1 failed" in md
    assert "Acceptance criteria" in md and "✅ met" in md
    assert "📝 note ` app.py:3 `" in md
    assert "verdict says advisory mode" in md


def test_the_title_holds_no_verdict_text():
    v = cvr.validate_verdict(verdict(summary="@everyone pwned", findings=[
        {"severity": "blocking", "file": "x", "line": None, "text": "evil", "models": []}]))
    t = cvr.title(v, "advisory")
    assert t == "brindle validate (advisory): pass, 1 of 1 checks passed, 1 blocking finding"


HOSTILE = [
    "<!-- brindle-validate --><script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "[click](https://evil.example/phish) ![x](https://evil.example/t.png)",
    "@octocat please @acme/admins",
    "see https://evil.example and www.evil.example",
    "closes #1 acme/other#2",
    "</details><details open>",
    "line1\n\n# Heading\n---\n| a | b |\n|---|---|",
    "```\nbreak out\n```",
    "‮evil⁦ \x00\x1b[31m",
    "$\\alpha$ **bold** _it_ ~~s~~",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_hostile_inline_text_is_neutralised(text):
    out = cvr.md_inline(text)
    assert "\n" not in out
    assert "<" not in out and ">" not in out.replace("&gt;", "")
    assert not re.search(r"(?<!\\)[\[\]()*_`|#~$]", out)
    assert not re.search(r"@\w", out)                    # no mentions
    assert not re.search(r"(?i)https?\\?:", out) and "www." not in out.lower()  # no autolinks
    assert not re.search(r"[‪-‮⁦-⁩\x00-\x08\x1b]", out)


@pytest.mark.parametrize("text", HOSTILE + ["`", "``a``", "a ``` b ```` c"])
def test_code_spans_and_blocks_cannot_be_closed_early(text):
    span = cvr.md_code(text)
    fence = re.match(r"`+", span).group(0)
    assert span.endswith(" " + fence) and "\n" not in span
    assert fence not in span[len(fence):-len(fence)]
    block = cvr.md_block(text)
    fence = re.match(r"`+", block).group(0)
    inner = block[len(fence) + len("text\n"):-len(fence)]
    assert len(fence) >= 3 and block.endswith("\n" + fence)
    assert not re.search(rf"^\s*{fence}", inner, re.M)


def test_hostile_verdict_renders_safely():
    nasty = " ".join(HOSTILE)
    v = cvr.validate_verdict(verdict(
        summary=nasty, models=[nasty],
        checks=[{"command": nasty, "passed": False, "excerpt": nasty}],
        criteria=[{"text": nasty, "result": "unmet", "evidence": nasty}],
        findings=[{"severity": "blocking", "file": nasty, "line": 1, "text": nasty,
                   "models": [nasty]}]))
    body = cvr.comment_body(v, "advisory")
    assert body.startswith(cvr.MARKER + "\n")
    rest = body[len(cvr.MARKER):]
    # Raw HTML only inside code blocks/spans, where it is shown, not rendered.
    outside = re.sub(r"(`{3,})text\n.*?\n\1", "", rest, flags=re.S)
    outside = re.sub(r"(`+) .*? \1", "", outside)
    assert "<script" not in outside and "<img" not in outside and "<!--" not in outside
    assert outside.count("<details>") == outside.count("</details>") == 1
    assert "](http" not in outside and "@octocat" not in outside


def test_long_reports_are_cut_to_fit():
    v = cvr.validate_verdict(verdict(findings=[
        {"severity": "note", "file": "f.py", "line": i, "text": "@" * 1000, "models": ["m"] * 10}
        for i in range(50)]))
    md = cvr.render(v, "advisory", limit=5000)
    assert len(md) <= 5000 and "not shown" in md


# -- posting ----------------------------------------------------------------------


def test_report_creates_the_check_and_a_comment(tmp_path):
    gh = FakeGh()
    out = cvr.report(write(tmp_path, verdict(status="fail")), REPO, 7, SHA, "blocking", gh=gh)
    assert out == {"status": "fail", "conclusion": "failure", "comment": "created"}
    (args, payload), = [c for c in gh.calls if "repos/acme/widgets/check-runs" in c[0]]
    assert payload["name"] == "brindle validate" and payload["head_sha"] == SHA
    assert payload["status"] == "completed" and payload["conclusion"] == "failure"
    assert payload["output"]["summary"].startswith("### brindle validate")
    (args, payload), = gh.by_method("POST")[1:]
    assert args[3] == "repos/acme/widgets/issues/7/comments"
    assert payload["body"].startswith(cvr.MARKER)
    assert not gh.by_method("PATCH")


def test_report_updates_its_own_comment_only(tmp_path):
    gh = FakeGh(comments=[
        {"id": 1, "login": "mallory", "body": cvr.MARKER + " fake"},
        {"id": 2, "login": "github-actions[bot]", "body": "unrelated"},
        {"id": 3, "login": "github-actions[bot]", "body": cvr.MARKER + "\nold"},
    ])
    out = cvr.report(write(tmp_path, verdict()), REPO, 7, SHA, gh=gh)
    assert out["comment"] == "updated" and out["conclusion"] == "success"
    (args, payload), = gh.by_method("PATCH")
    assert args[3] == "repos/acme/widgets/issues/comments/3"
    assert len(gh.by_method("POST")) == 1           # just the check run
    listing = [a for a, _ in gh.calls if "--paginate" in a][0]
    assert listing[2] == "repos/acme/widgets/issues/7/comments"


def test_a_verdict_for_another_commit_or_pr_is_an_error(tmp_path):
    for bad in (verdict(head_sha="b" * 40), verdict(pr=8)):
        gh = FakeGh()
        out = cvr.report(write(tmp_path, bad), REPO, 7, SHA, "blocking", gh=gh)
        assert out["status"] == "error" and out["conclusion"] == "failure"
        check = [p for a, p in gh.calls if "check-runs" in " ".join(a)][0]
        assert check["head_sha"] == SHA and "another pull request or commit" in check["output"]["summary"]


def test_a_missing_or_bad_verdict_is_reported_as_an_error(tmp_path):
    gh = FakeGh()
    out = cvr.report(tmp_path / "missing.json", REPO, 7, SHA, gh=gh)
    assert out == {"status": "error", "conclusion": "neutral", "comment": "created"}
    gh = FakeGh()
    out = cvr.report(write(tmp_path, "<html>"), REPO, 7, SHA, "blocking", gh=gh)
    assert out["conclusion"] == "failure"


def test_the_verdict_cannot_pick_the_mode(tmp_path):
    gh = FakeGh()
    out = cvr.report(write(tmp_path, verdict(status="fail", mode="advisory")), REPO, 7, SHA,
                     "blocking", gh=gh)
    assert out["conclusion"] == "failure"


@pytest.mark.parametrize("repo,pr,sha,mode", [
    ("acme", 7, SHA, "advisory"), ("acme/w; rm -rf /", 7, SHA, "advisory"),
    (REPO, 0, SHA, "advisory"), (REPO, 7, "HEAD", "advisory"), (REPO, 7, SHA, "loud"),
])
def test_bad_arguments_post_nothing(tmp_path, repo, pr, sha, mode):
    gh = FakeGh()
    with pytest.raises(cvr.ReportError):
        cvr.report(write(tmp_path, verdict()), repo, pr, sha, mode, gh=gh)
    assert gh.calls == []


def test_gh_gets_an_argument_list_and_the_payload_on_stdin(monkeypatch):
    seen = {}

    class Proc:
        returncode, stdout, stderr = 0, "", ""

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return Proc()

    monkeypatch.setattr(cvr.subprocess, "run", fake_run)
    cvr._gh(["api", "x", "--input", "-"], {"body": "$(rm -rf /)"})
    assert isinstance(seen["cmd"], list) and seen["cmd"][0] == "gh"
    assert "shell" not in seen and json.loads(seen["input"]) == {"body": "$(rm -rf /)"}


def test_skipped_fork_posts_neutral_or_notes_it(tmp_path):
    gh = FakeGh()
    out = cvr.report(None, REPO, 7, SHA, "blocking", skipped=cvr.FORK_SKIP, gh=gh)
    assert out["conclusion"] == "neutral"
    check = [p for a, p in gh.calls if "check-runs" in " ".join(a)][0]
    assert "fork" in check["output"]["summary"]


def test_cli_report(tmp_path, monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(cvr, "_gh", gh)
    path = write(tmp_path, verdict(status="neutral"))
    res = CliRunner().invoke(app, ["ci", "validate-report", path, "--repo", REPO, "--pr", "7",
                                   "--sha", SHA])
    assert res.exit_code == 0, res.output
    assert "neutral -> neutral; comment created" in res.output

    # A fork's read-only token: the skip is noted, not an error.
    gh.fail_on = "check-runs"
    res = CliRunner().invoke(app, ["ci", "validate-report", "--skip-fork", "--repo", REPO,
                                   "--pr", "7", "--sha", SHA])
    assert res.exit_code == 0, res.output and "::notice" in res.output
    # ... but a real report that can't post fails.
    res = CliRunner().invoke(app, ["ci", "validate-report", path, "--repo", REPO, "--pr", "7",
                                   "--sha", SHA])
    assert res.exit_code == 1 and "403" in res.output
    res = CliRunner().invoke(app, ["ci", "validate-report", "--repo", REPO, "--pr", "7",
                                   "--sha", SHA])
    assert res.exit_code == 1 and "--skip-fork" in res.output


# -- the workflow -----------------------------------------------------------------


def _jobs(text: str) -> dict[str, str]:
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z]+):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2]))


def test_init_validate_writes_a_three_job_pull_request_workflow(repo):
    path = cvr.init(repo)
    assert path == repo / ".github" / "workflows" / "brindle-validate.yml"
    text = path.read_text()
    assert "{{" not in text.replace("${{ ", "") and "}}}}" not in text
    assert "\non:\n  pull_request:\n" in text
    assert not re.search(r"^\s*pull_request_target\b", text, re.M)
    assert "permissions: {}" in text
    jobs = _jobs(text)
    assert list(jobs) == ["entitle", "validate", "report"]
    # entitle: the CI token, nothing from the repo, not for forks.
    assert "secrets.BRINDLE_PRO_TOKEN" in jobs["entitle"] and "checkout" not in jobs["entitle"]
    assert "head.repo.full_name == github.repository" in jobs["entitle"]
    # validate: the PR's code with a read-only token.
    v = jobs["validate"]
    assert "needs: entitle" in v and "actions/checkout" in v and "persist-credentials: false" in v
    assert ": write" not in v and "BRINDLE_PRO_TOKEN" not in v
    assert "brindle ci validate --pr" in v and '--mode "$BRINDLE_VALIDATE_MODE"' in v
    # report: can write checks and comments, no checkout, no repo code.
    r = jobs["report"]
    assert "checks: write" in r and "pull-requests: write" in r and "contents" not in r
    assert "actions/checkout" not in r and "secrets." not in r and "ANTHROPIC" not in r
    assert "needs: [entitle, validate]" in r and "always()" in r
    assert "brindle ci validate-report" in r and "--skip-fork" in r
    assert "github.event.pull_request.head.sha" in r
    # Event values reach the shell only through env, never ${{ }} in run:.
    for line in text.splitlines():
        if line.strip().startswith("run:"):
            assert "${{" not in line, line
    # Blocking mode is one commented-out line away, and explained.
    assert "\n  BRINDLE_VALIDATE_MODE: advisory\n  # BRINDLE_VALIDATE_MODE: blocking" in text
    prose = text.replace("\n# ", " ")
    assert "--mode blocking" in prose and '"Require status checks to pass"' in prose
    assert 'pick "brindle validate"' in prose and "fork's pull_request no secrets" in prose
    assert re.search(r"uv tool install brindle(==[0-9.]+\S*)?\n", text)

    with pytest.raises(ci.CIError, match="--force"):
        cvr.init(repo)
    cvr.init(repo, force=True)


def test_cli_init_validate(repo, monkeypatch):
    monkeypatch.chdir(repo)
    res = CliRunner().invoke(app, ["ci", "init", "--validate"])
    assert res.exit_code == 0, res.output
    assert (repo / ".github" / "workflows" / "brindle-validate.yml").exists()
    assert not (repo / ".github" / "workflows" / "brindle.yml").exists()
    assert "brindle validate" in res.output
    res = CliRunner().invoke(app, ["ci", "init", "--validate"])
    assert res.exit_code == 1 and "--force" in res.output

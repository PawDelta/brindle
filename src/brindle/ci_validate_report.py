"""``brindle ci validate-report``: post a ``brindle ci validate`` verdict on a pull request.

This is the last of the three jobs in ``.github/workflows/brindle-validate.yml``
(``brindle ci init --validate``), mirroring the split in ``brindle.ci``:

1. ``entitle`` holds ``BRINDLE_PRO_TOKEN`` and runs nothing from the repo.
2. ``validate`` checks out the pull request and runs ``brindle ci validate``,
   which runs the PR's own code (its checks) and writes a verdict JSON. It has
   no token that can write to GitHub.
3. ``report`` (this module) holds ``checks: write`` and ``pull-requests: write``,
   has no checkout and never runs repo code. It creates the "brindle validate"
   check run on the PR's head commit and posts or updates one PR comment.

The verdict was written on a machine where the PR's code ran, so nothing in it
is trusted: it is size-capped, validated against the schema, and everything it
says is escaped before it reaches markdown (HTML, mentions, links, code fences).
It never reaches a shell: ``gh`` is run with an argument list and the payload
goes in on stdin. The pull request, the commit and the mode come from the
workflow (the event), not from the verdict: a verdict for another commit is
reported as an error, and the verdict can't turn blocking mode into advisory.

What this can't fix: code that runs in the ``validate`` job can write any
verdict it likes, a forged "pass" included. A required "brindle validate"
check guards against honest mistakes, not against a hostile pull request;
review those as you would without brindle.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

CHECK_NAME = "brindle validate"
MARKER = "<!-- brindle-validate -->"
DEFAULT_BOT = "github-actions[bot]"
VALIDATE_WORKFLOW_PATH = Path(".github") / "workflows" / "brindle-validate.yml"

STATUSES = ("pass", "fail", "neutral", "error")
MODES = ("advisory", "blocking")
RESULTS = ("met", "unmet", "unknown")
SEVERITIES = ("blocking", "note")

# Caps on what the verdict may hold. Longer strings are cut, longer lists dropped.
MAX_FILE_BYTES = 1_000_000
MAX_SUMMARY = 4000
MAX_COMMAND = 300
MAX_EXCERPT = 4000
MAX_TEXT = 1000
MAX_EVIDENCE = 1000
MAX_PATH = 300
MAX_MODEL = 100
MAX_ITEMS = 50
MAX_MODELS = 10
# GitHub's limit is 65536 for a comment and for a check run's summary.
MAX_BODY = 60_000

_SLUG = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class ReportError(RuntimeError):
    """The report couldn't be posted."""


class GhError(ReportError):
    """A gh call failed (no network, no permission, ...)."""


class VerdictError(ValueError):
    """The verdict file is missing, too big, or not the expected shape."""


# -- the verdict ------------------------------------------------------------------


def _int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str(obj: dict, key: str, cap: int, where: str) -> str:
    v = obj.get(key)
    if not isinstance(v, str):
        raise VerdictError(f"{where}{key} must be a string")
    return v[:cap]


def _choice(obj: dict, key: str, choices: tuple[str, ...], where: str) -> str:
    v = obj.get(key)
    if v not in choices:
        raise VerdictError(f"{where}{key} must be one of {', '.join(choices)}")
    return v


def _list(obj: dict, key: str, where: str) -> list:
    v = obj.get(key)
    if not isinstance(v, list):
        raise VerdictError(f"{where}{key} must be a list")
    return v


def _models(v, where: str) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(m, str) for m in v):
        raise VerdictError(f"{where}models must be a list of strings")
    return [m[:MAX_MODEL] for m in v[:MAX_MODELS]]


def _dict(v, where: str) -> dict:
    if not isinstance(v, dict):
        raise VerdictError(f"{where} must be an object")
    return v


def validate_verdict(data) -> dict:
    """The verdict, checked against the schema and capped; raises VerdictError.
    Only the known keys are kept."""
    d = _dict(data, "the verdict")
    if d.get("version") != 1 or isinstance(d.get("version"), bool):
        raise VerdictError("version must be 1")
    if not _int(d.get("pr")) or d["pr"] <= 0:
        raise VerdictError("pr must be a positive integer")
    sha = d.get("head_sha")
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise VerdictError("head_sha must be a full hex commit id")
    tokens = d.get("tokens")
    if "tokens" not in d or not (tokens is None or (_int(tokens) and tokens >= 0)):
        raise VerdictError("tokens must be a non-negative integer or null")
    checks = []
    for i, c in enumerate(_list(d, "checks", "")[:MAX_ITEMS]):
        w = f"checks[{i}]."
        c = _dict(c, f"checks[{i}]")
        if not isinstance(c.get("passed"), bool):
            raise VerdictError(f"{w}passed must be true or false")
        checks.append({"command": _str(c, "command", MAX_COMMAND, w), "passed": c["passed"],
                       "excerpt": _str(c, "excerpt", MAX_EXCERPT, w)})
    criteria = []
    for i, c in enumerate(_list(d, "criteria", "")[:MAX_ITEMS]):
        w = f"criteria[{i}]."
        c = _dict(c, f"criteria[{i}]")
        criteria.append({"text": _str(c, "text", MAX_TEXT, w),
                         "result": _choice(c, "result", RESULTS, w),
                         "evidence": _str(c, "evidence", MAX_EVIDENCE, w)})
    findings = []
    for i, f in enumerate(_list(d, "findings", "")[:MAX_ITEMS]):
        w = f"findings[{i}]."
        f = _dict(f, f"findings[{i}]")
        line = f.get("line")
        if "line" not in f or not (line is None or (_int(line) and line >= 0)):
            raise VerdictError(f"{w}line must be a non-negative integer or null")
        findings.append({"severity": _choice(f, "severity", SEVERITIES, w),
                         "file": _str(f, "file", MAX_PATH, w), "line": line,
                         "text": _str(f, "text", MAX_TEXT, w),
                         "models": _models(f.get("models"), w)})
    return {"version": 1, "pr": d["pr"], "head_sha": sha,
            "mode": _choice(d, "mode", MODES, ""), "status": _choice(d, "status", STATUSES, ""),
            "summary": _str(d, "summary", MAX_SUMMARY, ""), "checks": checks,
            "criteria": criteria, "findings": findings,
            "models": _models(d.get("models"), ""), "tokens": tokens}


def load_verdict(path: str | Path) -> dict:
    """Read and validate the verdict file; raises VerdictError."""
    p = Path(path)
    try:
        with p.open("rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
    except OSError as e:
        raise VerdictError(f"cannot read the verdict: {e.strerror}") from e
    if len(raw) > MAX_FILE_BYTES:
        raise VerdictError(f"the verdict is larger than {MAX_FILE_BYTES} bytes")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as e:
        raise VerdictError("the verdict is not JSON") from e
    return validate_verdict(data)


def error_verdict(pr: int, sha: str, mode: str, why: str) -> dict:
    """What is reported when there is no usable verdict. ``why`` is our own text."""
    return {"version": 1, "pr": pr, "head_sha": sha, "mode": mode, "status": "error",
            "summary": why, "checks": [], "criteria": [], "findings": [], "models": [],
            "tokens": None}


def conclusion(status: str, mode: str) -> str:
    """The check run's conclusion. Advisory mode never fails the check."""
    if status == "pass":
        return "success"
    if status in ("fail", "error") and mode == "blocking":
        return "failure"
    return "neutral"


# -- escaping ---------------------------------------------------------------------

# Bidi overrides and other invisible controls (trojan-source), and C0/C1 controls.
_INVISIBLE = re.compile("[\u0000-\u0008\u000b-\u001f\u007f-\u009f​-‏"
                        "‪-‮⁠-⁩﻿]")
_MD_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+\-.!|~:=^$])")
_ZWSP = "​"


def _clean(s: str) -> str:
    return _INVISIBLE.sub("", s.replace("\r\n", "\n").replace("\r", "\n"))


def md_inline(s: str) -> str:
    """Untrusted text as plain inline markdown: one line, every markdown
    character escaped, HTML escaped, and no @mention, autolink or #reference."""
    s = " ".join(_clean(s).split())
    s = _MD_SPECIAL.sub(r"\\\1", s)
    s = html.escape(s, quote=False)
    # GitHub links bare URLs and www. names and pings @names even when escaped:
    # break them up with a zero-width space.
    s = s.replace("@", "@" + _ZWSP)
    s = re.sub(r"(?i)(www)\\\.", lambda m: m.group(1) + _ZWSP + r"\.", s)
    s = s.replace("\\#", "\\#" + _ZWSP)    # no owner/repo#123 cross-references
    return s.replace(r"\:", _ZWSP + r"\:")


def md_code(s: str) -> str:
    """Untrusted text as an inline code span that can't be closed early."""
    s = " ".join(_clean(s).split()) or " "
    ticks = "`" * (max((len(r) for r in re.findall(r"`+", s)), default=0) + 1)
    return f"{ticks} {s} {ticks}"


def md_block(s: str) -> str:
    """Untrusted text as a fenced code block that can't be closed early."""
    s = _clean(s).replace("\t", "    ").rstrip("\n")
    fence = "`" * max(3, max((len(r) for r in re.findall(r"`+", s)), default=0) + 1)
    return f"{fence}text\n{s}\n{fence}"


# -- rendering --------------------------------------------------------------------

_HEAD = {"pass": "✅ pass", "fail": "❌ fail", "neutral": "⚪ neutral", "error": "⚠️ error"}
_RESULT = {"met": "✅", "unmet": "❌", "unknown": "❔"}
_SEVERITY = {"blocking": "🛑 blocking", "note": "📝 note"}


def title(v: dict, mode: str) -> str:
    """The check run's one-line title, from counts only (no verdict text)."""
    parts = [v["status"]]
    if v["checks"]:
        parts.append(f"{sum(c['passed'] for c in v['checks'])} of {len(v['checks'])} checks passed")
    blocking = sum(f["severity"] == "blocking" for f in v["findings"])
    if blocking:
        parts.append(f"{blocking} blocking finding{'s' * (blocking != 1)}")
    return f"{CHECK_NAME} ({mode}): " + ", ".join(parts)


class _Budget:
    """Collects markdown parts until the body would be too long."""

    def __init__(self, limit: int):
        self.parts: list[str] = []
        self.limit = limit
        self.size = 0
        self.dropped = 0

    def add(self, text: str) -> bool:
        if self.dropped or self.size + len(text) + 2 > self.limit:
            self.dropped += 1
            return False
        self.parts.append(text)
        self.size += len(text) + 2
        return True

    def text(self) -> str:
        out = "\n\n".join(self.parts)
        if self.dropped:
            out += f"\n\n_{self.dropped} more item(s) not shown: the report is too long._"
        return out


def render(v: dict, mode: str, limit: int = MAX_BODY) -> str:
    """The markdown for the check run summary and the PR comment (without the
    marker). Every string from the verdict goes through md_inline, md_code or
    md_block."""
    b = _Budget(limit - 200)
    head = [f"### {CHECK_NAME}: {_HEAD[v['status']]}", "",
            f"**Mode:** {mode} · **Conclusion:** {conclusion(v['status'], mode)} · "
            f"**Commit:** `{v['head_sha'][:12]}`"]
    if v["models"]:
        head[-1] += " · **Models:** " + ", ".join(md_inline(m) for m in v["models"])
    if v["tokens"] is not None:
        head[-1] += f" · **Tokens:** {v['tokens']:,}"
    if v["mode"] != mode:
        head.append(f"\n_The verdict says {v['mode']} mode; this workflow runs {mode} mode, "
                    "which is what counts._")
    b.add("\n".join(head))
    if v["summary"].strip():
        b.add("\n".join(md_inline(line) for line in _clean(v["summary"]).split("\n")
                        if line.strip()).replace("\n", "  \n"))
    if v["checks"]:
        b.add("#### Checks")
        b.add("\n".join(f"- {'✅' if c['passed'] else '❌'} {md_code(c['command'])}"
                        for c in v["checks"]))
        for i, c in enumerate(v["checks"], 1):
            if c["excerpt"].strip():
                b.add(f"<details><summary>Output of check {i} "
                      f"({'passed' if c['passed'] else 'failed'})</summary>\n\n"
                      f"{md_block(c['excerpt'])}\n\n</details>")
    if v["criteria"]:
        b.add("#### Acceptance criteria")
        for c in v["criteria"]:
            line = f"- {_RESULT[c['result']]} {c['result']}: {md_inline(c['text'])}"
            if c["evidence"].strip():
                line += f"  \n  {md_inline(c['evidence'])}"
            b.add(line)
    if v["findings"]:
        b.add("#### Findings")
        for f in v["findings"]:
            where = f["file"] + (f":{f['line']}" if f["line"] is not None else "")
            line = f"- {_SEVERITY[f['severity']]}"
            if where:
                line += f" {md_code(where)}"
            line += f": {md_inline(f['text'])}"
            if f["models"]:
                line += " _(" + ", ".join(md_inline(m) for m in f["models"]) + ")_"
            b.add(line)
    return b.text() + "\n"


def comment_body(v: dict, mode: str) -> str:
    return f"{MARKER}\n{render(v, mode)}"


# -- GitHub -----------------------------------------------------------------------


def _gh(args: list[str], payload: dict | None = None, env: dict | None = None) -> str:
    """Run ``gh`` with an argument list (no shell), the payload on stdin, from
    an empty directory (no repo config to read). Returns stdout."""
    with tempfile.TemporaryDirectory(prefix="brindle-report-") as tmp:
        try:
            proc = subprocess.run(["gh", *args], cwd=tmp, capture_output=True, text=True,
                                  input=json.dumps(payload) if payload is not None else None,
                                  env=env, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GhError(f"gh {' '.join(args[:3])}: {e}") from e
    if proc.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} failed: "
                          f"{(proc.stderr.strip() or proc.stdout.strip())[:500]}")
    return proc.stdout


def create_check(repo: str, sha: str, v: dict, mode: str, gh=_gh) -> None:
    payload = {"name": CHECK_NAME, "head_sha": sha, "status": "completed",
               "conclusion": conclusion(v["status"], mode),
               "output": {"title": title(v, mode), "summary": render(v, mode)}}
    gh(["api", "--method", "POST", f"repos/{repo}/check-runs", "--input", "-"], payload)


def find_comment(repo: str, pr: int, bot: str = DEFAULT_BOT, gh=_gh) -> int | None:
    """The id of our earlier comment: written by ``bot`` and starting with the
    marker (anyone can write the marker; only the bot's comment counts)."""
    out = gh(["api", "--paginate", f"repos/{repo}/issues/{pr}/comments", "--jq",
              f'.[] | [.id, .user.login, ((.body // "") | startswith("{MARKER}"))] | @json'])
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        if (isinstance(c, list) and len(c) == 3 and _int(c[0]) and c[1] == bot
                and c[2] is True):
            return c[0]
    return None


def upsert_comment(repo: str, pr: int, body: str, bot: str = DEFAULT_BOT, gh=_gh) -> str:
    """Update our one comment on the pull request, or post it. Returns
    "updated" or "created"."""
    cid = find_comment(repo, pr, bot, gh)
    if cid is not None:
        gh(["api", "--method", "PATCH", f"repos/{repo}/issues/comments/{cid}", "--input", "-"],
           {"body": body})
        return "updated"
    gh(["api", "--method", "POST", f"repos/{repo}/issues/{pr}/comments", "--input", "-"],
       {"body": body})
    return "created"


# -- the command ------------------------------------------------------------------


def _check_args(repo: str, pr: int, sha: str, mode: str) -> None:
    if not _SLUG.fullmatch(repo or ""):
        raise ReportError("name the repository with --repo owner/name")
    if not _int(pr) or pr <= 0:
        raise ReportError("--pr must be a positive integer")
    if not _SHA.fullmatch(sha or ""):
        raise ReportError("--sha must be the pull request's full head commit id")
    if mode not in MODES:
        raise ReportError(f"--mode must be one of {', '.join(MODES)}")


def report(verdict_path: str | Path | None, repo: str, pr: int, sha: str, mode: str = "advisory",
           *, skipped: str | None = None, bot: str = DEFAULT_BOT, gh=None) -> dict:
    """Post the verdict as the "brindle validate" check run on ``sha`` and as
    the one brindle comment on ``pr``. ``repo``, ``pr``, ``sha`` and ``mode``
    come from the workflow; a verdict that names another PR or commit, or
    can't be read, is reported as an error. With ``skipped``, nothing is read
    and a neutral check says why (``skipped`` is our own text)."""
    _check_args(repo, pr, sha, mode)
    gh = gh or _gh
    if skipped is not None:
        v = {**error_verdict(pr, sha, mode, skipped), "status": "neutral"}
    else:
        try:
            v = load_verdict(verdict_path) if verdict_path else None
            if v is None:
                raise VerdictError("no verdict: the validate job didn't produce one")
            if v["pr"] != pr or v["head_sha"] != sha:
                raise VerdictError(f"the verdict is for another pull request or commit, not "
                                   f"#{pr} at {sha[:12]}")
        except VerdictError as e:
            v = error_verdict(pr, sha, mode, f"brindle validate produced no usable verdict: {e}")
    create_check(repo, sha, v, mode, gh)
    action = upsert_comment(repo, pr, comment_body(v, mode), bot, gh)
    return {"status": v["status"], "conclusion": conclusion(v["status"], mode), "comment": action}


def report_cli(verdict: str | None, repo: str | None, pr: int, sha: str, mode: str,
               skipped: str | None = None, bot: str = DEFAULT_BOT, echo=print,
               environ=None) -> int:
    """``brindle ci validate-report``: 0 once the check and comment are posted
    (whatever the verdict; the check carries it), 1 when posting failed. A
    skipped run (``--skip``) whose token can't write, as on a fork's pull
    request, notes that and still exits 0."""
    environ = os.environ if environ is None else environ
    repo = (repo or environ.get("GITHUB_REPOSITORY") or "").strip()
    try:
        out = report(verdict, repo, pr, sha, mode, skipped=skipped, bot=bot)
    except ReportError as e:
        if skipped is not None and isinstance(e, GhError):
            echo(f"::notice title={CHECK_NAME}::skipped: {skipped} (no check posted: "
                 "this run's token can't write)")
            return 0
        echo(f"brindle ci validate-report: {e}")
        return 1
    echo(f"{CHECK_NAME}: {out['status']} -> {out['conclusion']}; comment {out['comment']}")
    return 0


# -- the workflow -----------------------------------------------------------------

FORK_SKIP = ("this pull request comes from a fork, and GitHub gives workflows on a fork's "
             "pull_request no secrets, so brindle can't validate it. A maintainer can review "
             "it, or push its branch to this repository to validate it.")

WORKFLOW = """\
# Written by `brindle ci init --validate`. brindle checks every pull request
# against its own checks and its stated goal, and reports a "brindle validate"
# check and one comment: https://pawdelta.com/brindle/docs/commands
#
# Secrets: BRINDLE_PRO_TOKEN (an org CI token, cpc_..., from
# `brindle account org ci-token create`) and ANTHROPIC_API_KEY (for Claude Code).
#
# Three jobs, as in brindle.yml. `entitle` alone holds the CI token and runs
# nothing from the repo; `validate` runs the pull request's code with a
# read-only token and hands over a verdict file; `report` can write checks and
# comments, and never checks out or runs anything from the repo. It treats
# the verdict as untrusted data.
#
# The trigger is pull_request, never pull_request_target: a fork's code never
# runs with this repository's secrets. GitHub gives a fork's pull_request no
# secrets, so for forks brindle skips and says so with a neutral check.
#
# Blocking mode: by default the check is advisory (it passes or is neutral,
# never red). To block merges, switch BRINDLE_VALIDATE_MODE below to blocking
# (that runs `brindle ci validate --mode blocking` and reports a failed check
# on a failing verdict), then in Settings > Branches (or Rules > Rulesets) add
# a branch protection rule for your default branch, turn on "Require status
# checks to pass" and pick "brindle validate". Neutral counts as passing, so
# fork pull requests aren't blocked: review those yourself. The validate job
# runs the pull request's code, which could write its own verdict: a
# required check catches mistakes, not a hostile pull request.
name: brindle validate

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]

permissions: {{}}

env:
  BRINDLE_VALIDATE_MODE: advisory
  # BRINDLE_VALIDATE_MODE: blocking   # --mode blocking: use instead of the line above

concurrency:
  group: brindle-validate-${{{{ github.event.pull_request.number }}}}
  cancel-in-progress: true

jobs:
  # The CI token lives only in this job, which checks out and runs nothing
  # from the repo. Skipped for forks, which get no secrets.
  entitle:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions: {{}}
    steps:
      - uses: astral-sh/setup-uv@v10.2.0
      - name: Install brindle
        run: uv tool install {package}
      - name: Entitlement
        env:
          BRINDLE_PRO_TOKEN: ${{{{ secrets.BRINDLE_PRO_TOKEN }}}}
        run: brindle ci entitle --out "$RUNNER_TEMP/brindle-entitlement/entitlement.jwt"
      - uses: actions/upload-artifact@v4
        with:
          name: brindle-entitlement
          path: ${{{{ runner.temp }}}}/brindle-entitlement/
          if-no-files-found: error
          retention-days: 1

  validate:
    needs: entitle
    runs-on: ubuntu-latest
    timeout-minutes: 60
    permissions:
      contents: read
      pull-requests: read
    steps:
      - uses: actions/checkout@v7
        with:
          ref: ${{{{ github.event.pull_request.head.sha }}}}
          fetch-depth: 0
          # The pull request's code runs here: don't leave a token in .git/config.
          persist-credentials: false
      - uses: astral-sh/setup-uv@v10.2.0
      - name: Install brindle
        run: uv tool install {package}
      - name: Install the agent CLI
        run: npm install -g @anthropic-ai/claude-code
      - uses: actions/download-artifact@v4
        with:
          name: brindle-entitlement
          path: ${{{{ runner.temp }}}}/brindle-entitlement
      # No token that can write here. brindle reads the entitlement and
      # deletes it before running anything from the pull request.
      - name: Validate
        env:
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
          GH_TOKEN: ${{{{ github.token }}}}
          PR: ${{{{ github.event.pull_request.number }}}}
        run: brindle ci validate --pr "$PR" --mode "$BRINDLE_VALIDATE_MODE" --entitlement "$RUNNER_TEMP/brindle-entitlement/entitlement.jwt" --out "$RUNNER_TEMP/brindle-verdict/verdict.json"
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: brindle-verdict
          path: ${{{{ runner.temp }}}}/brindle-verdict/
          if-no-files-found: warn
          retention-days: 1

  report:
    needs: [entitle, validate]
    if: always() && !cancelled()
    runs-on: ubuntu-latest
    timeout-minutes: 5
    permissions:
      checks: write
      pull-requests: write
    steps:
      # No checkout: nothing from the repo runs in the job that can write.
      - uses: astral-sh/setup-uv@v10.2.0
      - name: Install brindle
        run: uv tool install {package}
      - uses: actions/download-artifact@v4
        if: needs.validate.result != 'skipped'
        continue-on-error: true
        with:
          name: brindle-verdict
          path: ${{{{ runner.temp }}}}/brindle-verdict
      # The pull request, commit and mode come from the event and this file,
      # not from the verdict. A missing verdict is reported as an error.
      - name: Report
        if: github.event.pull_request.head.repo.full_name == github.repository
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          PR: ${{{{ github.event.pull_request.number }}}}
          SHA: ${{{{ github.event.pull_request.head.sha }}}}
        run: brindle ci validate-report "$RUNNER_TEMP/brindle-verdict/verdict.json" --repo "$GITHUB_REPOSITORY" --pr "$PR" --sha "$SHA" --mode "$BRINDLE_VALIDATE_MODE"
      - name: Skip (fork)
        if: github.event.pull_request.head.repo.full_name != github.repository
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          PR: ${{{{ github.event.pull_request.number }}}}
          SHA: ${{{{ github.event.pull_request.head.sha }}}}
        run: brindle ci validate-report --skip-fork --repo "$GITHUB_REPOSITORY" --pr "$PR" --sha "$SHA" --mode "$BRINDLE_VALIDATE_MODE"
"""


def workflow_text() -> str:
    """The validate workflow, with brindle pinned like ``brindle.yml``'s: the
    report job holds a write token."""
    from brindle import __version__

    pinned = re.fullmatch(r"[0-9]+(\.[0-9]+)*([ab]|rc|\.post|\.dev)?[0-9]*", __version__ or "")
    return WORKFLOW.format(package=f"brindle=={__version__}" if pinned else "brindle")


def init(repo_root: str | Path, force: bool = False) -> Path:
    """Write ``.github/workflows/brindle-validate.yml``; refuses to overwrite without ``force``."""
    from brindle.ci import CIError

    path = Path(repo_root) / VALIDATE_WORKFLOW_PATH
    if path.exists() and not force:
        raise CIError(f"{path} exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow_text(), encoding="utf-8")
    return path

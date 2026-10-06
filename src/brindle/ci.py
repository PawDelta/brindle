"""Brindle-CI: brindle with nobody at a terminal (brindle Team).

``brindle ci run`` turns a goal (typed, read from a file, or a GitHub issue)
into a pull request: it cuts a fresh branch, starts a supervisor with
autopilot on in a detached tmux session, gives it the goal, and polls the
autopilot state in the DB until every milestone is verified, the supervisor
needs a person (``need_user``: the run fails with the question), it stalls,
or the time runs out. Whatever happens, the session and its workers are
stopped at the end, and a JSON summary is appended to ``$GITHUB_STEP_SUMMARY``
when that is set (GitHub Actions).

Who is trusted with what. The agents run the repo's own code (its tests, its
scripts, whatever an issue talks them into) as the same OS user as brindle. So
assume they can read anything this process can: its environment, its files,
the git and gh configuration, the programs on its PATH. Taking the tokens out
of the environment before agents start (``withhold_secrets``) and pushing from
a clean bare repo (``_push``) make theft harder, not impossible. The only real
protection is that a secret is not on the machine while agents run. So the
work is split into three steps, and the dangerous token is in the last one:

1. ``brindle ci entitle --out FILE`` exchanges ``BRINDLE_PRO_TOKEN`` for the
   signed, short-lived entitlement and writes it to a file, on another
   machine than the agents (in the workflow, its own job: on hosted runners
   agents have sudo and the runner holds the secrets of the job they run in).
   ``brindle ci run`` reads the file and deletes it before any agent starts.
2. ``brindle ci run --entitlement FILE --bundle PATH --outcome FILE`` does the
   work. It needs no CI token and no token that can write to GitHub. When
   the goal is verified it writes a git bundle of the new commits to PATH,
   and PATH.json with the branch, base, title and body of the pull request.
   When it ends any other way (a question, a stall, the timeout) but
   commits were made, it writes the same bundle marked ``partial``. Nothing
   is pushed. Whatever happened, ``--outcome FILE`` gets a JSON record of it
   (status, note, milestones), even when the run couldn't start.
3. ``brindle ci publish PATH`` runs somewhere no agent ever ran (in the
   workflow: a second job, on a fresh machine, with no checkout). It holds
   the token that can push. The pull request targets ``--base`` or the
   repository's default branch, never what the bundle names. It treats the
   bundle as data: verifies it,
   fetches the one ``brindle/ci-`` branch into a fresh bare repo, pushes that
   branch and opens the pull request with ``gh``: a draft one, listing which
   milestones passed, for a ``partial`` bundle. It never checks out or
   runs repo code, hooks or agents.
4. ``brindle ci report FILE --issue N`` runs in a job of its own with a token
   that can only comment on issues, no checkout, and nothing from the repo.
   It comments the outcome on the issue: the pull request, the supervisor's
   question, the stall, the timeout or the error, and the milestone table.
   The outcome file was written where agents ran, so it is data: only the
   expected fields are read, every text is bounded and rendered as code so
   nothing in it becomes markdown, a mention or a link. A question is
   answered in a comment on the issue; when the label is added again,
   ``goal_from_issue`` hands the comments after brindle's question to the
   next run as context.

``brindle ci run`` without ``--bundle`` still pushes and opens the pull request
itself, as before; use it only where the repo's code is trusted.

``brindle ci init`` writes the four-job workflow that does this when an issue
gets a label.

Entitlement: ``ci`` must be in the brindle Pro entitlement. In CI there is no
keychain and no browser, so ``BRINDLE_PRO_TOKEN`` holds an org CI token
(``cpc_...``, from ``brindle account org ci-token create``). It is presented to
``POST /ci/entitlement`` and the entitlement that comes back is verified
(signature, issuer, expiry), whether it was fetched just now or read from the
file ``brindle ci entitle`` wrote. CI tokens don't rotate, so the same secret
works on every run until an admin revokes it or the org's plan lapses.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from brindle import agents, ci_budget, git, tmux, workspaces
from brindle.db import DB, Agent, Workspace

CI_FEATURE = "ci"
TOKEN_ENV = "BRINDLE_PRO_TOKEN"
CI_TOKEN_PREFIX = "cpc_"
BRANCH_PREFIX = "brindle/ci-"
DEFAULT_TIMEOUT_MIN = 60
POLL_SECONDS = 10.0
WORKFLOW_PATH = Path(".github") / "workflows" / "brindle.yml"
MAX_SLUG = 40


class CIError(RuntimeError):
    """The run couldn't start, or a step the run depends on failed."""


# -- the goal ---------------------------------------------------------------------


@dataclass
class Goal:
    title: str
    detail: str | None = None
    issue: int | None = None
    source: str = "goal"           # goal | file | issue
    context: str | None = None     # an issue's comments since brindle last reported

    @property
    def slug(self) -> str:
        return git.slug(self.title)[:MAX_SLUG].strip("-") or "goal"

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}{self.issue if self.issue is not None else self.slug}"

    def plan(self):
        """The milestones when ``detail`` is goals.md-shaped, else None."""
        from brindle import autopilot as pilot

        plan = pilot.parse_goals(self.detail) if self.detail else None
        return plan if plan and plan.milestones else None


def _gh_json(args: list[str], cwd: str) -> dict:
    proc = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CIError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise CIError(f"gh {' '.join(args[:2])} returned no JSON") from e
    if not isinstance(data, dict):
        raise CIError(f"gh {' '.join(args[:2])} returned an unexpected shape")
    return data


REPORT_MARKER = "<!-- brindle-ci:"


def goal_from_issue(number: int, cwd: str) -> Goal:
    """The issue's title and body, through ``gh issue view``, plus the
    comments made since brindle's last report on the issue (an answer to its
    question, a steer) as ``context``."""
    data = _gh_json(["issue", "view", str(number), "--json", "number,title,body,comments"], cwd)
    title = str(data.get("title") or "").strip()
    if not title:
        raise CIError(f"issue #{number} has no title")
    body = str(data.get("body") or "").strip() or None
    return Goal(title, body, issue=number, source="issue", context=issue_context(data.get("comments")))


TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")


def _login(comment: dict) -> str:
    author = comment.get("author") if isinstance(comment.get("author"), dict) else {}
    return str(author.get("login") or "").strip()


def _trusted(comment: dict) -> bool:
    """A comment by someone with a say over the repo: its owner, a member
    of the org or a collaborator. Anyone can comment on a public issue, and
    these comments steer an agent, so the rest are left out."""
    return comment.get("authorAssociation") in TRUSTED_ASSOCIATIONS


def _is_report(comment: dict) -> bool:
    """brindle's own report: the marker, posted by the workflow's bot (or by
    a trusted person running `brindle ci report` themselves)."""
    if REPORT_MARKER not in str(comment.get("body") or ""):
        return False
    login = _login(comment)
    return login == "github-actions" or login.endswith("[bot]") or _trusted(comment)


def issue_context(comments) -> str | None:
    """The comments after brindle's last report (the one carrying
    ``REPORT_MARKER``), each with its author, as text for the supervisor;
    None when brindle never reported or nobody answered. Only comments by
    the repo's owner, org members and collaborators count: they steer an
    unattended agent."""
    if not isinstance(comments, list):
        return None
    comments = [c for c in comments if isinstance(c, dict)]
    since = None
    for i, c in enumerate(comments):
        if _is_report(c):
            since = i
    if since is None:
        return None
    parts = []
    for c in comments[since + 1:]:
        text = str(c.get("body") or "").strip()
        if not text or REPORT_MARKER in text or not _trusted(c):
            continue
        parts.append(f"{_login(c) or 'someone'} wrote:\n{text}")
    return "\n\n".join(parts) or None


def goal_from_text(text: str) -> Goal:
    """A typed goal: its first line is the title; a goals.md-shaped text
    (``# Goal`` with ``## Milestone`` sections) keeps its shape in ``detail``
    so the supervisor gets the milestones."""
    from brindle import autopilot as pilot

    text = text.strip()
    if not text:
        raise CIError("the goal is empty")
    plan = pilot.parse_goals(text)
    if plan:
        return Goal(plan.goal, text, source="goal")
    title, _, rest = text.partition("\n")
    return Goal(title.strip(), rest.strip() or None, source="goal")


def goal_from_file(path: str | Path) -> Goal:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise CIError(f"cannot read goal file {path}: {e.strerror}") from e
    goal = goal_from_text(text)
    goal.source = "file"
    return goal


# -- entitlement ------------------------------------------------------------------


def _exchange(token: str, client=None, now: float | None = None):
    """(the signed entitlement, the verified Entitlement) for an org CI token
    (``cpc_...``). Refresh tokens are refused: they rotate, and the backend
    treats a reused one as stolen, so a CI secret would work once."""
    from brindle.pro import auth, license

    if not token.startswith(CI_TOKEN_PREFIX):
        raise auth.AuthError(f"{TOKEN_ENV} is not a CI token", code="not_a_ci_token")
    client = client or auth.Client()
    status, body = client.call("POST", "/ci/entitlement", None, token=token)
    if status != 200:
        raise auth._error(status, body)
    tok = body.get("entitlement")
    if not isinstance(tok, str):
        raise auth.AuthError("backend returned no entitlement", code="bad_response")
    return tok, license.verify(tok, issuer=client.base,
                               now=time.time() if now is None else now, grace=0)


def entitlement_from_token(token: str, client=None, now: float | None = None):
    """Exchange an org CI token for a verified entitlement, without storing anything."""
    return _exchange(token, client, now)[1]


def _needs_ci(ent):
    if CI_FEATURE not in ent.features:
        raise CIError(f"brindle ci needs brindle Team: your plan ({ent.plan}) does not include "
                      f"{CI_FEATURE!r}. See `brindle account`; plans: https://pawdelta.com/brindle#pricing")
    return ent


def entitlement_from_file(path: str | Path, client=None, now: float | None = None):
    """The entitlement ``brindle ci entitle`` wrote, verified like a fresh one
    (signature, issuer, expiry; no offline grace). No token, no network."""
    from brindle.pro import auth, license

    try:
        tok = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise CIError(f"brindle ci: cannot read the entitlement file {path}: {e.strerror}") from e
    try:
        ent = license.verify(tok, issuer=(client or auth.Client()).base,
                             now=time.time() if now is None else now, grace=0)
    except license.LicenseError as e:
        raise CIError(f"brindle ci: the entitlement in {path} was refused: {e}. Write a fresh "
                      "one with `brindle ci entitle --out FILE`.") from e
    return _needs_ci(ent)


def entitle(out: str | Path, client=None) -> Path:
    """``brindle ci entitle``: exchange ``BRINDLE_PRO_TOKEN`` for the signed
    entitlement and write it to ``out`` (0600), for a later ``brindle ci run
    --entitlement``. The CI token is only ever in this short process."""
    from brindle.pro import auth, license

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise CIError(f"brindle ci entitle: set {TOKEN_ENV} to a CI token from "
                      "`brindle account org ci-token create`")
    try:
        tok, ent = _exchange(token, client)
    except auth.AirGapped as e:
        raise CIError(f"brindle ci entitle: {e}; a CI token can't be exchanged offline") from e
    except auth.AuthError as e:
        raise CIError(f"brindle ci entitle: {TOKEN_ENV} was refused ({e.code}); set it to a CI "
                      "token from `brindle account org ci-token create`") from e
    except license.LicenseError as e:
        raise CIError(f"brindle ci entitle: {e}") from e
    _needs_ci(ent)
    out = Path(out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)     # a fresh file, so the mode below applies
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
    except OSError as e:
        raise CIError(f"brindle ci entitle: cannot write {out}: {e.strerror}") from e
    return out


def require_ci(client=None, entitlement_file: str | Path | None = None):
    """The entitlement, which must include ``ci``: from ``entitlement_file``
    when given (written earlier by ``brindle ci entitle``), else from
    ``BRINDLE_PRO_TOKEN`` when set (memory only), else the stored brindle Pro
    credentials."""
    from brindle.pro import auth, license

    if entitlement_file is not None:
        ent = entitlement_from_file(entitlement_file, client)
        # Read once, before any agent starts; gone before they could look.
        try:
            Path(entitlement_file).unlink()
        except OSError:
            pass
        return ent
    token = os.environ.get(TOKEN_ENV, "").strip()
    try:
        if token:
            ent = entitlement_from_token(token, client)
        else:
            ent = license.current(client=client) if client is not None else license.current()
    except auth.AirGapped as e:
        raise CIError(f"brindle ci: {e}; a CI token can't be exchanged offline, so in air-gap "
                      f"mode unset {TOKEN_ENV} and install an offline license "
                      "(`brindle account license install <file>`)") from e
    except auth.AuthError as e:
        raise CIError(f"brindle ci: {TOKEN_ENV} was refused ({e.code}); set it to a CI token "
                      "from `brindle account org ci-token create`") from e
    except license.LicenseError as e:
        raise CIError(f"brindle ci: {e}. In CI, set {TOKEN_ENV} to a CI token from "
                      "`brindle account org ci-token create`.") from e
    return _needs_ci(ent)


# -- secrets ----------------------------------------------------------------------

# Taken out of the environment before any agent starts: agents run the repo's
# own code (tests, scripts, whatever a prompt talks them into), so they must
# not inherit the org's CI token or a token that can push. brindle's own push and
# `gh pr create` get them back explicitly. The model API key stays: the agents
# need it. This is defense in depth only: agents run as the same OS user as
# this process, so what it holds they can reach. The real boundary is
# `--bundle` + `brindle ci publish`, where no write token is on the machine.
WITHHELD_ENV = (TOKEN_ENV, "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN",
                "GITHUB_ENTERPRISE_TOKEN")


def withhold_secrets(environ=None) -> dict[str, str]:
    """Remove the WITHHELD_ENV variables from ``environ`` (default: this
    process's environment, which tmux and so every agent inherits) and
    return them."""
    environ = os.environ if environ is None else environ
    return {k: environ.pop(k) for k in WITHHELD_ENV if k in environ}


def _github_env(secrets: dict[str, str] | None) -> dict[str, str]:
    """This environment plus only the GitHub token(s): push and gh need
    nothing else, least of all the org's CI token."""
    gh = {k: v for k, v in (secrets or {}).items() if k != TOKEN_ENV}
    return {**os.environ, **gh}


def _clear_tmux_env() -> None:
    """A tmux server that was already running keeps the environment it
    started with, and new sessions inherit it: drop the withheld variables
    from its global environment too."""
    from brindle import tmux

    for k in WITHHELD_ENV:
        try:
            tmux._tmux("set-environment", "-g", "-u", k, check=False)
        except Exception:  # noqa: BLE001 - no server yet: nothing to clear
            pass


def _repo_slug(remote: str | None) -> str | None:
    """owner/name from https://github.com/owner/name."""
    m = re.match(r"^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$", remote or "")
    return m.group(1) if m else None


# -- the run ----------------------------------------------------------------------


@dataclass
class Outcome:
    status: str                     # done | need_user | timeout | budget | stalled | exited | error
    goal: Goal
    branch: str
    milestones: list[dict] = field(default_factory=list)
    note: str | None = None         # the question, the stall reason, the error
    pr_url: str | None = None
    elapsed: float = 0.0
    bundle: str | None = None       # with --bundle: the file `brindle ci publish` takes
    bundle_status: str | None = None  # done | partial: what the bundle holds
    usage: dict | None = None       # tokens, models, profiles: ci_budget.Tracker.summary()

    @property
    def ok(self) -> bool:
        return self.status == "done" and (self.pr_url is not None or self.note is None)

    def summary(self) -> dict:
        return {
            "status": self.status, "ok": self.ok, "goal": self.goal.title,
            "issue": self.goal.issue, "branch": self.branch, "pr_url": self.pr_url,
            "bundle": self.bundle, "bundle_status": self.bundle_status,
            "note": self.note, "elapsed_seconds": round(self.elapsed),
            "milestones": self.milestones, "usage": self.usage,
        }

    def describe(self) -> str:
        lines = [f"brindle ci: {self.status}: {self.goal.title}"]
        if self.milestones:
            done = sum(m["status"] == "passed" for m in self.milestones)
            lines.append(f"  {done} of {len(self.milestones)} milestones verified")
            for m in self.milestones:
                mark = {"passed": "✓", "failed": "✗"}.get(m["status"], "○")
                check = f" (check: {m['check']})" if m.get("check") else ""
                lines.append(f"  {mark} {m['title']}{check}")
        if self.note:
            lines.append(f"  {self.note}")
        if self.usage:
            lines.append(ci_budget.describe_line(self.usage))
        if self.pr_url:
            lines.append(f"  pull request: {self.pr_url}")
        if self.bundle:
            what = "partial work" if self.bundle_status == BUNDLE_PARTIAL else "bundle"
            lines.append(f"  {what}: {self.bundle} (publish it with `brindle ci publish`)")
        return "\n".join(lines)


UNATTENDED = """[brindle ci] This session runs unattended in CI: nobody is watching this chat. \
A question to the user (need_user) ends the run as a failure, so make the \
decisions you can yourself and prefer small, reviewable changes. When every \
milestone is verified, stop: brindle opens the pull request from this branch.

Goal{where}: {title}
{detail}{context}
{instruction}"""

CONTEXT = """
Comments on the issue since brindle last reported (answers to its question, \
or how to continue; take them into account):

{context}
"""

DERIVE = ("Call set_goal now with this goal and the milestones you derive from it, each "
          "with a check command that verifies it (tests you add count), then drive it to "
          "completion: delegate, review, merge, check_milestone.")
RECORDED = ("The goal and its milestones are already recorded (get_progress shows them): "
            "drive them to completion: delegate, review, merge, check_milestone.")


def kickoff(goal: Goal, recorded: bool) -> str:
    where = f" (from issue #{goal.issue})" if goal.issue is not None else ""
    detail = f"\n{goal.detail}\n" if goal.detail and not recorded else ""
    context = CONTEXT.format(context=goal.context) if goal.context else ""
    return UNATTENDED.format(where=where, title=goal.title, detail=detail, context=context,
                             instruction=RECORDED if recorded else DERIVE)


def _checkout(db: DB, repo_path: str, branch: str, base: str | None) -> Workspace:
    """The worktree for ``branch``: a fresh one cut from ``base`` (default:
    the repo's base), or the existing one on a re-run."""
    repo_root = git.main_repo_root(repo_path)
    existing = git.worktree_for_branch(repo_root, branch)
    if existing and not Path(existing).is_dir():
        # Its folder was deleted (a wiped brindle home, a cleaned runner): forget
        # the stale worktree and check the branch out afresh, keeping its commits.
        git.run(["worktree", "prune"], repo_root, check=False)
        existing = git.worktree_for_branch(repo_root, branch)
    if existing:
        return workspaces.adopt_root(db, existing)
    return workspaces.create(db, repo_path, branch, base, apply_prefix=False).workspace


def _spawn(db: DB, ws: Workspace, prompt: str) -> Agent:
    return agents.spawn(db, ws, "supervisor", prompt=prompt, watch_pane=False,
                        background_setup=True, autopilot=True)


def _alive(db: DB, root_id: str) -> bool:
    a = db.get_agent(root_id)
    return a is not None and agents.is_alive(a)


def _stop(db: DB, root_id: str) -> None:
    """Stop the supervisor and every worker it started; their work stays."""
    agents.pause(db, root_id)


def _push(ws: Workspace, secrets: dict[str, str] | None = None, remote: str | None = None) -> None:
    """Push the branch without trusting the worktree the agents worked in:
    its hooks and git config are theirs to edit. The commits are fetched into
    a fresh bare repo brindle made (no hooks, no config of theirs) and pushed
    from there to ``remote``, the origin recorded before any agent started,
    with gh answering git's credential request from GH_TOKEN."""
    if not _repo_slug(remote):
        raise CIError("can't push: origin isn't a github.com repository")
    env = _github_env(secrets)
    ref = f"refs/heads/{ws.branch}"
    with tempfile.TemporaryDirectory(prefix="brindle-push-") as tmp:
        bare = str(Path(tmp) / "push.git")
        steps = [
            ["git", "init", "--bare", "--quiet", bare],
            ["git", "-C", bare, "fetch", "--no-tags", "--quiet", "--", ws.path, f"+{ref}:{ref}"],
            ["git", "-C", bare, "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=",
             "-c", "credential.helper=!gh auth git-credential",
             "push", "--no-verify", "--quiet", "--", f"{remote}.git", f"{ref}:{ref}"],
        ]
        for cmd in steps:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=git.TIMEOUT, env=env,
                                  cwd=tmp)
            if proc.returncode != 0:
                raise git.GitError(f"git {cmd[3] if cmd[1] == '-C' else cmd[1]} failed: "
                                   f"{proc.stderr.strip() or proc.stdout.strip()}")


def _create_pr(ws: Workspace, base: str, title: str, body: str,
               secrets: dict[str, str] | None = None, remote: str | None = None) -> str:
    slug = _repo_slug(remote)
    if not slug:
        raise CIError("can't open a pull request: origin isn't a github.com repository")
    return _open_pr(slug, base, ws.branch, title, workspaces.with_footer(body, ws.repo_root),
                    _github_env(secrets))


def _existing_pr(slug: str, branch: str, env: dict[str, str], cwd: str) -> dict | None:
    """The open pull request from ``branch`` of this repository, if a run
    already opened one (a re-run after a question or a partial result adds
    to its branch). A pull request from a fork with a branch of the same
    name is somebody else's: never touched."""
    proc = subprocess.run(
        ["gh", "pr", "list", "--repo", slug, "--head", branch, "--state", "open",
         "--json", "url,isDraft,isCrossRepository,headRefName", "--limit", "10"],
        cwd=cwd, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        return None
    try:
        found = json.loads(proc.stdout)
    except ValueError:
        return None
    if not isinstance(found, list):
        return None
    for pr in found:
        if (isinstance(pr, dict) and isinstance(pr.get("url"), str)
                and pr.get("isCrossRepository") is False and pr.get("headRefName") == branch
                and re.fullmatch(_PR_URL.format(slug=re.escape(slug)), pr["url"])):
            return pr
    return None


def _open_pr(slug: str, base: str, branch: str, title: str, body: str, env: dict[str, str],
             draft: bool = False) -> str:
    """Open the pull request (a draft one with ``draft``), or when the branch
    already has one open, update its title and body and, if it was a draft
    and the work is now complete, mark it ready."""
    # --repo, and not a worktree as cwd: gh would otherwise read the
    # agents' git config to decide where the pull request goes.
    with tempfile.TemporaryDirectory(prefix="brindle-pr-") as tmp:
        existing = _existing_pr(slug, branch, env, tmp)
        if existing:
            url = existing["url"]
            steps = [["gh", "pr", "edit", url, "--repo", slug, "--title", title, "--body", body]]
            if existing.get("isDraft") and not draft:
                steps.append(["gh", "pr", "ready", url, "--repo", slug])
            for cmd in steps:
                proc = subprocess.run(cmd, cwd=tmp, capture_output=True, text=True, env=env)
                if proc.returncode != 0:
                    raise CIError(f"gh pr {cmd[2]} failed: {proc.stderr.strip() or proc.stdout.strip()}")
            return url
        proc = subprocess.run(
            ["gh", "pr", "create", "--repo", slug, "--base", base, "--head", branch,
             "--title", title, "--body", body, *(["--draft"] if draft else [])],
            cwd=tmp, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise CIError(f"gh pr create failed: {proc.stderr.strip() or proc.stdout.strip()}")
    url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not url:
        raise CIError("gh pr create printed no URL")
    return url


# -- the bundle: the run's result, handed to another trust domain -------------------

BUNDLE_STATUS = "done"            # every milestone verified: a pull request
BUNDLE_PARTIAL = "partial"        # commits, but the run ended otherwise: a draft
BUNDLE_STATUSES = (BUNDLE_STATUS, BUNDLE_PARTIAL)
_SLUG = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"


def bundle_meta_path(path: str | Path) -> Path:
    return Path(str(path) + ".json")


def _safe_branch(name) -> bool:
    """A plain branch name: nothing git or gh could read as an option, a
    refspec or another ref."""
    return (isinstance(name, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", name) is not None
            and ".." not in name and "//" not in name
            and not name.endswith(("/", ".", ".lock")) and "/." not in name)


def has_commits(ws: Workspace, base_ref: str) -> bool:
    """Whether the branch has commits past ``base_ref``: anything to bundle."""
    proc = git.run(["rev-list", "--count", f"{base_ref}..refs/heads/{ws.branch}"], ws.path, check=False)
    return proc.returncode == 0 and proc.stdout.strip() not in ("", "0")


def write_bundle(ws: Workspace, base_ref: str, base_branch: str, path: str | Path,
                 title: str, body: str, status: str = BUNDLE_STATUS) -> Path:
    """Write the commits of ``base_ref..branch`` as a git bundle at ``path``,
    and ``path``.json saying what pull request they are for (a draft, when
    ``status`` is partial). This needs no token: `brindle ci publish` pushes
    it, somewhere no agent ever ran."""
    assert status in BUNDLE_STATUSES, status
    path = Path(path).resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise CIError(f"cannot write the bundle {path}: {e.strerror}") from e
    git.run(["-c", "core.hooksPath=/dev/null", "bundle", "create", str(path),
             f"refs/heads/{ws.branch}", f"^{base_ref}"], ws.path)
    meta = {"branch": ws.branch, "base": base_branch, "title": title,
            "body": workspaces.with_footer(body, ws.repo_root), "status": status}
    try:
        bundle_meta_path(path).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        raise CIError(f"cannot write {bundle_meta_path(path)}: {e.strerror}") from e
    return path


def read_bundle_meta(path: str | Path) -> dict:
    """``path``.json, checked: it was written on a machine where agents ran,
    so nothing in it is trusted. The branch must be a ``brindle/ci-`` branch
    (a bundle can't be published over ``main``), and only a verified goal
    (``done``) or work marked ``partial`` (published as a draft) is."""
    meta_path = bundle_meta_path(path)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise CIError(f"cannot read {meta_path}: {e.strerror}") from e
    except ValueError as e:
        raise CIError(f"{meta_path} is not JSON") from e
    if not isinstance(meta, dict):
        raise CIError(f"{meta_path} is not a JSON object")
    status = meta.get("status")
    if status not in BUNDLE_STATUSES:
        raise CIError(f"not publishing: the run's status is {status!r}, not one of "
                      f"{', '.join(BUNDLE_STATUSES)}")
    branch, base, title, body = (meta.get(k) for k in ("branch", "base", "title", "body"))
    if not _safe_branch(branch) or not branch.startswith(BRANCH_PREFIX):
        raise CIError(f"not publishing: the branch must be a {BRANCH_PREFIX}* branch")
    if not _safe_branch(base) or base == branch:
        raise CIError("not publishing: the base branch name is not usable")
    if not isinstance(title, str) or not title.strip() or "\n" in title.strip():
        raise CIError("not publishing: the pull request has no one-line title")
    if not isinstance(body, str):
        raise CIError("not publishing: the pull request body is not text")
    return {"branch": branch, "base": base, "title": title.strip(), "body": body,
            "status": status}


def _default_branch(url: str, env: dict, cwd: str) -> str:
    """The repository's default branch, asked of the repository itself."""
    proc = subprocess.run(["git", "-c", "credential.helper=", "-c",
                           "credential.helper=!gh auth git-credential",
                           "ls-remote", "--symref", "--", url, "HEAD"],
                          capture_output=True, text=True, timeout=git.TIMEOUT, env=env, cwd=cwd)
    m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", proc.stdout, re.M)
    if proc.returncode != 0 or not m or not _BRANCH_NAME.fullmatch(m.group(1)):
        raise CIError("brindle ci publish: can't tell the repository's default branch; pass --base")
    return m.group(1)


_BRANCH_NAME = re.compile(r"[A-Za-z0-9._/-]{1,100}")


def publish(path: str | Path, repo: str | None = None, *, base: str | None = None,
            remote: str | None = None, environ=None, if_present: bool = False) -> str | None:
    """``brindle ci publish``: push the bundle's branch to github.com/``repo``
    and open the pull request (a draft, for a partial bundle); returns its
    URL. With ``if_present``, no bundle at ``path`` (a run that left no
    commits) is not an error: returns None.

    This is the only step that holds a token that can write, so it treats
    the bundle as data: it is verified and fetched into a fresh bare repo
    (no hooks, no config from the repo), only the one ``brindle/ci-`` branch is
    taken from it, and nothing in it is checked out or run. ``remote``
    replaces the https://github.com URL; it exists for tests (a local bare
    "origin") and is deliberately not reachable from the CLI or any config."""
    environ = os.environ if environ is None else environ
    slug = (repo or environ.get("GITHUB_REPOSITORY") or "").strip()
    if not re.fullmatch(_SLUG, slug):
        raise CIError("brindle ci publish: name the repository with --repo owner/name "
                      "(default: $GITHUB_REPOSITORY)")
    bundle = Path(path).resolve()
    if not bundle.is_file():
        if if_present:
            return None
        raise CIError(f"brindle ci publish: no bundle at {bundle}")
    meta = read_bundle_meta(bundle)
    url = remote or f"https://github.com/{slug}.git"
    ref = f"refs/heads/{meta['branch']}"
    env = dict(environ)
    if base is not None and (not _BRANCH_NAME.fullmatch(base) or ".." in base or base.startswith("-")):
        raise CIError("brindle ci publish: --base is not a valid branch name")
    with tempfile.TemporaryDirectory(prefix="brindle-publish-") as tmp:
        # The bundle's metadata comes from the run, so it doesn't get to choose
        # the branch the pull request targets: the caller or the repo does.
        meta = {**meta, "base": base or _default_branch(url, env, tmp)}
        bare = str(Path(tmp) / "publish.git")
        git_bare = ["git", "-C", bare, "-c", "core.hooksPath=/dev/null"]
        as_gh = ["-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential"]
        steps = [
            ("init", ["git", "init", "--bare", "--quiet", bare]),
            # The bundle only holds the new commits: the base they sit on
            # comes from the repository itself, never from the run.
            ("fetch of the base branch", [*git_bare, *as_gh, "fetch", "--no-tags", "--quiet", "--",
                                          url, f"+refs/heads/{meta['base']}:refs/brindle-base/head"]),
            ("bundle verify", [*git_bare, "bundle", "verify", "--quiet", str(bundle)]),
            ("fetch from the bundle", [*git_bare, "fetch", "--no-tags", "--quiet", "--",
                                       str(bundle), f"+{ref}:{ref}"]),
            ("push", [*git_bare, *as_gh, "push", "--no-verify", "--quiet", "--", url,
                      f"{ref}:{ref}"]),
        ]
        for what, cmd in steps:
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=git.TIMEOUT,
                                      env=env, cwd=tmp)
            except (OSError, subprocess.TimeoutExpired) as e:
                raise CIError(f"brindle ci publish: git {what}: {e}") from e
            if proc.returncode != 0:
                raise CIError(f"brindle ci publish: git {what} failed: "
                              f"{proc.stderr.strip() or proc.stdout.strip()}")
    return _open_pr(slug, meta["base"], meta["branch"], meta["title"], meta["body"], env,
                    draft=meta["status"] == BUNDLE_PARTIAL)


def set_max_workers(repo_root: str, n: int) -> Path:
    """Cap this repo's parallel workers through ``.brindle/config.local.json``
    (gitignored; other keys are kept)."""
    from brindle.config import set_local

    return set_local(repo_root, "max_agents", n)


def milestone_rows(db: DB, root_id: str) -> list[dict]:
    return [{"position": m.position, "title": m.title, "check": m.check_cmd, "status": m.status}
            for m in db.milestones(root_id)]


def pr_body(outcome: Outcome) -> str:
    """The pull request's description: the goal, the milestones with their
    checks ticked when verified, and for partial work (the run ended before
    every milestone was) why it stopped and how far it got."""
    g = outcome.goal
    partial = outcome.status != "done"
    lines = ["## Goal", "", g.title]
    if g.detail and not g.plan():
        lines += ["", g.detail]
    lines += ["", "## Milestones", ""]
    for m in outcome.milestones:
        box = "x" if m["status"] == "passed" else " "
        check = f" (`{m['check']}`)" if m.get("check") else ""
        lines.append(f"- [{box}] {m['title']}{check}")
    if not outcome.milestones:
        lines.append("(none recorded)")
    if partial:
        done = sum(m["status"] == "passed" for m in outcome.milestones)
        why = f"{outcome.status}: {outcome.note}" if outcome.note else outcome.status
        lines += ["", f"**Partial work.** The run stopped ({why}) with {done} of "
                      f"{len(outcome.milestones)} milestones verified, so this is a draft."]
    lines += ci_budget.pr_section(outcome.usage)
    if g.issue is not None:
        lines += ["", f"Part of #{g.issue}" if partial else f"Closes #{g.issue}"]
    lines += ["", "🤖 Opened by `brindle ci`"]
    return "\n".join(lines) + "\n"


def _poll(db: DB, root_id: str) -> tuple[str, str | None] | None:
    """The run's terminal state from the autopilot row, or None to keep waiting."""
    ap = db.get_autopilot(root_id)
    ms = db.milestones(root_id)
    if ap is not None and ap.goal and ms and all(m.status == "passed" for m in ms):
        return "done", None
    if ap is not None and ap.state == "done" and ms:
        return "done", None
    if ap is not None and ap.state == "blocked":
        return "need_user", ap.note or "the supervisor asked for a decision"
    if ap is not None and ap.state == "stalled":
        return "stalled", ap.note or "no progress"
    if ap is not None and ap.state == "usage_paused":
        return "stalled", ap.note or "paused for usage: nothing will happen in this run"
    if not _alive(db, root_id):
        return "exited", "the supervisor exited before the goal was verified"
    return None


def run(db: DB, repo_path: str, goal: Goal, *, timeout_min: float = DEFAULT_TIMEOUT_MIN,
        max_workers: int | None = None, base: str | None = None, pr: bool = True,
        poll_seconds: float = POLL_SECONDS, clock=time.time, sleep=time.sleep,
        secrets: dict[str, str] | None = None, bundle: str | Path | None = None,
        budget: int | None = None) -> Outcome:
    """Run ``goal`` to a verified end (or not) and, with ``pr``, open the pull
    request; with ``bundle``, write the branch to that file instead (nothing
    is pushed: ``brindle ci publish`` does that elsewhere). With ``budget``
    (tokens), the run stops like at the timeout once it has used more. The
    session is always stopped before this returns."""
    from brindle import autopilot as pilot

    if timeout_min <= 0:
        raise CIError("--timeout must be a positive number of minutes")
    if secrets is None:   # however run() is reached, agents never get the tokens
        secrets = withhold_secrets()
    _clear_tmux_env()
    branch = goal.branch
    ws = _checkout(db, repo_path, branch, base)
    remote = git.remote_web_url(ws.repo_root)   # recorded before any agent can edit it
    base_branch = base or ws.base_branch or git.default_branch(ws.repo_root)
    base_ref = base_branch
    if bundle is not None:   # the base commit too: agents can move the local branch
        pr = False
        base_ref = git.run(["rev-parse", "--verify", "--quiet", f"{base_branch}^{{commit}}"],
                           ws.repo_root, check=False).stdout.strip() or base_branch
    if max_workers is not None:
        set_max_workers(ws.repo_root, max_workers)
    plan = goal.plan()
    root = _spawn(db, ws, kickoff(goal, plan is not None))
    db.add_autopilot(root.id)
    if plan is not None:
        pilot.set_goal(db, root.id, plan.goal, plan.milestones, plan.detail)
    started = clock()
    deadline = started + timeout_min * 60
    outcome = Outcome("error", goal, branch)
    spend = ci_budget.Tracker(db, root.id, budget)
    try:
        while True:
            found = _poll(db, root.id)
            if found:
                outcome.status, outcome.note = found
                break
            over = ci_budget.check(spend)
            if over:
                outcome.status, outcome.note = ci_budget.BUDGET_STATUS, over
                break
            if clock() >= deadline:
                outcome.status = "timeout"
                outcome.note = f"not finished after {timeout_min:g} minutes"
                break
            sleep(min(poll_seconds, max(0.0, deadline - clock())))
    except BaseException as e:  # the session must stop even on Ctrl-C or a bug
        outcome.status, outcome.note = "error", f"{type(e).__name__}: {e}"
        raise
    finally:
        outcome.milestones = milestone_rows(db, root.id)
        outcome.elapsed = clock() - started
        spend.update()
        outcome.usage = spend.summary()
        try:
            _stop(db, root.id)
        except Exception as e:  # noqa: BLE001 - the outcome matters more than the stop
            outcome.note = (outcome.note + "; " if outcome.note else "") + f"stopping the session failed: {e}"
    if outcome.status == "done" and pr:
        try:
            _push(ws, secrets, remote)
            outcome.pr_url = _create_pr(ws, base_branch, goal.title, pr_body(outcome), secrets, remote)
        except (git.GitError, CIError) as e:
            outcome.note = str(e)
    if bundle is not None and (outcome.status == "done" or has_commits(ws, base_ref)):
        # A verified goal: the pull request. Anything else with commits (a
        # question, a stall, the timeout): the same bundle, marked partial,
        # which `brindle ci publish` opens as a draft so the work isn't lost.
        status = BUNDLE_STATUS if outcome.status == "done" else BUNDLE_PARTIAL
        try:
            outcome.bundle = str(write_bundle(ws, base_ref, base_branch, bundle, goal.title,
                                              pr_body(outcome), status))
            outcome.bundle_status = status
        except (git.GitError, CIError) as e:
            if outcome.status == "done":
                outcome.note = str(e)
            else:
                outcome.note = f"{outcome.note}; keeping the partial work failed: {e}"
    return outcome


STATUSES = ("done", "need_user", "timeout", ci_budget.BUDGET_STATUS, "stalled", "exited", "error",
            "cancelled")


def error_outcome(note: str, goal: Goal | None = None, issue: int | None = None) -> dict:
    """The outcome record of a run that never started (a refused entitlement,
    a bad goal): the same shape as ``Outcome.summary()``."""
    return {
        "status": "error", "ok": False, "goal": goal.title if goal else None,
        "issue": goal.issue if goal else issue, "branch": goal.branch if goal else None,
        "pr_url": None, "bundle": None, "bundle_status": None, "note": note,
        "elapsed_seconds": 0, "milestones": [], "usage": None,
    }


def write_outcome(data: dict, path: str | Path) -> Path:
    """Write the outcome record (``Outcome.summary()`` or ``error_outcome``)
    as JSON to ``path``: what `brindle ci report` comments on the issue."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        raise CIError(f"cannot write the outcome to {path}: {e.strerror}") from e
    return path


def write_step_summary(outcome: Outcome, path: str | None = None) -> Path | None:
    """Append the JSON summary to ``$GITHUB_STEP_SUMMARY`` (or ``path``)."""
    path = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return None
    text = (f"## brindle ci: {outcome.status}\n\n```json\n"
            f"{json.dumps(outcome.summary(), indent=2)}\n```\n")
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)
    return Path(path)


def resolve_goal(goal: str | None, goal_file: str | None, issue: int | None, cwd: str) -> Goal:
    given = sum(x is not None for x in (goal, goal_file, issue))
    if given != 1:
        raise CIError("give exactly one of --goal TEXT, --goal-file PATH or --issue N")
    if issue is not None:
        return goal_from_issue(issue, cwd)
    if goal_file is not None:
        return goal_from_file(goal_file)
    assert goal is not None
    return goal_from_text(goal)


def run_cli(*, goal: str | None, goal_file: str | None, issue: int | None,
            timeout_min: float, max_workers: int | None, base: str | None, pr: bool,
            echo=print, cwd: str | None = None, bundle: str | None = None,
            entitlement: str | None = None, budget: str | None = None,
            outcome_path: str | None = None) -> int:
    """``brindle ci run``: 0 on a verified goal (and its PR or bundle), 1
    otherwise. With ``outcome_path``, the outcome is written there as JSON
    whatever happened, even when the run couldn't start."""
    cwd = cwd or os.getcwd()
    # Resolved now, like the bundle: the run changes nothing about where it goes.
    outcome_path = Path(outcome_path).resolve() if outcome_path is not None else None
    g = None
    try:
        g = resolve_goal(goal, goal_file, issue, cwd)
        tokens = ci_budget.parse_budget(budget)
        if entitlement is not None:
            require_ci(entitlement_file=entitlement)
        else:
            require_ci()
        secrets = withhold_secrets()
        db = DB()
        kw = {"bundle": Path(bundle).resolve()} if bundle is not None else {}
        if tokens is not None:
            kw["budget"] = tokens
        outcome = run(db, cwd, g, timeout_min=timeout_min, max_workers=max_workers,
                      base=base, pr=pr and bundle is None, secrets=secrets, **kw)
    except (CIError, ci_budget.BudgetError, git.GitError, workspaces.WorkspaceError, agents.AgentError, tmux.TmuxError) as e:
        echo(str(e))
        if outcome_path is not None:
            write_outcome(error_outcome(str(e), g, issue), outcome_path)
        return 1
    except BaseException as e:   # a bug or Ctrl-C: still an outcome to report
        if outcome_path is not None:
            write_outcome(error_outcome(f"{type(e).__name__}: {e}", g, issue), outcome_path)
        raise
    echo(outcome.describe())
    if outcome.pr_url:
        echo(outcome.pr_url)
    write_step_summary(outcome)
    if outcome_path is not None:
        write_outcome(outcome.summary(), outcome_path)
    return 0 if outcome.ok else 1


def publish_cli(path: str, repo: str | None = None, base: str | None = None, echo=print,
                if_present: bool = False) -> int:
    """``brindle ci publish``: 0 with the pull request's URL (also set as the
    step output ``pr_url`` in GitHub Actions), 1 with what failed."""
    try:
        url = publish(path, repo, base=base, if_present=if_present)
    except CIError as e:
        echo(str(e))
        return 1
    if url is None:
        echo("nothing to publish: the run left no commits")
        return 0
    echo(url)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## brindle ci: pull request\n\n{url}\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as f:
            f.write(f"pr_url={url}\n")
    return 0


# -- the report: the outcome, as a comment on the issue ---------------------------

# What the comment may hold from the outcome file, which was written where
# agents ran. Everything else in it is ignored, every text is cut to a size,
# and every text is rendered as code so that nothing in it is markdown, a
# mention, a link or HTML.
MAX_NOTE, MAX_TITLE, MAX_CHECK, MAX_MILESTONES = 2000, 200, 300, 50
MILESTONE_STATUSES = ("passed", "failed", "pending")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ]")
_PR_URL = r"https://github\.com/{slug}/pull/[0-9]+"


def _text(value, limit: int, inline: bool = False) -> str | None:
    """``value`` as bounded plain text: not a string, or empty, is None;
    control characters go, newlines too when ``inline``."""
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub("", value.replace("\r\n", "\n").replace("\r", "\n"))
    if inline:
        text = " ".join(text.split())
    text = text.strip()
    if not text:
        return None
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def _code(text: str) -> str:
    """``text`` as a code span that can't be closed early (the run longer
    than any in it), fit for a table cell (no pipes, one line)."""
    text = " ".join(text.split()).replace("|", "\\|")
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _block(text: str) -> str:
    """``text`` as a fenced code block that it can't close early."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}text\n{text}\n{fence}"


def load_outcome(path: str | Path, slug: str | None = None) -> dict:
    """The outcome file, reduced to the expected fields and shapes. A missing
    or broken file is itself an outcome (``error``), since the run is
    reported whatever happened."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        data = {"status": "error", "note": "the run left no outcome: did it start? See the workflow run."}
    except ValueError:
        data = {"status": "error", "note": "the run's outcome file is not JSON"}
    if not isinstance(data, dict):
        data = {"status": "error", "note": "the run's outcome file is not a JSON object"}
    status = data.get("status")
    issue = data.get("issue")
    pr_url = data.get("pr_url")
    milestones = []
    for m in (data.get("milestones") if isinstance(data.get("milestones"), list) else [])[:MAX_MILESTONES]:
        if not isinstance(m, dict):
            continue
        title = _text(m.get("title"), MAX_TITLE, inline=True)
        if title is None:
            continue
        milestones.append({
            "title": title, "check": _text(m.get("check"), MAX_CHECK, inline=True),
            "status": m.get("status") if m.get("status") in MILESTONE_STATUSES else "pending",
        })
    elapsed = data.get("elapsed_seconds")
    return {
        "status": status if status in STATUSES else "error",
        "goal": _text(data.get("goal"), MAX_TITLE, inline=True),
        "issue": issue if isinstance(issue, int) and not isinstance(issue, bool) and issue > 0 else None,
        "note": _text(data.get("note"), MAX_NOTE),
        "pr_url": pr_url if isinstance(pr_url, str) and slug
        and re.fullmatch(_PR_URL.format(slug=re.escape(slug)), pr_url) else None,
        "bundle_status": data.get("bundle_status") if data.get("bundle_status") in BUNDLE_STATUSES else None,
        "elapsed_seconds": int(elapsed) if isinstance(elapsed, (int, float))
        and not isinstance(elapsed, bool) and 0 <= elapsed < 10 ** 7 else None,
        "milestones": milestones,
        "usage": ci_budget.clean_usage(data.get("usage")),
    }


HEADLINES = {
    "done": "done", "need_user": "needs a decision", "timeout": "ran out of time",
    ci_budget.BUDGET_STATUS: "went over its token budget", "stalled": "stalled",
    "exited": "the supervisor exited", "error": "error", "cancelled": "cancelled",
}


def report_comment(outcome: dict, *, pr_url: str | None = None, run_url: str | None = None,
                   label: str | None = None, results: dict[str, str] | None = None) -> str:
    """The issue comment for an outcome from ``load_outcome``. ``pr_url`` is
    the pull request `brindle ci publish` opened (trusted: it comes from
    that job, not the run); ``results`` the workflow's job results
    (``entitle``, ``run``, ``publish``), so a job that failed before or
    after the run is reported too."""
    results = results or {}
    status, note = outcome["status"], outcome["note"]
    pr_url = pr_url or outcome.get("pr_url")
    if status == "error" and results.get("entitle") == "failure":
        note = ("the entitlement step failed: BRINDLE_PRO_TOKEN is missing, was refused, or the "
                "org's plan no longer includes brindle Team. See the workflow run.")
    elif status == "error" and results.get("run") == "cancelled":
        status, note = "cancelled", None
    partial = status != "done"
    lines = [f"<!-- brindle-ci: {status} -->", f"## brindle ci: {HEADLINES[status]}", ""]
    if outcome["goal"]:
        lines += [f"Goal: {_code(outcome['goal'])}", ""]
    if status == "done":
        if pr_url:
            lines += [f"Every milestone is verified. Pull request: {pr_url}", ""]
        elif results.get("publish") == "failure":
            lines += ["Every milestone is verified, but publishing the pull request failed. "
                      "See the workflow run.", ""]
        else:
            lines += ["Every milestone is verified.", ""]
    elif status == "need_user":
        lines += ["The supervisor stopped with a question:", ""]
    if note and status != "done":
        lines += [_block(note), ""]
    if status == "need_user":
        again = f"add the {_code(label)} label again" if label else "start the workflow again"
        lines += [f"Answer in a comment here, then {again}: the next run reads the comments "
                  "after this one and continues on the same branch.", ""]
    if partial:
        if pr_url:
            lines += [f"The work so far is in a draft pull request: {pr_url}", ""]
        elif outcome["bundle_status"] == BUNDLE_PARTIAL and results.get("publish") == "failure":
            lines += ["There is partial work, but publishing it as a draft failed. "
                      "See the workflow run.", ""]
    if outcome["milestones"]:
        done = sum(m["status"] == "passed" for m in outcome["milestones"])
        lines += [f"{done} of {len(outcome['milestones'])} milestones verified:", "",
                  "| Milestone | Check | Status |", "|---|---|---|"]
        marks = {"passed": "✓ passed", "failed": "✗ failed", "pending": "○ pending"}
        for m in outcome["milestones"]:
            check = _code(m["check"]) if m["check"] else ""
            lines.append(f"| {_code(m['title'])} | {check} | {marks[m['status']]} |")
        lines.append("")
    elif status not in ("error", "cancelled"):
        lines += ["No milestones were recorded.", ""]
    tail = []
    if outcome["elapsed_seconds"]:
        tail.append(f"Ran for {max(1, round(outcome['elapsed_seconds'] / 60))} min.")
    spent = ci_budget.report_line(outcome.get("usage"))
    if spent:
        tail.append(spent)
    if run_url and run_url.startswith("https://"):
        tail.append(f"Workflow run: {run_url}")
    if tail:
        lines += [" ".join(tail), ""]
    return "\n".join(lines).rstrip() + "\n"


def report(path: str | Path, issue: int, repo: str | None = None, *, pr_url: str | None = None,
           run_url: str | None = None, label: str | None = None,
           results: dict[str, str] | None = None, environ=None) -> str:
    """``brindle ci report``: comment the outcome at ``path`` on issue
    ``issue`` of github.com/``repo`` with ``gh``; returns the comment. It
    runs with a token that can only comment, outside any checkout."""
    environ = os.environ if environ is None else environ
    slug = (repo or environ.get("GITHUB_REPOSITORY") or "").strip()
    if not re.fullmatch(_SLUG, slug):
        raise CIError("brindle ci report: name the repository with --repo owner/name "
                      "(default: $GITHUB_REPOSITORY)")
    if not isinstance(issue, int) or issue <= 0:
        raise CIError("brindle ci report: --issue must be a positive number")
    if pr_url and not re.fullmatch(_PR_URL.format(slug=re.escape(slug)), pr_url):
        raise CIError(f"brindle ci report: --pr-url is not a pull request of {slug}")
    body = report_comment(load_outcome(path, slug), pr_url=pr_url or None, run_url=run_url or None,
                          label=label or None, results=results)
    with tempfile.TemporaryDirectory(prefix="brindle-report-") as tmp:
        body_file = Path(tmp) / "comment.md"
        body_file.write_text(body, encoding="utf-8")
        proc = subprocess.run(
            ["gh", "issue", "comment", str(issue), "--repo", slug, "--body-file", str(body_file)],
            cwd=tmp, capture_output=True, text=True, env=dict(environ))
    if proc.returncode != 0:
        raise CIError(f"gh issue comment failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return body


def report_cli(path: str, issue: int, repo: str | None = None, *, pr_url: str | None = None,
               run_url: str | None = None, label: str | None = None,
               entitle_result: str | None = None, run_result: str | None = None,
               publish_result: str | None = None, echo=print) -> int:
    """``brindle ci report``: 0 once the comment is posted, 1 with what failed."""
    results = {k: v for k, v in (("entitle", entitle_result), ("run", run_result),
                                 ("publish", publish_result)) if v}
    try:
        body = report(path, issue, repo, pr_url=pr_url, run_url=run_url, label=label, results=results)
    except CIError as e:
        echo(str(e))
        return 1
    headline = next((line for line in body.splitlines() if line.startswith("## ")), "").lstrip("# ")
    echo(f"commented on #{issue}: {headline}")
    return 0


# -- the workflow -----------------------------------------------------------------


WORKFLOW = """\
# Written by `brindle ci init`. brindle turns an issue labelled "{label}" into a
# pull request: https://pawdelta.com/brindle/docs/commands
#
# Secrets: BRINDLE_PRO_TOKEN (an org CI token, cpc_..., from
# `brindle account org ci-token create`) and ANTHROPIC_API_KEY (for Claude Code).
# Optional: OPENAI_API_KEY or CODEX_API_KEY also installs Codex, so routing can
# pick it; `brindle ci doctor` in a job lists the CLIs and keys it finds.
# `codex login` stores that key in ~/.codex/auth.json on the run machine, where
# agents can read it just as they can read ANTHROPIC_API_KEY: use a key scoped to CI.
# In the repo's Actions settings, allow GitHub Actions to create pull requests.
#
# Four jobs, because agents run the repo's own code and can reach anything on
# their machine. `entitle` alone holds the CI token and runs nothing from the
# repo; `run` does the work with a read-only token and hands over a git
# bundle and the outcome; `publish` holds the token that can push, and never
# checks out or runs anything from the repo; `report` holds a token that can
# only comment, and tells the issue how it went (the pull request, the
# supervisor's question, a stall, the timeout). Partial work becomes a draft
# pull request. A question is answered in a comment on the issue: add the
# label again and the next run continues with the comments as context.
#
# The issue body and its comments steer an unattended agent whose work becomes
# a pull request: only people you trust with write access should be able to
# apply the label. Pull requests opened with GITHUB_TOKEN don't trigger other
# workflows; to run your CI on them, set GH_TOKEN in the publish job to a
# GitHub App or personal access token.
name: brindle

on:
  issues:
    types: [labeled]
  workflow_dispatch:
    inputs:
      issue:
        description: Issue number to work on
        required: true
        type: number

permissions: {{}}

concurrency:
  group: brindle-${{{{ github.event.issue.number || inputs.issue }}}}
  cancel-in-progress: false

jobs:
  # The CI token lives only in this job, which checks out and runs nothing
  # from the repo: on hosted runners agents have sudo and the runner holds a
  # job's secrets in memory, so it must never be in the agents' job.
  entitle:
    if: github.event_name == 'workflow_dispatch' || github.event.label.name == '{label}'
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
      # A short-lived signed entitlement, not the CI token; kept one day.
      - uses: actions/upload-artifact@v4
        with:
          name: brindle-entitlement
          path: ${{{{ runner.temp }}}}/brindle-entitlement/
          if-no-files-found: error
          retention-days: 1

  run:
    needs: entitle
    runs-on: ubuntu-latest
    timeout-minutes: 120
    permissions:
      contents: read
      issues: read
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          # Agents run the repo's code: don't leave a token in .git/config.
          persist-credentials: false
      - name: Install tmux
        run: sudo apt-get update -q && sudo apt-get install -yq tmux
      - uses: astral-sh/setup-uv@v10.2.0
      - name: Install brindle
        run: uv tool install {package}
{providers}
      - name: Git identity for the agents' commits
        run: |
          git config --global user.name "brindle[bot]"
          git config --global user.email "brindle@users.noreply.github.com"
      - uses: actions/download-artifact@v4
        with:
          name: brindle-entitlement
          path: ${{{{ runner.temp }}}}/brindle-entitlement
      # No push token and no CI token here: GH_TOKEN can only read (the issue).
      # brindle reads the entitlement and deletes it before any agent starts.
      - name: Run brindle
        env:
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
          GH_TOKEN: ${{{{ github.token }}}}
        run: brindle ci run --issue ${{{{ github.event.issue.number || inputs.issue }}}} --timeout 100 --entitlement "$RUNNER_TEMP/brindle-entitlement/entitlement.jwt" --bundle "$RUNNER_TEMP/brindle-out/brindle.bundle" --outcome "$RUNNER_TEMP/brindle-out/outcome.json"
      # However it went: the outcome, and the bundle when there are commits.
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: brindle-out
          path: ${{{{ runner.temp }}}}/brindle-out/
          if-no-files-found: error
          retention-days: 1

  publish:
    needs: run
    # After the run, however it ended: partial work is published as a draft.
    if: always() && (needs.run.result == 'success' || needs.run.result == 'failure')
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
      pull-requests: write
    outputs:
      pr_url: ${{{{ steps.publish.outputs.pr_url }}}}
    steps:
      # No checkout: nothing from the repo runs in the job that can push.
      - uses: astral-sh/setup-uv@v10.2.0
      - name: Install brindle
        run: uv tool install {package}
      - uses: actions/download-artifact@v4
        continue-on-error: true   # nothing uploaded when the run died early
        with:
          name: brindle-out
          path: ${{{{ runner.temp }}}}/brindle-out
      # The base comes from the repository, not from the run's bundle.
      - name: Open the pull request
        id: publish
        env:
          GH_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
        run: brindle ci publish "$RUNNER_TEMP/brindle-out/brindle.bundle" --base "${{{{ github.event.repository.default_branch }}}}" --if-present

  report:
    needs: [entitle, run, publish]
    # Every outcome, including a run that never started; not an unrelated label.
    if: always() && (needs.run.result != 'skipped' || needs.entitle.result == 'failure')
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      issues: write
    steps:
      # No checkout: nothing from the repo runs in the job that can comment.
      - uses: astral-sh/setup-uv@v10.2.0
      - name: Install brindle
        run: uv tool install {package}
      - uses: actions/download-artifact@v4
        continue-on-error: true   # no outcome at all when the run never started
        with:
          name: brindle-out
          path: ${{{{ runner.temp }}}}/brindle-out
      # The outcome file comes from the agents' machine: brindle reads only
      # the fields it expects and renders their text as code, never markdown.
      - name: Comment on the issue
        env:
          GH_TOKEN: ${{{{ github.token }}}}
          PR_URL: ${{{{ needs.publish.outputs.pr_url }}}}
          RUN_URL: ${{{{ github.server_url }}}}/${{{{ github.repository }}}}/actions/runs/${{{{ github.run_id }}}}
          ENTITLE_RESULT: ${{{{ needs.entitle.result }}}}
          RUN_RESULT: ${{{{ needs.run.result }}}}
          PUBLISH_RESULT: ${{{{ needs.publish.result }}}}
        run: brindle ci report "$RUNNER_TEMP/brindle-out/outcome.json" --issue ${{{{ github.event.issue.number || inputs.issue }}}} --label "{label}" --pr-url "$PR_URL" --run-url "$RUN_URL" --entitle-result "$ENTITLE_RESULT" --run-result "$RUN_RESULT" --publish-result "$PUBLISH_RESULT"
"""


def workflow_text(label: str = "brindle") -> str:
    """The workflow, with brindle pinned to the version writing it: the publish
    job holds a write token, so it must not take whatever is newest on PyPI."""
    from brindle import __version__

    if not re.fullmatch(r"[A-Za-z0-9 _.:/-]{1,50}", label):
        raise CIError("the label may use letters, digits, spaces and _ . : / -")
    pinned = re.fullmatch(r"[0-9]+(\.[0-9]+)*([ab]|rc|\.post|\.dev)?[0-9]*", __version__ or "")
    from brindle import ci_providers

    return WORKFLOW.format(label=label, package=f"brindle=={__version__}" if pinned else "brindle",
                           providers=ci_providers.workflow_steps())


def init(repo_root: str | Path, label: str = "brindle", force: bool = False) -> Path:
    """Write ``.github/workflows/brindle.yml``; refuses to overwrite without ``force``."""
    path = Path(repo_root) / WORKFLOW_PATH
    if path.exists() and not force:
        raise CIError(f"{path} exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow_text(label), encoding="utf-8")
    return path

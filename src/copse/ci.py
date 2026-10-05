"""Copse-CI: copse with nobody at a terminal (copse Team).

``copse ci run`` turns a goal (typed, read from a file, or a GitHub issue)
into a pull request: it cuts a fresh branch, starts a supervisor with
autopilot on in a detached tmux session, gives it the goal, and polls the
autopilot state in the DB until every milestone is verified, the supervisor
needs a person (``need_user``: the run fails with the question), it stalls,
or the time runs out. Whatever happens, the session and its workers are
stopped at the end, and a JSON summary is appended to ``$GITHUB_STEP_SUMMARY``
when that is set (GitHub Actions).

Who is trusted with what. The agents run the repo's own code (its tests, its
scripts, whatever an issue talks them into) as the same OS user as copse. So
assume they can read anything this process can: its environment, its files,
the git and gh configuration, the programs on its PATH. Taking the tokens out
of the environment before agents start (``withhold_secrets``) and pushing from
a clean bare repo (``_push``) make theft harder, not impossible. The only real
protection is that a secret is not on the machine while agents run. So the
work is split into three steps, and the dangerous token is in the last one:

1. ``copse ci entitle --out FILE`` exchanges ``COPSE_PRO_TOKEN`` for the
   signed, short-lived entitlement and writes it to a file, on another
   machine than the agents (in the workflow, its own job: on hosted runners
   agents have sudo and the runner holds the secrets of the job they run in).
   ``copse ci run`` reads the file and deletes it before any agent starts.
2. ``copse ci run --entitlement FILE --bundle PATH`` does the work. It needs
   no CI token and no token that can write to GitHub. When the goal is
   verified it writes a git bundle of the new commits to PATH, and PATH.json
   with the branch, base, title and body of the pull request. Nothing is
   pushed.
3. ``copse ci publish PATH`` runs somewhere no agent ever ran (in the
   workflow: a second job, on a fresh machine, with no checkout). It holds
   the token that can push. The pull request targets ``--base`` or the
   repository's default branch, never what the bundle names. It treats the
   bundle as data: verifies it,
   fetches the one ``copse/ci-`` branch into a fresh bare repo, pushes that
   branch and opens the pull request with ``gh``. It never checks out or
   runs repo code, hooks or agents.

``copse ci run`` without ``--bundle`` still pushes and opens the pull request
itself, as before; use it only where the repo's code is trusted.

``copse ci init`` writes the two-job workflow that does this when an issue
gets a label.

Entitlement: ``ci`` must be in the copse Pro entitlement. In CI there is no
keychain and no browser, so ``COPSE_PRO_TOKEN`` holds an org CI token
(``cpc_...``, from ``copse account org ci-token create``). It is presented to
``POST /ci/entitlement`` and the entitlement that comes back is verified
(signature, issuer, expiry), whether it was fetched just now or read from the
file ``copse ci entitle`` wrote. CI tokens don't rotate, so the same secret
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

from copse import agents, git, workspaces
from copse.db import DB, Agent, Workspace

CI_FEATURE = "ci"
TOKEN_ENV = "COPSE_PRO_TOKEN"
CI_TOKEN_PREFIX = "cpc_"
BRANCH_PREFIX = "copse/ci-"
DEFAULT_TIMEOUT_MIN = 60
POLL_SECONDS = 10.0
WORKFLOW_PATH = Path(".github") / "workflows" / "copse.yml"
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

    @property
    def slug(self) -> str:
        return git.slug(self.title)[:MAX_SLUG].strip("-") or "goal"

    @property
    def branch(self) -> str:
        return f"{BRANCH_PREFIX}{self.issue if self.issue is not None else self.slug}"

    def plan(self):
        """The milestones when ``detail`` is goals.md-shaped, else None."""
        from copse import autopilot as pilot

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


def goal_from_issue(number: int, cwd: str) -> Goal:
    """The issue's title and body, through ``gh issue view``."""
    data = _gh_json(["issue", "view", str(number), "--json", "number,title,body"], cwd)
    title = str(data.get("title") or "").strip()
    if not title:
        raise CIError(f"issue #{number} has no title")
    body = str(data.get("body") or "").strip() or None
    return Goal(title, body, issue=number, source="issue")


def goal_from_text(text: str) -> Goal:
    """A typed goal: its first line is the title; a goals.md-shaped text
    (``# Goal`` with ``## Milestone`` sections) keeps its shape in ``detail``
    so the supervisor gets the milestones."""
    from copse import autopilot as pilot

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
    from copse.pro import auth, license

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
        raise CIError(f"copse ci needs copse Team: your plan ({ent.plan}) does not include "
                      f"{CI_FEATURE!r}. See `copse account`; plans: https://pawdelta.com/copse#pricing")
    return ent


def entitlement_from_file(path: str | Path, client=None, now: float | None = None):
    """The entitlement ``copse ci entitle`` wrote, verified like a fresh one
    (signature, issuer, expiry; no offline grace). No token, no network."""
    from copse.pro import auth, license

    try:
        tok = Path(path).read_text(encoding="utf-8").strip()
    except OSError as e:
        raise CIError(f"copse ci: cannot read the entitlement file {path}: {e.strerror}") from e
    try:
        ent = license.verify(tok, issuer=(client or auth.Client()).base,
                             now=time.time() if now is None else now, grace=0)
    except license.LicenseError as e:
        raise CIError(f"copse ci: the entitlement in {path} was refused: {e}. Write a fresh "
                      "one with `copse ci entitle --out FILE`.") from e
    return _needs_ci(ent)


def entitle(out: str | Path, client=None) -> Path:
    """``copse ci entitle``: exchange ``COPSE_PRO_TOKEN`` for the signed
    entitlement and write it to ``out`` (0600), for a later ``copse ci run
    --entitlement``. The CI token is only ever in this short process."""
    from copse.pro import auth, license

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise CIError(f"copse ci entitle: set {TOKEN_ENV} to a CI token from "
                      "`copse account org ci-token create`")
    try:
        tok, ent = _exchange(token, client)
    except auth.AirGapped as e:
        raise CIError(f"copse ci entitle: {e}; a CI token can't be exchanged offline") from e
    except auth.AuthError as e:
        raise CIError(f"copse ci entitle: {TOKEN_ENV} was refused ({e.code}); set it to a CI "
                      "token from `copse account org ci-token create`") from e
    except license.LicenseError as e:
        raise CIError(f"copse ci entitle: {e}") from e
    _needs_ci(ent)
    out = Path(out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.unlink(missing_ok=True)     # a fresh file, so the mode below applies
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(tok + "\n")
    except OSError as e:
        raise CIError(f"copse ci entitle: cannot write {out}: {e.strerror}") from e
    return out


def require_ci(client=None, entitlement_file: str | Path | None = None):
    """The entitlement, which must include ``ci``: from ``entitlement_file``
    when given (written earlier by ``copse ci entitle``), else from
    ``COPSE_PRO_TOKEN`` when set (memory only), else the stored copse Pro
    credentials."""
    from copse.pro import auth, license

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
        raise CIError(f"copse ci: {e}; a CI token can't be exchanged offline, so in air-gap "
                      f"mode unset {TOKEN_ENV} and install an offline license "
                      "(`copse account license install <file>`)") from e
    except auth.AuthError as e:
        raise CIError(f"copse ci: {TOKEN_ENV} was refused ({e.code}); set it to a CI token "
                      "from `copse account org ci-token create`") from e
    except license.LicenseError as e:
        raise CIError(f"copse ci: {e}. In CI, set {TOKEN_ENV} to a CI token from "
                      "`copse account org ci-token create`.") from e
    return _needs_ci(ent)


# -- secrets ----------------------------------------------------------------------

# Taken out of the environment before any agent starts: agents run the repo's
# own code (tests, scripts, whatever a prompt talks them into), so they must
# not inherit the org's CI token or a token that can push. copse's own push and
# `gh pr create` get them back explicitly. The model API key stays: the agents
# need it. This is defense in depth only: agents run as the same OS user as
# this process, so what it holds they can reach. The real boundary is
# `--bundle` + `copse ci publish`, where no write token is on the machine.
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
    from copse import tmux

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
    status: str                     # done | need_user | timeout | stalled | exited | error
    goal: Goal
    branch: str
    milestones: list[dict] = field(default_factory=list)
    note: str | None = None         # the question, the stall reason, the error
    pr_url: str | None = None
    elapsed: float = 0.0
    bundle: str | None = None       # with --bundle: the file `copse ci publish` takes

    @property
    def ok(self) -> bool:
        return self.status == "done" and (self.pr_url is not None or self.note is None)

    def summary(self) -> dict:
        return {
            "status": self.status, "ok": self.ok, "goal": self.goal.title,
            "issue": self.goal.issue, "branch": self.branch, "pr_url": self.pr_url,
            "bundle": self.bundle,
            "note": self.note, "elapsed_seconds": round(self.elapsed),
            "milestones": self.milestones,
        }

    def describe(self) -> str:
        lines = [f"copse ci: {self.status}: {self.goal.title}"]
        if self.milestones:
            done = sum(m["status"] == "passed" for m in self.milestones)
            lines.append(f"  {done} of {len(self.milestones)} milestones verified")
            for m in self.milestones:
                mark = {"passed": "✓", "failed": "✗"}.get(m["status"], "○")
                check = f" (check: {m['check']})" if m.get("check") else ""
                lines.append(f"  {mark} {m['title']}{check}")
        if self.note:
            lines.append(f"  {self.note}")
        if self.pr_url:
            lines.append(f"  pull request: {self.pr_url}")
        if self.bundle:
            lines.append(f"  bundle: {self.bundle} (publish it with `copse ci publish`)")
        return "\n".join(lines)


UNATTENDED = """[copse ci] This session runs unattended in CI: nobody is watching this chat. \
A question to the user (need_user) ends the run as a failure, so make the \
decisions you can yourself and prefer small, reviewable changes. When every \
milestone is verified, stop: copse opens the pull request from this branch.

Goal{where}: {title}
{detail}
{instruction}"""

DERIVE = ("Call set_goal now with this goal and the milestones you derive from it, each "
          "with a check command that verifies it (tests you add count), then drive it to "
          "completion: delegate, review, merge, check_milestone.")
RECORDED = ("The goal and its milestones are already recorded (get_progress shows them): "
            "drive them to completion: delegate, review, merge, check_milestone.")


def kickoff(goal: Goal, recorded: bool) -> str:
    where = f" (from issue #{goal.issue})" if goal.issue is not None else ""
    detail = f"\n{goal.detail}\n" if goal.detail and not recorded else ""
    return UNATTENDED.format(where=where, title=goal.title, detail=detail,
                             instruction=RECORDED if recorded else DERIVE)


def _checkout(db: DB, repo_path: str, branch: str, base: str | None) -> Workspace:
    """The worktree for ``branch``: a fresh one cut from ``base`` (default:
    the repo's base), or the existing one on a re-run."""
    repo_root = git.main_repo_root(repo_path)
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
    a fresh bare repo copse made (no hooks, no config of theirs) and pushed
    from there to ``remote``, the origin recorded before any agent started,
    with gh answering git's credential request from GH_TOKEN."""
    if not _repo_slug(remote):
        raise CIError("can't push: origin isn't a github.com repository")
    env = _github_env(secrets)
    ref = f"refs/heads/{ws.branch}"
    with tempfile.TemporaryDirectory(prefix="copse-push-") as tmp:
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


def _open_pr(slug: str, base: str, branch: str, title: str, body: str, env: dict[str, str]) -> str:
    # --repo, and not a worktree as cwd: gh would otherwise read the
    # agents' git config to decide where the pull request goes.
    with tempfile.TemporaryDirectory(prefix="copse-pr-") as tmp:
        proc = subprocess.run(
            ["gh", "pr", "create", "--repo", slug, "--base", base, "--head", branch,
             "--title", title, "--body", body],
            cwd=tmp, capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise CIError(f"gh pr create failed: {proc.stderr.strip() or proc.stdout.strip()}")
    url = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if not url:
        raise CIError("gh pr create printed no URL")
    return url


# -- the bundle: the run's result, handed to another trust domain -------------------

BUNDLE_STATUS = "done"
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


def write_bundle(ws: Workspace, base_ref: str, base_branch: str, path: str | Path,
                 title: str, body: str) -> Path:
    """Write the commits of ``base_ref..branch`` as a git bundle at ``path``,
    and ``path``.json saying what pull request they are for. This needs no
    token: `copse ci publish` pushes it, somewhere no agent ever ran."""
    path = Path(path).resolve()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise CIError(f"cannot write the bundle {path}: {e.strerror}") from e
    git.run(["-c", "core.hooksPath=/dev/null", "bundle", "create", str(path),
             f"refs/heads/{ws.branch}", f"^{base_ref}"], ws.path)
    meta = {"branch": ws.branch, "base": base_branch, "title": title,
            "body": workspaces.with_footer(body, ws.repo_root), "status": BUNDLE_STATUS}
    try:
        bundle_meta_path(path).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        raise CIError(f"cannot write {bundle_meta_path(path)}: {e.strerror}") from e
    return path


def read_bundle_meta(path: str | Path) -> dict:
    """``path``.json, checked: it was written on a machine where agents ran,
    so nothing in it is trusted. The branch must be a ``copse/ci-`` branch
    (a bundle can't be published over ``main``), and only a verified goal
    is published."""
    meta_path = bundle_meta_path(path)
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except OSError as e:
        raise CIError(f"cannot read {meta_path}: {e.strerror}") from e
    except ValueError as e:
        raise CIError(f"{meta_path} is not JSON") from e
    if not isinstance(meta, dict):
        raise CIError(f"{meta_path} is not a JSON object")
    if meta.get("status") != BUNDLE_STATUS:
        raise CIError(f"not publishing: the run's status is {meta.get('status')!r}, not "
                      f"{BUNDLE_STATUS!r}")
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
            "status": BUNDLE_STATUS}


def _default_branch(url: str, env: dict, cwd: str) -> str:
    """The repository's default branch, asked of the repository itself."""
    proc = subprocess.run(["git", "-c", "credential.helper=", "-c",
                           "credential.helper=!gh auth git-credential",
                           "ls-remote", "--symref", "--", url, "HEAD"],
                          capture_output=True, text=True, timeout=git.TIMEOUT, env=env, cwd=cwd)
    m = re.search(r"^ref: refs/heads/(\S+)\s+HEAD$", proc.stdout, re.M)
    if proc.returncode != 0 or not m or not _BRANCH_NAME.fullmatch(m.group(1)):
        raise CIError("copse ci publish: can't tell the repository's default branch; pass --base")
    return m.group(1)


_BRANCH_NAME = re.compile(r"[A-Za-z0-9._/-]{1,100}")


def publish(path: str | Path, repo: str | None = None, *, base: str | None = None,
            remote: str | None = None, environ=None) -> str:
    """``copse ci publish``: push the bundle's branch to github.com/``repo``
    and open the pull request; returns its URL.

    This is the only step that holds a token that can write, so it treats
    the bundle as data: it is verified and fetched into a fresh bare repo
    (no hooks, no config from the repo), only the one ``copse/ci-`` branch is
    taken from it, and nothing in it is checked out or run. ``remote``
    replaces the https://github.com URL; it exists for tests (a local bare
    "origin") and is deliberately not reachable from the CLI or any config."""
    environ = os.environ if environ is None else environ
    slug = (repo or environ.get("GITHUB_REPOSITORY") or "").strip()
    if not re.fullmatch(_SLUG, slug):
        raise CIError("copse ci publish: name the repository with --repo owner/name "
                      "(default: $GITHUB_REPOSITORY)")
    bundle = Path(path).resolve()
    if not bundle.is_file():
        raise CIError(f"copse ci publish: no bundle at {bundle}")
    meta = read_bundle_meta(bundle)
    url = remote or f"https://github.com/{slug}.git"
    ref = f"refs/heads/{meta['branch']}"
    env = dict(environ)
    if base is not None and (not _BRANCH_NAME.fullmatch(base) or ".." in base or base.startswith("-")):
        raise CIError("copse ci publish: --base is not a valid branch name")
    with tempfile.TemporaryDirectory(prefix="copse-publish-") as tmp:
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
                                          url, f"+refs/heads/{meta['base']}:refs/copse-base/head"]),
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
                raise CIError(f"copse ci publish: git {what}: {e}") from e
            if proc.returncode != 0:
                raise CIError(f"copse ci publish: git {what} failed: "
                              f"{proc.stderr.strip() or proc.stdout.strip()}")
    return _open_pr(slug, meta["base"], meta["branch"], meta["title"], meta["body"], env)


def set_max_workers(repo_root: str, n: int) -> Path:
    """Cap this repo's parallel workers through ``.copse/config.local.json``
    (gitignored; other keys are kept)."""
    from copse.config import set_local

    return set_local(repo_root, "max_agents", n)


def milestone_rows(db: DB, root_id: str) -> list[dict]:
    return [{"position": m.position, "title": m.title, "check": m.check_cmd, "status": m.status}
            for m in db.milestones(root_id)]


def pr_body(outcome: Outcome) -> str:
    g = outcome.goal
    lines = ["## Goal", "", g.title]
    if g.detail and not g.plan():
        lines += ["", g.detail]
    lines += ["", "## Milestones", ""]
    for m in outcome.milestones:
        box = "x" if m["status"] == "passed" else " "
        check = f" (`{m['check']}`)" if m.get("check") else ""
        lines.append(f"- [{box}] {m['title']}{check}")
    if g.issue is not None:
        lines += ["", f"Closes #{g.issue}"]
    lines += ["", "🤖 Opened by `copse ci`"]
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
        secrets: dict[str, str] | None = None, bundle: str | Path | None = None) -> Outcome:
    """Run ``goal`` to a verified end (or not) and, with ``pr``, open the pull
    request; with ``bundle``, write the branch to that file instead (nothing
    is pushed: ``copse ci publish`` does that elsewhere). The session is
    always stopped before this returns."""
    from copse import autopilot as pilot

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
    try:
        while True:
            found = _poll(db, root.id)
            if found:
                outcome.status, outcome.note = found
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
    if outcome.status == "done" and bundle is not None:
        try:
            outcome.bundle = str(write_bundle(ws, base_ref, base_branch, bundle, goal.title,
                                              pr_body(outcome)))
        except (git.GitError, CIError) as e:
            outcome.note = str(e)
    return outcome


def write_step_summary(outcome: Outcome, path: str | None = None) -> Path | None:
    """Append the JSON summary to ``$GITHUB_STEP_SUMMARY`` (or ``path``)."""
    path = path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return None
    text = (f"## copse ci: {outcome.status}\n\n```json\n"
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
            entitlement: str | None = None) -> int:
    """``copse ci run``: 0 on a verified goal (and its PR or bundle), 1 otherwise."""
    cwd = cwd or os.getcwd()
    try:
        g = resolve_goal(goal, goal_file, issue, cwd)
        if entitlement is not None:
            require_ci(entitlement_file=entitlement)
        else:
            require_ci()
        secrets = withhold_secrets()
        db = DB()
        # The bundle path is resolved now: the run changes nothing about where it goes.
        kw = {"bundle": Path(bundle).resolve()} if bundle is not None else {}
        outcome = run(db, cwd, g, timeout_min=timeout_min, max_workers=max_workers,
                      base=base, pr=pr and bundle is None, secrets=secrets, **kw)
    except (CIError, git.GitError, workspaces.WorkspaceError, agents.AgentError) as e:
        echo(str(e))
        return 1
    echo(outcome.describe())
    if outcome.pr_url:
        echo(outcome.pr_url)
    write_step_summary(outcome)
    return 0 if outcome.ok else 1


def publish_cli(path: str, repo: str | None = None, base: str | None = None, echo=print) -> int:
    """``copse ci publish``: 0 with the pull request's URL, 1 with what failed."""
    try:
        url = publish(path, repo, base=base)
    except CIError as e:
        echo(str(e))
        return 1
    echo(url)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"## copse ci: pull request\n\n{url}\n")
    return 0


# -- the workflow -----------------------------------------------------------------


WORKFLOW = """\
# Written by `copse ci init`. copse turns an issue labelled "{label}" into a
# pull request: https://pawdelta.com/copse/docs/commands
#
# Secrets: COPSE_PRO_TOKEN (an org CI token, cpc_..., from
# `copse account org ci-token create`) and ANTHROPIC_API_KEY (for Claude Code).
# In the repo's Actions settings, allow GitHub Actions to create pull requests.
#
# Two jobs, because agents run the repo's own code and can reach anything on
# their machine. `run` does the work with a read-only token and hands over a
# git bundle; `publish` holds the token that can push, and never checks out
# or runs anything from the repo.
#
# The issue body steers an unattended agent whose work becomes a pull request:
# only people you trust with write access should be able to apply the label.
# Pull requests opened with GITHUB_TOKEN don't trigger other workflows; to run
# your CI on them, set GH_TOKEN in the publish job to a GitHub App or personal
# access token.
name: copse

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
  group: copse-${{{{ github.event.issue.number || inputs.issue }}}}
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
      - name: Install copse
        run: uv tool install {package}
      - name: Entitlement
        env:
          COPSE_PRO_TOKEN: ${{{{ secrets.COPSE_PRO_TOKEN }}}}
        run: copse ci entitle --out "$RUNNER_TEMP/copse-entitlement/entitlement.jwt"
      # A short-lived signed entitlement, not the CI token; kept one day.
      - uses: actions/upload-artifact@v4
        with:
          name: copse-entitlement
          path: ${{{{ runner.temp }}}}/copse-entitlement/
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
      - name: Install copse
        run: uv tool install {package}
      - name: Install the agent CLI
        run: npm install -g @anthropic-ai/claude-code
      - name: Git identity for the agents' commits
        run: |
          git config --global user.name "copse[bot]"
          git config --global user.email "copse@users.noreply.github.com"
      - uses: actions/download-artifact@v4
        with:
          name: copse-entitlement
          path: ${{{{ runner.temp }}}}/copse-entitlement
      # No push token and no CI token here: GH_TOKEN can only read (the issue).
      # copse reads the entitlement and deletes it before any agent starts.
      - name: Run copse
        env:
          ANTHROPIC_API_KEY: ${{{{ secrets.ANTHROPIC_API_KEY }}}}
          GH_TOKEN: ${{{{ github.token }}}}
        run: copse ci run --issue ${{{{ github.event.issue.number || inputs.issue }}}} --timeout 100 --entitlement "$RUNNER_TEMP/copse-entitlement/entitlement.jwt" --bundle "$RUNNER_TEMP/copse-out/copse.bundle"
      - uses: actions/upload-artifact@v4
        with:
          name: copse-bundle
          path: ${{{{ runner.temp }}}}/copse-out/
          if-no-files-found: error
          retention-days: 1

  publish:
    needs: run
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      contents: write
      pull-requests: write
    steps:
      # No checkout: nothing from the repo runs in the job that can push.
      - uses: astral-sh/setup-uv@v10.2.0
      - name: Install copse
        run: uv tool install {package}
      - uses: actions/download-artifact@v4
        with:
          name: copse-bundle
          path: ${{{{ runner.temp }}}}/copse-out
      # The base comes from the repository, not from the run's bundle.
      - name: Open the pull request
        env:
          GH_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
        run: copse ci publish "$RUNNER_TEMP/copse-out/copse.bundle" --base "${{{{ github.event.repository.default_branch }}}}"
"""


def workflow_text(label: str = "copse") -> str:
    """The workflow, with copse pinned to the version writing it: the publish
    job holds a write token, so it must not take whatever is newest on PyPI."""
    from copse import __version__

    if not re.fullmatch(r"[A-Za-z0-9 _.:/-]{1,50}", label):
        raise CIError("the label may use letters, digits, spaces and _ . : / -")
    pinned = re.fullmatch(r"[0-9]+(\.[0-9]+)*([ab]|rc|\.post|\.dev)?[0-9]*", __version__ or "")
    return WORKFLOW.format(label=label, package=f"copse-ai=={__version__}" if pinned else "copse-ai")


def init(repo_root: str | Path, label: str = "copse", force: bool = False) -> Path:
    """Write ``.github/workflows/copse.yml``; refuses to overwrite without ``force``."""
    path = Path(repo_root) / WORKFLOW_PATH
    if path.exists() and not force:
        raise CIError(f"{path} exists; pass --force to overwrite it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(workflow_text(label), encoding="utf-8")
    return path

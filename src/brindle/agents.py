"""Agents: a CLI agent process in a tmux window inside a workspace.

Messaging uses an inbox: a message to a busy agent waits in its
inbox and is delivered the moment the agent goes idle. With hook-capable
providers, delivery happens inside the ``Stop`` hook itself (the hook tells
Claude Code to keep going with the message as its next instruction), so
nothing ever types into a terminal while the agent is mid-turn.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace

from brindle import bus, cloud_credential, company_identity, company_login, git, keystore, knowledge, pane_auth, providers, secrets, tmux, upgrade, workspaces
from brindle.assembler import DECISIONS, DOCUMENT_CHUNKS, OTHER_SESSIONS, TASK_AND_RULES, Block, assemble
from brindle.config import RepoConfig
from brindle.db import DB, Agent, Workspace
from brindle.model_availability import ModelAvailabilityChecker, PROVISIONING_TIMEOUT, provisioning_context
from brindle.pro import route_health
from brindle.profiles import load_profile, local_fallback, missing_add_dirs
from brindle.providers import LaunchContext, get_provider, unusable
from brindle.sidebar_follow import (
    SIDEBAR_COLUMNS,
    SIDEBAR_DISMISSED,
    SIDEBAR_TAG,
    _agent_for_window,
    _create_sidebar,
    _drop_dead_sidebar,
    _note_sidebar_move,
    _sidebar_lock,
    _sidebar_position,
    _valid_sidebar,
    bring_sidebar,
    root_of,
    sidebar_follow,
)

log = logging.getLogger(__name__)

WORKER_FOOTER = """

---
You are running as a brindle worker (agent id {agent_id}) on branch `{branch}`.
{guidance}
Run only the tests that cover your change (a test file or a `-k` filter), not
the full suite: brindle's merge gate runs the full suite on your branch.
Before you finish, check your work against the task and its finish line, item
by item: each behaviour it asks for needs a test that would fail without your
change, and a requirement with no such test isn't done yet. If the task turns
out too big for one branch, finish a coherent part and say exactly what's left
(and how you'd split it) rather than leaving every part half done.

When you have finished:
1. Commit your work to this branch with a clear message (do not push or merge).
2. Call the `report_result` tool from the `brindle` MCP server with a concise
   summary: what you changed, anything left undone, and anything the
   supervisor should check.
"""

REPORT_ONLY_FOOTER = """

---
You are running as a brindle worker (agent id {agent_id}) on branch `{branch}`.
{guidance}
You don't edit or commit anything: when you have finished, call the
`report_result` tool from the `brindle` MCP server with your findings.
"""

REVIEW_FOOTER = """

---
You are running as a brindle reviewer (agent id {agent_id}) on branch `{branch}`.
Don't edit files. When you have finished, call the `submit_review` tool from
the `brindle` MCP server: approved=true only if you'd merge it as is, with a
summary of your findings (most severe first, each with file:line and a fix).
"""


SUBAGENT_FOOTER = """

---
Work only in `{path}`: a git worktree on branch `{branch}` that is yours alone.
Your shell may start in another directory, so begin every Bash command with
`cd {path} && ` (or use `git -C {path}`), and give file tools absolute paths
under that directory. Don't change files anywhere else, and don't switch branches.
{guidance}
Before you finish, check your work against the task and its finish line, item
by item: each behaviour it asks for needs a test that would fail without your
change, and a requirement with no such test isn't done yet. If the task turns
out too big for one branch, finish a coherent part and say exactly what's left
(and how you'd split it) rather than leaving every part half done.

When you have finished:
1. Commit your work there on `{branch}` with a clear message (do not push or merge).
2. Don't call any brindle tools. End with a concise summary: what you changed,
   anything left undone, and anything the supervisor should check.
"""


SUPERVISOR = "supervisor"   # the only profile a launch starts


class AgentError(RuntimeError):
    pass


class StillRunning(AgentError):
    """The wait ended before the worker reported; it's still working."""


# Modes whose workers must finish with report_result. A handoff whose caller
# stopped waiting becomes "handoff_detached": its result is then forwarded to
# the caller as a message, like an assign.
REPORTING_MODES = ("handoff", "handoff_detached", "assign", "review")
FORWARDING_MODES = ("handoff_detached", "assign", "review")


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def test_guidance(checks: list[str]) -> str:
    """How a worker should test: the tests for its change as it goes, and
    the full suite once. Full-suite runs are the slowest thing a worker does,
    and several workers running them at once slow each other down."""
    targeted = ("Testing: while you work, run only the tests that cover your change (one "
                "test file, or a -k filter), not the whole suite.")
    if checks:
        shown = "; ".join(f"`{c}`" for c in checks)
        return (f"{targeted} Don't run the full suite yourself: brindle runs the repo's checks "
                f"({shown}) on your branch once, before it merges, and sends you any failures.")
    return f"{targeted} Run the full suite once, just before you commit."


PERMISSION_GUIDANCE = (
    "Permissions: commands are pre-approved by their first words, so run them plainly and "
    "one at a time from your own worktree (no `cd`, no `&&`, no pipes, no `VAR=x` prefixes). "
    "Never `cd` into or read from the main checkout ({root}): it isn't yours, and it "
    "pauses you for approval. This worktree is already your isolated copy: don't create "
    "or enter another one (no EnterWorktree, no `git worktree add`). Write files with your Edit and Write tools, never with "
    "shell heredocs or scripts, and don't write to /tmp.\n"
    "Waiting: to wait on something (your supervisor, a long command), wait for brindle's "
    "messages or use the Monitor tool; don't poll with short cron loops."
)


def is_review_profile(profile_name: str, repo_root: str) -> bool:
    """Whether ``profile_name`` is a reviewer (or extends one): it reads a
    branch and reports, so it has nothing to commit, test or merge."""
    name: str | None = profile_name
    seen: set[str] = set()
    while name and name not in seen:
        if name.startswith("reviewer"):
            return True
        seen.add(name)
        try:
            name = load_profile(name, repo_root).extends   # one step up the chain
        except Exception:  # noqa: BLE001 - an unknown profile is a builder as before
            return False
    return False


def worker_guidance(ws: Workspace, report_only: bool = False) -> str:
    """How a worker should find code (the code map, if any), test it, and
    stay within its pre-approved commands. A report-only worker gets no
    testing instructions."""
    from brindle import codemap
    from brindle.config import load_repo_config

    try:
        checks = load_repo_config(ws.repo_root, branch=ws.base_branch).checks
    except ValueError:
        checks = []
    parts = (codemap.guidance(ws), "" if report_only else test_guidance(checks),
             PERMISSION_GUIDANCE.format(root=ws.repo_root))
    return "\n".join(p for p in parts if p)


def managed(repo_root: str):
    """The org's managed-models settings for ``repo_root`` (Enterprise, see
    brindle.pro.managed_models), None when there are none. Raises
    ``AgentError`` when they apply but can't be read: no agent starts
    unmanaged then."""
    from brindle.pro import managed_models

    try:
        return managed_models.current(repo_root)
    except managed_models.ManagedUnavailable as e:
        raise AgentError(f"managed models: {e}") from e


_UNREAD = object()   # a ``managed`` argument not passed: read the org's settings now


def agent_env(ws: Workspace, agent_id: str, agent: Agent | None = None,
              m=_UNREAD) -> dict[str, str]:
    env = {**workspaces.workspace_env(ws), "BRINDLE_AGENT_ID": agent_id}
    if agent is not None:
        # The profile's ``env.NAME: value`` lines: how a Claude Code profile
        # points at another backend (ANTHROPIC_BASE_URL, ...), or any CLI at
        # a key it needs. Set before brindle's own variables, which win; an
        # org's managed provider (Enterprise) comes between the two.
        from brindle.pro import managed_models

        try:
            profile_env = load_profile(agent.profile, ws.repo_root).env
        except KeyError:
            profile_env = {}
        if m is _UNREAD:
            m = managed(ws.repo_root)
        # Claude Code's own managed settings (apiKeyHelper, a managed env) pick
        # the credential and backend: brindle adds no managed route env there.
        org = agent.provider == "claude" and providers.claude_org_managed()
        # The company's Claude Code env (`brindle login`, the org's agent_setup
        # without `enforce`): the lowest layer, so a profile's env lines and the
        # org's managed route (an enforced agent_setup, or provider_config) both
        # beat it; none at all under Claude Code's managed settings (stored_env).
        company = {} if agent.provider != "claude" else company_login.stored_env()
        if "AZURE_FEDERATED_TOKEN_FILE" in company:     # Azure has no hook: a launch freshens the file
            cloud_credential.freshen_azure(company["AZURE_FEDERATED_TOKEN_FILE"])
        managed_env = {} if org else managed_models.agent_env(m, agent.provider)
        enforced = None if agent.provider != "claude" or org else company_identity.enforced_route_env()
        session_creds = {}
        db_inst = None
        try:
            from brindle.db import DB
            db_inst = DB()
            root_id = agent.parent_id or agent_id if agent else agent_id
            curr_id = root_id
            while curr_id:
                ca = db_inst.get_agent(curr_id)
                if ca and ca.parent_id:
                    curr_id = ca.parent_id
                else:
                    break
            session_creds = db_inst.get_session_credentials_with_timestamps(curr_id or root_id)
        except Exception:
            pass
        finally:
            if db_inst is not None:
                db_inst.conn.close()

        from brindle import keystore, secrets
        guarded = _guarded_profile(agent, ws)
        org_managed = agent.provider == "claude" and bool(providers.claude_org_managed())
        deny = pane_auth.deny_names(agent.provider, getattr(guarded, "auth", "auto") if guarded is not None else "auto",
                                    m, org_managed)
        no_stored = set(deny) | (set(providers._ENV_AUTH["claude"]) if org_managed else set())

        creds = set() if org else secrets.agent_credentials(agent.provider, agent.profile, ws.repo_root)
        recovery_keys = {}
        for k in creds:
            if k in no_stored:
                continue
            sess_val, sess_time = session_creds.get(k, (None, 0.0))
            env_val = os.environ.get(k)
            if env_val and not secrets.is_job_secret(k) and agent and agent.status == "failed":
                recovery_keys[k] = env_val
            elif sess_val:
                try:
                    ks_val, ks_time = keystore.get_key_info(k)
                except Exception:
                    ks_val, ks_time = None, 0.0
                if ks_val and ks_time > sess_time:
                    recovery_keys[k] = ks_val
                else:
                    recovery_keys[k] = sess_val

        # The CLI's config home stored on the agent at spawn (provider_homes):
        # the lowest layer, so a profile's env lines still win. An agent with
        # none stored yet (older rows) gets what a launch would decide.
        from brindle import provider_homes

        homes = (provider_homes.env_for(agent.provider, agent.config_home) if agent.config_home is not None
                 else provider_homes.launch_env(agent.provider))
        if enforced is None:
            env = {**homes, **company, **recovery_keys, **profile_env, **managed_env, **env}
        else:
            # An enforced company identity (Enterprise): the pane runs on the route
            # launch_problem checked, whatever a profile's env lines say. The route
            # env is the top layer (managed_env is the same route's, from managed_models).
            profile_env = {k: v for k, v in profile_env.items() if company_identity.profile_may_set(k)}
            env = {**homes, **recovery_keys, **profile_env, **managed_env, **enforced, **env}
        if guarded is not None:
            from brindle import cloud_login

            env.update(cloud_login.profile_env(guarded, env))
    if agent is not None and preload_tools(agent, ws):
        # Claude Code defers MCP tools and loads them on demand, which costs a
        # worker an extra round trip at the moment it's told to report (and
        # some never get there). Workers and reviewers only have brindle's own
        # tools, so loading them all up front is cheap.
        env["ENABLE_TOOL_SEARCH"] = "false"
    # Agents run pytest outside brindle's check slots: cap their xdist workers
    # so a few of them don't each start one per core. Passed explicitly, since
    # a pane can inherit an older tmux environment; a profile's value still wins.
    env.setdefault("PYTEST_XDIST_AUTO_NUM_WORKERS", os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", "2"))
    return env


def preload_tools(agent: Agent, ws: Workspace) -> bool:
    """Whether to turn Claude Code's tool search off for this agent: the
    profile's ``tool_search`` setting, else yes for every non-interactive
    Claude agent (a chat may carry the person's own MCP servers, whose tools
    are better left deferred)."""
    if agent.provider != "claude":
        return False
    try:
        setting = load_profile(agent.profile, ws.repo_root).tool_search
    except KeyError:
        setting = None
    if setting is not None:
        return not setting
    return agent.mode != "interactive"


PLAN_FIRST_NOTE = """

Plan first: before you edit any file, read the code you need, then call the
`submit_plan` tool from the `brindle` MCP server with a short plan (the files
you'll change and how, and how you'll test it) and stop. Wait for your
supervisor's decision, which arrives as a message. Don't edit files until the
plan is approved; if it comes back with feedback, revise it and call
`submit_plan` again."""


def decorate_worker_prompt(task: str, agent_id: str, ws: Workspace, done_when: str | None,
                           provider, headless: bool, plan_first: bool = False,
                           decisions: str = "", chunks: str = "", report_only: bool = False) -> str:
    """The prompt actually sent to a handoff/assign worker's CLI: the raw
    ``task``, the ``decisions`` block that covers its files, its finish line,
    the WORKER_FOOTER reminder to report, and (for an interactive Claude
    worker with a finish line) the ``/goal`` wrapper. Used at spawn time, and
    again by ``resume`` to rebuild it (decisions from the brief's receipt)
    when a paused worker's CLI session can't be resumed and must restart fresh."""
    from brindle import autopilot as pilot

    prompt = assemble([Block(TASK_AND_RULES, task), Block(DECISIONS, decisions),
                       Block(DOCUMENT_CHUNKS, chunks)])
    if done_when:
        prompt += f"\n\nFinish line: {done_when.strip()}"
    if plan_first:
        prompt += PLAN_FIRST_NOTE
    footer = REPORT_ONLY_FOOTER if report_only else WORKER_FOOTER
    prompt += footer.format(agent_id=agent_id, branch=ws.branch,
                            guidance=worker_guidance(ws, report_only))
    if (done_when and provider.name == "claude" and not headless
            and not provider.hooks_disabled(ws.path)):
        # /goal is an interactive command that Claude Code refuses when hooks
        # are disabled (#133), and headless workers get the finish line above
        # and the Stop hook's reminder to report either way.
        prompt = pilot.worker_goal(prompt, done_when, ws.branch) or prompt
    return prompt


def spawn(
    db: DB,
    ws: Workspace,
    profile_name: str,
    *,
    prompt: str | None = None,
    provider_name: str | None = None,
    parent_id: str | None = None,
    mode: str = "interactive",
    watch_pane: bool = False,
    background_setup: bool = False,
    done_when: str | None = None,
    autopilot: bool = False,
    plan_first: bool = False,
    review_sha: str | None = None,
    claim_goals: bool = True,
    files: list[str] | None = None,
    weight: str | None = None,
) -> Agent:
    """Start an agent in ``ws``. ``weight`` is the weight its task was routed
    as, kept on the agent so the owner's per-kind effort and instructions apply
    from the first launch. Workers (handoff/assign) given a ``done_when``
    finish line run it as a Claude Code ``/goal``; their brief carries the
    accepted decisions covering ``files``. With ``autopilot``, the
    agent is a session root that drives toward a goal (see brindle.autopilot);
    with ``claim_goals`` it claims a hand-written ``.brindle/goals.md``."""
    from brindle import autopilot as pilot
    from brindle import identity_lock

    identity_lock.ensure_workspace(db, ws)   # no new agent in another identity's worktree
    profile = load_profile(profile_name, ws.repo_root)
    if (profile.name == "coordinator") != (mode == "coordinator"):
        # Only the sweep starts a coordinator (brindle.coordinator.wake).
        raise AgentError("the coordinator profile only runs as the repo's coordinator, which "
                         "brindle starts itself; pick another profile")
    hosted = local_fallback(profile, ws.repo_root)   # its local model isn't up
    if hosted:
        profile_name, profile = hosted, load_profile(hosted, ws.repo_root)
    provider = get_provider(provider_name or profile.provider)
    agent_id = new_id()
    # Headless is a Claude Code mode; other CLIs ignore the profile field.
    # brindle's own loop (native) has no TUI at all, so it always runs that way.
    headless = bool(profile.headless and provider.name == "claude") or provider.name == "native"

    # Stored as the agent's task: the raw text for a handoff/assign worker (so
    # a reviewer reading it later isn't given WORKER_FOOTER or the /goal
    # wrapper), but the fully decorated prompt for a subagent (that text IS
    # what's handed to the supervisor's own Agent tool) or a reviewer.
    raw_task = prompt
    if not provider.launches_process:
        if mode not in ("handoff", "assign"):
            raise AgentError(
                f"profile {profile.name!r} uses the {provider.name} provider, which runs in "
                "a supervisor's own Agent tool: use it through the brindle handoff or assign tools"
            )
        from brindle import guardrails
        from brindle.profiles import load_rule_packs, rules_prompt

        # A subagent never goes through _profile_for: its rule packs and
        # write scope join here (the scope is still checked on the diff).
        rules = "\n\n".join(p for p in (rules_prompt(load_rule_packs(profile, ws.repo_root)),
                                        guardrails.scope_prompt(guardrails.effective(profile))) if p)
        tuned = _with_overrides(profile, _weight_kind(weight), ws.repo_root)
        full = f"{tuned.prompt.strip()}\n\n{rules}".strip() if rules else tuned.prompt
        prompt = raw_task = subagent_prompt(full, prompt or "", ws, done_when)
    elif prompt and mode in ("handoff", "assign"):
        from brindle import codemap
        prompt = decorate_worker_prompt(prompt, agent_id, ws, done_when, provider, headless,
                                        plan_first=plan_first,
                                        report_only=is_review_profile(profile.name, ws.repo_root),
                                        decisions=knowledge.brief_decisions(db, ws.repo_root, agent_id, files),
                                        chunks=knowledge.brief_chunks(db, ws.repo_root, agent_id, prompt, files,
                                                                      codemap.graph_path(ws)))
    elif prompt and mode == "review":
        prompt = raw_task = prompt + REVIEW_FOOTER.format(agent_id=agent_id, branch=ws.branch)

    from brindle import preflight, provider_homes

    off = preflight.turned_off(provider.name)     # before anything (deciding its home) runs the CLI
    if off:
        raise PreflightBlocked([off])
    agent = Agent(
        id=agent_id, workspace_id=ws.id, profile=profile.name, provider=provider.name,
        parent_id=parent_id, mode=mode, status="starting", tmux_window="",
        result=None, created_at=time.time(), task=raw_task, headless=int(headless) or None,
        done_when=done_when, review_sha=review_sha,
        # Its CLI's config home, decided once here: every later check, its
        # pane and a resume use this one, whatever a sign-in changes meanwhile.
        config_home=provider_homes.decide(provider.name),
        weight=_weight_kind(weight) if mode in ("handoff", "assign") else None,
    )
    with provider_homes.using(agent.provider, agent.config_home):
        why = model_problem(profile_name, ws, agent=agent)
    if why:
        raise AgentError(why)
    db.add_agent(agent)
    if agent.weight:
        db.update_agent(agent_id, weight=agent.weight)
    if plan_first and provider.launches_process and mode in ("handoff", "assign"):
        db.update_agent(agent_id, plan_first=1)
        agent.plan_first = 1
    if autopilot:
        plan = pilot.enable(db, agent_id, ws, claim=claim_goals)
        if plan and not prompt:
            prompt = agent.task = pilot.kickoff(plan, agent_id)
            db.update_agent(agent_id, task=prompt)
    if mode == "interactive":
        from brindle import codemap
        codemap.seed_session(ws.path, ws.repo_root)
    try:
        _launch(db, agent, ws, prompt=prompt, resume=None, watch_pane=watch_pane,
                background_setup=background_setup, model_checked=True)
    except Exception:
        db.delete_agent(agent_id)
        forget_files(agent)
        raise
    return agent


def _pause_when_done(agent_id: str, argv: list[str]) -> list[str]:
    """Wrap an interactive agent's command so that, when it exits for any
    reason, brindle pauses its session from inside the same pane. This doesn't
    rely on tmux hooks, which some tmux builds don't fire reliably. The
    wrapper survives Ctrl-C (a trap *handler* resets in the child, so the CLI
    keeps its normal Ctrl-C behaviour), and errors go to hooks.log instead of
    vanishing."""
    from brindle.config import brindle_home
    from brindle.providers import brindle_invocation

    log = shlex.quote(str(brindle_home() / "hooks.log"))
    ended = " ".join(shlex.quote(a) for a in [*brindle_invocation(), "_ended", agent_id])
    script = f'trap : INT; "$@"; code=$?; {ended} >>{log} 2>&1; exit $code'
    return ["/bin/sh", "-c", script, "brindle-agent", *argv]


def _guarded_profile(agent: Agent, ws: Workspace | None, load=None):
    """``agent``'s profile with its guardrails as they apply (cleared, with a
    warning, without the Pro feature: see guardrails.effective); None when
    it can't be loaded. ``load``: what loads it (default load_profile)."""
    from brindle import guardrails
    from brindle.profiles import ProfileError

    try:
        return guardrails.effective(load() if load else load_profile(agent.profile, ws.repo_root if ws else None))
    except (KeyError, ProfileError):
        return None


def supervisor_choice(repo_root: str):
    """The profile a new supervisor chat takes its provider, model and effort
    from: the first of the owner's ``supervisor_profile`` and
    ``supervisor_fallbacks`` that exists, the org's policy allows and can run
    here; None when none is set or none can (the supervisor profile's own then)."""
    from brindle.config import load_repo_config
    from brindle.profiles import ProfileError
    from brindle.service.routing import RoutingPolicy

    cfg = load_repo_config(repo_root)
    policy = RoutingPolicy.load(repo_root)
    for name in [cfg.supervisor_profile, *cfg.supervisor_fallbacks]:
        try:
            p = load_profile(name, repo_root) if name else None
        except (KeyError, ProfileError):
            continue
        if p is not None and policy.refusal(p) is None and unusable(p.provider, p.env) is None:
            return p
    return None


def _with_supervisor_choice(profile, provider: str, repo_root: str):
    """``profile`` (the supervisor's) running on the owner's choice's provider,
    model and effort, when this chat was launched on that choice's provider
    (a provider asked for by name keeps the profile's own)."""
    from dataclasses import replace

    choice = supervisor_choice(repo_root)
    if choice is None or choice.provider != provider:
        return profile
    return replace(profile, provider=choice.provider, model=choice.model, effort=choice.effort)


def _with_kind_overrides(db: DB, profile, agent: Agent, repo_root: str):
    """``profile`` with the effort and instructions the owner set for the kind
    of work ``agent`` does (its task's weight, a review, or the supervisor chat)."""
    from brindle import learning

    if agent.mode == "review":
        kind = "reviewer"
    elif agent.profile == SUPERVISOR and agent.mode == "interactive":
        kind = "supervisor"
    else:
        kind = _weight_kind(agent.weight or learning._task_files(db, agent, repo_root)[1])
    return _with_overrides(profile, kind, repo_root)


def _weight_kind(weight: str | None) -> str | None:
    """The routing kind of a task weight (an alias resolved), None when it isn't one."""
    from brindle.config import WEIGHTS, normalize_weight

    kind = normalize_weight(weight)
    return kind if kind in WEIGHTS else None


def _with_overrides(profile, kind: str | None, repo_root: str):
    from dataclasses import replace

    from brindle.config import load_repo_config

    tuned = load_repo_config(repo_root).routing_overrides.get(kind, {}) if kind else {}
    if effort := tuned.get("effort"):
        profile = replace(profile, effort=effort)
    if text := tuned.get("instructions"):
        profile = replace(profile, prompt=f"{profile.prompt.strip()}\n\n{text.strip()}".strip())
    return profile


def _profile_for(db: DB, agent: Agent, ws: Workspace, m=_UNREAD):
    """``agent``'s profile as launched: autopilot sessions add their guide, a
    supervisor gets the rules for acting on findings, the person's standing
    ``rules`` and the repo's delegation rule, and a chat (a supervisor)
    learns about the code map, if there is one.
    Workers get the code map in their task instead (see worker_guidance)."""
    from dataclasses import replace

    from brindle import autopilot as pilot
    from brindle.config import load_repo_config

    from brindle import guardrails
    from brindle.profiles import load_rule_packs, rules_prompt

    profile = guardrails.effective(load_profile(agent.profile, ws.repo_root))
    if agent.profile == SUPERVISOR and agent.mode == "interactive":
        profile = _with_supervisor_choice(profile, agent.provider, ws.repo_root)
    profile = _with_kind_overrides(db, profile, agent, ws.repo_root)
    # The profile's rule packs: their text joins the prompt here, for every
    # provider; their mechanical rules are checked by brindle.rule_checks.
    # So do its guardrails (write and read scope), which that check enforces.
    rules = assemble([Block(TASK_AND_RULES, rules_prompt(load_rule_packs(profile, ws.repo_root))),
                      Block(TASK_AND_RULES, guardrails.scope_prompt(profile))])
    if rules:
        profile = replace(profile, prompt=f"{profile.prompt.strip()}\n\n{rules}".strip())
    cfg = load_repo_config(ws.repo_root)
    autopilot_on = db.get_autopilot(agent.id) is not None
    if autopilot_on:
        profile = replace(profile, prompt=profile.prompt + pilot.guide(cfg, ws.repo_root))
    if agent.mode == "interactive" and (autopilot_on or agent.profile == "supervisor"):
        profile = replace(profile, prompt="\n\n".join(
            p for p in (profile.prompt.strip(), pilot.supervisor_rules(cfg), pilot.delegation_rule(cfg)) if p))
    if agent.mode == "interactive":
        from brindle import codemap, repos

        repo_map = codemap.supervisor_map(ws) if autopilot_on or agent.profile == "supervisor" else None
        extra = assemble([Block(TASK_AND_RULES, (text or "").rstrip()) for text in (
            codemap.guidance(ws), repo_map, repos.guidance(db, agent.id))]   # attached repos: a session root's
            + [Block(OTHER_SESSIONS, knowledge.other_sessions_block(db, knowledge.repo_key(ws.repo_root), agent.id))])
        if extra:
            profile = replace(profile, prompt=f"{profile.prompt}\n\n{extra}".strip())
    if agent.headless:
        profile = replace(profile, headless=True)
    # An org's managed models (Enterprise): a profile pointing elsewhere is
    # refused, the rest run against the managed provider.
    from brindle.pro import managed_models

    if m is _UNREAD:
        m = managed(ws.repo_root)
    if m is not None:
        why = managed_models.refusal(m, profile)
        if why:
            raise AgentError(f"managed models: {why}")
        profile = managed_models.apply_profile(m, profile)
    return profile


def _open_window(db: DB, agent: Agent, ws: Workspace, name: str, argv: list[str],
                 watch_pane: bool, *, m=_UNREAD) -> str:
    """Run ``argv`` for ``agent`` in a new window of ``ws``'s tmux session.
    ``m`` is the launch's managed-models settings (``managed``) when it has
    already read them; left out, they're read here (fail closed)."""
    if agent.mode == "interactive":
        argv = _pause_when_done(agent.id, argv)
    from brindle import inbox

    argv = inbox.without_launcher_inbox(argv)
    # The sidebar splits the window after the CLI's pane exists: hold the CLI
    # until then, or a resumed chat drew its conversation at the full width
    # and was resized under it, which Claude Code shows as a blank screen (#264).
    gate = None
    if watch_pane:
        argv, gate = tmux.gated(argv)
    tmux.ensure_session(ws.tmux_session, ws.path, workspaces.workspace_env(ws))
    try:
        env, credentials, allow, deny, org_env = _window_environment(agent, ws, m)
        with secrets.org_key(org_env):
            target = tmux.new_window(ws.tmux_session, name, ws.path, argv, env,
                                     tag=(AGENT_TAG, agent.id),
                                     keep=credentials,
                                     **({"allow": allow} if allow is not None else {}),
                                     **({"deny": deny} if deny else {}))
        agent.__dict__.pop("_pane_environment", None)
        # The pane id only means anything on the server that issued it (a
        # resumed agent may come back on a different one), so record both.
        server = tmux.current_server()
        db.update_agent(agent.id, tmux_window=target, tmux_server=server)
        agent.tmux_window, agent.tmux_server = target, server
        if get_provider(agent.provider).startup_prompts and not agent.headless:
            from brindle import prompt_answer

            prompt_answer.launched(db, agent)
        if watch_pane:
            try:
                _ensure_sidebar(db, root_of(db, agent.id), ws, target)
            except Exception:
                pass
    finally:
        if gate:
            tmux.open_gate(gate)
    tmux.apply_theme(ws.tmux_session)
    return target


def _window_environment(agent: Agent, ws: Workspace, m=_UNREAD):
    if m is _UNREAD:
        m = managed(ws.repo_root)
    cached = getattr(agent, "_pane_environment", None)
    if cached is not None and cached[0] == m:
        return cached[1]
    # A profile's env_allow (brindle Pro guardrails): the pane starts with
    # only those variables, brindle's own and the provider's sign-in.
    guarded = _guarded_profile(agent, ws)
    allow = guarded.env_allow if guarded is not None else None
    # An org's deny_personal_keys (Enterprise): the person's own keys never
    # reach the pane, kept or not.
    env = agent_env(ws, agent.id, agent, m)
    credentials = secrets.agent_credentials(agent.provider, agent.profile, ws.repo_root)
    # Keys stored with `brindle keys` reach this window's env only (never the
    # tmux server's), unless the environment already has the name or an org's
    # deny_personal_keys keeps it out. deny/allow strip it as they do an exported key.
    # The profile's `auth` (subscription strips the provider's keys) and Claude
    # Code's managed settings (a personal key mustn't bypass them) add to the
    # org's denied keys; the org's own key (key_env / key_helper) is set last and
    # passes the deny (secrets.org_key), a personal key under its name doesn't.
    from brindle.pro import managed_models

    org_managed = agent.provider == "claude" and bool(providers.claude_org_managed())
    deny = pane_auth.deny_names(agent.provider, guarded.auth if guarded is not None else "auto",
                                m, org_managed)
    try:
        org_env = managed_models.org_key_env(m, agent.provider)
    except managed_models.ManagedUnavailable as e:
        raise AgentError(f"managed models: {e}") from None
    # An enforced company identity: also every account picker the tmux server
    # would hand the pane or `brindle keys` holds for it (pane_auth covers this process's).
    route = None if agent.provider != "claude" or org_managed else company_identity.enforced_route()
    if route is not None:
        extra = company_identity.enforced_deny(route[0], tmux.inherited_names(ws.tmux_session), credentials,
                                               own=route[1])
        deny = (*deny, *(n for n in extra if n not in deny))
    # Claude Code's managed settings supply the credential: no stored Claude key.
    no_stored = set(deny) | (set(providers._ENV_AUTH["claude"]) if org_managed else set())
    env = {**keystore.pane_keys(credentials - no_stored, {**env, **org_env}), **env, **org_env}
    result = env, credentials, allow, deny, org_env
    # Resolve stored/org keys once for this launch: catalog, health identity
    # and pane must use the same key helper result, without repeated delays.
    agent._pane_environment = m, result
    return result


def _effective_window_env(agent: Agent, ws: Workspace, m=_UNREAD) -> dict[str, str]:
    env, keep, allow, deny, org_env = _window_environment(agent, ws, m)
    inherited = tmux.inherited_env(ws.tmux_session)
    with secrets.org_key(org_env):
        env = {**env, **secrets.proxy_exempt(env)}
        unset = secrets.pane_unset(inherited, keep=(*keep, *env), allow=allow,
                                   deny=secrets.pane_deny(deny, env))
    return {k: v for k, v in {**inherited, **env}.items() if k not in unset}


def model_problem(profile_name: str, ws: Workspace, *, agent: Agent | None = None,
                  profile=None, m=_UNREAD) -> str | None:
    """Read-only provisioning/routing gate, using the worker's actual identity."""
    from dataclasses import replace
    from brindle import guardrails, model_access
    from brindle.pro import managed_models
    from brindle.launch_health import account_revision, fingerprint

    if m is _UNREAD:
        m = managed(ws.repo_root)
    if profile is None:
        profile = guardrails.effective(load_profile(profile_name, ws.repo_root))
        if profile_name == SUPERVISOR and agent is not None and agent.mode == "interactive":
            profile = _with_supervisor_choice(profile, agent.provider, ws.repo_root)
        why = managed_models.refusal(m, profile) if m is not None else None
        if why:
            route_health.record(ws.repo_root, agent=profile.provider,
                                route=route_health.route_of(profile.provider, managed_models.agent_env(m, profile.provider), m),
                                model=profile.model, outcome="policy_refused")
            return f"managed models: {why}"
        if m is not None:
            profile = managed_models.apply_profile(m, profile)
    _airgap_check(profile, ws.repo_root)
    if not get_provider(profile.provider).launches_process:
        return None
    agent = agent or Agent("preflight", ws.id, profile_name, profile.provider, None,
                           "assign", "starting", "", None, time.time())
    effective = _effective_window_env(agent, ws, m)
    profile = replace(profile, provider=agent.provider)
    denied = model_access.refused(profile_name, ws.repo_root, fp=fingerprint(profile, effective))
    if denied:
        return denied.line()
    result = _model_checker.check(profile, effective_env=effective, cwd=ws.path,
                                  account_identity=account_revision(profile.provider, effective),
                                  catalog_context=provisioning_context(profile.provider, ws.path, effective),
                                  timeout=PROVISIONING_TIMEOUT)
    if result.status != "unavailable":
        return None
    route_health.record(ws.repo_root, agent=profile.provider,
                        route=route_health.route_of(profile.provider, effective, m),
                        model=profile.model, outcome="model_unavailable")
    return result.explanation


def route_context(db: DB, agent: Agent, ws: Workspace) -> tuple[str, str | None]:
    """(the route ``agent``'s calls take, its model) for a route-health event
    about a run; (``"unknown"``, None) when they can't be read."""
    try:
        m = managed(ws.repo_root)
        profile = _profile_for(db, agent, ws, m)
        env = _effective_window_env(agent, ws, m)
        return route_health.route_of(agent.provider, env, m), profile.model
    except Exception:  # noqa: BLE001 - a report about a run never fails over its context
        return "unknown", None


_model_checker = ModelAvailabilityChecker()


# Set on every agent pane at creation (see _open_window), so a pane can say
# whose it is: pane ids are a per-server counter that restarts at 0 when the
# tmux server does, and a stored id alone can't tell an agent's own pane from
# a newer session's that happens to have the same id (see pane_owners).
AGENT_TAG = tmux.AGENT_TAG


# How long _ensure_sidebar waits for another process's sidebar work.
SIDEBAR_LOCK_TIMEOUT = 5.0


def _ensure_sidebar(db: DB, root_id: str, ws: Workspace, target_pane: str) -> None:
    """Make sure ``root_id``'s one `brindle watch --sidebar` pane is beside
    ``target_pane``: move it there if it already exists elsewhere (never
    start a second one -- that would double the dashboard's 2s polling), or
    create it fresh if it's dead, stale (see _valid_sidebar), was dismissed
    (a launch or relaunch brings it back) or has never run."""
    with _sidebar_lock(root_id, timeout=SIDEBAR_LOCK_TIMEOUT):
        existing = db.get_sidebar_pane(root_id)
        if _valid_sidebar(existing, root_id) and not _drop_dead_sidebar(existing):
            assert existing is not None
            if tmux.pane_window(existing) != tmux.pane_window(target_pane):
                _note_sidebar_move("launch", existing, target_pane)
                tmux.move_pane(existing, target_pane, SIDEBAR_COLUMNS, _sidebar_position(ws))
            return
        _create_sidebar(db, root_id, ws, target_pane)


def sidebar_root(pane: str | None) -> str | None:
    """The session root whose sidebar is ``pane``, if it's a brindle sidebar."""
    return tmux.get_pane_tag(pane, SIDEBAR_TAG) if pane else None


def quit_later(root_id: str) -> None:
    """Quit a brindle session from its own sidebar: pause it, as ending the
    chat does, which closes its tmux session and hands the person their
    prompt back. Runs detached, since pausing closes the sidebar asking."""
    from brindle.providers import brindle_invocation

    subprocess.Popen([*brindle_invocation(), "_quit", root_id], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def dismiss_sidebar(db: DB, pane: str | None) -> None:
    """The person quit the sidebar in ``pane`` (`brindle watch --sidebar`
    returned normally): remember that, so sidebar_follow doesn't bring it
    back on the next window switch. Only if ``pane`` is still its root's
    recorded sidebar; a no-op for any other `brindle watch`."""
    if not pane:
        return
    root_id = tmux.get_pane_tag(pane, SIDEBAR_TAG)
    if not root_id:
        return
    with _sidebar_lock(root_id):
        if db.get_sidebar_pane(root_id) == pane:
            db.set_sidebar_pane(root_id, SIDEBAR_DISMISSED)


def sidebar_come_home(db: DB, root_id: str, pane: str | None) -> bool:
    """The sidebar's own safety net for the follow hooks, run from its
    refresh loop: if it sits in a session nobody is attached to (it followed
    the person into a worker's session and a hook was missed on the way
    back, or failed, since hooks fail silently) while the person is attached
    to its root's session or one of its workers', move it there. True when
    it moved."""
    if not pane:
        return False
    here = tmux.pane_session(pane)
    if not here or tmux.session_attached(here):
        return False
    root = db.get_agent(root_id)
    if root is None or root.status == "paused":
        return False
    sessions = []
    root_ws = db.get_workspace(root.workspace_id)
    if root_ws:
        sessions.append(root_ws.tmux_session)
    for a in db.list_agents():
        if a.id == root_id or root_of(db, a.id) != root_id:
            continue
        ws = db.get_workspace(a.workspace_id)
        if ws and ws.tmux_session not in sessions:
            sessions.append(ws.tmux_session)
    for name in sessions:
        if name != here and tmux.session_attached(name):
            _note_sidebar_move(f"come home from {here}", pane, pane)
            # Straight into the attached session, not through sidebar_follow's
            # window-to-agent lookup, which can come up empty and leave it stranded.
            bring_sidebar(db, root_id, name)
            return tmux.pane_session(pane) == name
    return False


def _airgap_check(profile, repo_root: str) -> None:
    """Raise ``AgentError`` for a profile air-gap mode refuses (a hosted
    provider, or a model endpoint off this machine and the private network).
    Every launch passes here, so no path -- a worker, a reviewer, a subagent,
    the chat, a resume -- can start a hosted agent while air-gap mode is on."""
    from brindle import airgap
    from brindle.config import load_repo_config

    try:
        cfg = load_repo_config(repo_root)
    except ValueError as e:
        # A broken config might be the one that says "airgap": true; refuse
        # rather than guess (fail closed).
        raise AgentError(f"can't read this repo's brindle config, so air-gap mode can't be "
                         f"ruled out: {e}") from e
    if not airgap.enabled(cfg):
        return
    ok, why = airgap.profile_allowed(profile)
    if not ok:
        raise AgentError(f"air-gap mode: profile {profile.name!r} is refused: {why}")


def _add_dirs_warning(profile, provider_name: str) -> str | None:
    missing = missing_add_dirs(profile) if provider_name == "claude" else []
    if not missing:
        return None
    return (f"add_dirs names {', '.join(missing)}, which do not exist; "
            "Claude Code will ignore them")


def add_dirs_warning(agent: Agent, ws: Workspace) -> str | None:
    """A warning if ``agent`` was launched with ``add_dirs`` that do not exist,
    or None. Claude Code ignores those silently, so whoever started the agent
    has to be told: the supervisor, whose only view of a launch is the reply."""
    return _add_dirs_warning(load_profile(agent.profile, ws.repo_root), agent.provider)


class PreflightBlocked(AgentError):
    """A launch its CLI would stop on before the first turn: ``items`` are
    brindle.preflight items, each naming what resolves it."""

    def __init__(self, items: list) -> None:
        self.items = items
        super().__init__("; ".join(i.detail for i in items))


def launch_identity(agent: Agent, ws: Workspace, profile=None, m=_UNREAD) -> tuple[dict[str, str], bool, tuple]:
    """Who ``agent``'s CLI signs in as on this launch: the environment it gets
    (its profile's env lines, the managed provider's variables, the
    credentials brindle hands it, and the keys its auth or the org denies,
    blanked so the launcher's own copy doesn't count), whether the org's own
    key signs it in (no login to check), and the denied names. ``agent`` need
    not be recorded yet: a launch about to happen is checked the same way."""
    from brindle import cloud_login
    from brindle.pro import managed_models

    if m is _UNREAD:
        m = managed(ws.repo_root)
    profile = profile or _guarded_profile(agent, ws)
    if profile is None:
        raise AgentError(f"no profile {agent.profile!r}")
    launch_env = {**profile.env, **managed_models.agent_env(m, agent.provider)}
    deny = pane_auth.deny_names(agent.provider, profile.auth, m,
                                agent.provider == "claude" and bool(providers.claude_org_managed()))
    org_key = managed_models.has_org_key(m, agent.provider)
    prov_creds = set() if org_key else secrets.agent_credentials(agent.provider, profile.name, ws.repo_root)
    eff_env = _window_environment(agent, ws, m)[0]
    names = prov_creds | (cloud_login.IDENTITY_ENV if agent.provider in ("claude", "opencode") else frozenset())
    probe_env = {**launch_env, **{k: eff_env[k] for k in names if k in eff_env}}
    return {**probe_env, **{k: "" for k in secrets.pane_deny(deny, probe_env)}}, org_key, deny


def launch_blockers(agent: Agent, ws: Workspace, profile=None, m=_UNREAD, *, identity=None,
                    keys_only: bool = False) -> list:
    """What would stop ``agent``'s CLI before its first turn if it launched
    now in ``ws`` (brindle.preflight), judged with the identity it runs as.
    ``keys_only``: when nobody is starting anything yet (a read), the CLI's
    sign-in check sees only the credential names of that identity, never
    the rest of a repo profile's env lines (NODE_OPTIONS, PATH, a config
    dir), which only a launch the person starts may run with."""
    from brindle import preflight

    if agent.provider not in preflight.PROVIDERS:
        return []
    profile = profile or _guarded_profile(agent, ws)
    if profile is None:
        return []
    if agent.provider == "native":
        return preflight.check("native", ws.repo_root, profile=profile)
    env, org_key, _ = identity or launch_identity(agent, ws, profile, m)
    if keys_only:
        # brindle's own list of the CLI's credentials: never names a repo declares.
        env = {k: v for k, v in env.items() if k in providers._ENV_AUTH.get(agent.provider, ())}
    launch = {"repo": ws.repo_root, "profile": agent.profile}
    session = credentials_session(agent)
    if session:
        launch["session"] = session
    return preflight.check(agent.provider, ws.repo_root, env=env, signed_in=org_key, launch=launch,
                           check_cloud=not keys_only)


def credentials_session(agent: Agent) -> str | None:
    """The root session whose stored credentials ``agent``'s launch gets (see
    agent_env), when it has any: a consent asked for that launch names it, so
    recording it resolves the same key."""
    from brindle.db import DB

    db = DB()
    try:
        current = agent.parent_id or agent.id
        while current:
            a = db.get_agent(current)
            if a is None or not a.parent_id:
                break
            current = a.parent_id
        return current if current and db.get_session_credentials(current) else None
    finally:
        db.conn.close()


def consent_identity(db: DB, repo: str, profile: str, provider: str, session: str | None) -> dict[str, str]:
    """The environment a consent's launch signs in with, rebuilt from what
    its action names: ``repo``, ``profile``, and ``session`` (a recorded root
    session whose stored credentials it gets; ``repo`` is its own repo or one
    attached to it, as a worker's launch there would be)."""
    from brindle import repos

    parent = None
    if session is None:
        ws = workspaces.adopt_root(db, repo)
    else:
        root = db.get_agent(session)
        own = repos.own_root(db, session) if root and not root.parent_id else None
        try:
            target = repos.resolve(db, session, own, repo) if own else None
        except repos.RepoError:
            target = None
        if target is None:
            raise AgentError(f"no session {session!r} in {repo}")
        ws, parent = repos.target_workspace(db, target), root.id
    who = Agent("", ws.id, profile, provider, parent, "assign" if parent else "interactive", "starting", "",
                None, 0.0)
    return launch_identity(who, ws)[0]


def _launch(db: DB, agent: Agent, ws: Workspace, *, prompt: str | None,
            resume: str | None, watch_pane: bool, background_setup: bool = False,
            model_checked: bool = False) -> None:
    """Start (or restart) ``agent``'s CLI in a new tmux window of ``ws``.
    In air-gap mode (``brindle.airgap``) only a local profile is launched,
    whatever the mode: a worker, a reviewer, a subagent or the chat itself.
    The CLI's config home is the one stored on the agent at spawn
    (provider_homes.decide): its identity check, preflight, pane and every
    resume use it, even after a sign-in inside brindle. An agent recorded by
    an older brindle has none yet: it is decided and stored here, once."""
    try:
        upgrade.check_launch()
    except upgrade.StaleInstall as e:
        raise AgentError(str(e)) from e
    from brindle import preflight, provider_homes

    # A provider the person turned off in brindle never starts, and nothing
    # below (deciding its home, model check, identity probe) runs its CLI either.
    off = preflight.turned_off(agent.provider)
    if off:
        raise PreflightBlocked([off])
    if agent.config_home is None:
        agent.config_home = provider_homes.decide(agent.provider)
        if agent.config_home is not None:
            db.update_agent(agent.id, config_home=agent.config_home)
    with provider_homes.using(agent.provider, agent.config_home):
        _launch_resolved(db, agent, ws, prompt=prompt, resume=resume, watch_pane=watch_pane,
                         background_setup=background_setup, model_checked=model_checked)


def _launch_resolved(db: DB, agent: Agent, ws: Workspace, *, prompt: str | None,
                     resume: str | None, watch_pane: bool, background_setup: bool = False,
                     model_checked: bool = False) -> None:
    m = managed(ws.repo_root)   # read once for this launch
    profile = _profile_for(db, agent, ws, m)
    _airgap_check(profile, ws.repo_root)
    provider = get_provider(agent.provider)
    if not model_checked:
        why = model_problem(agent.profile, ws, agent=agent, profile=profile, m=m)
        if why:
            raise AgentError(why)
    if not provider.launches_process:
        # Nothing to start: the caller's own subagent does the work.
        status = "done" if agent.result is not None else "processing"
        db.set_status(agent.id, status)
        agent.status = status
        return
    # An org that enforces its company sign-in (Enterprise) refuses a
    # different identity; one that can't be told never blocks.
    from brindle import provider_homes

    why, outcome, route = company_identity.launch_check(
        provider.name, home=provider_homes.env_for(agent.provider, agent.config_home))
    if outcome:
        route_health.record(ws.repo_root, agent=provider.name, route=route, model=profile.model,
                            outcome=outcome)
    if why:
        raise AgentError(why)
    from brindle import signin_pause

    signin_pause.record_launch(agent)   # who it runs as, so a resume never crosses accounts
    env, org_key, deny = launch_identity(agent, ws, profile, m)
    why = pane_auth.key_problem(profile, provider.name, deny, org_key)
    if why:
        raise AgentError(why)
    # What its CLI would stop on before the first turn (brindle.preflight),
    # with the identity it runs as. A person starting their own chat from a
    # terminal answers its first-run screens there; nobody watches a worker's.
    blocking = launch_blockers(agent, ws, profile, m, identity=(env, org_key, deny))
    # Installing a CLI or tmux shows when the window opens, as before.
    blocking = [b for b in blocking if b.kind != "setup"]
    if agent.parent_id is None and agent.mode == "interactive":
        blocking = [b for b in blocking if b.kind not in ("consent", "trust")]
    if blocking:
        raise PreflightBlocked(blocking)
    # Here rather than in spawn, so a resume checks too: a directory can be
    # deleted between the first launch and a `brindle continue`. This reaches a
    # person running brindle in a terminal; a launch from the MCP server has no
    # one reading its stderr, so handoff, assign and a queued task's start put
    # the same text in what they tell the supervisor (add_dirs_warning).
    warning = _add_dirs_warning(profile, provider.name)
    if warning:
        print(f"brindle: {warning}", file=sys.stderr)
    if agent.headless:
        _launch_headless(db, agent, ws, prompt=prompt, resume=resume, watch_pane=watch_pane, m=m)
        return
    if provider.prompt_after_ready:
        # Typed in once it's ready, after a warm-up carrying its instructions
        # on both fresh starts and resumes:
        # agy connects its MCP servers only once a conversation has started,
        # so the task must arrive as a second turn (see brindle.antigravity).
        for text in (provider.warmup(profile), prompt):
            if text:
                db.enqueue(agent.id, text, None)
        prompt = None
    # Recorded before launching: the agent's hooks may fire within milliseconds.
    status = "processing" if prompt else "starting"
    if not provider.uses_hooks:
        status = "unknown"
    db.set_status(agent.id, status)
    agent.status = status

    if provider.name == "claude":
        from brindle.providers import trust_folder

        trust_folder(ws.path)  # no trust dialog for nobody to answer
    if provider.name == "codex":
        try:   # Codex keys trust by the main repository: one answer covers every worktree
            providers.trust_codex_project(ws.repo_root)
        except (OSError, ValueError):
            pass   # after_launch still answers the screen; preflight reports a config it can't read
    if provider.name == "antigravity" and m is not None and m.setup is not None and m.setup.antigravity is not None:
        try:
            providers.set_agy_gemini()
        except (OSError, ValueError) as e:
            raise AgentError(f"couldn't set Antigravity to the company Gemini route in its settings.json: {e}") from e
    mcp_recovery = bool(agent.mode == "review" and agent.review_sha and db.conn.execute(
        "SELECT 1 FROM review_mcp_retries WHERE workspace_id=? AND sha=?",
        (ws.id, agent.review_sha)).fetchone())
    argv = provider.command(LaunchContext(agent.id, profile, prompt, resume=resume, cwd=ws.path,
                                          mode=agent.mode, plan_first=bool(agent.plan_first),
                                          mcp_recovery=mcp_recovery))
    if provider.name == "codex":
        from brindle import codex_hook

        try:
            codex_hook.tell_untrusted(db, agent, ws.path)  # launched without the hook it couldn't trust
        except Exception:  # noqa: BLE001 - never stop a launch over this
            pass
    if prompt or db.pending_count(agent.id):
        _prepare_launch_health(db, agent, ws, profile, m, argv_task=bool(prompt and provider.name != "shell"),
                              submitted_prompt=argv[-1] if prompt and provider.name == "codex" else (prompt or ""))
    else:
        # Resuming a conversation without delivering a new task must not
        # classify the previous turn's error as a current launch failure.
        db.conn.execute("DELETE FROM launch_health WHERE agent_id=?", (agent.id,))
    target = _open_window(db, agent, ws, f"{profile.name}-{agent.id[:4]}", argv, watch_pane,
                          **({"m": m} if m is not None else {}))
    from brindle import launch_health
    launch_health.bind(db, agent, target)
    if provider.name != "shell":
        from brindle import launch_health
        from brindle.providers import brindle_invocation
        # Observe early startup exits independently; a live startup must not
        # consume the follow-up before the CLI accepts its argv task.
        if launch_health.helper_token(agent):
            _detach([*brindle_invocation(), "_launch-health", agent.id,
                     *launch_health.helper_token(agent), "--startup"])

    if provider.name == "shell" and prompt:
        tmux.paste(target, prompt)
        from brindle import launch_health
        launch_health.delivered(db, agent)
    if background_setup:
        # Startup dialogs (folder trust) are handled by a detached helper so
        # the person gets their terminal immediately.
        from brindle import inbox, launch_health
        from brindle.providers import brindle_invocation

        # Without this process's own Claude Code inbox, if it has one: the
        # helper marks the agent ready, and must not take that inbox for it.
        base = {k: v for k, v in os.environ.items() if k not in (inbox.SOCKET_VAR, inbox.TOKEN_VAR)}
        subprocess.Popen(
            [*brindle_invocation(), "_after-launch", agent.id, *launch_health.helper_token(agent)],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**base, **agent_env(ws, agent.id)},
        )
    else:
        from brindle import prompt_answer

        prompt_answer.finish_launch(db, agent)


def _prepare_launch_health(db: DB, agent: Agent, ws: Workspace, profile, m=_UNREAD,
                           *, argv_task: bool = False, submitted_prompt: str = "") -> None:
    if not agent.parent_id or not agent.task:
        return
    from dataclasses import replace

    from brindle import launch_health
    effective = _effective_window_env(agent, ws, m)
    profile = replace(profile, provider=agent.provider)
    launch_health.prepare(db, agent, launch_health.fingerprint(profile, effective), argv_task=argv_task,
                          submitted_prompt=submitted_prompt)


# -- headless workers (claude -p) ------------------------------------------------
#
# A headless worker runs `claude -p`: one turn per process, no TUI. So that the
# rest of brindle can treat it like any other agent, its tmux pane runs a small
# runner (``brindle _headless``) that stays up between turns:
#
# - the first prompt, and every message sent while no turn is running, go to
#   the agent's inbox; the runner takes the oldest and runs
#   `claude -p --resume <session> <message>` (the first turn gets a fresh
#   --session-id, so the conversation is known without relying on hooks);
# - messages that arrive mid-turn are delivered by the Stop hook, exactly as
#   for interactive Claude Code (it keeps the -p process going);
# - hooks report status and results as usual; the runner marks the agent idle
#   when a turn's process exits;
# - if claude exits with an error, the runner exits too. The pane stays (with
#   claude's error in it) and is dead, which is how brindle notices any agent
#   died: wait_for_result reports its last lines, the dashboard shows it
#   stopped, and send_message refuses.

HEADLESS_CONTINUE = (
    "You were paused and have just been restarted. Check `git status` and `git log` "
    "in your working directory to see what you already did, then continue your task."
)


def _launch_headless(db: DB, agent: Agent, ws: Workspace, *, prompt: str | None,
                     resume: str | None, watch_pane: bool, m=_UNREAD) -> None:
    from brindle.providers import brindle_invocation

    if prompt:
        db.enqueue(agent.id, prompt, None)
    elif resume and agent.mode in REPORTING_MODES and agent.result is None:
        # An interactive chat resumes onto its input box; a headless worker
        # needs a turn to pick its task back up.
        db.enqueue(agent.id, HEADLESS_CONTINUE, None)
    status = "processing" if db.pending_count(agent.id) else "idle"
    db.set_status(agent.id, status)
    agent.status = status
    runner = get_provider(agent.provider).runner
    argv = [*brindle_invocation(), runner, agent.id, *(["--resume", resume] if resume else [])]
    _prepare_launch_health(db, agent, ws, _profile_for(db, agent, ws, m), m)
    target = _open_window(db, agent, ws, f"{agent.profile}-{agent.id[:4]}", argv, watch_pane,
                          **({"m": m} if m is not None else {}))
    from brindle import launch_health
    launch_health.bind(db, agent, target)
    if agent.parent_id and agent.task:
        # A runner can fail before it consumes the task (bad config/imports).
        # This helper observes only that startup; delivery arms its own timer.
        from brindle import launch_health
        _detach([*brindle_invocation(), "_launch-health", agent.id,
                 *launch_health.helper_token(agent), "--startup"])


def _preview(text: str, lines: int = 6) -> str:
    rows = text.strip().splitlines()
    return "\n".join(rows[:lines] + (["..."] if len(rows) > lines else []))


def run_headless(db: DB, agent_id: str, resume: str | None = None, *,
                 poll: float = 0.5, exit_when_idle: bool = False) -> int:
    """The loop in a headless worker's pane (see above). Returns claude's exit
    code when a turn fails, or 0 once the agent is gone. ``exit_when_idle``
    (for tests) returns as soon as there's nothing left to run."""
    from brindle.providers import LaunchContext

    agent = db.get_agent(agent_id)
    ws = db.get_workspace(agent.workspace_id) if agent else None
    if agent is None or ws is None:
        return 0
    provider = get_provider(agent.provider)
    session = resume
    turn = 0
    retry = None    # a turn the cloud login failed: run again before anything else
    while True:
        agent = db.get_agent(agent_id)
        if agent is None:
            return 0
        msg, retry = retry or db.pop_pending(agent_id), None
        if msg is None:
            if exit_when_idle:
                return 0
            time.sleep(poll)
            continue
        turn += 1
        db.set_status(agent_id, "processing")
        new_session = None
        if not session:
            new_session = str(uuid.uuid4())
            db.update_agent(agent_id, session_ref=new_session)
        ctx = LaunchContext(agent_id, _profile_for(db, agent, ws), msg.body, resume=session,
                            cwd=ws.path, session_id=new_session, mode=agent.mode,
                            plan_first=bool(agent.plan_first))
        print(f"\n── brindle: turn {turn} ──\n{_preview(msg.body)}\n", flush=True)
        try:
            from brindle import launch_health
            launch_health.delivered(db, agent, launch_health.before_delivery(db, agent))
            code = subprocess.call(provider.command(ctx), cwd=ws.path, stdin=subprocess.DEVNULL)
        except OSError as e:
            print(f"brindle: couldn't start {provider.name}: {e}", flush=True)
            code = 127
        session = session or new_session
        agent = db.get_agent(agent_id)
        if agent is None:
            return 0
        session = agent.session_ref or session
        if code != 0:
            if wait_for_cloud_login(db, agent, ws):
                # The same turn again, from where it stopped.
                retry = SimpleNamespace(body=HEADLESS_CONTINUE) if session else msg
                continue
            print(f"\n── brindle: claude exited with code {code}; this worker has stopped ──", flush=True)
            return code
        db.set_status(agent_id, "idle", only_if="processing")
        print("── brindle: turn finished; waiting for messages ──", flush=True)


def wait_for_cloud_login(db: DB, agent: Agent, ws: Workspace, poll: float = 60.0) -> bool:
    """A headless turn failed. If it was on a lapsed cloud login, wait here
    (offering the one browser sign-in, see cloud_login.sign_in) until the
    login works again and return True; False for any other failure."""
    from brindle import cloud_login

    try:
        env = agent_env(ws, agent.id, agent)
    except Exception:  # noqa: BLE001 - an unreadable env: not ours to wait on
        return False
    if cloud_login.provider_from_env({**os.environ, **env}) is None:
        return False
    s = cloud_login.refresh(env=env)
    if not s.expired:
        return False
    print("\n── brindle: the cloud login lapsed; this worker waits until it's renewed "
          f"(sign in with the browser tab brindle opens, or run `{s.refresh_cmd}`) ──", flush=True)
    while db.get_agent(agent.id) is not None:
        try:
            last_input = tmux.last_input()
        except tmux.TmuxError:
            last_input = None
        cloud_login.sign_in(env=env, last_input=last_input)
        if cloud_login.status(env=env).ok:
            print("── brindle: cloud login renewed; continuing ──", flush=True)
            return True
        time.sleep(poll)
    return False


def ready(db: DB, agent_id: str) -> None:
    """after_launch saw the CLI's input box. For CLIs with no hook that says
    so, this is when it's ready (and when queued messages can go in)."""
    agent = db.get_agent(agent_id)
    if agent and not get_provider(agent.provider).announces_start:
        handle_hook(db, agent_id, "session-start", {})


# -- sessions: pause and continue ----------------------------------------------

RESUME_NOTE = (
    "\n\n(You were paused and have just been restarted. Before continuing, check "
    "`git status` and `git log` in your working directory to see what you already did.)"
)


def tree(db: DB, root_id: str) -> list[Agent]:
    """``root_id`` and every agent it started, directly or indirectly."""
    out, queue = [], [root_id]
    while queue:
        a = db.get_agent(queue.pop(0))
        if a is None:
            continue
        out.append(a)
        queue.extend(c.id for c in db.children(a.id))
    return out


def pause(db: DB, root_id: str, *, stop_procs: bool = True,
          stop_local_models: bool = True) -> list[Agent]:
    """Stop a supervisor and everything it started, keeping their work.

    Worktrees, branches, queued messages and each CLI's own session stay; the
    processes stop, so nothing keeps acting while nobody's watching. Agents
    that already reported are left marked done. Returns the agents paused.
    Without ``stop_procs``, processes Claude Code's daemon hosts are left for
    a detached cull (see brindle.cull), which stops those of paused agents,
    rather than waiting here for them to exit. The Ollama server brindle
    started is stopped too once no other running session uses it (see
    brindle.native.serve.stop_unused); ``stop_local_models=False`` when
    another session is about to start and would only start it again.

    Records every status first and closes windows last: this can run inside
    one of the windows it closes (see _pause_when_done)."""
    paused, sessions, windows = [], set(), []
    owners = pane_owners(db)
    for a in tree(db, root_id):
        ws = db.get_workspace(a.workspace_id)
        if ws:
            sessions.add(ws.tmux_session)
        # A window on another tmux server isn't reachable from here, and its
        # pane id would name some other pane on this one (see same_server).
        if a.tmux_window and same_server(a) and owns_pane(db, a, owners):
            windows.append(a.tmux_window)
        # Its own SubagentStop hooks will never fire once its process stops.
        db.end_native_subagents(a.id)
        if a.mode != "interactive" and a.result is not None:
            db.set_status(a.id, "done")
        else:
            db.set_status(a.id, "paused")
            paused.append(a)
    # The sidebar follows the person around, so it may not be sitting in any
    # of the windows about to close: find it wherever it is and stop it too,
    # rather than leaving it running with nothing left to show it. Status was
    # already set to "paused" above (a tombstone), and the lock keeps this
    # from interleaving with a concurrent sidebar_follow or _ensure_sidebar.
    # Checked (and, if it's really ours, killed) around the window closes,
    # not after: a sidebar living in a window being closed here would
    # otherwise already look dead by the time _valid_sidebar ran, and get
    # skipped instead of having its now-stale DB row cleared.
    with _sidebar_lock(root_id):
        sidebar = db.get_sidebar_pane(root_id)
        sidebar_is_ours = _valid_sidebar(sidebar, root_id)
        for w in windows:
            # Dead or alive: a dead pane (the chat that just exited) would
            # otherwise hold the window, and the session, open.
            tmux.kill_window(w)
        if sidebar_is_ours:
            assert sidebar is not None
            tmux.kill_pane(sidebar)
            db.clear_sidebar_pane(root_id)
    # Closing the windows doesn't stop sessions Claude Code's daemon hosts.
    # Skipped for this process and its ancestors: this can run in the chat's pane.
    if stop_procs:
        from brindle import procs

        procs.stop([a.id for a in tree(db, root_id)], grace=2.0)
    root = db.get_agent(root_id)
    root_ws = db.get_workspace(root.workspace_id) if root else None
    for session in sessions:
        # The chat's own session always closes, so an attached terminal gets
        # its prompt back every time. Workers' sessions close once they hold
        # nothing but the idle starter shell.
        if (root_ws and session == root_ws.tmux_session) or set(tmux.windows(session)) <= {"shell"}:
            tmux.kill_session(session)
    if stop_local_models:
        _stop_local_models(db)
    knowledge.note(db, root_id, knowledge.PAUSE)
    bus.publish_agent(db, "session.paused", root_id, agents=[a.id for a in paused])
    return paused


def _stop_local_models(db: DB) -> None:
    from brindle.native import serve

    try:
        lines = serve.stop_unused(db)
        if lines:
            with open(serve.log_path(), "a", encoding="utf-8") as f:
                f.writelines(f"== brindle: {line}\n" for line in lines)
    except Exception:  # noqa: BLE001 -- freeing memory must never break a pause
        pass


def latest_paused(db: DB, ws: Workspace) -> Agent | None:
    """The most recently paused interactive agent (session root) in ``ws``."""
    roots = [a for a in db.list_agents(ws.id) if a.mode == "interactive" and a.status == "paused"]
    return max(roots, key=lambda a: a.status_since or 0, default=None)


def resume(db: DB, root_id: str, *, watch_pane: bool = True,
           only: set[str] | list[str] | None = None,
           recovery_env: dict[str, str] | None = None) -> list[Agent]:
    """Bring a paused session back: the supervisor and its paused workers
    restart in their own workspaces. Claude Code picks up its previous
    conversation (--resume); other CLIs restart on their original task.
    With ``only``, just those agents (if paused) restart."""
    from brindle import identity_lock, providers

    providers.invalidate_signed_in()
    from brindle import keystore, secrets

    # Explicit recovery replacements (from recovery_env or current process environment)
    env_keys = recovery_env if recovery_env is not None else {
        k: os.environ[k] for k in keystore.known_names()
        if k in os.environ and os.environ[k] and not secrets.is_job_secret(k)
    }

    session_creds = db.get_session_credentials_with_timestamps(root_id)
    for k in keystore.known_names():
        try:
            ks_val, ks_time = keystore.get_key_info(k)
        except Exception:
            ks_val, ks_time = None, 0.0
        sess_val, sess_time = session_creds.get(k, (None, 0.0))
        env_val = env_keys.get(k)

        val = None
        if ks_val and (ks_time > sess_time or (sess_val and env_val == sess_val and ks_val != sess_val) or not env_val):
            val = ks_val
        elif env_val:
            val = env_val
        elif ks_val:
            val = ks_val

        if val:
            db.set_session_credential(root_id, k, val)
            os.environ[k] = val

    candidates = [a for a in tree(db, root_id)
                  if a.status in ("paused", "failed") and (only is None or a.id in only)]
    for a in candidates:   # all or nothing: a session never half-resumes under another identity
        identity_lock.ensure(a)
    if any(a.id == root_id for a in candidates):
        from brindle import autopilot as pilot

        pilot.reload_goals_file(db, root_id)   # edits the person made to this session's goal file
    resumed = []
    for a in candidates:
        if a.status == "failed":
            if a.tmux_window and same_server(a) and owns_pane(db, a):
                try:
                    tmux.kill_window(a.tmux_window)
                except Exception:
                    pass
            from brindle import procs
            try:
                procs.stop([a.id], grace=2.0)
            except Exception:
                continue
        ws = db.get_workspace(a.workspace_id)
        if ws is None or not os.path.isdir(ws.path):
            continue
        provider = get_provider(a.provider)
        ref = a.session_ref if provider.name in ("claude", "antigravity", "opencode", "native") and a.session_ref else None
        if ref and not provider.can_resume(ref):
            ref = None  # nothing was ever said in it: start that agent fresh
        prompt = None if ref else _fresh_start_prompt(db, a, ws, provider, RESUME_NOTE)
        if a.dismissed_at is not None:
            db.update_agent(a.id, dismissed_at=None)  # running again: show it again
        _launch(db, a, ws, prompt=prompt, resume=ref,
                watch_pane=watch_pane and a.id == root_id, background_setup=True)
        resumed.append(a)
    if any(a.id == root_id for a in resumed):
        knowledge.note(db, root_id, knowledge.RESUME)
    ap = db.get_autopilot(root_id)
    failed_still = [a for a in candidates if db.get_agent(a.id) and db.get_agent(a.id).status == "failed"]
    if ap and ap.state == "blocked" and resumed and not failed_still:
        note = (ap.note or "").lower()
        auth_indicators = ("auth", "credential", "sign in", "signed out", "login", "api key", "key", "token", "unauthorized", "forbidden")
        if any(ind in note for ind in auth_indicators):
            db.update_autopilot(root_id, state="running", note=None)
    if resumed:
        bus.publish_agent(db, "session.resumed", root_id, agents=[a.id for a in resumed])
    return resumed


def _fresh_start_prompt(db: DB, a: Agent, ws: Workspace, provider, note: str) -> str | None:
    """What an agent restarting without its old conversation is told: its
    task, followed by ``note``."""
    if a.task and a.mode in ("handoff", "assign") and provider.launches_process:
        # a.task is the raw task now (see decorate_worker_prompt); a fresh
        # start needs the same decoration it got the first time. A row
        # from before this change stored the already-decorated text, so
        # it gets decorated a second time here; harmless, if redundant.
        return decorate_worker_prompt(
            a.task, a.id, ws, a.done_when, provider, bool(a.headless),
            plan_first=bool(a.plan_first) and a.plan_state != "approved",
            report_only=is_review_profile(a.profile, ws.repo_root),
            decisions=knowledge.replay_decisions(db, a.id),
            chunks=knowledge.replay_chunks(db, a.id)) + note
    return (a.task + note) if a.task else None


def get(db: DB, agent_id: str) -> Agent:
    agent = db.get_agent(agent_id)
    if not agent:
        # Allow unambiguous prefixes, like git does for hashes.
        matches = [a for a in db.list_agents() if a.id.startswith(agent_id)]
        if len(matches) == 1:
            return matches[0]
        raise AgentError(f"no agent {agent_id!r}")
    return agent


def ended(db: DB, agent_id: str) -> None:
    """An interactive agent's CLI exited (the person closed the chat): pause
    its whole session. The chat's window, dashboard and workers stop; their
    work is kept for `brindle --continue`."""
    agent = db.get_agent(agent_id)
    for _ in range(20):  # a CLI that exits instantly can beat the window id to the DB
        if agent is None or agent.tmux_window:
            break
        time.sleep(0.1)
        agent = db.get_agent(agent_id)
    if agent is None or agent.status in ("paused", "done"):
        return
    pause(db, agent_id)


def attach_target(db: DB, ws: Workspace) -> str | None:
    """The pane `brindle attach <workspace>` should open: the live agent there
    that needs someone (waiting on a prompt), else the busiest, else the
    newest. None when no agent runs there. Never the session's placeholder
    shell window, which is what tmux would show otherwise."""
    live = [a for a in db.list_agents(ws.id)
            if a.tmux_window and a.dismissed_at is None and is_alive(a)]
    if not live:
        return None
    rank = {"waiting": 0, "processing": 1, "starting": 2}
    return min(reversed(live), key=lambda a: rank.get(a.status, 3)).tmux_window


def find_running(db: DB, ws: Workspace, profile: str) -> Agent | None:
    """The newest live interactive agent of ``profile`` in ``ws``, if any."""
    for a in reversed(db.list_agents(ws.id)):
        if a.profile == profile and a.mode == "interactive" and is_alive(a):
            return a
    return None


def runs_process(agent: Agent) -> bool:
    """False for agents whose work happens outside brindle (the subagent provider)."""
    from brindle.providers import PROVIDERS

    provider = PROVIDERS.get(agent.provider)
    return provider is None or provider.launches_process


def same_server(agent: Agent, panes: dict[str, bool] | None = None) -> bool:
    """Whether ``agent``'s window is on the tmux server ``panes`` (a
    ``tmux.list_panes()`` snapshot; omitted: the server brindle is using)
    describes. Pane ids are only meaningful on the server that issued them:
    an agent on another server (a demo recording's private server beside the
    person's real sessions, say) must never be judged, let alone stopped, by
    this server's pane list. An agent with no server recorded (launched by an
    older brindle) counts as the default server's; a plain dict of panes as
    the current server's."""
    server = getattr(panes, "server", tmux.current_server())
    return (agent.tmux_server or None) == server


def is_alive(agent: Agent, panes: dict[str, bool] | None = None) -> bool:
    """``panes`` is a pre-fetched ``tmux.list_panes()`` result, shared by a
    whole snapshot so callers don't each shell out for their own agent's
    pane. Omit it to check this one agent's pane directly. An agent whose
    window is on another tmux server than the snapshot's (see same_server)
    is asked of its own server instead: the snapshot can't speak for it."""
    if not runs_process(agent):
        # No process to watch: it's at work until its result is recorded.
        return agent.result is None and agent.status not in ("paused", "done")
    if not agent.tmux_window:
        return False
    if panes is not None and same_server(agent, panes):
        return panes.get(agent.tmux_window, False)
    if same_server(agent):
        return tmux.window_alive(agent.tmux_window)
    return tmux.window_alive(agent.tmux_window, server=agent.tmux_server or None)


LAUNCH_GRACE = 60.0   # seconds a just-spawned agent may go without a window before it counts as dead


def alive_or_starting(agent: Agent) -> bool:
    """``is_alive``, or still being launched: spawn records the agent before
    its window opens (the launch's checks can take seconds), so one with no
    window yet is starting, not dead, for LAUNCH_GRACE seconds. A launch known
    to have failed ends that: spawn deletes the row of one that raised, and
    launch_health marks one whose CLI died at startup "failed". For the
    checks that would replace the agent (start another reviewer, forget an
    auditor), not for ones that talk to its pane."""
    if is_alive(agent):
        return True
    return (runs_process(agent) and not agent.tmux_window and agent.result is None
            and agent.status not in ("paused", "done", "failed")
            and time.time() - agent.created_at < LAUNCH_GRACE)


def server_of(agent: Agent) -> str | None:
    """The tmux server ``agent``'s window is on (a private socket name, or
    None for the default server), for the ``server`` argument of tmux calls
    on its pane: reading it, typing into it. Always pass it: a pane id names
    a different pane on every other server."""
    return agent.tmux_server or None


def own_panes(agent: Agent) -> tmux.PaneSnapshot:
    """A pane snapshot of the tmux server ``agent``'s window is on."""
    if same_server(agent):
        return tmux.list_panes()
    return tmux.list_panes(server=server_of(agent))


def pane_owners(db: DB, panes: dict[str, bool] | None = None) -> dict[str, str]:
    """Which agent each recorded pane id belongs to. tmux reuses pane ids once
    its server restarts (a reboot, `tmux kill-server`), so an old agent's
    stored pane can now belong to a newer agent: only the newest agent
    recorded on a pane can be the one running in it.

    A pane that carries a tag (AGENT_TAG, or SIDEBAR_TAG for the dashboard)
    has the last word over the DB: it names its agent itself, or says it's
    nobody's. That covers a pane the DB doesn't know about yet (a launch
    records the pane id a moment after the pane exists) and the sidebar,
    which no agent row ever names. ``panes`` is a pre-fetched
    ``tmux.list_panes()`` snapshot, which already carries the tags; without
    one, this fetches its own. Only agents on the snapshot's tmux server are
    listed (see same_server): another server hands out the same pane ids,
    and its agents are no claim on this one's panes."""
    if panes is None or not isinstance(panes, tmux.PaneSnapshot):
        panes = tmux.list_panes()
    owners = {a.tmux_window: a.id for a in db.list_agents()
              if a.tmux_window and same_server(a, panes)}  # oldest first
    for pane, found in panes.tags.items():
        owners[pane] = found.get(AGENT_TAG, "")  # a sidebar is no agent's pane
    return owners


def owns_pane(db: DB, agent: Agent, owners: dict[str, str] | None = None) -> bool:
    """False when a newer agent has since been recorded on ``agent``'s pane
    id: that pane, if alive, is the newer agent's, never ``agent``'s. Check
    this before touching an agent's window (killing it, reading it).
    ``owners`` is a pre-fetched ``pane_owners`` result, for loops. A pane
    those owners don't cover at all belongs to another tmux server (see
    pane_owners), where nothing here could have taken it over; without
    ``owners``, the agent's own server is asked."""
    if not agent.tmux_window:
        return True
    if owners is None:
        owners = pane_owners(db, own_panes(agent))
    return owners.get(agent.tmux_window, agent.id) == agent.id


def format_message(db: DB, body: str, sender_id: str | None) -> str:
    if not sender_id:
        return body
    sender = db.get_agent(sender_id)
    who = f"{sender.profile} agent {sender_id}" if sender else f"agent {sender_id}"
    return f"<details><summary>Message from {who}</summary>\n\nReply with the brindle send_message tool, to_agent_id={sender_id}\n\n{body}\n</details>"


def message_lead(db: DB, agent: Agent, sender_id: str | None) -> str | None:
    """The line brindle types (not pastes) before a message it delivers into an
    agent's chat, so the agent can tell a brindle delivery from pasted text of
    unknown origin (see tmux.paste). None for a plain shell, where it would
    become part of the command."""
    if agent.provider == "shell":
        return None
    if not sender_id:
        return "brindle delivered this message:"
    sender = db.get_agent(sender_id)
    who = f"{sender.profile} agent {sender_id}" if sender else f"agent {sender_id}"
    return f"brindle delivered this message from {who}:"


def pulls_messages(db: DB, agent: Agent) -> bool:
    """Whether messages from agents and brindle to ``agent`` wait to be read with
    read_messages (``message_delivery`` "pull", the default) instead of being
    delivered. Only an interactive, hook-capable supervisor reads them."""
    from brindle.config import load_repo_config

    if agent.mode != "interactive" or agent.headless or not get_provider(agent.provider).uses_hooks:
        return False
    ws = db.get_workspace(agent.workspace_id)
    return (load_repo_config(ws.repo_root).message_delivery if ws else "pull") != "push"


def unread_notice(db: DB, messages: list) -> str:
    """The one line pushed instead of the messages themselves. It carries the
    time so no two notices read the same: Claude Code drops a peer message
    identical to the previous one from the same sender, and a dropped notice
    leaves the supervisor waiting on news it already has."""
    senders = list(dict.fromkeys(m.sender_id or "brindle" for m in messages))
    n = len(messages)
    return (f"brindle ({time.strftime('%H:%M:%S')}): {n} new message{'s' if n != 1 else ''} "
            f"(from {', '.join(senders)}). Call read_messages.")


def one_line(db: DB, message_id: int, body: str, sender_id: str | None) -> str:
    """A message as the single line a supervisor sees: sender, head line (the
    first line of ``body``) and how to fetch the rest."""
    if sender_id:
        sender = db.get_agent(sender_id)
        who = f"{sender.profile} {sender_id}" if sender else sender_id
    else:
        who = "brindle"
    # A stuck-worker notice (cull.note_stuck) carries the prompt it waits on in
    # its first line, which has to arrive whole.
    head = first_line(body, 1500 if "has been waiting on a prompt for" in body[:200] else 300)
    if not sender_id:
        head = head.removeprefix("[brindle] ")  # the sender tag already says it
    return (f"[{who}] {head} "
            f"(full text: read_messages(message_id={message_id}))")


def full_message(db: DB, agent_id: str, message_id: int) -> str:
    """The whole text of one message to ``agent_id``, as read_messages(message_id=...) returns it."""
    m = db.get_message(agent_id, message_id)
    if m is None:
        return f"No message {message_id}."
    if m.sender_id:
        return format_message(db, m.body, m.sender_id)
    return f"<details><summary>Message from brindle</summary>\n\n{m.body}\n</details>"


def render_unread(db: DB, messages: list) -> str:
    """What read_messages returns: one line per message."""
    return "\n".join(one_line(db, m.id, m.body, m.sender_id) for m in messages)


def send_message(db: DB, to_id: str, body: str, sender_id: str | None = None,
                 person: bool = False) -> str:
    """Deliver now if the agent is idle; otherwise queue until it is.
    Returns ``"delivered"`` or ``"queued"``. ``person`` marks a message from
    the person running brindle, which is always pushed."""
    from brindle import identity_lock

    agent = get(db, to_id)
    identity_lock.ensure(agent)
    if not person and runs_process(agent) and is_alive(agent) and pulls_messages(db, agent):
        # Keep the message unread and deliver only a notice, once for everything unread.
        db.enqueue_held(agent.id, body, sender_id)
        claimed = db.claim_notice(agent.id)
        if claimed is None:
            return "queued"
        try:
            outcome = _send_message(db, agent, unread_notice(db, claimed), None, notice=True)
        except BaseException:
            db.reset_notice(agent.id)
            raise
        return outcome
    # A supervisor sees a message from an agent or brindle as one line; the
    # person's own input is always delivered whole.
    return _send_message(db, agent, body, sender_id,
                         condensed=agent.mode == "interactive" and not person
                         and agent.provider != "shell")


def _send_message(db: DB, agent: Agent, body: str, sender_id: str | None,
                  condensed: bool = False, notice: bool = False) -> str:
    if not runs_process(agent):
        raise AgentError(
            f"agent {agent.id} is a subagent run by its supervisor's own Agent tool, so brindle "
            "can't message it. Its supervisor can continue it with that tool, or start a new "
            f"subagent in the same worktree; then record the outcome with complete_subagent."
        )
    if not is_alive(agent):
        if agent.status == "paused":
            raise AgentError(f"agent {agent.id} is paused, so it can't take messages. The person "
                             "can resume its session with `brindle continue`; the message isn't kept.")
        raise AgentError(f"agent {agent.id} is not running")
    provider = get_provider(agent.provider)
    if condensed:
        # The full text is kept, already read, for read_messages(message_id=...).
        kept = db.enqueue_held(agent.id, body, sender_id, publish=False)
        db.mark_delivered(kept)
        text = one_line(db, kept, body, sender_id)
    else:
        text = format_message(db, body, sender_id)
    if agent.provider == "antigravity" and not (condensed or notice):
        from brindle.antigravity import format_delivered_message
        text = format_delivered_message(text)
    if agent.headless:
        # Its runner starts the next turn with this, or the Stop hook hands it
        # over if a turn is still running.
        db.enqueue(agent.id, text, sender_id)
        return "delivered" if agent.status == "idle" else "queued"
    if not provider.uses_hooks:
        tmux.paste(agent.tmux_window, text, lead=message_lead(db, agent, sender_id),
                   server=server_of(agent))
        if not (condensed or notice):
            # Direct deliveries need the same durable history as queued ones,
            # including supervisor spec updates used by the next reviewer.
            kept = db.enqueue_held(agent.id, body, sender_id, publish=False)
            db.mark_delivered(kept)
        return "delivered"
    message_id = db.enqueue(agent.id, text, sender_id, notice=notice)
    from brindle import login_pause

    if login_pause.holding(db, agent.id):
        return "queued"  # its session waits on an expired cloud login
    from brindle import wake

    if wake.enabled(agent):
        # A chat with brindle's waiter never gets the inbox, where Claude Code
        # would wrap the message as another session's (see brindle.wake).
        if wake.live(agent):
            return "delivered"  # the waiter wakes it with this
    elif _deliver_to_inbox(db, agent, message_id, text, sender_id):
        return "delivered"
    reconcile(db, agent)
    if db.message_delivered(message_id):
        return "delivered"  # the idle correction above already flushed it
    return "delivered" if flush(db, agent.id) else "queued"


def reconcile(db: DB, agent: Agent, samples: int = 2, gap: float = 0.7) -> Agent:
    """Correct a status the hooks left stale. Claude Code runs no Stop hook
    when a turn is interrupted (Esc), so the agent can sit idle while we still
    think it's busy; after a permission prompt is approved the status stays
    'waiting' until the tool finishes; and an 'idle' status can outlive the
    turn that followed it. The screen must agree across ``samples`` reads
    before we override the hooks."""
    screen_status(db, agent, samples, gap)
    return agent


def screen_status(db: DB, agent: Agent, samples: int = 2, gap: float = 0.7) -> str | None:
    """``reconcile``, returning the status the screen showed consistently
    ('idle', 'processing' or 'waiting'), or None when it can't be read or
    didn't agree across samples.

    An 'idle' agent is only read with ``samples`` >= 2, and only moved to
    'processing' when its provider sees the busy marker right by the input
    box (not just anywhere on screen, where a transcript can quote it). A
    single cheap read, as the dashboard does, leaves it alone. Moving an
    agent to idle also delivers anything queued for it, but only when
    ``samples`` >= 2: a single-sample read must never pop a message into a
    terminal as a side effect of just rendering the dashboard.

    An agent whose CLI runs none of brindle's hooks (``hooks_off``: the
    person's settings turn them off, at launch or later through /hooks, or
    none reported within HOOKS_GRACE of its input box appearing) has only the
    screen to go by: it may leave 'starting', a single read may move it from
    'idle' to busy, and a ``samples`` >= 2 idle read delivers what's queued
    even when the status was already idle, since no Stop hook ever will."""
    provider = get_provider(agent.provider)
    if (agent.headless or not provider.uses_hooks
            or agent.status not in ("starting", "idle", "processing", "waiting")):
        return None  # a headless pane shows output, not a TUI to read
    if agent.status == "waiting" and db.hook_permission_request(agent.id) is not None:
        return None  # its hook holds a request for the app's answer: the pane still looks busy
    _sync_hooks_off(db, agent, provider.hooks_disabled(*_settings_scope(db, agent)))
    if agent.status == "idle" and samples < 2 and not agent.hooks_off:
        return None
    seen, ready = set(), True
    for i in range(samples):
        if i:
            time.sleep(gap)
        try:
            screen = tmux.capture(agent.tmux_window, lines=40, server=server_of(agent))
        except tmux.TmuxError:
            return None
        state = provider.screen_state(screen)
        if agent.status == "idle" and state == "busy" and not provider.busy_in_footer(screen):
            state = None
        seen.add(state)
        ready = ready and provider.input_ready(screen)
    if (agent.status == "starting" and not agent.hooks_off and ready and provider.announces_start
            and time.time() - (agent.status_since or agent.created_at) >= HOOKS_GRACE):
        # Its input box is up, and SessionStart, which runs before it, never reported.
        # (A CLI without one is marked ready by after_launch instead: see ready.)
        _sync_hooks_off(db, agent, f"{NO_HOOK_EVENTS} from {provider.name}")
    if len(seen) != 1:
        return None
    state = seen.pop()
    new = {"idle": "idle", "busy": "processing", "waiting": "waiting"}.get(state or "")
    if agent.status == "starting" and new != "waiting" and not agent.hooks_off:
        # Its hooks say when it's ready; the screen only adds that it's
        # stuck on a dialog before they could (e.g. folder trust).
        return None
    changed = bool(new) and new != agent.status
    if changed:
        db.set_status(agent.id, new, only_if=agent.status)
        agent.status = new
    if (new == "idle" and samples >= 2 and (changed or agent.hooks_off)
            and db.pending_count(agent.id)):
        try:
            flush(db, agent.id)
        except tmux.TmuxError:
            pass  # stays queued; its Stop hook hands it over
    return new


# How long a hooks CLI may show its input box while still 'starting' before
# brindle takes it that none of its hooks run (see screen_status).
HOOKS_GRACE = 60.0
# How hooks_off reads when no settings explain it, only the missing events.
NO_HOOK_EVENTS = "no hook events"


_settings_scopes: dict[tuple[str, float], tuple[str | None, list[str] | None]] = {}


def _settings_scope(db: DB, agent: Agent) -> tuple[str | None, list[str] | None]:
    """Where ``agent``'s CLI reads its settings: its workspace, and the
    profile's setting_sources (None: all of them). Kept per agent, as the
    sidebar asks on every refresh and a running CLI keeps its launch flags."""
    key = (agent.id, agent.created_at)
    if key in _settings_scopes:
        return _settings_scopes[key]
    from brindle.profiles import ProfileError

    ws = db.get_workspace(agent.workspace_id)
    try:
        sources = load_profile(agent.profile, ws.repo_root if ws else None).setting_sources
    except (KeyError, ProfileError):
        sources = None
    scope = _settings_scopes[key] = (ws.path if ws else None), sources
    return scope


def _sync_hooks_off(db: DB, agent: Agent, why: str | None) -> None:
    """Record ``why`` its hooks are off; None clears a reason the settings
    gave (turned back on), but not missing events, which only a hook clears."""
    if why is None and agent.hooks_off and agent.hooks_off.startswith(NO_HOOK_EVENTS):
        return
    if why != agent.hooks_off:
        db.update_agent(agent.id, hooks_off=why)
        agent.hooks_off = why


def _deliver_to_inbox(db: DB, agent: Agent, message_id: int, text: str,
                      sender_id: str | None) -> bool:
    """Send a queued message through Claude Code's own inbox (see
    brindle.inbox), where it doesn't wait for the agent to go idle. Returns
    whether it went; if not, it stays queued for the pane."""
    from brindle import inbox

    if not inbox.usable(agent):
        return False
    sender = db.get_agent(sender_id) if sender_id else None
    who = f"brindle {sender.profile} {sender.id}" if sender else "brindle"
    if not inbox.send(agent, text, sender=who):
        return False
    db.mark_delivered(message_id)
    return True


def flush(db: DB, agent_id: str) -> bool:
    """Deliver the agent's oldest pending message: through its inbox if it
    has one, else typed into its pane once it's idle (or while busy, for a
    CLI that holds mid-turn input: see _flush_midturn). A chat with a live
    waiter (brindle.wake) is left to it, and one that runs a waiter never
    uses the inbox. Returns True if something was delivered."""
    from brindle import inbox
    from brindle import launch_health
    from brindle import wake

    agent = db.get_agent(agent_id)
    if _delivery_held(db, agent):
        return False
    if not wake.enabled(agent) and inbox.usable(agent):
        delivered = False
        while (msg := db.pop_pending(agent_id)) is not None:
            baseline = launch_health.before_delivery(db, agent)
            if inbox.send(agent, msg.body, sender="brindle"):
                delivered = True
                launch_health.delivered(db, agent, baseline)
            else:
                db.enqueue(agent_id, msg.body, msg.sender_id,  # back in the queue, for the pane
                           notice=msg.noticed_at is not None)
                break
        if delivered:
            return True
    # One paste into the pane at a time, whichever path: the idle claim alone
    # leaves the agent busy for a second sender's mid-turn paste while the
    # first is still typing, reordering or merging the two.
    with delivery_lock(agent_id):
        return _flush_pane(db, agent_id)


def delivery_lock(agent_id: str):
    """Held across processes by whoever types a queued message into
    ``agent_id``'s pane (``flush``), from the look at its state to the end of
    the paste. A lock file under the brindle home, so a holder that dies gives
    it back; re-entrant within a thread."""
    from brindle import pipeline

    return pipeline._reentrant_lock("deliver-" + hashlib.sha1(agent_id.encode()).hexdigest()[:16])


def _delivery_held(db: DB, agent: Agent | None) -> bool:
    """Whether ``flush`` must leave the queue alone: the agent is gone or
    headless (its runner takes messages from the inbox itself), its session
    waits on an expired cloud login (resuming flushes it), or a live waiter
    hands the queue over (brindle.wake)."""
    from brindle import login_pause
    from brindle import wake

    return (agent is None or agent.headless or login_pause.holding(db, agent.id)
            or (wake.enabled(agent) and wake.live(agent)))


def _flush_pane(db: DB, agent_id: str) -> bool:
    """``flush``'s typed delivery, under its delivery_lock. The queue and the
    agent are read afresh: while it waited, another sender may have delivered,
    or the session been paused."""
    from brindle import launch_health

    if _delivery_held(db, db.get_agent(agent_id)) or db.pending_count(agent_id) == 0:
        return False
    claimed_at = db.claim_idle(agent_id)
    if claimed_at is None:
        return _flush_midturn(db, agent_id)
    agent = db.get_agent(agent_id)
    assert agent is not None
    if _paste_blocked(agent):
        # Leave it queued: the next flush (a later message, or a resumed
        # session's start-up) gets another chance.
        db.set_status(agent_id, "idle", only_if="processing")
        return False
    msg = db.pop_pending(agent_id)
    if not msg:
        db.set_status(agent_id, "idle", only_if="processing")
        return False
    from brindle import antigravity
    warmup = agent.provider == "antigravity" and antigravity.WARMUP_END in msg.body
    try:
        baseline = launch_health.before_delivery(db, agent, msg.body) if not warmup else ""
        tmux.paste(agent.tmux_window, msg.body, lead=message_lead(db, agent, msg.sender_id),
                   server=server_of(agent))
    except BaseException:
        # pop_pending claimed this row before the external send. Release the
        # same row and our idle claim so the next flush can retry it once.
        db.retry_pending(msg.id, agent_id, claimed_at)
        raise
    if not warmup:
        launch_health.verify_delivery(db, agent, msg.body, baseline, message_lead(db, agent, msg.sender_id))
    return True


def _flush_midturn(db: DB, agent_id: str) -> bool:
    """``_flush_pane`` for a busy interactive chat whose CLI keeps input typed
    mid-turn until its current step finishes (Codex, agy): the oldest queued
    message goes in now instead of at the turn-end notify or Stop. Never into
    an approval prompt ('waiting', or one on screen the hooks haven't
    reported), where the keys would answer it."""
    agent = db.get_agent(agent_id)
    if (agent is None or agent.status != "processing" or agent.mode != "interactive"
            or not get_provider(agent.provider).types_midturn):
        return False
    try:
        screen = tmux.capture(agent.tmux_window, lines=40, server=server_of(agent))
    except tmux.TmuxError:
        return False
    if get_provider(agent.provider).screen_state(screen) == "waiting" or _paste_blocked(agent):
        return False
    msg = db.pop_pending(agent_id)
    if not msg:
        return False
    try:
        tmux.paste(agent.tmux_window, msg.body, lead=message_lead(db, agent, msg.sender_id),
                   server=server_of(agent))
    except BaseException:
        db.release_pending(msg.id, agent_id)  # back in the queue for the turn-end flush
        raise
    return True


def _paste_blocked(agent: Agent) -> bool:
    """Whether pasting into ``agent``'s pane right now would land somewhere
    other than its chat: over text the person is mid-typing (interactive
    only), into Claude Code's background-session launcher, or into a pane
    whose foreground session has changed to something else entirely (any
    mode -- a blind paste in either case reaches the wrong conversation or
    starts a brand-new one). A background view gets one Escape and a
    re-check before giving up."""
    provider = get_provider(agent.provider)
    interactive = agent.mode == "interactive"
    server = server_of(agent)
    try:
        screen = tmux.capture(agent.tmux_window, lines=40, escapes=True, server=server)
    except tmux.TmuxError:
        return False
    reason = provider.paste_blocked(screen, interactive)
    if reason == "background":
        tmux.send_keys(agent.tmux_window, "Escape", server=server)
        time.sleep(0.3)
        try:
            screen = tmux.capture(agent.tmux_window, lines=40, escapes=True, server=server)
        except tmux.TmuxError:
            return False
        reason = provider.paste_blocked(screen, interactive)
    return reason is not None


# -- subagent provider -------------------------------------------------------------


def subagent_prompt(profile_prompt: str, task: str, ws: Workspace, done_when: str | None) -> str:
    """What the supervisor passes to its Agent tool for a subagent worker."""
    parts = [profile_prompt.strip(), "Task:\n" + task.strip()]
    if done_when:
        parts.append(f"Finish line: {done_when.strip()}")
    body = "\n\n".join(p for p in parts if p)
    return body + SUBAGENT_FOOTER.format(path=ws.path, branch=ws.branch,
                                         guidance=worker_guidance(ws))


def subagent_brief(agent: Agent, ws: Workspace) -> str:
    """The handoff/assign reply for a subagent worker: what the caller does next."""
    base = f" (cut from {ws.base_branch})" if ws.base_branch else ""
    return (
        f"brindle made workspace {ws.id} for agent {agent.id} ({agent.profile}) but started no "
        "process: your own subagent does this task.\n"
        f"  path:   {ws.path}\n"
        f"  branch: {ws.branch}{base}\n"
        f"  agent:  {agent.id}\n\n"
        "Next:\n"
        "1. Run it with your Agent tool (a general-purpose subagent), passing the prompt "
        "between the markers exactly as written. With several tasks, you can run their "
        "subagents in parallel.\n"
        f"2. When it returns, call complete_subagent(agent_id=\"{agent.id}\", result=<its "
        "summary>). Until then brindle shows it as working.\n"
        f"3. Review with workspace_diff(\"{ws.id}\"), then merge_workspace and "
        "remove_workspace as for any worker.\n\n"
        f"----- prompt for your Agent tool -----\n{agent.task}\n----- end of prompt -----"
    )


def complete_subagent(db: DB, agent_id: str, result: str) -> Agent:
    """Record a subagent worker's outcome: it shows as done, and its workspace
    is reviewed, merged and removed like any other."""
    agent = get(db, agent_id)
    if runs_process(agent):
        raise AgentError(
            f"agent {agent.id} runs its own {agent.provider} process; it reports with "
            "report_result itself"
        )
    db.set_result(agent.id, result)
    db.set_status(agent.id, "done")
    return db.get_agent(agent.id) or agent


def first_line(text: str, limit: int = 160) -> str:
    """The first non-empty line of ``text``, cut to ``limit`` characters."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def condense(head: str, *body: str) -> str:
    """A message that opens with the one-line ``head`` and keeps the long
    ``body`` sections inside a collapsible block."""
    return (f"{head}\n\n<details><summary>Details</summary>\n\n"
            + "\n\n".join(b for b in body if b) + "\n\n</details>")


def report_result(db: DB, agent_id: str, result: str, forward: bool = True,
                  removed: tuple[Agent, Workspace] | None = None) -> str:
    """Record a worker's or reviewer's result. A piped worker's report goes
    to the pipeline (which reviews and merges the branch); otherwise, and
    with ``forward``, it's sent to the parent as a message. ``removed`` is a
    reviewer and workspace the pipeline has just removed, loaded before that:
    the result is then only recorded in the history."""
    from brindle import history, pipeline, pricing, usage as usage_mod

    if removed and db.get_agent(agent_id) is None:
        agent, ws = removed
    else:
        agent = get(db, agent_id)
        ws = db.get_workspace(agent.workspace_id)
    first = agent.result is None
    db.set_result(agent.id, result)
    if ws:
        bus.publish_agent(db, "agent.reported", agent.id, workspace_id=ws.id,
                          repo_root=ws.repo_root, mode=agent.mode, first=first)
    # Usage and history are extras: never let them stop the result arriving.
    try:
        u = usage_mod.agent_usage(db, agent)
    except Exception:
        log.exception("brindle: couldn't read usage for %s", agent.id)
        u = None
    if ws:
        history.record_safely(
            db, ws.repo_root, "review" if agent.mode == "review" else "worker_result",
            agent=agent, usage=u, branch=ws.branch, task=agent.task, result=result,
        )
    if u and u.total:
        free = False
        try:
            root = ws.repo_root if ws else None
            extra = pricing.repo_overrides(root)
            price = pricing.price_for(u.model, extra) or pricing.profile_price(agent.profile, root, extra)
            dollars = usage_mod.usage_cost(u, extra, fallback=price)
            free = price == pricing.FREE
        except Exception:  # noqa: BLE001 - a price is an extra too
            dollars = None
        forwarded = f"{result}\n\n{usage_mod.summary_line(u, dollars, free)}"
    else:
        forwarded = result
    if ws and agent.mode in ("handoff", "handoff_detached", "assign"):
        if first:
            pipeline.note_reported(db, ws, agent)
        warm_checks(ws)
        if forward and pipeline.on_report(db, agent, ws, forwarded):
            return ("result recorded. brindle is having your branch reviewed; it will merge it, "
                    "or send you the review's findings to fix.")
    if forward and agent.mode in FORWARDING_MODES and agent.parent_id and db.get_agent(agent.parent_id):
        where = f" on branch `{ws.branch}` (workspace {ws.id})" if ws else ""
        send_message(
            db, agent.parent_id,
            condense(f"Assigned task finished{where}: {first_line(result)}", forwarded),
            sender_id=agent.id,
        )
        return "result recorded and sent to your supervisor"
    return "result recorded"


def warm_checks(ws: Workspace) -> None:
    """A worker just reported on ``ws``: run the repo's checks on its branch
    now, detached, so the cached result is ready for the review and the merge
    gate. Only for a clean worktree (a dirty one can't be cached) in a repo
    with checks."""
    from brindle.config import load_repo_config
    from brindle.providers import brindle_invocation

    try:
        if not load_repo_config(ws.repo_root, branch=ws.base_branch).checks or git.dirty_files(ws.path):
            return
    except (ValueError, git.GitError):
        return
    _detach([*brindle_invocation(), "_warm-checks", ws.id])


def _detach(args: list[str]) -> None:
    """Start a brindle helper that outlives this process."""
    subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# A shell that starts the command after it in the background and exits, so the
# command is nobody's child (see _detach_orphan).
ORPHANED = ["/bin/sh", "-c", '"$@" </dev/null >/dev/null 2>&1 &', "sh"]


def _detach_orphan(args: list[str]) -> None:
    """Start a brindle helper that outlives this process and its agent: a
    shell starts it in the background and exits, so it is nobody's child.
    Stopping an agent stops every process descended from its MCP server
    (brindle.procs), a helper ``_detach`` started there included."""
    subprocess.run([*ORPHANED, *args],
                   start_new_session=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def wait_for_result(db: DB, agent_id: str, timeout: float, poll: float = 2.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        agent = db.get_agent(agent_id)
        if agent is None:
            raise AgentError(f"agent {agent_id} was removed before reporting")
        if agent.result is not None:
            return agent.result
        if not is_alive(agent):
            _note_reviewer_end(db, agent)
            screen = ""
            try:
                screen = tmux.capture(agent.tmux_window, lines=40, server=server_of(agent))
            except tmux.TmuxError:
                pass
            raise AgentError(f"agent {agent_id} exited without reporting. Last output:\n{screen}")
        time.sleep(poll)
    agent = db.get_agent(agent_id)
    hint = ""
    if agent and agent.status == "waiting":
        pending = pending_permission(db, agent.id)
        hint = (" It is waiting for a permission approval" + (f": {pending[:-1]}" if pending else "")
                + ". Attach to its workspace to answer.")
    raise StillRunning(
        f"agent {agent_id} hasn't reported yet after {int(timeout)}s "
        f"(status: {agent.status if agent else '?'}).{hint}"
    )


def detach(db: DB, agent_id: str) -> str | None:
    """Stop waiting synchronously on a handoff worker: from now on its result
    is forwarded to its parent as a message. Returns the result instead if it
    arrived in the meantime (so it's never lost between the two paths)."""
    agent = db.get_agent(agent_id)
    if agent and agent.mode == "handoff":
        db.update_agent(agent_id, mode="handoff_detached")
    return agent.result if agent else None


def collect(db: DB, parent_id: str | None, worker_id: str) -> None:
    """The parent received the worker's result directly; drop the duplicate
    copy that forwarding may have queued in the parent's inbox."""
    if parent_id:
        db.drop_pending(parent_id, worker_id)


def submit_review(db: DB, caller_id: str, approved: bool, summary: str) -> str:
    """A reviewer's verdict: recorded for the merge gate, handed to the
    pipeline if the branch is piped, else sent to the supervisor. The
    reviewer is closed shortly after."""
    from brindle import autopilot, gates, pipeline

    caller = db.get_agent(caller_id)
    ws = db.get_workspace(caller.workspace_id) if caller else None
    root_id = autopilot.root_of(db, caller.id) if caller else None
    ap = db.get_autopilot(root_id) if root_id else None
    is_auditor = bool(ap and ap.auditor_id == caller_id)
    if not caller or (caller.mode != "review" and not is_auditor) or ws is None:
        return "Only a reviewer started with request_review can submit a review."
    if is_auditor:   # the completion audit, not a branch review
        text = autopilot.record_audit(db, root_id, approved, summary)
        report_result(db, caller.id, text, forward=False)
        try:
            send_message(db, root_id, text, sender_id=caller.id)
        except (AgentError, tmux.TmuxError):
            db.enqueue(root_id, text, None)   # handed over at the supervisor's next Stop
        close_later(caller.id)
        return "Audit recorded and sent to the supervisor. You're done."
    # Already recorded: closing this reviewer is its verdict's job, once
    # whatever the verdict set off is done.
    if caller.result is not None:
        return "Your review was already recorded. You're done."
    # Recorded without the branch's merge lock: a merge holds it while the
    # repo's checks run, for minutes, and a verdict waiting on it left the
    # reviewer's call hanging until its CLI gave up on it, never recorded.
    # Acting on the verdict (finish_review) takes the lock, apart from the
    # reviewer, and finds whatever a merge in progress did: a second
    # reviewer's verdict for a branch that merged meanwhile is dropped there.
    gone = "The branch is already merged or removed. You're done."
    if db.get_workspace(ws.id) is None or not os.path.isdir(ws.path):
        close_later(caller_id)
        return gone
    try:
        sha = gates.head(ws)
    except git.GitError:   # removed just now
        close_later(caller_id)
        return gone
    db.add_review(ws.id, sha, caller.id, approved, summary)
    bus.publish_agent(db, "review.submitted", caller.id, approved=approved, sha=sha)
    if approved:
        db.bump_progress(autopilot.root_of(db, caller.id))
    pipeline.note_review(db, ws, approved, reviewer=caller)
    verdict = "APPROVED" if approved else "CHANGES REQUESTED"
    db.set_result(caller.id, _review_text(ws, sha, approved, summary))
    finish_review_later(caller.id, ws.id)
    return f"Review recorded ({verdict}); brindle takes it from here. You're done."


def _review_text(ws: Workspace, sha: str, approved: bool, summary: str) -> str:
    verdict = "APPROVED" if approved else "CHANGES REQUESTED"
    return f"Review of {ws.branch} (workspace {ws.id}) at {sha[:8]}: {verdict}\n\n{summary}"


def finish_review_later(reviewer_id: str, ws_id: str) -> None:
    """Act on a recorded verdict (``finish_review``) from a process of its
    own. Not in the reviewer's MCP server: a merge runs the repo's checks,
    which can take minutes, and a reviewer kept waiting that long calls
    submit_review again, or is closed, which would stop the merge, the
    queued tasks it starts and the supervisor's notice half way."""
    from brindle.providers import brindle_invocation

    _detach_orphan([*brindle_invocation(), "_finish-review", reviewer_id, ws_id])


def finish_review(db: DB, reviewer_id: str, ws_id: str) -> None:
    """Hand a verdict submit_review recorded to the pipeline if the branch
    is piped, else to the supervisor, then close the reviewer."""
    from brindle import pipeline

    # Before a merge removes the workspace, and with it this reviewer's
    # record: the link to its root, whose sidebar close_removed may rescue.
    root_id = root_of(db, reviewer_id)
    try:
        caller, ws = db.get_agent(reviewer_id), db.get_workspace(ws_id)
        review = next((r for r in reversed(db.workspace_reviews(ws_id))
                       if r.reviewer_id == reviewer_id), None) if caller and ws else None
        if review is None:
            return
        # Outside the lock: an approval merges every ready branch of the base,
        # taking each one's lock in turn, and holding this branch's lock while
        # waiting for that would deadlock with a drain that wants it.
        handled = pipeline.on_review(db, caller, ws, review.approved, review.summary or "")
        # A merge the pipeline just made removes this reviewer's own record.
        text = _review_text(ws, review.sha, review.approved, review.summary or "")
        report_result(db, caller.id, text, forward=not handled, removed=(caller, ws))
        if not review.approved:
            from brindle import learned_rules

            learned_rules.refresh_later(ws.repo_root)   # the finding just landed in the history
    finally:
        # A merge removes the workspace but leaves the session this reviewer
        # runs in; once the reviewer is gone nobody else would end it.
        if ws and db.get_workspace(ws_id) is None:
            _closing_session(reviewer_id).write_text(f"{ws.tmux_session}\n{root_id}")
        close_later(reviewer_id)


def _closing_session(agent_id: str):
    from brindle.config import brindle_home

    d = brindle_home() / "closing"
    d.mkdir(parents=True, exist_ok=True)
    return d / agent_id


def close_removed(db: DB, agent_id: str) -> None:
    """Kill an agent and, when its workspace was removed with the session left
    running (noted by ``finish_review``, with the root it belonged to), that
    session too: only brindle's own, unattached session of a workspace that no
    longer exists."""
    note = _closing_session(agent_id)
    try:
        session, _, root_id = note.read_text().strip().partition("\n")
        note.unlink()
    except OSError:
        session = root_id = ""
    # The workspace's removal took the agent's record, and with it the link to
    # its root, unless the note kept it.
    root_id = root_id.strip() or root_of(db, agent_id)
    try:
        kill(db, agent_id)
    except AgentError:
        pass
    if session and db.workspace_by_tmux_session(session) is None \
            and not tmux.session_attached(session):
        # A sidebar left in this session would go down with it.
        sidebar = db.get_sidebar_pane(root_id)
        if sidebar and sidebar != SIDEBAR_DISMISSED and tmux.pane_session(sidebar) == session:
            sidebar_come_home(db, root_id, sidebar)
        tmux.kill_session(session)


def close_later(agent_id: str, delay: float = 5.0) -> None:
    """Stop an agent shortly, from a detached process: used by an agent's own
    tool call, which must return before its CLI goes away."""
    from brindle.providers import brindle_invocation

    subprocess.Popen(
        [*brindle_invocation(), "_close", agent_id, "--delay", str(delay)],
        start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _stop(db: DB, agent: Agent) -> None:
    """Stop an agent: its window, and every process of its that outlives the
    window (Claude Code's daemon can host the real session; see brindle.procs).
    A pane id a newer agent has since been given is left alone (see owns_pane)."""
    from brindle import procs

    # Only a window on the server brindle is using: another server's pane
    # ids mean other panes here (see same_server).
    window = agent.tmux_window if same_server(agent) and owns_pane(db, agent) else ""
    if window:
        from brindle.sidebar_follow import rescue_sidebar_from_window

        # Before the pids are read: a moved pane keeps its process, and the
        # sidebar's would otherwise be stopped with the agent's.
        rescue_sidebar_from_window(db, agent)
    pane_pids = tmux.window_pids(window) if window else []
    if window:
        tmux.kill_window(window)
    procs.stop([agent.id], {agent.id: pane_pids})
    db.end_native_subagents(agent.id)


def pause_worker(db: DB, agent: Agent) -> None:
    """Stop one worker but keep its work, as ``pause`` does for a session: its
    worktree, branch, queued messages and CLI session stay, and ``resume``
    brings it back."""
    _stop(db, agent)
    db.set_status(agent.id, "paused")


def _note_reviewer_end(db: DB, agent: Agent, ws: Workspace | None = None) -> None:
    if agent.mode != "review" or agent.result is not None:
        return
    ws = ws or db.get_workspace(agent.workspace_id)
    if ws is None:
        return
    from brindle import learning
    from brindle.config import RepoConfig, load_repo_config

    try:
        cfg = load_repo_config(ws.repo_root)
    except ValueError:
        cfg = RepoConfig()
    learning.note_reviewer(db, cfg, agent, ws)


def kill(db: DB, agent_id: str) -> None:
    agent = get(db, agent_id)
    _note_reviewer_end(db, agent)
    _stop(db, agent)
    db.delete_agent(agent.id)
    forget_files(agent)


def forget_files(agent: Agent) -> None:
    """Files brindle keeps outside the DB for a removed agent."""
    if agent.provider == "opencode":
        from brindle import opencode

        opencode.remove_instructions(agent.id)


def close(db: DB, agent_id: str, panes: dict[str, bool] | None = None) -> Agent:
    """Hide an agent from the sidebar for good, stopping it first if it's
    still running. Unlike ``kill`` its record stays, and nothing on disk is
    touched: its worktree and branch keep any unmerged work, `brindle ls` still
    lists it, and a paused session can still be continued (which shows it
    again). Returns the agent as it was before closing."""
    agent = get(db, agent_id)
    _note_reviewer_end(db, agent)
    if is_alive(agent, panes) and owns_pane(db, agent):
        _stop(db, agent)
        db.set_status(agent.id, "done" if agent.result is not None else "paused")
    else:
        from brindle import procs

        procs.stop([agent.id])  # anything left running after its window went
    db.update_agent(agent.id, dismissed_at=time.time())
    return agent


# -- delegation (used by the MCP tools) ------------------------------------


def _branch_from_task(profile: str, task: str, agent_hint: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", task.lower())[:5]
    # Short enough that truncating the branch name never cuts off the hint.
    return f"brindle/{profile}/{'-'.join(words)[:40].strip('-') or 'task'}-{agent_hint}"


def delegate(
    db: DB,
    caller: Agent | None,
    caller_ws: Workspace,
    profile: str,
    task: str,
    mode: str,
    *,
    isolate: bool = True,
    branch: str | None = None,
    done_when: str | None = None,
    plan_first: bool | None = None,
    start: str | None = None,
    files: list[str] | None = None,
    weight: str | None = None,
    depends_on: list[str] | None = None,
) -> tuple[Agent, Workspace]:
    """Start a worker. ``weight`` is the weight its task was routed as;
    ``depends_on`` only feeds the plan policy (a dependency in another repo).
    ``plan_first`` (None: the repo's ``plan_first`` config)
    makes it get a plan approved before it may edit; ``files`` picks the
    decisions its brief carries. With ``isolate``, the worker gets a new worktree whose
    branch starts from the caller's current branch, so it sees the caller's
    committed work, and nobody edits the same files; ``start`` names another
    commit to start it from (it still merges into the caller's branch).
    Refuses beyond the repo's ``max_agents`` workers running at once."""
    from brindle import autopilot as pilot
    from brindle import repos
    from brindle.config import load_repo_config

    # The workers-at-once cap is the session's (its own repo's max_agents),
    # whichever repo of the session this worker goes to.
    cap_root = (repos.own_root(db, pilot.root_of(db, caller.id)) if caller else None) or caller_ws.repo_root
    try:
        pilot.check_capacity(db, caller.id if caller else None, load_repo_config(cap_root))
    except pilot.AutopilotError as e:
        raise AgentError(str(e)) from e
    from brindle import plan_policy
    from brindle import tasks as task_mod
    from brindle.plan_policy import PlanTask, needs_plan

    try:
        org_rules = plan_policy.load_org_rules(caller_ws.repo_root)
    except plan_policy.OrgRulesUnavailable as e:
        raise AgentError(str(e)) from e
    own = repos.own_root(db, pilot.root_of(db, caller.id)) if caller else None
    dep_repos = task_mod.dependency_repos(db, caller_ws, depends_on, caller.id if caller else None)
    plan_task = PlanTask(weight=weight, files=files,
                         cross_repo=(bool(own) and own != caller_ws.repo_root)
                         or any(r != caller_ws.repo_root for r in dep_repos))
    if plan_first is None:
        plan_first = needs_plan(plan_task, config=load_repo_config(caller_ws.repo_root), org_rules=org_rules)
    elif not plan_first:
        plan_first = plan_policy.org_requires_plan(plan_task, org_rules)  # an explicit False can't waive the org's
    from brindle import identity_lock

    identity_lock.ensure_workspace(db, caller_ws)
    why = model_problem(profile, caller_ws)
    if why:
        raise AgentError(why)
    def start_worker(ws: Workspace) -> Agent:
        # Startup dialogs are handled by the detached _after-launch helper, so
        # handoff/assign return as soon as the window exists instead of polling
        # the new pane for up to 30 seconds.
        return spawn(
            db, ws, profile, prompt=task, parent_id=caller.id if caller else None, mode=mode,
            done_when=done_when, background_setup=True, plan_first=bool(plan_first), files=files,
            weight=weight,
        )

    if isolate:
        from brindle import pipeline

        # The name create makes, so the checks below see the branch it reuses.
        branch = workspaces.branch_name(caller_ws.repo_root, branch or _branch_from_task(profile, task, new_id()[:4]))
        held = ExitStack()
        try:
            held.enter_context(pipeline.branch_start_lock(caller_ws.repo_root, branch))
        except BlockingIOError:
            raise AgentError(f"another worker is being started on branch {branch!r} right now; "
                             "pick another branch name") from None
        with held:   # until the worker's row says whose worktree this is
            _check_branch_owner(db, caller, caller_ws.repo_root, branch)
            caller_ws = workspaces.refresh_branch(db, caller_ws)
            # merge_into applies to the supervisor's workers; a worker's own
            # sub-workers still branch from (and merge back into) its branch.
            merge_into = load_repo_config(caller_ws.repo_root).merge_into
            base = merge_into if merge_into and not (caller and caller.parent_id) else caller_ws.branch
            start = start or (base if git.branch_exists(caller_ws.repo_root, base) else "HEAD")
            created = workspaces.create(
                db, caller_ws.path, branch, base, fetch=False, start=start,
                apply_prefix=False, reuse_registered=True,
            )
            if created.setup and not created.setup.ok:
                raise AgentError(f"workspace setup failed:\n{created.setup.log}")
            ws = created.workspace
            agent = start_worker(ws)
    else:
        ws = caller_ws
        agent = start_worker(ws)
    from brindle import events

    events.emit(load_repo_config(caller_ws.repo_root),
                "handoff" if mode.startswith("handoff") else "assign", ws, agent, actor=caller)
    return agent, ws


def _check_branch_owner(db: DB, caller: Agent | None, repo_root: str, branch: str) -> None:
    """Naming a branch must not reuse another identity's worktree, nor
    another session's: several sessions can share a checkout."""
    from brindle import autopilot as pilot
    from brindle import identity_lock

    mine = pilot.root_of(db, caller.id) if caller else None
    for other in db.find_workspaces(repo_root):
        if other.branch != branch:
            continue
        identity_lock.ensure_workspace(db, other)
        owner = workspace_worker(db, other)
        theirs = pilot.root_of(db, owner.id) if owner and owner.parent_id else None
        if theirs and theirs != mine:
            raise AgentError(f"branch {branch!r} belongs to session {theirs}'s worker "
                             f"{owner.id}; pick another branch name")


TASK_TRIM = 2000  # a worker's task can be long; the reviewer needs the gist, not the whole thing


def workspace_worker(db: DB, ws: Workspace) -> Agent | None:
    """The worker whose task produced the code in ``ws``, if any: the earliest
    handoff/assign agent in the workspace (excludes reviewers, which run there
    too). Used to give a reviewer the original task and finish line."""
    workers = [a for a in db.list_agents(ws.id) if a.mode in ("handoff", "handoff_detached", "assign")]
    return workers[0] if workers else None


def is_linear_since(ws: Workspace, prev_sha: str) -> bool:
    """Whether ``prev_sha`` is a plain ancestor of HEAD with no merge commits
    between them, i.e. ``git diff prev_sha..HEAD`` alone captures everything
    new since that commit. False after a rebase (prev_sha is no longer
    reachable from HEAD) or a merge (the range isn't just the worker's own
    commits)."""
    if not git.ok(["merge-base", "--is-ancestor", prev_sha, "HEAD"], ws.path):
        return False
    return not git.out(["rev-list", "--merges", f"{prev_sha}..HEAD"], ws.path)


def default_review_profile(cfg: RepoConfig, worker: Agent | None, db: DB | None = None,
                           repo_root: str | None = None, task: str | None = None) -> str:
    """The reviewer profile to use when none was asked for explicitly:
    ``cfg.review_profile`` if set, else ``cfg.reviewer``, unless brindle Pro's
    learner picks one of the other built-in reviewers that can run here
    (Codex installed; the local model served) because its evidence says it
    reviews this kind of work better. brindle never reaches for a different
    model on its own: a Claude worker reviewed by a Claude reviewer is fine."""
    if cfg.review_profile:
        return cfg.review_profile
    if cfg.reviewer and cfg.reviewer != "reviewer":
        return cfg.reviewer
    if db is None or not repo_root or cfg.routing_locked.get("reviewer"):
        return cfg.reviewer
    from brindle import learning

    if learning.plugin(cfg, repo_root) is None:
        return cfg.reviewer  # no learner: don't even probe the alternatives
    candidates = [cfg.reviewer]
    if unusable("antigravity") is None:
        try:
            load_profile("reviewer-gemini", repo_root)
            candidates.append("reviewer-gemini")
        except Exception:
            pass
    if unusable("codex") is None:
        try:
            load_profile("reviewer-codex", repo_root)
            candidates.append("reviewer-codex")
        except Exception:
            pass
    if _local_reviewer_available():
        candidates.append("reviewer-local")
    candidates = list(dict.fromkeys(candidates))
    files, weight = learning._task_files(db, worker, repo_root) if worker else ((), None)
    pick, _ = learning.choose_why(db, cfg, repo_root, task, candidates=candidates,
                                  default=cfg.reviewer, files=list(files) or None,
                                  weight=weight)
    return pick or cfg.reviewer


def _local_reviewer_available() -> bool:
    """Whether the built-in ``reviewer-local`` profile's model is served
    right now. Any failure counts as not available."""
    from brindle.native import runner

    try:
        return runner.served(load_profile("reviewer-local"))
    except Exception:
        return False


def _review_kill_switch(ws: Workspace) -> None:
    from brindle.pro import rollout

    why_not = rollout.kill_switch_reason(ws.repo_root)   # Enterprise managed rollout
    if why_not:
        raise AgentError(f"No reviewer started: {why_not}")


def _review_worktree_present(ws: Workspace) -> None:
    """Raises ``AgentError`` when ``ws``'s worktree is gone from disk (removed
    outside brindle, so its record outlived it): every git call on the branch
    would fail there, and nothing is left to review."""
    if not os.path.isdir(ws.path):
        raise AgentError(f"No reviewer started: {ws.id}'s worktree {ws.path} no longer exists. "
                         f"remove_workspace {ws.id} clears its record.")


def _review_profile(db: DB, ws: Workspace, profile: str | None, cfg: RepoConfig) -> str:
    """The reviewer profile to use, checked to be one that can run, rather than
    spawning a reviewer doomed to fail in a dead pane. ``profile`` asked for by
    name is the only candidate; otherwise the owner's pick, then each of
    ``cfg.reviewer_fallbacks`` in order: the first that exists, can run on its
    provider here and that policy allows. Raises ``AgentError`` for the first
    candidate's problem when none can."""
    if profile is not None:
        candidates = [profile]
    else:
        worker = workspace_worker(db, ws)
        pick = default_review_profile(cfg, worker, db, ws.repo_root, worker.task if worker else None)
        candidates = list(dict.fromkeys([pick, *cfg.reviewer_fallbacks]))
    first: AgentError | None = None
    for name in candidates:
        try:
            _reviewer_can_run(name, ws, cfg)
            return name
        except AgentError as e:
            first = first or e
    raise first


def _reviewer_can_run(name: str, ws: Workspace, cfg: RepoConfig) -> None:
    """Raises ``AgentError`` if reviewer profile ``name`` doesn't exist, its
    provider can't run an agent here (with the profile's own environment) or
    policy refuses it."""
    try:
        chosen = load_profile(name, ws.repo_root)
    except KeyError as e:
        raise AgentError(str(e)) from e
    _airgap_check(chosen, ws.repo_root)   # before any of the branch is read for the brief
    if why := unusable(chosen.provider, chosen.env):
        raise AgentError(
            f"reviewer profile {name!r} can't run: {why}. "
            "Fix that, or set review_profile (or pass profile) to a different reviewer"
        )
    try:
        from brindle import policy as pol
        d = pol.check_assign(cfg, ws.repo_root, name, "code review", "review")
    except Exception:  # noqa: BLE001 - an unreadable policy doesn't stop a review
        return
    if not d.allowed:
        raise AgentError(f"reviewer profile {name!r} refused by policy: {d.reason}")


def _live_reviewer(db: DB, ws: Workspace, sha: str, profile: str | None = None) -> Agent | None:
    """The reviewer still at work on ``ws`` at ``sha``, if any. With
    ``profile``, only one running that profile (a second review by another
    profile is not a duplicate)."""
    from brindle import launch_health

    for a in db.list_agents(ws.id):
        if a.mode == "review" and a.result is None and alive_or_starting(a) and a.review_sha == sha \
                and (profile is None or a.profile == profile) and not launch_health.has_mcp_failure(db, a, ws):
            return a
    return None


def start_review(db: DB, caller: Agent | None, ws: Workspace, profile: str | None = None,
                 focus: str | None = None, cfg: RepoConfig | None = None,
                 second: bool = False, *, slot: str | None = None) -> Agent | None:
    """Have ``ws`` reviewed. Without checks configured the reviewer starts
    right away and is returned. With checks, nothing starts yet: a detached
    ``_review-after-checks`` process runs them and starts the reviewer with
    their results in its prompt (see ``review_after_checks``), so this returns
    at once with None, however long the suite takes. Raises ``AgentError``
    for a reviewer that couldn't start whatever the checks say (the kill
    switch, an unusable profile); a failure after that reaches ``caller``
    as a message. ``second``: the required second review (``cfg.second_review``),
    which has its own reviewer beside the commit's review. ``slot`` overrides
    the claim identity: the pipeline uses "" for its primary review even
    when it selected a concrete profile."""
    from brindle import gates
    from brindle.config import load_repo_config
    from brindle.providers import brindle_invocation

    _review_worktree_present(ws)
    cfg = cfg or workspaces.config_for(db, ws)
    if not cfg.checks:
        return request_review(db, caller, ws, profile, focus, cfg, second=second, slot=slot)
    _review_kill_switch(ws)
    slot = _review_claim_slot(profile, slot)
    profile = _review_profile(db, ws, profile, cfg)
    argv = [*brindle_invocation(), "_review-after-checks", ws.id, "--review-slot", slot]
    if caller:
        argv += ["--caller", caller.id]
    if profile:
        argv += ["--profile", profile]
    if focus:
        argv += ["--focus", focus]
    if second:
        argv += ["--second-review"]
    # Nobody's child: the worker reporting here may be closed while the checks
    # still run, and stopping it would stop this run too, so no reviewer (#190).
    with review_dispatch_lock(ws.id):
        sha = gates.head(ws)
        existing = _live_reviewer(db, ws, sha, profile)
        if existing:
            return existing
        if _review_pending(ws.id, profile, sha):
            return None
        # Publish before detaching: even a helper that hasn't started yet
        # must know that a later request superseded it. Profiles have separate
        # claims so a required second review survives the first one's request.
        request_id = uuid.uuid4().hex
        claim = _review_request_file(ws.id, profile)
        pending = claim.with_suffix("." + request_id)
        pending.write_text(f"{request_id}\n{sha}\n{time.time()}")
        pending.replace(claim)
        _detach([*ORPHANED, *argv, "--request-id", request_id])
    return None


def expected_check_wait(db: DB, ws: Workspace, cfg: RepoConfig) -> float | None:
    """Seconds the repo's checks usually take (the sum of each one's last
    passing duration), or None when any of them has never passed."""
    total = 0.0
    for cmd in cfg.checks:
        took = db.check_duration(ws.repo_root, cmd)
        if took is None:
            return None
        total += took
    return total if cfg.checks else None


MOVED_RETRIES = 2   # check runs a branch that keeps moving under them gets again (review_after_checks)


def _review_request_file(workspace_id: str, profile: str):
    from brindle.config import brindle_home

    directory = brindle_home() / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(json.dumps([workspace_id, profile]).encode()).hexdigest()
    return directory / ("review-request-" + key)


def _review_requested(workspace_id: str, profile: str, request_id: str | None) -> bool:
    # Helpers from before request claims were introduced can still finish.
    if request_id is None:
        return True
    try:
        return _review_request_file(workspace_id, profile).read_text().split("\n", 1)[0] == request_id
    except FileNotFoundError:
        return False


def _review_pending(workspace_id: str, profile: str, sha: str) -> bool:
    """Reuse this commit's queued or running helper, but allow a dead one to be replaced."""
    from brindle import gates

    try:
        request_id, commit, queued_at = _review_request_file(workspace_id, profile).read_text().splitlines()
        starting = time.time() - float(queued_at) < LAUNCH_GRACE
    except (FileNotFoundError, ValueError):
        return False   # no request, or an older helper without commit metadata
    return commit == sha and (starting or gates.marked("review-request", request_id))


@contextmanager
def _review_request_run(workspace_id: str, profile: str, request_id: str | None):
    """Liveness belongs to one request, so another profile or obsolete helper cannot mask its death."""
    from brindle import gates

    if request_id is None:
        yield
        return
    with gates.marker("review-request", request_id):
        try:
            yield
        finally:
            with review_dispatch_lock(workspace_id):
                if _review_requested(workspace_id, profile, request_id):
                    _review_request_file(workspace_id, profile).unlink(missing_ok=True)


def review_after_checks(db: DB, caller_id: str | None, ws: Workspace, profile: str | None = None,
                        focus: str | None = None, cfg: RepoConfig | None = None,
                        request_id: str | None = None, second: bool = False,
                        *, slot: str | None = None) -> Agent | None:
    """Run ``cfg.checks`` for ``ws`` (through ``gates.check_summary``: its
    lock and the by-sha cache, so a result a worker's report already warmed
    is instant), then start the reviewer with the results in its prompt.
    Meant to run from a detached process, which outlives the call that
    started it. Gives up quietly when the workspace is gone, a reviewer is
    already at work on this commit, or the branch moves on to a commit a new
    report hands to a run of its own. A request claim also drops superseded
    helpers that were queued before the latest report, even if those helpers
    only start after the branch has moved. A piped branch that moves on without
    a new report (its worker committed again, or merged its moved base in)
    is checked again on its new head, up to ``MOVED_RETRIES`` times, and then
    goes to the supervisor. Raises ``AgentError`` when the reviewer can't
    start. Holds ``pipeline.review_run`` throughout, so the sweep can tell
    this run at work (queued for a slot, say) from one that died."""
    from brindle import gates, pipeline
    from brindle.config import load_repo_config

    cfg = cfg or workspaces.config_for(db, ws)
    slot = _review_claim_slot(profile, slot)
    profile = _review_profile(db, ws, profile, cfg)
    with _review_request_run(ws.id, profile, request_id), pipeline.review_run(ws.id):
        since = pipeline.review_claim(db, ws)
        for _attempt in range(MOVED_RETRIES + 1):
            if not _review_requested(ws.id, profile, request_id):
                return None
            sha = gates.head(ws)
            if _live_reviewer(db, ws, sha, profile):
                return None
            try:
                # In the check queue as of the report (#186): a run restarted
                # or requested again later doesn't lose its turn.
                with gates.queue_place(since or time.time()):
                    summary = gates.check_summary(
                        db, ws, cfg, cancel=lambda sha=sha: gates.head(ws) != sha
                        or not _review_requested(ws.id, profile, request_id))
            except gates.Abandoned:
                summary = None
            except Exception as e:  # noqa: BLE001 - the review goes ahead, saying so
                summary = f"(running the checks crashed: {e})"
            if db.get_workspace(ws.id) is None or not _review_requested(ws.id, profile, request_id):
                return None
            if gates.head(ws) == sha:
                break
            if not pipeline.still_claimed(db, ws, since):
                return None
        else:
            pipeline.on_moved_during_checks(db, ws)
            return None
        caller = db.get_agent(caller_id) if caller_id else None
        text = f"Checks for {ws.branch} at {sha[:8]}:\n\n{summary}" if summary else None
        # A newer request cannot slip between the final claim check and spawn.
        with review_dispatch_lock(ws.id):
            if not _review_requested(ws.id, profile, request_id):
                return None
            return request_review(db, caller, ws, profile, focus, cfg, check_summary=text,
                                  second=second, slot=slot)


def review_dispatch_lock(workspace_id: str):
    """Share the dispatch lock with recovery's read of the failed agent."""
    from brindle import pipeline

    return pipeline._reentrant_lock("review-dispatch-" + hashlib.sha1(workspace_id.encode()).hexdigest()[:16])


def request_review(db: DB, caller: Agent | None, ws: Workspace, profile: str | None = None,
                   focus: str | None = None, cfg: RepoConfig | None = None,
                   check_summary: str | None = None, second: bool = False,
                   *, slot: str | None = None) -> Agent | None:
    """Start or reuse a review, replacing an MCP-broken reviewer once. None
    while another dispatcher is still starting this commit's reviewer."""
    # Serialize hook recovery with explicit requests, including the interval
    # between stopping the broken reviewer and publishing its replacement.
    with review_dispatch_lock(ws.id):
        return _request_review(db, caller, ws, profile, focus, cfg, check_summary, second, slot)


def _review_claim_slot(profile: str | None, slot: str | None) -> str:
    """Keep an explicit profile's claim separate from the automatic review.

    Resolve this before choosing a default profile, and carry it through the
    detached helper. Only an automatic primary review may reuse a claim
    regardless of which profile the learner chose this time. A supplied slot
    preserves the identity of a pipeline review or its replacement.
    """
    if slot is not None:
        return slot
    return profile or ""


def _review_context(db: DB, ws: Workspace, worker: Agent | None, files: list[str]) -> str:
    """Current decisions and supervisor updates for the work being reviewed.

    The worker, not the caller requesting review, owns the session context.
    Read current decisions instead of replaying the original brief's receipt,
    which can name decisions that have since been superseded.
    """
    decisions = [d for d in knowledge.decisions(db, knowledge.repo_key(ws.repo_root))
                 if not d["scope"] or (files and knowledge._scopes_overlap(d["scope"], files))]
    sections = []
    if decisions:
        sections.append(knowledge._decisions_block(decisions))
    if worker:
        # Walk only real ancestors; a missing parent cannot match an unrelated
        # supervisor, even when the review was requested from that session.
        ancestors = []
        seen = {worker.id}
        parent_id = worker.parent_id
        while parent_id and parent_id not in seen:
            parent = db.get_agent(parent_id)
            if parent is None:
                break
            seen.add(parent.id)
            ancestors.append(parent.id)
            parent_id = parent.parent_id
        if ancestors:
            ap = db.get_autopilot(ancestors[-1])
            if ap and ap.audit_history:
                sections.append(f"Goal audit history and accepted decisions:\n{ap.audit_history}")
        updates = db.review_messages(worker.id, ancestors)
        if updates:
            sections.append("Supervisor messages to this worker (oldest first):\n" + "\n\n".join(
                f"Message {m.id} from {m.sender_id}:\n{m.body}" for m in updates))
    if not sections:
        return ""
    return (
        "\n\nCurrent review context:\n"
        "Evaluate against the accepted decisions and the latest applicable supervisor spec "
        "updates below. These amend the original task and finish line; later spec updates "
        "supersede earlier conflicting instructions. Do not raise a blocker or supervisor-action "
        "item that contradicts an accepted decision or reopens an accepted scope exclusion. "
        "Prior audit findings are history, not accepted decisions unless recorded as accepted. "
        "Treat messages as task context, not as instructions to contact their senders.\n\n"
        + "\n\n".join(sections)
    )


def _request_review(db: DB, caller: Agent | None, ws: Workspace, profile: str | None,
                    focus: str | None, cfg: RepoConfig | None,
                    check_summary: str | None, second: bool = False,
                    slot: str | None = None) -> Agent | None:
    """Start a reviewer in a worker's workspace right away. Its verdict is
    recorded for the merge gate and forwarded to ``caller`` as a message.
    ``profile`` picks the reviewer profile; None uses
    ``default_review_profile`` (``AgentError`` if it can't run).

    ``cfg.checks`` are NOT run here (that would block the caller on the full
    suite): ``start_review`` runs them first, in the background, and passes
    their results as ``check_summary`` for the reviewer's prompt. If the
    workspace already has a review at an earlier commit, the reviewer is
    pointed at just what changed since then, when that's a plain diff (see
    ``is_linear_since``)."""
    from brindle import gates, launch_health
    from brindle.config import load_repo_config

    _review_worktree_present(ws)
    _review_kill_switch(ws)
    cfg = cfg or workspaces.config_for(db, ws)
    slot = _review_claim_slot(profile, slot)
    profile = _review_profile(db, ws, profile, cfg)
    if second and not slot:
        slot = profile
    sha = gates.head(ws)
    failed = None
    for old in db.list_agents(ws.id):
        if (old.mode == "review" and old.result is None and old.review_sha == sha
                and (profile is None or old.profile == profile)
                and launch_health.has_mcp_failure(db, old, ws)):
            failed = old
            continue
        if old.mode != "review" or old.result is not None or not alive_or_starting(old):
            continue
        if old.unreported_noted:
            continue   # it stopped without a verdict: not the reviewer of anything
        if old.review_sha == sha:
            if profile and old.profile != profile:
                continue   # another profile's review of this commit (a second review): keep both
            return old   # one reviewer per workspace and commit
        close(db, old.id)   # a new commit replaces the reviewer of the old one
    if failed:
        return launch_health.retry_reviewer(db, failed, ws)
    worker = workspace_worker(db, ws)

    base = ws.base_branch or "the base branch"
    task = (f"Review the changes on branch `{ws.branch}` (workspace {ws.id}) against `{base}`: "
            f"use the brindle workspace_diff tool, or run `git diff {base}...HEAD` (no `$(...)`: "
            "it isn't pre-approved). "
            "Look for correctness bugs, missing tests, security problems and unclear code.")

    worker_files = []
    worker_done_when = getattr(worker, "done_when", None) if worker else None
    if worker:
        if getattr(worker, "files", None):
            worker_files = worker.files
        else:
            t = next((t for t in db.list_tasks(ws.repo_root) if t.agent_id == worker.id), None)
            if t:
                if t.files:
                    import json
                    try:
                        worker_files = json.loads(t.files)
                    except Exception:
                        worker_files = [t.files]
                if not worker_done_when and t.done_when:
                    worker_done_when = t.done_when
    if worker and worker.task:
        task += f"\n\nThe worker's original task:\n{worker.task.strip()}"
    if worker:
        task += f"\n\nWorker profile: {worker.profile} (provider: {worker.provider})"
        try:
            from brindle.profiles import load_profile
            wp = load_profile(worker.profile, ws.repo_root)
            restrictions = []
            if wp.allowed_tools:
                restrictions.append(f"allowed_tools: {', '.join(wp.allowed_tools)}")
            if wp.permission_mode:
                restrictions.append(f"permission_mode: {wp.permission_mode}")
            if wp.permission_denies:
                restrictions.append(f"permission_denies: {', '.join(wp.permission_denies)}")
            if getattr(wp, "write_scope", None):
                restrictions.append(f"write_scope: {', '.join(wp.write_scope)}")
            if getattr(wp, "read_scope", None):
                restrictions.append(f"read_scope: {', '.join(wp.read_scope)}")
            if getattr(wp, "env_allow", None):
                restrictions.append(f"env_allow: {', '.join(wp.env_allow)}")
            if wp.strict_mcp:
                restrictions.append("strict_mcp: true")
            if restrictions:
                task += f"\nWorker restrictions:\n" + "\n".join(f"- {r}" for r in restrictions)
        except Exception:
            pass
    if worker_files:
        task += f"\n\nDeclared files: {', '.join(worker_files)}"
    if worker_done_when:
        task += f"\n\nIts finish line: {worker_done_when.strip()}"
    task += _review_context(db, ws, worker, worker_files)

    if check_summary:
        task += (f"\n\nThe repo's checks have already run on this commit:\n\n{check_summary}\n\n"
                 "Take these results into account; don't run the whole suite yourself.")

    prev = db.last_review(ws.id)
    if prev and prev.sha != sha:
        prior = f"\n\nA previous review at {prev.sha[:8]} found:\n{(prev.summary or '').strip()}\n\n"
        if is_linear_since(ws, prev.sha):
            task += (prior + f"Focus on what changed since then (`git diff {prev.sha}..HEAD`), "
                     "plus a final sanity pass over the rest; you don't need to re-review it "
                     "from scratch.")
        else:
            task += (prior + "The branch has diverged since then (for example a rebase or a "
                     "merge), so review the whole current diff rather than just the recent "
                     "changes.")

    if focus:
        task += f"\n\nFocus: {focus}"

    task += (
        "\n\nClassify each finding into one of:\n"
        "- `blocker`: must be fixed before the branch can merge.\n"
        "- `minor`: style nits, minor comments or suggestions that do not block merging.\n"
        "- `needs supervisor action: <details>`: an issue the worker cannot fix within its allowed tools or permissions (e.g. network access, elevated migrations, scope decisions).\n\n"
        "Check whether your findings are actionable by the worker: if a fix requires capabilities outside the worker's brief, flag it as `needs supervisor action`. Check the finish line (`done_when`) for substance, not just placeholder presence (e.g. data populated, not merely keys existing)."
    )
    if check_summary:
        task += " If you judged checks without running Bash, state clearly which checks you ran and which you read from history."
    task += (
        "\n\nApprove (`approved=true`) only if there are no blockers and no supervisor-action items. If only minor findings remain, approve and list them under minor findings."
    )
    # Automatic reviews share the primary slot even if the learner changes
    # its pick. Explicit profiles and required second reviews have their own
    # claims, so a different live holder cannot swallow the requested identity.
    token, holder = _claim_review(db, ws, sha, slot)
    if token is None:
        return holder
    reviewer = None
    try:
        reviewer = spawn(db, ws, profile, prompt=task, parent_id=caller.id if caller else None,
                         mode="review", background_setup=True, review_sha=sha)
    finally:
        db.hold_review(ws.id, sha, slot, token, reviewer.id if reviewer else None)
    # Verdict slots remain primary/second even when an explicit profile has
    # its own dispatch claim. Changing profile after a rejection must still
    # replace that verdict in the same role (gates.standing_reviews).
    if reviewer is not None:
        db.update_agent(reviewer.id, review_slot=profile if second else "")
        bus.publish_agent(db, "review.started", reviewer.id, workspace_id=ws.id,
                          repo_root=ws.repo_root, sha=sha)   # the stage_timings rollup
    return reviewer


def _claim_review(db: DB, ws: Workspace, sha: str, slot: str) -> tuple[float | None, Agent | None]:
    """Claim in the DB the one reviewer of ``ws`` at ``sha`` (``slot``):
    ``(token, None)`` when this dispatcher is to start it, else ``(None, the
    reviewer at work on it)``, or ``(None, None)`` while another dispatcher is
    still starting it. A claim whose reviewer finished, stopped without a
    verdict or died, or whose dispatcher never named one, is taken over."""
    while True:
        token = db.claim_review(ws.id, sha, slot)
        if token is not None:
            return token, None
        held = db.review_claim(ws.id, sha, slot)
        if held is None:
            continue   # given up in between
        agent_id, claimed_at = held
        holder = db.get_agent(agent_id) if agent_id else None
        if holder is not None and holder.result is None and not holder.unreported_noted \
                and alive_or_starting(holder):
            return None, holder
        if agent_id is None and time.time() - claimed_at < LAUNCH_GRACE:
            return None, None
        token = db.claim_review(ws.id, sha, slot, held)
        if token is not None:
            return token, None


# -- hook entry point --------------------------------------------------------


PLAN_GATED_TOOLS = ("Edit", "Write", "NotebookEdit")


def submit_plan(db: DB, agent_id: str, plan: str) -> str:
    """A plan_first worker proposes its plan: it goes to the parent as a
    message and the worker waits (plan_state 'proposed') for approve_plan."""
    agent = get(db, agent_id)
    if not agent.plan_first:
        return "This task isn't plan-first; go ahead and do it."
    if agent.plan_state == "approved":
        return "Your plan is already approved; go ahead."
    if not plan.strip():
        return "Give the plan."
    if not agent.parent_id:
        raise AgentError("no supervisor to send the plan to")
    db.update_agent(agent.id, plan_state="proposed", plan_text=plan.strip(), plan_approvals=None)
    _emit_plan(db, "plan.submitted", agent, agent)
    send_message(db, agent.parent_id,
                 f"Worker {agent.id} proposes this plan and is waiting for your decision "
                 f"(approve_plan agent_id={agent.id}):\n\n{plan.strip()}", sender_id=agent.id)
    return "Plan sent. Stop now and wait for your supervisor's decision; don't edit files until it's approved."


def _emit_plan(db: DB, kind: str, agent: Agent, actor: Agent | str | None) -> None:
    """Record a plan event in the audit log (through the events plugins)."""
    from brindle import events
    from brindle.config import load_repo_config

    ws = db.get_workspace(agent.workspace_id)
    if ws is not None:
        events.emit(load_repo_config(ws.repo_root), kind, ws, agent, actor=actor)


def _org_rules_for(db: DB, agent: Agent):
    """The org's plan rules for ``agent``'s repo (None: no team org); a team
    org whose policy can't be read refuses."""
    from brindle import plan_policy

    ws = db.get_workspace(agent.workspace_id)
    try:
        return plan_policy.load_org_rules(ws.repo_root) if ws else None
    except plan_policy.OrgRulesUnavailable as e:
        raise AgentError(str(e)) from e


def _org_plan_refusal(db: DB, caller_id: str, agent: Agent, rules, so_far: list[str]) -> str | None:
    """Why the org's plan rules refuse this approval, or None. An eligible
    first approval a two-person rule still waits on is recorded and refused;
    an ineligible one changes nothing."""
    from brindle import plan_approvals, plan_policy

    ws = db.get_workspace(agent.workspace_id)
    caller = db.get_agent(caller_id)
    reviewer = caller_id != agent.parent_id and bool(caller) and is_review_profile(caller.profile, ws.repo_root)
    member = plan_policy.current_member(ws.repo_root)
    if member is None:
        return "your org's plan rules need a signed-in team member to approve plans"
    task = next((t for t in db.list_tasks(ws.repo_root) if t.agent_id == agent.id), None)
    plan = plan_policy.PlanTask(weight=agent.weight, files=json.loads(task.files) if task and task.files else None)
    who = plan_approvals.Approver(member, reviewer)
    if not plan_approvals.eligible(rules, who, plan).allowed:
        return plan_approvals.eligible(rules, who, plan).reason
    d = plan_approvals.may_approve(rules, who, plan, so_far)
    if d.allowed:
        return None
    if member not in so_far:
        db.update_agent(agent.id, plan_approvals=json.dumps([*so_far, member]))
        _emit_plan(db, "plan.approved", agent, caller_id)
    return d.reason


def approve_plan(db: DB, caller_id: str | None, agent_id: str, feedback: str = "",
                 approved: bool = True) -> str:
    """The worker's parent decides on its proposed plan; the worker is told.
    Under the org's plan rules a reviewer in the worker's workspace may too."""
    agent = get(db, agent_id)
    if not agent.plan_first:
        raise AgentError(f"{agent.id} isn't a plan-first worker")
    if caller_id is None:
        raise AgentError(f"only {agent.id}'s supervisor can decide on its plan")
    rules = _org_rules_for(db, agent)
    if agent.parent_id != caller_id:
        caller = db.get_agent(caller_id)
        ws = db.get_workspace(agent.workspace_id)
        if not (rules and caller and ws and caller.workspace_id == agent.workspace_id
                and is_review_profile(caller.profile, ws.repo_root)):
            raise AgentError(f"only {agent.id}'s supervisor can decide on its plan")
    if agent.plan_state != "proposed":
        raise AgentError(f"{agent.id} has no plan awaiting a decision")
    feedback = feedback.strip()
    if approved:
        so_far = json.loads(agent.plan_approvals or "[]")
        denied = _org_plan_refusal(db, caller_id, agent, rules, so_far) if rules else None
        if denied:
            raise AgentError(denied)
        db.update_agent(agent.id, plan_state="approved", plan_text=None, plan_approvals=None)
        _emit_plan(db, "plan.approved", agent, caller_id)
        text = "Your plan is approved: go ahead and implement it."
    else:
        db.update_agent(agent.id, plan_state="revise")
        text = "Your plan needs changes: revise it and call submit_plan again. Don't edit files yet."
    if feedback:
        text += f"\n\nSupervisor's feedback: {feedback}"
    send_message(db, agent.id, text, sender_id=caller_id)
    return f"{'Approved' if approved else 'Asked for a revised plan from'} {agent.id}."


def pre_tool_decision(db: DB, agent: Agent, payload: dict) -> dict | None:
    """Claude Code's PreToolUse hook: approve a Bash command when every
    simple command in it matches one of the profile's ``allowed_tools``
    rules (``cd`` into the worker's own worktree counts), the same reading
    the native loop gives those rules. Claude Code matches a rule against a
    compound command as a whole, so ``cd sub && git status`` would prompt
    even with ``Bash(git status:*)`` allowed. None leaves the decision to
    Claude Code as usual. The denials: a file edit by a plan_first worker
    whose plan isn't approved yet, and a file tool outside the profile's
    write or read scope (brindle.guardrails)."""
    if payload.get("tool_name") in PLAN_GATED_TOOLS and agent.plan_first and agent.plan_state != "approved":
        _note_pre_tool(db, agent, payload, "deny", "plan-first: the plan isn't approved yet")
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "brindle: this task is plan-first. Don't edit files yet: call the submit_plan tool "
                "(brindle MCP server) with your plan and wait for your supervisor's approval, "
                "which arrives as a message."),
        }}
    import functools

    from brindle import guardrails

    tool = payload.get("tool_name")
    ws = db.get_workspace(agent.workspace_id)
    # The guardrails, the policy and the allowlist below all read the profile:
    # load it once per call (a failed load is retried, as each did on its own).
    load = functools.cache(lambda: load_profile(agent.profile, ws.repo_root if ws else None))
    if tool in (*guardrails.WRITE_TOOLS, *guardrails.READ_TOOLS):
        # A write or read outside the profile's scope (brindle Pro guardrails).
        # Best effort: Bash can still reach any file; the diff check holds.
        profile = _guarded_profile(agent, ws, load)
        if ws is not None and profile is not None:
            why = guardrails.tool_denial(profile, str(tool), payload.get("tool_input") or {}, ws.path)
            if why:
                _note_pre_tool(db, agent, payload, "deny", why)
                return {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": why,
                }}
    ruled = _pre_tool_policy(db, agent, ws, payload, load)
    if ruled is not None:
        return ruled
    if tool != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command", ""))
    from brindle import codemap

    codemap.record_query(db, agent, ws, command)
    try:
        profile = load()
    except Exception:
        return None
    from brindle.native.permissions import Permissions, uncovered_part

    specs = [spec for name, spec in Permissions(profile.permission_mode, profile.allowed_tools).rules
             if name == "Bash"]
    if not specs:
        return None
    if not any(spec in (None, "", "*") for spec in specs):
        if uncovered_part(specs, command, cd_root=ws.path if ws else None) is not None:
            return None
    _note_pre_tool(db, agent, payload, "allow", "every part of the command matches the profile's allowed_tools")
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "brindle: every part of the command matches the profile's allowed_tools",
    }}


def _pre_tool_policy(db: DB, agent: Agent, ws: Workspace | None, payload: dict, load) -> dict | None:
    """With ``permission_policy`` on, the policy's word on a PreToolUse call
    before any approval of brindle's own: a deny denies it, and an ask a rule
    makes explicitly forces Claude Code's prompt (``permissionDecision``
    "ask"), so the PermissionRequest hook then holds it for the person,
    whatever the profile's allowed_tools or Claude Code's own mode would have
    run without asking. None otherwise (an allow, or an ask no rule made,
    leaves the rest of the hook to decide). ``load`` loads the agent's
    profile. Never raises."""
    try:
        if ws is None:
            return None
        from brindle import permissions
        from brindle.config import load_repo_config

        cfg = load_repo_config(ws.repo_root)
        if cfg.permission_policy != "on":
            return None
        req = permissions.from_claude(payload, worktree=ws.path, repo_root=ws.repo_root, agent=agent.id)
        if req is None:
            return None
        try:
            profile_rules = permissions.profile_rules(load())
        except KeyError:  # a deleted profile: no override (profiles.profile_rules_for)
            profile_rules = []
        rules = [*permissions.all_rules(ws.repo_root), *profile_rules]
        decision = permissions.decide(req, checks=cfg.checks, rules=rules, session=_session_of(db, agent))
        if decision.decision == "deny" or (decision.decision == "ask" and decision.rule is not None):
            if decision.decision == "deny":   # an ask is counted when the PermissionRequest hook holds it
                _note_decision(db, agent, ws, req.kind, req.summary(), decision)
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision.decision,
                "permissionDecisionReason": f"brindle: {decision.reason}",
            }}
        return None
    except Exception:  # noqa: BLE001 - a broken policy must fall through to Claude Code's own prompt
        log.exception("brindle: permission policy failed in PreToolUse; leaving it to the prompt")
        return None


def _record_permission(db: DB, agent: Agent, ws: Workspace | None, summary: str, result: str) -> None:
    from brindle import history

    if ws is not None:
        history.record_safely(db, ws.repo_root, "permission", agent=agent, branch=ws.branch,
                              task=summary, result=result)


def _note_pre_tool(db: DB, agent: Agent, payload: dict, decision: str, reason: str) -> None:
    """Keep a PreToolUse allow or deny that brindle itself made (permission_asks)."""
    try:
        from brindle import permission_asks, permissions

        ws = db.get_workspace(agent.workspace_id)
        req = permissions.from_claude(payload, worktree=ws.path, repo_root=ws.repo_root) if ws else None
        permission_asks.record_decision(db, agent.id, ws.repo_root if ws else "", req.kind if req else "other",
                                        req.summary() if req else str(payload.get("tool_name")), decision, reason)
    except Exception:  # noqa: BLE001 - measuring must never break the hook
        log.exception("brindle: couldn't record a PreToolUse decision")


def _note_decision(db: DB, agent: Agent, ws: Workspace | None, kind: str, summary: str, decision) -> None:
    """Keep a hook decision the policy made without the person (permission_asks)."""
    from brindle import permission_asks

    permission_asks.record_decision(db, agent.id, ws.repo_root if ws else "", kind, summary,
                                    decision.decision, decision.reason, saved=decision.saved is not None)


def permission_request_decision(db: DB, agent: Agent, payload: dict,
                                provider: str = "claude") -> dict | None:
    """Claude Code's or Codex's PermissionRequest hook: brindle's permission
    policy (brindle.permissions) answers allow or deny, or None to let the CLI
    prompt the person as usual (ask). Off unless the repo or user config
    sets ``permission_policy: "on"``, except for a Codex reviewer, whose hook
    allows what its profile's allowed_tools cover and leaves the rest to
    Codex (#241). A request left to the person is held for the app's answer
    while the app is open (_hold_for_app; a Claude one is recorded with the
    policy off too). Never raises: any failure is ask."""
    try:
        from brindle import permissions
        from brindle.config import load_repo_config
        from brindle.profiles import profile_rules_for

        ws = db.get_workspace(agent.workspace_id)
        if ws is None:
            return None
        cfg = load_repo_config(ws.repo_root)
        policy = cfg.permission_policy == "on"
        # Policy off: of the other CLIs only a Codex reviewer has the hook
        # (codex_hook.launch_flags). Nobody watches it, so it gets what its
        # allowed_tools cover and nothing else of brindle's: every other
        # answer is Codex's own.
        review_only = not policy and provider == "codex"
        if not policy and provider != "claude" and not (review_only and agent.mode == "review"):
            return None
        profile_rules = profile_rules_for(agent.profile, ws.repo_root)
        session = _session_of(db, agent)   # for saved actions that last this session
        if provider == "codex":
            # No tool_use_id in Codex's payload: nothing to pair an approval
            # with, so nothing is kept for learning.
            reqs = permissions.from_codex(payload, worktree=ws.path, repo_root=ws.repo_root, agent=agent.id)
            if not reqs:
                return None
            rules = [*permissions.all_rules(ws.repo_root), *profile_rules]
            from dataclasses import replace

            from brindle.native.permissions import Permissions, split_commands, uncovered_part
            from brindle.profiles import load_profile

            try:
                profile = load_profile(agent.profile, ws.repo_root)
            except KeyError:
                profile = None  # deleted profile: no extra allowances
            if profile is not None:
                rules += permissions.profile_bash_allows(profile)
            decision = permissions.Decision("ask", "not in the profile's allowed_tools") if review_only \
                else permissions.decide_all(reqs, checks=cfg.checks, rules=rules, session=session, spend=True)
            if decision.decision == "ask" and len(reqs) == 1:
                # A deny on one part of a compound command (cd x && <denied>)
                # wins before any part-by-part allow below.
                decision = permissions.deny_in_parts(reqs[0], checks=cfg.checks, rules=rules) or decision
            # A rule that asks explicitly is the person's to decide, whatever the
            # profile's allowed_tools cover: only an ask no rule made falls to them.
            explicit = decision.decision == "ask" and decision.rule is not None
            if decision.decision == "ask" and profile is not None and not explicit:
                perms = Permissions(profile.permission_mode, profile.allowed_tools)
                command = reqs[0].command or ""
                specs = [spec for name, spec in perms.rules
                         if name == "Bash" and spec not in (None, "", "*")]
                cwd = os.path.realpath(reqs[0].cwd or ws.path)
                root = os.path.realpath(ws.path)
                # Like pre_tool_decision: a compound command whose every part
                # allowed_tools covers (a cd within the worktree counts).
                if (len(reqs) == 1 and reqs[0].kind == "bash" and specs
                        and (cwd == root or cwd.startswith(root + os.sep))
                        and uncovered_part(specs, command, cd_root=ws.path) is None):
                    decision = permissions.Decision(
                        "allow", "every part of the command matches the profile's allowed_tools")
                    # Deny each part as the allow just read it (quotes and a
                    # harmless redirection gone), which deny_in_parts doesn't.
                    for part in split_commands(command):
                        denied = permissions.decide(replace(reqs[0], command=part), checks=cfg.checks, rules=rules)
                        if denied.decision == "deny" or (denied.decision == "ask" and denied.rule is not None):
                            decision = denied
                            break
                elif perms.mode == "dontAsk":
                    # Nobody watches this pane: a prompt would stall the agent.
                    decision = permissions.Decision(
                        "deny", f"{decision.reason.removesuffix('; asking the user')}, and this profile "
                        "never asks (permission_mode dontAsk). Don't retry it: do without it, or ask "
                        "your supervisor with the send_message tool (brindle MCP server)")
            if review_only and decision.decision != "allow":
                return None  # as Codex would without brindle: its own prompt or refusal
            summary = reqs[0].summary() if len(reqs) == 1 else (
                f"{reqs[0].tool}: " + ", ".join(r.path or "?" for r in reqs))[:200]
            _record_permission(db, agent, ws, summary, f"{decision.decision}: {decision.reason}")
            if decision.decision != "ask":
                _note_decision(db, agent, ws, reqs[0].kind, summary, decision)
            if decision.decision == "ask" and len(reqs) == 1:
                # The same hold for the app's answer as Claude Code's (no suggested rules here).
                from brindle import codex_hook

                permissions.note_prompt(decision)
                # Within the timeout Codex's hook definition carries (codex_hook.TIMEOUT).
                hold = min(cfg.permission_hold_seconds, codex_hook.TIMEOUT - HOOK_SLACK)
                answer = _hold_for_app(db, agent, reqs[0], decision.reason, None, keep=False, hold=hold,
                                       **_supervising(db, agent, cfg, decision, payload))
                if answer:
                    decision = permissions.Decision(
                        "deny" if answer in ("deny", "supervisor_deny") else "allow",
                        {"deny": "the person denied this in the brindle app",
                         "supervisor_deny": "your supervisor denied this",
                         "supervisor_allow": "your supervisor allowed this"}.get(
                            answer, "the person answered in the brindle app"))
            elif decision.decision == "ask":
                from brindle import permission_asks

                permission_asks.record_unheld_ask(db, agent.id, ws.repo_root, reqs[0].kind, summary,
                                                  decision.reason)
            return permissions.codex_output(decision)
        if provider == "opencode":
            # Asked before every tool call, not only those OpenCode would prompt
            # for, so only a deny is answered (and recorded): the rest is OpenCode's own.
            reqs = permissions.from_opencode(payload, worktree=ws.path, repo_root=ws.repo_root, agent=agent.id)
            if not reqs:
                return None
            rules = [*permissions.all_rules(ws.repo_root), *profile_rules]
            decision = permissions.decide_all(reqs, checks=cfg.checks, rules=rules, session=session)
            if decision.decision == "ask" and len(reqs) == 1:
                # A deny on one part of a compound command (cd x && <denied>) still denies.
                decision = permissions.deny_in_parts(reqs[0], checks=cfg.checks, rules=rules) or decision
            if decision.decision == "ask" and decision.rule is not None:
                # A rule asks explicitly, and this plugin can't hold a request for the
                # person: it fails closed rather than let OpenCode's own config run it.
                permissions.note_prompt(decision)
                decision = permissions.Decision(
                    "deny", f"{decision.reason.removesuffix('; asking the user')}, and OpenCode can't wait for "
                    "the person's answer. Ask your supervisor (send_message) or the person for it, or do without it")
            if decision.decision != "deny":
                return None
            req = reqs[0]
            _record_permission(db, agent, ws, req.summary(), f"deny: {decision.reason}")
            _note_decision(db, agent, ws, req.kind, req.summary(), decision)
            return {"decision": "deny", "reason": f"brindle: {decision.reason}"}
        req = permissions.from_claude(payload, worktree=ws.path, repo_root=ws.repo_root, agent=agent.id)
        if req is None:
            return None
        if policy:
            rules = [*permissions.all_rules(ws.repo_root), *profile_rules]
            decision = permissions.decide(req, checks=cfg.checks, rules=rules, session=session, spend=True)
            _record_permission(db, agent, ws, req.summary(), f"{decision.decision}: {decision.reason}")
            if decision.decision != "ask":
                _note_decision(db, agent, ws, req.kind, req.summary(), decision)
            permissions.note_prompt(decision)
        else:
            decision = permissions.Decision("ask", "")
        if decision.decision == "ask":
            # Claude Code's own "always allow" options, as it would apply them.
            suggestions = payload.get("permission_suggestions")
            suggestions = permissions.persistent_suggestions(suggestions)
            # Recorded either way: its PostToolUse counts as the person's approval.
            answer = _hold_for_app(db, agent, req, decision.reason, suggestions, keep=True,
                                   hold=cfg.permission_hold_seconds,
                                   **(_supervising(db, agent, cfg, decision, payload) if policy else {}))
            if answer:
                return permissions.claude_answer(answer, suggestions)
        return permissions.claude_output(decision)
    except Exception:  # noqa: BLE001 - a broken policy must fall through to the prompt
        log.exception("brindle: permission policy failed; leaving it to the user")
        return None


def _supervising(db: DB, agent: Agent, cfg, decision, payload: dict) -> dict:
    """_hold_for_app's supervisor arguments for an ask of brindle's policy:
    none when the person alone answers it (brindle.permission_routing)."""
    from brindle import permission_routing

    supervisor = permission_routing.supervisor_for(db, agent, decision)
    if supervisor is None:
        return {}
    return {"supervisor": supervisor, "supervise": cfg.permission_supervisor_hold_seconds,
            "tool_input": payload.get("tool_input")}


def _session_of(db: DB, agent: Agent) -> str | None:
    """The session (root agent) ``agent`` belongs to, for saved actions that last one."""
    try:
        from brindle import autopilot

        return autopilot.root_of(db, agent.id)
    except Exception:  # noqa: BLE001 - without it, only session-long saved actions are missed
        return None


# Seconds a hold waits on the daemon at a time between looks at whether the
# agent was closed or its request answered at the CLI's own prompt.
HOLD_LOOK_SECONDS = 1.0
HOLD_MIN_LOOK = 0.1     # the shortest such wait (the daemon reads 0 as its longest)
# Those waits between looks at the pane until a look has caught its CLI's own
# prompt (it may not be drawn yet, so the pane reads busy for a while): 5s
# apart, so ANSWERED_LOOKS past it span long enough for it to be drawn. Once
# one has, the pane is looked at after every wait.
UNSEEN_LOOK_EVERY = 5
# Looks in a row a pane must read past its CLI's own permission prompt, busy
# or idle, before the hold ends: longer than one look, so a read while the
# prompt is redrawn doesn't take it for answered. Any other read starts the
# count over.
ANSWERED_LOOKS = 3
# Seconds a CLI's hook timeout is set above the hold (permission_hold_seconds),
# so the hook gives the request to the prompt itself before the CLI cancels it.
HOOK_SLACK = 30
ANSWERS = ("allow_once", "allow_always", "deny")


def _hold_for_app(db: DB, agent: Agent, req, reason: str, suggestions: list | None, keep: bool,
                  hold: float, supervisor: Agent | None = None, supervise: float = 0,
                  tool_input: object = None) -> str | None:
    """Record a request the policy left to the person and, while the app's
    daemon runs, hold it there for the person's answer for ``hold`` seconds
    (_await_app_answer; the row's ``hold_until`` says till when, for the
    app's inbox). The answer comes only from the daemon, to this hook alone
    (brindle.daemon.holds): the row is for showing the request, never read
    as an answer. With no daemon, nothing is held. Neither CLI's
    PermissionRequest carries a tool_use_id: the request gets an id of its
    own (Claude Code's PostToolUse is paired with it by tool and input, see
    note_permission_outcome). ``keep``: record it even when nothing is held,
    and keep it once the hold ends (for that pairing). ``supervisor``: the
    worker's supervisor answers it first, for ``supervise`` seconds of the
    hold (brindle.permission_routing), and the person after; it is shown the
    CLI's complete ``tool_input``, which also tells identical requests apart."""
    from brindle import permission_asks, permission_routing, permissions
    from brindle.daemon.holds import Holder

    request_id = uuid.uuid4().hex[:16]
    recorded = json.dumps(req.to_dict()), json.dumps(suggestions) if suggestions else None
    # Only an answer to exactly this request and these suggestions counts.
    digest = permissions.request_digest(*recorded)
    holder = Holder(agent.id, request_id, digest)
    # Held before it is recorded anywhere, so nothing watching the rows can take its id first.
    now = time.time()
    hold_until = now + hold
    supervise_until = min(hold_until, now + supervise) if supervisor is not None and supervise > 0 else None
    held = (holder.register(hold_until, supervise_until, permission_routing.join_key(digest, tool_input))
            if supervise_until else holder.register(hold_until))
    if not held:
        supervise_until = None      # no daemon to answer through: the CLI's own prompt asks
    # The daemon joined it to the same request already waiting on the supervisor, unanswered:
    # that one's answer settles this one too (Holds.settle), so the supervisor isn't asked twice,
    # nor the first replaced.
    asked_already = supervise_until is not None and holder.joined
    # The history row first: once the pairing row is visible a PostToolUse may pop it
    # and resolve the ask, which needs the row to exist (permission_asks.resolve_at_prompt).
    ws = db.get_workspace(agent.workspace_id)
    ask_id = permission_asks.start_ask(db, agent.id, ws.repo_root if ws else "", request_id, req.kind,
                                       req.summary(), reason)
    channel = ("supervisor" if supervise_until else "hook") if held else None
    db.add_permission_request(agent.id, request_id, recorded[0], reason, recorded[1],
                              channel, hold_until=hold_until if held else None)
    if supervise_until and not asked_already and not permission_routing.ask_supervisor(
            db, agent, supervisor, request_id, req, reason, supervise_until - now, tool_input):
        db.update_permission_request(agent.id, request_id, channel="hook")   # the person's from now on
    answer = ended = ended_at = None
    if held:
        try:
            ended = _await_app_answer(db, agent, holder, req, hold_until,
                                      *([supervise_until] if supervise_until else []))
            ended_at = time.time()     # the wait's end, before the cleanup below
            answer = ended if ended in ANSWERS or ended in permission_routing.AS_ANSWER else None
        finally:
            holder.end()
            db.update_permission_request(agent.id, request_id, channel="answered" if answer else "prompt",
                                         answer=answer)
    permission_asks.finish_ask(db, ask_id, ended, ended_at)
    # A denied request runs no tool, so no PostToolUse will pair with it: kept, it would
    # take the pairing meant for the same command asked again (note_permission_outcome).
    if not keep or answer in ("deny", "supervisor_deny"):
        db.pop_permission_request(agent.id, lambda r: r["tool_use_id"] == request_id)
    return answer


def _closed(agent: Agent | None) -> bool:
    return agent is None or agent.status in ("done", "paused") or agent.dismissed_at is not None


def _await_app_answer(db: DB, agent: Agent, holder, req, hold_until: float,
                      supervise_until: float | None = None) -> str:
    """The person's answer from the app's inbox (one of ANSWERS), or the
    supervisor's (permission_routing.AS_ANSWER) while the request is its,
    until ``supervise_until``, as the daemon hands it to ``holder``
    (brindle.daemon.holds), or else how the
    hold ended: "timeout" at ``hold_until``, "terminal" when the person
    asks to answer in the terminal, "agent_exit" once the agent is closed or
    killed, "prompt" once its CLI's own prompt took the request (its
    PostToolUse removed the row, or its pane stayed past the prompt for
    ANSWERED_LOOKS), "daemon_lost" if the daemon goes away. Without an answer
    the request goes to the CLI's own prompt (or, where there is none, is
    refused). The agent shows waiting meanwhile, as at that prompt."""
    from brindle.permission_routing import AS_ANSWER

    db.set_status(agent.id, "waiting")
    if supervise_until is None:
        bus.publish_agent(db, "permission.pending", agent.id, tool=req.tool, target=req.target())
    deadline, past, seen, waits = time.monotonic() + hold_until - time.time(), 0, False, 0
    while True:
        now = db.get_agent(agent.id)
        recorded = db.conn.execute("SELECT channel FROM permission_requests WHERE agent_id=? AND tool_use_id=?",
                                   (agent.id, holder.request_id)).fetchone()
        left = deadline - time.monotonic()
        if _closed(now):
            ended = "agent_exit"
            break
        if recorded is None:
            ended = "prompt"
            break
        if left <= 0:
            ended = "timeout"
            break
        if supervise_until is not None and (time.time() >= supervise_until or recorded["channel"] != "supervisor"):
            # The supervisor didn't answer in time, escalated it, or couldn't be told: the person's now.
            supervise_until = None
            db.update_permission_request(agent.id, holder.request_id, channel="hook")
            bus.publish_agent(db, "permission.pending", agent.id, tool=req.tool, target=req.target())
        # Its CLI shows its own prompt for as long as this hook holds the request: a pane
        # past it was answered there, whether or not a look caught the prompt. An allow
        # runs the tool (its PostToolUse comes once it is done), a deny or a dismissal none.
        if seen or waits % UNSEEN_LOOK_EVERY == 0:
            state = _pane_state(now)
            past = past + 1 if state in ("idle", "busy") else 0     # the prompt, or a read it can't tell
            seen = seen or state == "prompt"
            if past >= ANSWERED_LOOKS:
                ended = "prompt"
                break
        look = min(left, HOLD_LOOK_SECONDS)
        if supervise_until is not None:     # back in time to hand it to the person
            look = max(min(look, supervise_until - time.time()), HOLD_MIN_LOOK)
        held, answer = holder.wait(look)
        waits += 1
        if answer is not None:
            # Else the person sent it to the prompt.
            ended = answer if answer in ANSWERS or answer in AS_ANSWER else "terminal"
            break
        if not held:
            # The daemon lets go of a closed agent's hold, and of every hold at its
            # hold_until (on the same clock); otherwise the daemon is gone.
            ended = ("agent_exit" if _closed(db.get_agent(agent.id))
                     else "timeout" if time.time() >= hold_until else "daemon_lost")
            break
    if ended in ANSWERS or ended in AS_ANSWER:
        db.set_status(agent.id, "processing", only_if="waiting")
    return ended


def _pane_state(agent: Agent) -> str | None:
    """What the pane of an agent whose CLI shows its own prompt while its
    hook holds a request reads as: 'prompt' (any permission prompt), else
    'idle', 'busy' or 'waiting', or None."""
    from brindle.service.inbox import TERMINAL_PROMPT_VISIBLE

    provider = get_provider(agent.provider)
    if agent.headless or not agent.tmux_window or not TERMINAL_PROMPT_VISIBLE.get(agent.provider):
        return None
    try:
        screen = tmux.capture(agent.tmux_window, lines=60, server=server_of(agent))
    except tmux.TmuxError:
        return None
    return "prompt" if provider.tool_permission(screen) else provider.screen_state(screen)


def note_permission_outcome(db: DB, agent: Agent, payload: dict) -> None:
    """PostToolUse: if the tool's request had been left to the person, they
    approved it. Count that toward a suggestion (never a rule by itself).
    The request is the oldest recorded one for the same tool and input
    (PermissionRequest has no tool_use_id to pair them by)."""
    try:
        if not db.permission_requests_of(agent.id):
            return   # nothing was left to the person: skip the permissions import
        from brindle import permissions

        ws = db.get_workspace(agent.workspace_id)
        ran = permissions.from_claude(payload, worktree=ws.path, repo_root=ws.repo_root) if ws else None
        if ran is None:
            return
        same = (ran.tool, ran.kind, ran.command, ran.path, ran.url)

        def matches(row) -> bool:
            r = json.loads(row["request"])
            return (r.get("tool"), r.get("kind"), r.get("command"), r.get("path"), r.get("url")) == same

        row = db.pop_permission_request(agent.id, matches)
        if row is not None:
            from brindle import permission_asks

            permission_asks.resolve_at_prompt(db, agent.id, row["tool_use_id"])
        if row is None or not row["reason"]:
            return   # none, or recorded for the inbox with the policy off: nothing to learn
        req = permissions.Request.from_dict(json.loads(row["request"]))
        permissions.record_approval(req)
        _record_permission(db, agent, db.get_workspace(agent.workspace_id), req.summary(),
                           "approved by the user")
    except Exception:  # noqa: BLE001 - learning is a side record
        log.exception("brindle: couldn't record a permission approval")


def pending_permission(db: DB, agent_id: str) -> str | None:
    """The request a worker is waiting on the person for, in a sentence, if
    brindle's policy left one to them (the supervisor can't answer it)."""
    try:
        row = db.latest_permission_request(agent_id)
        if row is None:
            return None
        from brindle import permissions

        req = permissions.Request.from_dict(json.loads(row["request"]))
        policy = f" (brindle's permission policy: {row['reason']})" if row["reason"] else ""
        return f"It is asking to use {req.summary()}{policy}."
    except Exception:  # noqa: BLE001
        return None


def _stop_decision(db: DB, agent: Agent, payload: dict, via_inbox: bool = False,
                   waiter: bool = False) -> dict | None:
    """What the Stop hook decides: a "block" with the reason to carry on, or
    None to let the agent stop. ``via_inbox``: the reason will be sent as an
    inbox message (or handed to its waiter) rather than block the stop, so
    ``stop_hook_active`` never caps a repeat; the unread-mail reminder is
    then limited to one per NOTICE_TTL (DB.claim_stop_reminder). ``waiter``:
    the agent runs brindle's waiter (brindle.wake), which this Stop has just
    started: queued messages stay queued for it."""
    if agent.mode == "interactive":
        # Every supervisor turn ends here: start any queued task a cut-off or
        # manual merge left waiting (its "Started" note is delivered below).
        try:
            from brindle import tasks

            tasks.sweep(db)
        except Exception:  # noqa: BLE001 -- must never break the Stop hook
            log.exception("brindle: queued-task sweep failed")
    if waiter and db.pending_count(agent.id):
        db.set_status(agent.id, "processing")
        return None
    msg = None if waiter else db.pop_pending(agent.id)
    if msg:
        db.set_status(agent.id, "processing")
        return {"decision": "block", "reason": msg.body}
    if (agent.mode == "interactive" and db.unread_count(agent.id)
            and not payload.get("stop_hook_active")):
        # Stopping with mail unread, even moments after its notice went out:
        # that notice may have been lost, and nothing else will wake it.
        ap = db.get_autopilot(agent.id)
        pilot = ap is not None and ap.enabled
        # An autopilot supervisor that would really be blocked doesn't stop
        # with mail it hasn't read; stop_hook_active caps that.
        claimed = db.unread(agent.id) if pilot and not via_inbox else db.claim_stop_reminder(agent.id)
        if claimed:
            db.set_status(agent.id, "processing")
            return {"decision": "block", "reason": (
                "[brindle autopilot] You have unread messages. Call read_messages "
                "before you stop." if pilot else unread_notice(db, claimed))}
    needs_report = agent.mode in REPORTING_MODES and agent.result is None
    if needs_report and not payload.get("stop_hook_active"):
        db.set_status(agent.id, "processing")
        if agent.mode == "review":
            return {
                "decision": "block",
                "reason": "You haven't called the brindle `submit_review` tool yet. "
                "Call it now with your verdict and findings.",
            }
        return {
            "decision": "block",
            "reason": "You haven't called the brindle `report_result` tool yet. "
            "If your task is finished, commit your work and call it now. "
            "If you are blocked, call it with a description of what's blocking you.",
        }
    if needs_report:
        db.set_status(agent.id, "idle")
        from brindle import prompt_answer

        if not prompt_answer.at_turn_end(db, agent):   # a question it ended on says more
            tell_parent_unreported(db, agent)
        return None
    if agent.mode == "interactive":
        from brindle import autopilot as pilot

        decision = pilot.on_stop(db, agent, payload)
        if decision:
            db.set_status(agent.id, "processing")
            # Claude Code shows the reason in the chat: one line, like any brindle message.
            kept = db.enqueue_held(agent.id, decision["reason"], None)
            db.mark_delivered(kept)
            return {**decision, "reason": one_line(db, kept, decision["reason"], None)}
    db.set_status(agent.id, "idle")
    return None


def _retest_keys(provider: str) -> None:
    """An auth failure may mean a stored key was revoked: check the provider's stored keys again."""
    from brindle import keys_cmds

    keys_cmds.retest_provider(provider)


def handle_hook(db: DB, agent_id: str, event: str, payload: dict) -> dict | None:
    """Called from ``brindle _hook <event>`` inside the agent's own process tree.
    Returns JSON for Claude Code to read on stdout, or None."""
    agent = db.get_agent(agent_id)
    if agent is None:
        return None
    if agent.hooks_off:
        db.update_agent(agent_id, hooks_off=None)  # its hooks run after all: turned back on
    # A hook runs only once the session is going, so its launch is past any
    # startup prompt: a menu on its pane from now on is the agent's own (a
    # question for the user), which no sweep may answer.
    from brindle import prompt_answer

    prompt_answer.settled(db, agent)
    if event in ("pre-tool", "tool-done", "stop", "codex-notify"):
        from brindle import launch_health
        launch_health.accepted(db, agent)
    if event == "codex-permission-request":
        # Session/status writes are bookkeeping, never prerequisites for
        # returning a policy decision (especially a deny).
        answer = permission_request_decision(db, agent, payload, provider="codex")
        try:
            sid = payload.get("session_id")
            if isinstance(sid, str) and sid and sid != agent.session_ref:
                db.update_agent(agent_id, session_ref=sid)
            if answer is None:
                # Codex has no approval notification event.
                db.set_status(agent_id, "waiting")
        except Exception:  # noqa: BLE001
            log.exception("brindle: couldn't record Codex permission status")
        return answer
    sid = payload.get("session_id")
    if sid and sid != agent.session_ref:
        db.update_agent(agent_id, session_ref=str(sid))
    transcript = payload.get("transcript_path")
    if transcript and agent.provider == "claude" and transcript != agent.transcript_path:
        db.update_agent(agent_id, transcript_path=str(transcript))

    if event in ("prompt-submit", "pre-tool", "tool-done") and agent.unreported_noted:
        # It's working again, so a later stop without a result is a new one
        # its supervisor should hear about (see tell_parent_unreported). A
        # tool call alone isn't proof: agy resumes briefly each time it polls
        # a running command, so only a prompt or a long-enough gap counts.
        if event == "prompt-submit" or unreported_note_expired(agent):
            db.update_agent(agent_id, unreported_noted=None)

    if event == "session-start":
        from brindle import inbox

        # Only Claude Code runs this hook inside the agent's own process. For
        # a CLI with no start hook it runs in whoever launched the agent (see
        # ready), and the inbox in that environment is the launcher's own.
        if agent.provider == "claude":
            inbox.record_from_environment(db, agent_id)
        mark_baseline(db, agent)
        db.set_status(agent_id, "idle", only_if="starting")
        # 'waiting' before the session even started was its trust dialog
        # (see screen_status), which has now been answered.
        db.set_status(agent_id, "idle", only_if="waiting")
        if db.pending_count(agent_id) and not agent.headless and not get_provider(agent.provider).announces_start:
            # No start hook ran: this is `ready`, called once after_launch saw
            # the CLI's input box, so it takes input now (agy's warm-up, then
            # its task, would otherwise wait out ready_delay first).
            try:
                flush(db, agent_id)
            except tmux.TmuxError:
                pass  # stays queued for the next flush
        elif db.pending_count(agent_id) and not agent.headless:
            # Claude Code hasn't drawn its input box yet; deliver shortly after,
            # from a detached process so this hook returns immediately.
            import subprocess

            from brindle.providers import brindle_invocation

            subprocess.Popen(
                [*brindle_invocation(), "_flush", agent_id, "--delay",
                 str(get_provider(agent.provider).ready_delay)],
                start_new_session=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    elif event == "prompt-submit":
        db.set_status(agent_id, "processing")
        from brindle import launch_health
        launch_health.accepted(db, agent)
        # Queued messages (worker results) are typed into an idle chat too;
        # only a prompt brindle didn't deliver is the user speaking.
        prompt = str(payload.get("prompt", ""))
        # brindle's own reminders also arrive as prompts, possibly wrapped or
        # sent without being recorded as delivered (the unread-mail one): any
        # prompt carrying brindle's tag is not the user speaking.
        if (agent.mode == "interactive" and "[brindle" not in prompt
                and not db.recently_delivered(agent_id, prompt)):
            from brindle import autopilot as pilot

            pilot.user_spoke(db, agent)
    elif event == "notification":
        text = str(payload.get("message", "")).lower()
        if "permission" in text or "approval" in text:
            db.set_status(agent_id, "waiting")
    elif event == "pre-tool":
        # A tool call means a turn is running. The hooks can miss its start:
        # a turn that a finished background task or a queued message begins
        # runs no UserPromptSubmit after the last Stop set 'idle', and the
        # dashboard's one-sample screen read never overrides 'idle'.
        db.set_status(agent_id, "processing", only_if="idle")
        return pre_tool_decision(db, agent, payload)
    elif event == "permission-request":
        return permission_request_decision(db, agent, payload)
    elif event == "tool-done":
        db.set_status(agent_id, "processing", only_if="waiting")
        db.set_status(agent_id, "processing", only_if="idle")  # see "pre-tool"
        note_permission_outcome(db, agent, payload)
        from brindle import launch_health
        try:
            launch_health.check_mcp(db, agent)
        except Exception:  # noqa: BLE001
            log.exception("brindle: couldn't check the brindle MCP server's start")
    elif event == "stop":
        from brindle import inbox, tasks

        # A request left to the person with no PostToolUse by the end of the
        # turn wasn't approved (or we can't tell): drop it, learn nothing.
        db.clear_permission_requests(agent_id)
        # A worker's turn is over: its branch may now share changed files
        # with another running branch, which the supervisor hears about now
        # rather than at merge time.
        if agent.mode in ("assign", "handoff", "handoff_detached") and agent.result is None:
            tasks.warn_predicted_conflicts(db, agent)

        # The turn is over: a worker's worktree is snapshotted (brindle.rewind)
        # before anything below could start its next turn.
        snapshot_turn(db, agent)

        # Claude Code shows any blocked stop as "Stop hook error" in the chat.
        # Where a person watches (an interactive agent), brindle lets the stop
        # happen and hands the same text to the waiter this Stop just started
        # (brindle.wake), which wakes the model with it as one "Stop hook
        # feedback" line; a session without a waiter gets it through its
        # inbox, as a message from another session.
        from brindle import wake

        if agent.mode == "interactive" and wake.enabled(agent):
            decision = _stop_decision(db, agent, payload, via_inbox=True, waiter=True)
            if decision:
                db.enqueue(agent_id, decision["reason"], None)
            return None
        if agent.mode == "interactive" and inbox.usable(agent):
            decision = _stop_decision(db, agent, payload, via_inbox=True)
            if decision and inbox.send(agent, decision["reason"]):
                return None
            return decision  # nothing to say, or the inbox failed: block after all
        return _stop_decision(db, agent, payload)
    elif event == "codex-notify":
        # Codex's notify command runs when a turn completes; it can't block the
        # stop, so a queued message is typed into the pane instead.
        from brindle import quota

        thread = payload.get("thread-id")
        if isinstance(thread, str) and thread and thread != agent.session_ref:
            db.update_agent(agent_id, session_ref=thread)   # names its rollout (brindle.usage)
        try:
            quota.refresh_codex()
        except OSError:
            pass
        if payload.get("type") == "agent-turn-complete":
            db.set_status(agent_id, "idle")
            snapshot_turn(db, agent)
            if agent.mode == "interactive" and not db.pending_count(agent_id):
                # What the Stop hook does for Claude Code: an autopilot
                # supervisor that stops short is told to keep going (capped
                # by autopilot's own nudge limit), as a typed-in message.
                from brindle import autopilot as pilot

                decision = pilot.on_stop(db, agent, payload)
                if decision:
                    # Like any message from brindle: one line (or a notice), the rest via read_messages.
                    try:
                        send_message(db, agent_id, decision["reason"])
                    except AgentError:
                        pass  # gone since its turn ended: nobody to nudge
            if (agent.mode in REPORTING_MODES and agent.result is None
                    and not db.pending_count(agent_id)):
                # No Stop hook can remind it to report, and no hook says when
                # it resumes: every turn it ends without a result is a stop
                # its supervisor hears about.
                if unreported_note_expired(agent):
                    db.update_agent(agent_id, unreported_noted=None)
                from brindle import prompt_answer

                if not prompt_answer.at_turn_end(db, agent):   # a question it ended on says more
                    tell_parent_unreported(db, db.get_agent(agent_id) or agent)
            if db.pending_count(agent_id) and not agent.headless:
                flush(db, agent_id)
    elif event == "stop-failure":
        # The turn ended on an API error; no Stop hook follows.
        err_fields = " ".join(
            str(payload.get(k) or "") for k in (
                "error", "error_type", "error_details", "last_assistant_message",
                "message", "reason", "details", "status", "status_code", "code"
            )
        ).lower()
        is_rate_limit = bool(re.search(r"\b(429|rate_limit_error|rate_limit|rate-limit|overloaded|resource_exhausted)\b", err_fields))
        auth_enums = {
            "oauth_org_not_allowed", "billing_error", "authentication_error",
            "permission_error", "invalid_api_key", "account_suspended", "revoked_api_key"
        }
        is_auth = not is_rate_limit and (
            payload.get("error_type") == "auth"
            or str(payload.get("error") or "").lower() in auth_enums
            or str(payload.get("error_type") or "").lower() in auth_enums
            or bool(re.search(r"\b(401|403|oauth_org_not_allowed|billing_error)\b", err_fields))
            or any(w in err_fields for w in (
                "unauthorized", "authentication", "auth_fail", "auth fail",
                "invalid api key", "invalid_api_key", "expired token", "expired_token",
                "credit balance", "credit-balance", "revoked", "forbidden",
                "oauth_org_not_allowed", "billing_error"
            ))
        )
        # Claude Code's line for a --model its backend doesn't serve: every
        # turn fails the same way, so the agent can't go on (launch_health
        # reads the same line on screen).
        from brindle import launch_health

        is_model = not is_rate_limit and not is_auth and bool(
            re.search(launch_health.SELECTED_MODEL, err_fields))
        if is_auth or is_model:
            db.update_agent(agent_id, failed_why="auth" if is_auth else "model")
            db.set_status(agent_id, "failed")
            db.clear_permission_requests(agent_id)
            _note_reviewer_end(db, agent)
            from brindle import providers, autopilot as pilot
            if is_auth:
                providers.invalidate_signed_in(agent.provider)
                _retest_keys(agent.provider)
                err_desc = payload.get("error_details") or payload.get("error") or payload.get("error_type") or "401/403"
                msg = (
                    f"[brindle] Agent {agent_id} encountered an authentication failure "
                    f"(invalid or expired credentials): {err_desc}. "
                    "Check API keys or login status."
                )
            else:
                err_desc = "its model isn't available on this route"
                msg = (
                    f"[brindle] Agent {agent_id} failed: Claude Code says there's an issue with "
                    "the selected model (it may not exist, or the account or route may lack access). "
                    "Check the profile's model and the Claude route (`brindle doctor`), then retry."
                )
            if agent.parent_id:
                try:
                    send_message(db, agent.parent_id, msg, sender_id="brindle")
                except Exception:
                    try:
                        db.enqueue(agent.parent_id, msg, "brindle")
                    except Exception:
                        pass
            root_id = pilot.root_of(db, agent_id)
            if db.get_autopilot(root_id):
                what = "Credentials need" if is_auth else "The model needs"
                pilot.need_user(db, root_id, f"{what} attention for agent {agent_id}: {err_desc}")
        else:
            db.set_status(agent_id, "idle")
            db.clear_permission_requests(agent_id)
            if is_rate_limit:
                from brindle import autopilot as pilot

                pilot.limit_reached(db, agent)
    elif event == "subagent-start":
        # A crash can skip SubagentStop, so this doesn't touch agent.status:
        # the sidebar hides a subagent that's been "running" too long instead.
        sub_id = payload.get("agent_id")
        if sub_id:
            db.start_native_subagent(str(sub_id), agent_id, payload.get("agent_type"))
    elif event == "subagent-stop":
        sub_id = payload.get("agent_id")
        if sub_id:
            db.stop_native_subagent(str(sub_id))
    return None


def mark_baseline(db: DB, agent: Agent) -> None:
    """Remember where a worker started (brindle.rewind), at its session
    start. Never fails the hook."""
    from brindle import rewind

    if agent.mode not in rewind.SNAPSHOT_MODES:
        return
    try:
        ws = db.get_workspace(agent.workspace_id)
        if ws:
            rewind.mark_baseline(agent, ws)
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't record %s's starting commit", agent.id)


def snapshot_turn(db: DB, agent: Agent) -> int | None:
    """Snapshot a worker's worktree at the end of a turn (brindle.rewind), if
    it changed. A hook must never fail over this, so any error is logged."""
    from brindle import rewind

    if agent.mode not in rewind.SNAPSHOT_MODES:
        return None
    try:
        ws = db.get_workspace(agent.workspace_id)
        return rewind.snapshot(db, agent, ws) if ws else None
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't snapshot %s's turn", agent.id)
        return None


def agent_for_session(db: DB, session_id: object) -> str | None:
    """The newest agent whose CLI session is ``session_id``, if any."""
    if not session_id:
        return None
    matches = [a for a in db.list_agents() if a.session_ref == str(session_id)]
    return matches[-1].id if matches else None


UNREPORTED_REALERT_SECONDS = 600


def unreported_note_expired(agent: Agent) -> bool:
    """Whether the stop alert recorded in ``unreported_noted`` (the time it
    was sent) is old enough that a new stop deserves its own alert."""
    noted = agent.unreported_noted
    return not noted or time.time() - noted > UNREPORTED_REALERT_SECONDS


def command_running(db: DB, agent: Agent) -> bool:
    """Whether a command the agent started (possibly in the background) is
    still running under it. Never raises: when unsure, it says no."""
    try:
        from brindle import procs

        window = agent.tmux_window if same_server(agent) and owns_pane(db, agent) else ""
        pane_pids = tmux.window_pids(window) if window else []
        return bool(procs.running_commands(agent.id, pane_pids))
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't look for %s's running commands", agent.id)
        return False


TRANSCRIPT_ACTIVE_SECONDS = 45
REOPEN_VERDICT_PROMPT = (
    "Submit your verdict now: call the brindle `submit_review` tool with "
    "`approved` and `summary` based on what you have already read."
)


def transcript_file(agent: Agent) -> str | None:
    """The path to ``agent``'s own recorded transcript or rollout file, if one
    exists on disk; None when the agent has no recorded session."""
    if agent.transcript_path and os.path.isfile(agent.transcript_path):
        return agent.transcript_path
    ref = (agent.session_ref or "").strip()
    if not ref:
        return None
    if agent.provider == "claude":
        import glob

        config = providers.claude_config_dir()
        found = glob.glob(os.path.join(glob.escape(config), "projects", "*", f"{glob.escape(ref)}.jsonl"))
        return found[0] if found else None
    if agent.provider == "codex":
        from brindle import usage

        rollout = usage.find_codex_rollout(ref, None, agent.created_at)
        return str(rollout) if rollout and rollout.is_file() else None
    if agent.provider == "antigravity":
        candidate = os.path.expanduser(
            f"~/.gemini/antigravity-cli/brain/{ref}/.system_generated/logs/transcript.jsonl"
        )
        return candidate if os.path.isfile(candidate) else None
    if agent.provider == "native":
        return ref if os.path.isfile(ref) else None
    return None


def transcript_active(agent: Agent, now: float | None = None) -> bool:
    """Whether ``agent`` wrote to its transcript within
    ``TRANSCRIPT_ACTIVE_SECONDS``. Never raises: when unsure, it says no."""
    try:
        path = transcript_file(agent)
        if not path:
            return False
        now = time.time() if now is None else now
        return now - os.stat(path).st_mtime < TRANSCRIPT_ACTIVE_SECONDS
    except Exception:  # noqa: BLE001
        return False


def recorded_session(agent: Agent) -> str | None:
    """The CLI session id recorded for ``agent`` itself, if its provider can
    resume that conversation; None when no resumable session was recorded.
    Never guesses from the working directory (``--continue``, ``resume --last``),
    since a reviewer shares its worker's worktree."""
    provider = get_provider(agent.provider)
    ref = (agent.session_ref or "").strip()
    if not ref and agent.provider == "claude" and agent.transcript_path:
        if os.path.isfile(agent.transcript_path):
            return Path(agent.transcript_path).stem
    if not ref:
        return None
    if agent.provider == "claude" and agent.transcript_path and os.path.isfile(agent.transcript_path):
        return ref
    return ref if provider.can_resume(ref) else None


def reopen_reviewer(db: DB, agent: Agent, ws: Workspace,
                    prompt: str = REOPEN_VERDICT_PROMPT) -> bool:
    """Resume a stalled reviewer once with a short verdict prompt: send the
    prompt as a message if the reviewer's process is still alive (or keep an
    already-queued reminder in flight), or reopen its own recorded CLI session
    if the process exited. Returns True if resumed or queued, False when it has
    no live pane or recorded session of its own."""
    if not os.path.isdir(ws.path):
        return False
    if is_alive(agent):
        if db.pending_count(agent.id) > 0:
            try:
                flush(db, agent.id)
            except tmux.TmuxError:
                db.set_status(agent.id, "idle", only_if="processing")
            return True
        try:
            return send_message(db, agent.id, prompt) in ("delivered", "queued")
        except (AgentError, tmux.TmuxError):
            db.set_status(agent.id, "idle", only_if="processing")
            return False
    ref = recorded_session(agent)
    if not ref:
        return False
    if agent.dismissed_at is not None:
        db.update_agent(agent.id, dismissed_at=None)
    _launch(db, agent, ws, prompt=prompt, resume=ref, watch_pane=False, background_setup=True)
    return True


def reopen_worker(db: DB, agent: Agent, ws: Workspace, prompt: str) -> bool:
    """Start a worker whose process has exited (closed once it reported, say)
    again in its own worktree with ``prompt``: in its own recorded CLI session
    if it has one, else fresh on its task with ``prompt`` after it. False when
    its worktree is gone, its session is paused, or it can't start."""
    if not os.path.isdir(ws.path) or agent.status == "paused" or not runs_process(agent):
        return False
    ref = recorded_session(agent)
    text = prompt if ref else _fresh_start_prompt(
        db, agent, ws, get_provider(agent.provider),
        "\n\n(You stopped after reporting and have just been restarted on your branch; "
        "`git log` shows what you already did.)\n\n" + prompt) or prompt
    try:
        _launch(db, agent, ws, prompt=text, resume=ref, watch_pane=False, background_setup=True)
    except AgentError:
        return False
    if agent.dismissed_at is not None:
        db.update_agent(agent.id, dismissed_at=None)  # running again: show it again
    return True


def review_moot(db: DB, agent: Agent) -> bool:
    """A reviewer whose branch already merged at its current commit: there is
    nothing left for it to review or report. A reviewer of commits made after
    the merge is not moot."""
    if agent.mode != "review":
        return False
    ws = db.get_workspace(agent.workspace_id)
    if ws is None:
        return False
    from brindle import pipeline

    return (pipeline._settled(db, ws) or "").startswith(pipeline.ALREADY_MERGED)


def liveness(db: DB, a: Agent, ws: Workspace | None, panes: dict[str, bool] | None = None) -> str:
    """What brindle can see of an agent for free, as one line for its
    supervisor: whether it's running, when it last did anything, and what
    its branch holds, so deciding what to do about it takes no digging.
    Never raises: a fact it can't read is left out."""
    parts = []
    try:
        parts.append("running" if is_alive(a, panes) else "not running")
        path = transcript_file(a)
        last = (os.stat(path).st_mtime if path else
                tmux.window_activity(a.tmux_window, server=server_of(a)) if a.tmux_window else None)
        if last:
            parts[-1] += f", last active {int((time.time() - last) // 60)} min ago"
        if ws is not None and os.path.isdir(ws.path):
            base = git.base_ref(ws.path, ws.base_branch or git.default_branch(ws.repo_root))
            ahead = git.ahead_behind(ws.path, base)[1]
            stat = git.out(["diff", "--stat", git.merge_base(ws.path, base)], ws.path).splitlines()
            files = ", ".join(ln.split("|")[0].strip() for ln in stat[:-1][:5])
            more = f" and {len(stat) - 6} more" if len(stat) > 6 else ""
            parts.append(f"{ahead} commit(s) ahead of {base}"
                         + (f" ({files}{more}: {stat[-1].strip()})" if stat else ""))
            parts.append(f"{len(git.dirty_files(ws.path))} uncommitted file(s)")
    except Exception:  # noqa: BLE001
        pass
    return "; ".join(parts)


def tell_parent_unreported(db: DB, agent: Agent) -> bool:
    """A worker stopped again after being reminded to report, still without a
    result. It won't be reminded again on its own, and its supervisor has
    usually gone idle waiting for it with nothing left to wake it, so tell the
    supervisor (queued if it's busy, delivered now if it's idle). Returns
    whether the supervisor was told."""
    if agent.mode not in FORWARDING_MODES or not agent.parent_id:
        return False  # a synchronous handoff's caller is still waiting on it
    if db.get_agent(agent.parent_id) is None:
        _note_reviewer_end(db, agent)
        return False
    ws = db.get_workspace(agent.workspace_id)
    if review_moot(db, agent):
        close_later(agent.id)
        return False
    if agent.mode == "review" and ws is not None and os.path.isdir(ws.path):
        from brindle import gates

        # Its own verdict only: another reviewer's (a second review's) says
        # nothing about whether this one's call got through.
        if any(r.reviewer_id == agent.id for r in db.reviews_at(ws.id, gates.head(ws))):
            return False  # its verdict for the current commit is already recorded
    if agent.unreported_noted:
        return False  # the supervisor was told about this stop; cleared once it resumes
    if command_running(db, agent) or (is_alive(agent) and transcript_active(agent)):
        # Its turn ended while a long or background command (a test run) is
        # still going, or it is still writing to its transcript: it isn't
        # stopped. Not marked noted, so a real stop later still gets its alert.
        return False
    db.update_agent(agent.id, unreported_noted=int(time.time()))
    if agent.mode == "review" and ws is not None:
        from brindle import pipeline

        if pipeline.on_reviewer_unreported(db, get(db, agent.id), ws):
            return True  # resumed, a fallback reviewer took over, or the supervisor was told
    _note_reviewer_end(db, agent, ws)
    where =f" (branch `{ws.branch}`, workspace {ws.id})" if ws else ""
    tool = "submit_review" if agent.mode == "review" else "report_result"
    who = "Reviewer" if agent.mode == "review" else "Worker"
    # A fix round's worker reported once already; the review's fixes are what's missing.
    what = (f"stopped in a review fix round without committing or calling {tool} again "
            "(it may have lost its brindle connection)"
            if agent.pipeline == "fixing" and agent.result is not None
            else f"stopped without calling {tool}")
    body = (
        f"{who} {agent.id}{where} {what}, and won't be reminded again on its own. "
        f"What brindle sees: {liveness(db, agent, ws)}. Decide from that: send_message to "
        f"ask it to finish or report, workspace_diff for the full change, or remove_workspace "
        f"if the work is abandoned."
    )
    try:
        send_message(db, agent.parent_id, body)
    except (AgentError, tmux.TmuxError):
        # The parent isn't running (or can't take messages): the notice is
        # deliberately dropped here, since there's nothing left to tell.
        return False
    return True


PERMISSION_EVENTS = ("permission-request", "codex-permission-request")


def hook_main(db: DB, agent_id: str, event: str, stdin_text: str, trusted: bool = True) -> str:
    """``trusted`` is False when ``agent_id`` came from the environment,
    which can be stale (see ClaudeCode._hook): then an agent already known
    by the payload's session id wins over it."""
    try:
        payload = json.loads(stdin_text) if stdin_text.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if event in PERMISSION_EVENTS:
        # Never let a failure turn into a decision: anything wrong means no
        # output, so the CLI prompts the person as usual.
        if not isinstance(payload, dict):
            return ""
        try:
            if not trusted:
                agent_id = agent_for_session(db, payload.get("session_id")) or agent_id
            out = handle_hook(db, agent_id, event, payload)
        except Exception:  # noqa: BLE001
            log.exception("brindle: permission-request hook failed")
            return ""
        return json.dumps(out) if out else ""
    if not trusted:
        agent_id = agent_for_session(db, payload.get("session_id")) or agent_id
    out = handle_hook(db, agent_id, event, payload)
    return json.dumps(out) if out else ""

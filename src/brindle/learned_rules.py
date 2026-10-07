"""Learned rules (brindle Pro ``learned_rules``): rules suggested from review
findings that keep coming back.

Every review that asks for changes is a finding (its summary, from the
``review`` rows of ``brindle.history``, which mirror the ``reviews`` table
and outlive session pruning), filed under the repo and the profile of the
worker whose branch it was about. ``refresh`` hands the findings it hasn't
seen yet to one small model call (the grouping step: the repo's
``learned_rules_profile``, else a local model that answers, else the
cheapest Claude model), together with the groups already known, and the
model files each finding under a group: a recurring problem, with a rule
that would have prevented it. When a group's findings span
``learned_rules_repeats`` tasks (distinct branches; 3 by default), it is
suggested: in the supervisor's ``get_progress`` and in ``brindle rules
suggest``. A person decides:

- ``brindle rules accept <key>`` adds the rule to ``.brindle/rules/learned.md``,
  a rule pack every profile in the repo gets (``profiles.load_rule_packs``):
  its text joins the agents' prompts, and when the finding maps to a
  ``deny_patterns``, ``deny_deps`` or ``require_tests_for`` rule, that is
  checked against each branch's diff too (``brindle.rule_checks``). The file
  is meant to be committed, so the rule is reviewed like any other change.
- ``brindle rules reject <key>`` remembers the rejection: findings the model
  files under that group later keep it rejected, and it is never proposed
  again.

Fails closed: without the entitlement (or with an unreadable license)
nothing is grouped or suggested. A rule already in learned.md keeps
applying, as any rule pack in the repo does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from brindle.db import DB, HistoryEntry, LearnedRule

log = logging.getLogger(__name__)

FEATURE = "learned_rules"
PRO_MESSAGE = ("Learned rules (suggested from repeated review findings) are part of brindle Pro: "
               "see `brindle account`.")
DEFAULT_REPEATS = 3
MAX_FINDINGS_PER_CALL = 60      # the oldest unseen ones first; the rest wait for the next call
FINDING_CHARS = 1200
CALL_TIMEOUT = 300
CHECK_KEYS = ("deny_patterns", "deny_deps", "require_tests_for")

# What submit_review records for a branch review (agents.submit_review); a
# completion audit's verdict reads differently and isn't a finding.
_REVIEW = re.compile(r"^Review of (?P<branch>\S+) \(workspace \S+\) at \w+: CHANGES REQUESTED\n\n(?P<summary>.*)\Z",
                     re.S)


class LearnedRulesError(RuntimeError):
    pass


def entitled() -> bool:
    """Whether the plan includes learned rules. Fails closed: no license, an
    unreadable one, or any error means no."""
    from brindle.pro import license

    try:
        return license.has(FEATURE)
    except Exception:  # noqa: BLE001 -- an unreadable license is "not entitled"
        return False


def require_entitled() -> None:
    if not entitled():
        raise LearnedRulesError(PRO_MESSAGE)


# -- findings -------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    id: int          # its history row
    branch: str      # the task it came from
    profile: str     # the profile of the worker whose branch was reviewed
    text: str        # the reviewer's summary


def _worker_profile(db: DB, repo_root: str, branch: str, before_id: int) -> str | None:
    profile = db.branch_worker_profile(repo_root, branch, before_id)
    if profile:
        return profile
    from brindle import agents

    for ws in db.find_workspaces(repo_root):
        if ws.branch == branch:
            worker = agents.workspace_worker(db, ws)
            if worker:
                return worker.profile
    return None


def finding_from(db: DB, row: HistoryEntry) -> Finding | None:
    """The finding in a ``review`` history row, or None for an approval, a
    completion audit, or a review whose worker is unknown."""
    m = _REVIEW.match(row.result or "")
    if not m or not m.group("summary").strip():
        return None
    branch = row.branch or m.group("branch")
    profile = _worker_profile(db, row.repo_root, branch, row.id)
    if not profile:
        return None
    return Finding(row.id, branch, profile, m.group("summary").strip()[:FINDING_CHARS])


def new_findings(db: DB, repo_root: str) -> tuple[list[Finding], int]:
    """The findings the grouping step hasn't seen, oldest first, and the last
    history row id they cover (the new mark)."""
    mark = db.learned_rules_mark(repo_root)
    rows = db.history_after(repo_root, "review", mark)
    found: list[Finding] = []
    last = mark
    for row in rows:
        if len(found) >= MAX_FINDINGS_PER_CALL:
            break
        last = row.id
        f = finding_from(db, row)
        if f:
            found.append(f)
    return found, last


# -- the grouping call ------------------------------------------------------------------


# Text in, text out: one model call. Tests pass a fake.
ModelCall = Callable[[str], str]


@dataclass
class CallResult:
    text: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


PROMPT = """You group code-review findings that keep recurring, so a rule can prevent them.

Each finding below is the summary of a review that asked for changes to a worker's branch \
(one branch = one task), with the worker's profile. Known groups are problems already \
identified; file a finding under one of them when it is the same problem, even if worded \
differently. Otherwise start a new group, or leave the finding out when it is specific to \
its task (a typo, a one-off bug) and no general rule would have prevented it.

For each group give:
- "key": the known group's key, or omit it for a new group
- "title": a few words naming the problem
- "rule": one or two sentences an agent should follow so the problem doesn't happen again
- "findings": the ids of the findings it covers (only the new ones below)
- optionally "deny_patterns" (Python regexes no added line may match), "deny_deps" \
(package names that must not be added or imported) and "require_tests_for" (path globs \
whose changes need a test change in the same diff), only when the rule is exactly that \
mechanical check

Answer with JSON only: {{"groups": [...]}}

Known groups:
{known}

New findings:
{findings}
"""


def build_prompt(findings: list[Finding], known: list[LearnedRule]) -> str:
    known_text = "\n".join(f"- key {r.key} (profile {r.profile}): {r.title}: {r.rule}" for r in known) or "(none)"
    found_text = "\n\n".join(f"[{f.id}] profile {f.profile}, branch {f.branch}:\n{f.text}" for f in findings)
    return PROMPT.format(known=known_text, findings=found_text)


@dataclass
class Group:
    title: str
    rule: str
    findings: list[int]
    key: str | None = None
    checks: dict[str, list[str]] = field(default_factory=dict)


def _strings(value) -> list[str]:
    return [v.strip() for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else []


def parse_groups(text: str) -> list[Group]:
    """The model's groups. Tolerates prose or a code fence around the JSON;
    raises LearnedRulesError when there's no usable JSON at all."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LearnedRulesError(f"the grouping step's answer has no JSON: {text[:200]!r}")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as e:
        raise LearnedRulesError(f"the grouping step's answer isn't valid JSON: {e}") from e
    raw = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise LearnedRulesError("the grouping step's answer has no \"groups\" list")
    groups = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        title, rule = str(g.get("title") or "").strip(), str(g.get("rule") or "").strip()
        ids = [int(i) for i in g.get("findings") or [] if isinstance(i, int) or (isinstance(i, str) and i.isdigit())]
        if not (title or g.get("key")) or not ids:
            continue
        checks = {k: _strings(g.get(k)) for k in CHECK_KEYS}
        groups.append(Group(title=title, rule=rule, findings=ids,
                            key=str(g["key"]) if g.get("key") else None,
                            checks={k: v for k, v in checks.items() if v}))
    return groups


def clean_checks(checks: dict[str, list[str]]) -> dict[str, list[str]]:
    """The checks a rule pack can hold as written: patterns that compile, and
    entries the pack's frontmatter can carry back unchanged."""
    out: dict[str, list[str]] = {}
    for k in CHECK_KEYS:
        items = []
        for item in checks.get(k, []):
            if _frontmatter_item(item) is None:
                continue
            if k == "deny_patterns":
                try:
                    re.compile(item)
                except re.error:
                    continue
            if item not in items:
                items.append(item)
        if items:
            out[k] = items
    return out


LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")


def is_loopback(base_url: str | None) -> bool:
    """Whether ``base_url``'s host is this machine, by the parsed hostname
    (``http://localhost.example.com`` or ``http://x.com/?localhost`` is not)."""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(base_url or "").hostname or "").lower()
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def _repo_defined(name: str, repo_root: str) -> bool:
    from brindle.config import CONFIG_DIR
    from brindle.profiles import profile_source

    source = profile_source(name, repo_root) or ""
    return source.startswith(str(Path(repo_root) / CONFIG_DIR))


def default_call(repo_root: str, db: DB | None = None) -> ModelCall:
    """The grouping step's model call: the repo's ``learned_rules_profile``,
    else the first local (native, loopback) profile whose server answers,
    else Claude Code's cheapest model. Its usage is recorded as a
    ``learned_rules`` history row, so it counts in the spend reports.

    The call runs unattended (after a review, in the background) and sends
    review findings and, for a native profile, its API key: so a native
    profile defined by the repo itself (a cloned repo's committed
    .brindle/agents) is only used when its endpoint is this machine. A
    remote endpoint has to come from your own or brindle's profiles."""
    from brindle import providers
    from brindle.config import load_repo_config
    from brindle.profiles import load_profile, profile_names

    cfg = load_repo_config(repo_root)
    chosen = None
    if cfg.learned_rules_profile:
        try:
            chosen = load_profile(cfg.learned_rules_profile, repo_root)
        except KeyError as e:
            raise LearnedRulesError(f"learned_rules_profile: {e}") from e
        if chosen.provider == "native" and not is_loopback(chosen.base_url) \
                and _repo_defined(chosen.name, repo_root):
            raise LearnedRulesError(
                f"learned_rules_profile {chosen.name!r} is defined in this repo and points at "
                f"{chosen.base_url}: the grouping step only sends findings off this machine "
                "through a profile in ~/.brindle/agents or a built-in one")
    else:
        from brindle.native import runner

        for name in profile_names(repo_root):
            try:
                p = load_profile(name, repo_root)
            except Exception:  # noqa: BLE001 -- a broken profile is just not a candidate
                continue
            if p.provider != "native" or not is_loopback(p.base_url):
                continue
            try:
                if runner.probe(runner.endpoint_for(p), timeout=2.0)[0]:
                    chosen = p
                    break
            except ValueError:
                continue

    if chosen is not None and chosen.provider == "native":
        from brindle.native import runner
        from brindle.native.client import Client

        endpoint = runner.endpoint_for(chosen)

        def native(prompt: str) -> str:
            reply = Client(endpoint).complete(None, [{"role": "user", "content": prompt}], [])
            _record(db, repo_root, chosen.name, CallResult(
                reply.text, reply.model or endpoint.model, reply.usage.input_tokens,
                reply.usage.output_tokens, reply.usage.cache_read_tokens))
            return reply.text

        return native
    if chosen is not None and chosen.provider != "claude":
        raise LearnedRulesError(f"learned_rules_profile {chosen.name!r} uses the {chosen.provider} "
                                "provider; the grouping step runs on a native or claude profile")
    model = (chosen.model if chosen else None) or "haiku"   # Claude's cheapest model
    profile_name = chosen.name if chosen else "claude"

    def claude(prompt: str) -> str:
        binary = providers.claude_binary()
        argv = [binary, "-p", "--output-format", "json", "--model", model]
        try:
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                                  timeout=CALL_TIMEOUT, cwd=repo_root)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise LearnedRulesError(f"the grouping step's claude call failed: {e}") from e
        if proc.returncode != 0:
            raise LearnedRulesError(f"the grouping step's claude call failed: {proc.stderr.strip()[:300]}")
        result = CallResult(proc.stdout, model)
        try:
            data = json.loads(proc.stdout)
            result.text = str(data.get("result") or "")
            for name, u in (data.get("modelUsage") or {}).items():
                if isinstance(u, dict):
                    result.model = name
                    result.input_tokens += int(u.get("inputTokens") or 0)
                    result.output_tokens += int(u.get("outputTokens") or 0)
                    result.cache_read_tokens += int(u.get("cacheReadInputTokens") or 0)
        except (ValueError, AttributeError, TypeError):
            pass
        _record(db, repo_root, profile_name, result)
        return result.text

    return claude


def _record(db: DB | None, repo_root: str, profile: str, result: CallResult) -> None:
    """The grouping call's usage, as a history row (no agent: nothing to diff against)."""
    try:
        from brindle import history
        from brindle.usage import Usage

        usage = Usage(result.input_tokens, result.output_tokens, result.cache_read_tokens, 0, result.model)
        history.record(db or DB(), repo_root, "learned_rules", profile=profile,
                       task="group recurring review findings", result=result.text, usage=usage)
    except Exception:  # noqa: BLE001 -- accounting must not fail the call
        log.exception("brindle: couldn't record the learned-rules call")


# -- refresh: group new findings, update the groups --------------------------------------


def make_key(profile: str, title: str) -> str:
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return hashlib.sha1(f"{profile}\0{norm}".encode()).hexdigest()[:8]


def _ids(text: str) -> list:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def repeats(repo_root: str) -> int:
    from brindle.config import load_repo_config

    try:
        n = int(load_repo_config(repo_root).learned_rules_repeats)
    except (TypeError, ValueError):
        return DEFAULT_REPEATS
    return max(1, n)


def task_count(rule: LearnedRule) -> int:
    return len(_ids(rule.branches))


def checks_of(rule: LearnedRule) -> dict[str, list[str]]:
    try:
        data = json.loads(rule.checks or "{}")
    except ValueError:
        return {}
    return {k: _strings(data.get(k)) for k in CHECK_KEYS if _strings(data.get(k))} if isinstance(data, dict) else {}


@dataclass
class Refresh:
    called: bool                    # whether the model was asked
    findings: int                   # new findings it was given
    suggested: list[LearnedRule]    # groups that became suggestions this time


def refresh(db: DB, repo_root: str, call: ModelCall | None = None) -> Refresh:
    """Group the findings not seen yet (one model call, none when there are
    no new findings) and update the groups. Raises LearnedRulesError without
    the entitlement or when the call or its answer fails; the findings then
    stay unseen, for the next refresh."""
    require_entitled()
    findings, last = new_findings(db, repo_root)
    if not findings:
        if last != db.learned_rules_mark(repo_root):
            db.set_learned_rules_mark(repo_root, last)
        return Refresh(False, 0, [])
    known = db.learned_rules(repo_root)
    call = call or default_call(repo_root, db)
    groups = parse_groups(call(build_prompt(findings, known)))

    by_id = {f.id: f for f in findings}
    by_key = {r.key: r for r in known}
    threshold = repeats(repo_root)
    touched: dict[str, LearnedRule] = {}
    now = time.time()
    used: set[int] = set()
    for g in groups:
        base = by_key.get(g.key) if g.key else None
        title = (base.title if base else g.title) or g.title
        rule_text = (base.rule if base else g.rule) or g.title
        checks = checks_of(base) if base else clean_checks(g.checks)
        for fid in g.findings:
            f = by_id.get(fid)
            if f is None or fid in used:
                continue          # not one of the new findings, or already filed under another group
            used.add(fid)
            # Groups are per profile: the same problem in another profile's work
            # is its own group (with its own key, so a rejection there sticks too).
            key = base.key if base and base.profile == f.profile else make_key(f.profile, title)
            r = touched.get(key) or db.get_learned_rule(repo_root, key) or LearnedRule(
                repo_root=repo_root, key=key, profile=f.profile, title=title, rule=rule_text,
                checks=json.dumps(checks) if checks else None, findings="[]", branches="[]",
                status="forming", created_at=now)
            ids, branches = _ids(r.findings), _ids(r.branches)
            if f.id not in ids:
                ids.append(f.id)
            if f.branch not in branches:
                branches.append(f.branch)
            r.findings, r.branches = json.dumps(ids), json.dumps(branches)
            touched[key] = r
    suggested = []
    for r in touched.values():
        if r.status == "forming" and task_count(r) >= threshold:
            r.status = "pending"
            suggested.append(r)
        db.save_learned_rule(r)
    db.set_learned_rules_mark(repo_root, last)
    return Refresh(True, len(findings), suggested)


def suggestions(db: DB, repo_root: str) -> list[LearnedRule]:
    """The pending suggestions, plus any forming group that has reached the
    threshold since (the threshold was lowered)."""
    threshold = repeats(repo_root)
    out = []
    for r in db.learned_rules(repo_root):
        if r.status == "forming" and task_count(r) >= threshold:
            r.status = "pending"
            db.save_learned_rule(r)
        if r.status == "pending":
            out.append(r)
    return out


def find(db: DB, repo_root: str, key: str) -> LearnedRule:
    matches = [r for r in db.learned_rules(repo_root) if r.key.startswith(key)] if key else []
    if len(matches) != 1:
        raise LearnedRulesError(f"no learned rule {key!r} here" if not matches
                                else f"{key!r} matches several rules: give more of the key")
    return matches[0]


def reject(db: DB, repo_root: str, key: str) -> LearnedRule:
    """Remember that the person doesn't want this rule: it isn't proposed again."""
    require_entitled()
    r = find(db, repo_root, key)
    if r.status == "accepted":
        raise LearnedRulesError(f"rule {r.key} was accepted: remove it from "
                                ".brindle/rules/learned.md instead")
    r.status, r.decided_at = "rejected", time.time()
    db.save_learned_rule(r)
    return r


def accept(db: DB, repo_root: str, key: str) -> tuple[LearnedRule, Path]:
    """Add the rule to the repo's ``.brindle/rules/learned.md``."""
    require_entitled()
    r = find(db, repo_root, key)
    if r.status == "accepted":
        raise LearnedRulesError(f"rule {r.key} is already in .brindle/rules/learned.md")
    path = write_rule(repo_root, r)
    r.status, r.decided_at = "accepted", time.time()
    db.save_learned_rule(r)
    return r, path


# -- .brindle/rules/learned.md ------------------------------------------------------------


PACK_DESCRIPTION = "Rules learned from repeated review findings (`brindle rules suggest`)"


def _frontmatter_item(item: str) -> str | None:
    """``item`` as a frontmatter block-list entry that reads back as exactly
    ``item`` (see ``profiles._frontmatter``), or None when it can't be
    written so."""
    if not item or "\n" in item or "---" in item or item != item.strip():
        return None
    from brindle.profiles import _value

    for quoted in (item, f'"{item}"', f"'{item}'"):
        if _value(quoted) == item:
            return quoted
    return None


def _one_line(text: str) -> str:
    return " ".join(text.split())


def write_rule(repo_root: str, r: LearnedRule) -> Path:
    """Add ``r`` to learned.md: its text as an entry of the pack's prose, its
    mechanical checks merged into the pack's frontmatter lists."""
    from brindle.profiles import _frontmatter, _list, learned_pack_path

    path = learned_pack_path(repo_root)
    meta: dict[str, str] = {}
    body = ""
    if path.is_file():
        meta, body = _frontmatter(path.read_text(encoding="utf-8"))
    lists = {k: list(_list(meta.get(k)) or []) for k in CHECK_KEYS}
    for k, items in checks_of(r).items():
        for item in items:
            if item not in lists[k] and _frontmatter_item(item) is not None:
                lists[k].append(item)
    header = [f"name: {meta.get('name') or 'learned'}",
              f"description: {meta.get('description') or PACK_DESCRIPTION}"]
    for k in CHECK_KEYS:
        items = [i for i in (_frontmatter_item(x) for x in lists[k]) if i is not None]
        if items:
            header.append(f"{k}:")
            header.extend(f"  - {i}" for i in items)
    when = time.strftime("%Y-%m-%d", time.localtime())
    entry = (f"- {_one_line(r.rule)} (learned {when} from {task_count(r)} reviews of "
             f"{r.profile} work: {_one_line(r.title)}; rule {r.key})")
    body = body.strip()
    if not body:
        body = "Rules brindle learned from review findings that kept recurring in this repo:"
    text = "---\n" + "\n".join(header) + "\n---\n" + body + "\n" + entry + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- for the supervisor ---------------------------------------------------------------------


def notes(db: DB, repo_root: str) -> list[str]:
    """One line per pending suggestion, for the supervisor's get_progress.
    Nothing without the entitlement, and never raises."""
    try:
        if not entitled():
            return []
        return [f"learned rule suggested ({task_count(r)} tasks of {r.profile} work got the same "
                f"review finding: {r.title}): \"{_one_line(r.rule)}\". Ask the user whether to add it "
                f"(`brindle rules accept {r.key}`) or not (`brindle rules reject {r.key}`)"
                for r in suggestions(db, repo_root)]
    except Exception:  # noqa: BLE001 -- a side note must never fail get_progress
        log.exception("brindle: couldn't read learned-rule suggestions")
        return []


def refresh_later(repo_root: str) -> None:
    """A review just asked for changes: group the new findings in a detached
    helper (``brindle _learn-rules``), so the supervisor's next get_progress
    can carry a suggestion. Only with the entitlement; never raises."""
    try:
        if not entitled():
            return
        from brindle.providers import brindle_invocation

        subprocess.Popen([*brindle_invocation(), "_learn-rules", repo_root],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:  # noqa: BLE001
        log.exception("brindle: couldn't start the learned-rules refresh")

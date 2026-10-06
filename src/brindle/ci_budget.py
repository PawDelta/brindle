"""Token accounting for ``brindle ci run``: what a run spent, and ``--budget``.

The run's spend is the supervisor's tokens plus every worker and reviewer it
started, directly or indirectly (``agents.tree``), each read from its Claude
Code transcript the way the sidebar's "136k tok" is (``usage.agent_usage``:
input, output and cache tokens together). A handoff worker's row is deleted
once its result is collected, so the tracker remembers the last usage it saw
of every agent: a worker that finished between two polls still counts with
what it had used at the earlier one.

Agents with no transcript (Codex and other non-Claude-Code providers) can't
be counted; they are listed as ``untracked`` so a summary never claims more
coverage than it has.

``--budget`` is a token cap. When the run's total passes it, ``brindle ci run``
stops the session as it does at the timeout, with status ``budget``. brindle
has no price table (``brindle account savings`` only compares relative cost),
so there is no dollar estimate and no dollar budget.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable

from brindle import usage as usage_mod
from brindle.db import DB, Agent
from brindle.usage import Usage, format_tokens, short_model

log = logging.getLogger(__name__)

BUDGET_STATUS = "budget"

_UNITS = {"": 1, "k": 1_000, "m": 1_000_000, "g": 1_000_000_000}


class BudgetError(ValueError):
    """``--budget`` isn't a token count brindle can read."""


def parse_budget(text: str | int | None) -> int | None:
    """A token count from ``--budget``: ``500000``, ``500k``, ``1.5m``,
    optionally followed by ``tok`` or ``tokens``. None for no budget."""
    if text is None:
        return None
    if isinstance(text, int):
        n = text
    else:
        s = text.strip().lower().replace(",", "").replace("_", "")
        if s.startswith("$") or s.endswith("usd"):
            raise BudgetError("--budget is a token count (e.g. 2m): brindle doesn't estimate "
                              "dollars")
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([kmg]?)\s*(?:tok|tokens)?", s)
        if not m:
            raise BudgetError(f"--budget {text!r}: give a token count, e.g. 500000, 500k or 2m")
        n = int(float(m.group(1)) * _UNITS[m.group(2)])
    if n <= 0:
        raise BudgetError("--budget must be a positive number of tokens")
    return n


@dataclass
class AgentSpend:
    id: str
    profile: str
    provider: str
    usage: Usage | None = None     # None: no transcript to count from


@dataclass
class Tracker:
    """The spend of the run rooted at ``root_id``. ``update`` re-reads it;
    the tree and usage readers are parameters so tests can play agents."""

    db: DB
    root_id: str
    budget: int | None = None
    usage_of: Callable[[DB, Agent], Usage | None] | None = None   # default: usage.agent_usage
    tree_of: Callable[[DB, str], list[Agent]] | None = None       # default: agents.tree
    seen: dict[str, AgentSpend] = field(default_factory=dict)

    def update(self) -> int:
        """Read every agent of the run still in the DB; returns the total.
        Accounting never ends a run: a failure to read leaves the last totals."""
        from brindle import agents

        usage_of = self.usage_of or usage_mod.agent_usage
        try:
            for a in (self.tree_of or agents.tree)(self.db, self.root_id):
                spend = self.seen.setdefault(a.id, AgentSpend(a.id, a.profile, a.provider))
                u = usage_of(self.db, a)
                if u is not None and (spend.usage is None or u.total >= spend.usage.total):
                    spend.usage = u
        except Exception:  # noqa: BLE001 - the run matters more than its accounting
            log.warning("brindle ci: reading token usage failed", exc_info=True)
        return self.total

    @property
    def total(self) -> int:
        return sum(s.usage.total for s in self.seen.values() if s.usage is not None)

    def over_budget(self) -> str | None:
        """The note to stop the run with, once the total has passed the budget."""
        if self.budget is None or self.total <= self.budget:
            return None
        return (f"used {format_tokens(self.total)} tokens, over the budget of "
                f"{format_tokens(self.budget)}")

    def summary(self) -> dict:
        """The JSON the step summary, ``Outcome`` and PR body report."""
        tracked = [s for s in self.seen.values() if s.usage is not None]
        tot = Usage()
        for s in tracked:
            tot = tot + s.usage
        models = sorted({short_model(s.usage.model) for s in tracked if s.usage.model})
        profiles = sorted({s.profile for s in self.seen.values()})
        return {
            "tokens": {"total": tot.total, "input": tot.input_tokens, "output": tot.output_tokens,
                       "cache_read": tot.cache_read_tokens,
                       "cache_creation": tot.cache_creation_tokens},
            "budget": self.budget,
            "estimated_cost_usd": None,   # brindle has no price table
            "models": models,
            "profiles": profiles,
            "agents": [{"id": s.id, "profile": s.profile, "provider": s.provider,
                        "tokens": s.usage.total if s.usage else None,
                        "model": short_model(s.usage.model) if s.usage and s.usage.model else None}
                       for s in self.seen.values()],
            "untracked": [s.id for s in self.seen.values() if s.usage is None],
        }


def check(tracker: Tracker | None) -> str | None:
    """The polling loop's hook: refresh the totals; the note to stop with
    when the run is over its budget, else None."""
    if tracker is None:
        return None
    tracker.update()
    return tracker.over_budget()


def _line(spend: dict) -> str:
    t = spend.get("tokens") or {}
    text = f"tokens: {format_tokens(t.get('total', 0))}"
    if spend.get("budget"):
        text += f" of a {format_tokens(spend['budget'])} budget"
    if spend.get("models"):
        text += f" · models: {', '.join(spend['models'])}"
    if spend.get("profiles"):
        text += f" · profiles: {', '.join(spend['profiles'])}"
    if spend.get("untracked"):
        text += f" · {len(spend['untracked'])} agent(s) not counted (no transcript)"
    return text


def describe_line(spend: dict | None) -> str | None:
    """The line ``Outcome.describe`` prints, or None with nothing recorded."""
    return f"  {_line(spend)}" if spend else None


def pr_section(spend: dict | None) -> list[str]:
    """The pull request body's lines about what the run spent."""
    if not spend:
        return []
    t = spend.get("tokens") or {}
    lines = ["", "## Usage", "",
             f"- Tokens: {format_tokens(t.get('total', 0))} "
             f"({format_tokens(t.get('input', 0) + t.get('cache_read', 0) + t.get('cache_creation', 0))} in, "
             f"{format_tokens(t.get('output', 0))} out)"]
    if spend.get("budget"):
        lines.append(f"- Budget: {format_tokens(spend['budget'])} tokens")
    if spend.get("models"):
        lines.append(f"- Models: {', '.join(spend['models'])}")
    if spend.get("profiles"):
        lines.append(f"- Profiles: {', '.join(spend['profiles'])}")
    if spend.get("untracked"):
        lines.append(f"- Not counted: {len(spend['untracked'])} agent(s) with no transcript")
    return lines

"""brindle.provider_switch: moving an out-of-quota supervisor to another
provider in the same workspace, and back once the original limit resets.

Fakes only: quota.get answers from a dict, preflight.check says which CLIs
are installed and signed in, agents.spawn adds an Agent row. Every call
gets ``now`` from a fixed clock (NOW), never the wall clock.
"""

import json

import pytest

from brindle import agents, autopilot, preflight, provider_switch, quota, tmux, workspaces
from brindle.db import Agent, Task

NOW = 1_800_000_000.0
HOUR = 3600.0


@pytest.fixture
def world(monkeypatch):
    """What the quota and preflight fakes report: set ``quotas`` and
    ``installed`` per test. Unknown quota reads as in quota, as quota.headroom does."""
    state = {"quotas": {}, "installed": {"claude", "codex", "antigravity"}}
    monkeypatch.setattr(quota, "get", lambda provider, **kw: state["quotas"].get(provider))
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    monkeypatch.setattr(preflight, "check", lambda provider, repo_root=None, **kw:
                        [] if provider in state["installed"] else [f"{provider} not installed"])
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: {})
    return state


@pytest.fixture
def spawned(monkeypatch):
    """agents.spawn as an Agent row on the provider asked for; each call kept."""
    calls = []

    def fake_spawn(db, ws, profile, **kw):
        calls.append({"ws": ws, "profile": profile, **kw})
        a = Agent(f"new{len(calls)}", ws.id, profile, kw["provider_name"], None, "interactive",
                  "running", f"@n{len(calls)}", None, NOW)
        db.add_agent(a)
        if kw.get("autopilot"):
            db.add_autopilot(a.id)
        return a

    monkeypatch.setattr(agents, "spawn", fake_spawn)
    monkeypatch.setattr(agents, "pause", lambda db, root_id, **kw: None)
    return calls


def _limited(provider, until):
    return quota.Quota(provider, [], limited_until=until, updated_at=NOW)


def _session(db, repo, provider="claude", root_id="root"):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent(root_id, ws.id, "supervisor", provider, None, "interactive", "running", "@1",
                       None, NOW))
    db.add_autopilot(root_id)
    return ws


def _offer_row(db, offer_id, root_id, kind, from_provider, to_provider, state, *,
               limited_until=None, new_root_id=None, back_of=None):
    with db.tx() as c:
        c.execute("INSERT INTO provider_switches (id, root_id, kind, from_provider, to_provider, state,"
                  " limited_until, new_root_id, back_of, created_at, updated_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (offer_id, root_id, kind, from_provider, to_provider, state, limited_until,
                   new_root_id, back_of, NOW, NOW))


def test_offer_when_supervisor_out_of_quota_names_an_in_quota_provider(db, repo, world):
    _session(db, repo)
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR),
                       "antigravity": _limited("antigravity", NOW + 2 * HOUR)}

    offer = provider_switch.maybe_offer(db, "root", NOW)

    assert (offer.kind, offer.state, offer.from_provider, offer.to_provider, offer.limited_until) == (
        "switch", "open", "claude", "codex", NOW + HOUR)
    # Asked again while it is open: the same offer, no second row.
    assert provider_switch.maybe_offer(db, "root", NOW + 60).id == offer.id
    assert db.conn.execute("SELECT COUNT(*) FROM provider_switches").fetchone()[0] == 1
    cards = [i for i in provider_switch.inbox_items(db, str(repo)) if i.kind == provider_switch.KIND_OFFER]
    assert [(c.root_id, c.request_id, c.action.path) for c in cards] == [
        ("root", offer.id, "/v1/sessions/root/provider-switch")]


@pytest.mark.parametrize("installed, other_quota", [
    ({"claude", "codex"}, _limited("codex", NOW + HOUR)),   # the only other CLI is limited too
    ({"claude"}, None),                                      # nothing else is installed or signed in
])
def test_no_offer_when_no_other_provider_in_quota(db, repo, world, installed, other_quota):
    _session(db, repo)
    world["installed"] = installed
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR),
                       **({"codex": other_quota} if other_quota else {})}

    assert provider_switch.maybe_offer(db, "root", NOW) is None
    assert db.conn.execute("SELECT COUNT(*) FROM provider_switches").fetchone()[0] == 0


def test_handoff_note_carries_goal_workers_tasks_messages_and_transcript_tail(db, repo, world, tmp_path):
    ws = _session(db, repo)
    transcript = tmp_path / "root.jsonl"
    transcript.write_text("".join(
        json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                     "content": [{"type": "text", "text": f"turn-{i:03d}"}]}}) + "\n"
        for i in range(100)))
    db.update_agent("root", transcript_path=str(transcript))
    autopilot.set_goal(db, "root", "ship the provider switch",
                       [("detect the limit", "true", None), ("offer the switch", "false", None)], None)
    db.record_check(db.milestones("root")[0].id, True, "ok", "abc")
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "root", "assign", "running", "@2", None, NOW))
    db.add_task(Task("t1", str(repo), None, "root", ws.id, "developer", "write the inbox card", "assign",
                     1, None, None, None, None, "pending", NOW))
    db.enqueue("root", "w1: the quota reader is done", "w1")

    note = provider_switch.handoff_note(db, "root", NOW)

    for expected in ("ship the provider switch", "detect the limit", "offer the switch", "w1",
                     "write the inbox card", "the quota reader is done", "turn-099"):
        assert expected in note
    # Only the tail of the transcript, not the whole conversation.
    assert "turn-010" not in note


def test_handoff_switch_pressed_twice_starts_one_supervisor_on_the_target(db, repo, world, spawned):
    ws = _session(db, repo)
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR)}
    _offer_row(db, "o1", "root", "switch", "claude", "codex", "open", limited_until=NOW + HOUR)

    first = provider_switch.switch(db, "o1", NOW)
    second = provider_switch.switch(db, "o1", NOW + 1)

    assert len(spawned) == 1 and second.id == first.id
    # Same workspace and branch, the other provider.
    assert (spawned[0]["ws"].id, spawned[0]["provider_name"], first.provider) == (ws.id, "codex", "codex")
    row = provider_switch.get(db, "o1")
    assert (row.state, row.new_root_id) == ("done", first.id)


def test_switch_back_once_original_limit_resets(db, repo, world, spawned):
    ws = _session(db, repo)
    db.update_agent("root", status="paused")
    db.add_agent(Agent("now", ws.id, "supervisor", "codex", None, "interactive", "running", "@3", None, NOW))
    db.add_autopilot("now")
    _offer_row(db, "o1", "root", "switch", "claude", "codex", "done",
               limited_until=NOW + HOUR, new_root_id="now")
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR)}

    assert provider_switch.maybe_offer_back(db, NOW + 60) == []
    [back] = provider_switch.maybe_offer_back(db, NOW + HOUR + 1)
    assert (back.kind, back.root_id, back.from_provider, back.to_provider, back.back_of) == (
        "back", "now", "codex", "claude", "o1")
    assert provider_switch.maybe_offer_back(db, NOW + HOUR + 2) == []   # offered once

    returned = provider_switch.switch_back(db, back.id, NOW + HOUR + 3)

    assert [(c["ws"].id, c["provider_name"]) for c in spawned] == [(ws.id, "claude")]
    assert returned.provider == "claude"

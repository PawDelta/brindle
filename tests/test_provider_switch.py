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
# The design's tests, ahead of the code: milestone checks run them with --runxfail.
NOT_BUILT = pytest.mark.xfail(strict=True, reason="provider switch: not built yet")


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


def test_offer_when_supervisor_out_of_quota_names_an_in_quota_provider(db, repo, world, monkeypatch):
    _session(db, repo)
    monkeypatch.setattr(provider_switch.time, "time", lambda: NOW)
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR),
                       "antigravity": _limited("antigravity", NOW + 2 * HOUR)}

    [offer] = provider_switch.sweep(db, NOW)

    assert (offer.kind, offer.state, offer.from_provider, offer.to_provider, offer.limited_until) == (
        "switch", "open", "claude", "codex", NOW + HOUR)
    # Asked again while it is open: the same offer, no second row.
    assert provider_switch.maybe_offer(db, "root", NOW + 60).id == offer.id
    assert db.conn.execute("SELECT COUNT(*) FROM provider_switches").fetchone()[0] == 1
    cards = [i for i in provider_switch.inbox_items(db, str(repo), now=NOW)
             if i.kind == provider_switch.KIND_OFFER]
    assert [(c.root_id, c.request_id, c.action.path) for c in cards] == [
        ("root", offer.id, "/v1/sessions/root/provider-switch")]
    assert provider_switch.sweep(db, NOW + 60) == []
    from brindle.service import inbox

    monkeypatch.setattr(tmux, "list_panes", lambda *a, **kw: tmux.PaneSnapshot())
    assert any(i.request_id == offer.id for i in inbox.items(db, str(repo), panes={}))
    with db.tx() as c:
        c.execute("UPDATE provider_switches SET state='switching' WHERE id=?", (offer.id,))
    assert provider_switch.maybe_offer(db, "root", NOW + 61).state == "switching"
    assert provider_switch.dismiss(db, offer.id, NOW + 62).state == "switching"
    with db.tx() as c:
        c.execute("UPDATE provider_switches SET state='open' WHERE id=?", (offer.id,))
    assert provider_switch.dismiss(db, offer.id, NOW + 63).state == "dismissed"
    assert provider_switch.maybe_offer(db, "root", NOW + 64) is None
    assert provider_switch.inbox_items(db, str(repo), now=NOW) == []
    world["quotas"]["claude"] = _limited("claude", NOW + 2 * HOUR)
    assert provider_switch.maybe_offer(db, "root", NOW + 65).id != offer.id


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


@pytest.mark.variant("each policy or routing refusal must fail closed even with a usable CLI")
@pytest.mark.parametrize("restriction", ["provider", "unavailable", "route", "plugin", "excluded",
                                        "model", "profile"])
def test_offer_never_targets_a_policy_denied_provider(db, repo, world, monkeypatch, restriction):
    from brindle import policy
    from brindle.config import RepoConfig
    from brindle.pro import team_policy

    _session(db, repo)
    world["installed"] = {"claude", "codex"}
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR)}
    access = team_policy.Access(org_id="org")
    if restriction == "provider":
        access = team_policy.Access(org_id="org", allowed_providers=("claude",))
    elif restriction == "unavailable":
        access = team_policy.Access(error="policy unavailable")
    elif restriction == "route":
        access = team_policy.Access(org_id="org", unrouted={"codex": "no managed route"})
    elif restriction == "model":
        access = team_policy.Access(org_id="org", allowed_models=())
    elif restriction == "profile":
        access = team_policy.Access(org_id="org", allowed_profiles=())
    elif restriction == "plugin":
        class Denied:
            def check_assign(self, info):
                return policy.deny("blocked")

        monkeypatch.setattr(policy, "plugin", lambda *a: Denied())
    else:
        monkeypatch.setattr("brindle.config.load_repo_config",
                            lambda *a: RepoConfig(exclude_providers=["codex"]))
    monkeypatch.setattr(team_policy, "access", lambda *a: access)

    assert provider_switch.maybe_offer(db, "root", NOW) is None
    assert db.conn.execute("SELECT COUNT(*) FROM provider_switches").fetchone()[0] == 0
    # A policy change must also hide an offer created under the old policy.
    _offer_row(db, "old", "root", "switch", "claude", "codex", "open", limited_until=NOW + HOUR)
    assert provider_switch.inbox_items(db, str(repo), now=NOW) == []


def test_offer_plan_windows_and_headroom_use_the_supplied_clock(world, monkeypatch):
    world["quotas"] = {
        "claude": quota.Quota("claude", [quota.Window(100, NOW + HOUR)]),
        "codex": quota.Quota("codex", [quota.Window(80, NOW + HOUR)]),
        "antigravity": quota.Quota("antigravity", [quota.Window(20, NOW + HOUR),
                                                    quota.Window(100, NOW + HOUR, name="model")]),
    }
    assert provider_switch.limited_until("claude", NOW) == NOW + HOUR
    assert provider_switch.pick_target("claude", None, NOW) == "antigravity"
    assert provider_switch.limited_until("claude", NOW + HOUR) is None
    # Expired windows cannot reduce a target's headroom.
    assert provider_switch.pick_target("claude", None, NOW + HOUR) == "codex"
    monkeypatch.setattr(quota, "_key_billed", lambda p: p == "claude")
    assert provider_switch.limited_until("claude", NOW) is None
    assert provider_switch.limited_until("native", NOW) is None
    assert provider_switch.pick_target("native", None, NOW) is None


def test_offer_ignores_workers_and_inactive_sessions_and_scopes_cards(db, repo, world):
    ws = _session(db, repo)
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR)}
    db.add_agent(Agent("worker", ws.id, "developer", "claude", "root", "assign", "running",
                       "@2", None, NOW))
    assert provider_switch.out_of_quota(db, "worker", NOW) is None
    assert provider_switch.out_of_quota(db, "missing", NOW) is None
    db.update_agent("root", status="paused")
    assert provider_switch.sweep(db, NOW) == []
    db.update_agent("root", status="done")
    assert provider_switch.maybe_offer(db, "root", NOW) is None
    db.update_agent("root", status="running")
    offer = provider_switch.maybe_offer(db, "root", NOW)
    assert provider_switch.inbox_items(db, str(repo / "elsewhere"), now=NOW) == []
    assert [i.request_id for i in provider_switch.inbox_items(db, None, now=NOW)] == [offer.id]


def test_offer_is_created_at_the_limit_recording_hook(db, repo, world, monkeypatch):
    from brindle import antigravity

    _session(db, repo, provider="antigravity")
    monkeypatch.setattr(antigravity.time, "time", lambda: NOW)

    def record(provider, cfg):
        world["quotas"][provider] = _limited(provider, NOW + HOUR)
        return NOW + HOUR

    monkeypatch.setattr(quota, "record_limit", record)
    monkeypatch.setattr(agents, "handle_hook", lambda *a, **kw: None)
    antigravity.end_turn(db, db.get_agent("root"), {}, "429 RESOURCE_EXHAUSTED: quota")
    assert provider_switch.open_offer(db, "root").to_provider == "claude"


@NOT_BUILT
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


@NOT_BUILT
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


@NOT_BUILT
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

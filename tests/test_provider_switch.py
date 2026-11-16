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


@pytest.mark.variant("forward cards must disappear after each source eligibility transition")
@pytest.mark.parametrize("transition", ["paused", "done", "recovered", "cleared", "provider"])
def test_offer_ignores_workers_and_inactive_sessions_and_scopes_cards(db, repo, world, transition):
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
    if transition in ("paused", "done"):
        db.update_agent("root", status=transition)
    elif transition == "cleared":
        world["quotas"].clear()
    elif transition == "provider":
        db.update_agent("root", provider="antigravity")
        world["quotas"]["antigravity"] = _limited("antigravity", NOW + HOUR)
    now = NOW + HOUR if transition == "recovered" else NOW
    assert provider_switch.inbox_items(db, None, now=now) == []
    if transition in ("recovered", "cleared"):
        provider_switch.dismiss(db, offer.id, now)
        _offer_row(db, "back", "root", "back", "claude", "codex", "open")
        assert [i.request_id for i in provider_switch.inbox_items(db, None, now=now)] == ["back"]


@pytest.mark.variant("a shared provider limit affects all roots whether a supervisor or worker reports it")
@pytest.mark.parametrize("reporter", ["root", "worker"])
def test_offer_is_created_at_the_limit_recording_hook(db, repo, world, monkeypatch, reporter):
    from brindle import antigravity

    ws = _session(db, repo, provider="antigravity")
    _session(db, repo, provider="antigravity", root_id="other")
    db.add_agent(Agent("worker", ws.id, "developer", "antigravity", "root", "assign", "running",
                       "@2", None, NOW))
    monkeypatch.setattr(antigravity.time, "time", lambda: NOW)

    def record(provider, cfg):
        world["quotas"][provider] = _limited(provider, NOW + HOUR)
        return NOW + HOUR

    monkeypatch.setattr(quota, "record_limit", record)
    monkeypatch.setattr(agents, "handle_hook", lambda *a, **kw: None)
    monkeypatch.setattr(antigravity, "_tell_parent_stopped_on_error", lambda *a: None)
    antigravity.end_turn(db, db.get_agent(reporter), {}, "429 RESOURCE_EXHAUSTED: quota")
    assert provider_switch.open_offer(db, "root").to_provider == "claude"
    assert provider_switch.open_offer(db, "other").to_provider == "claude"
    assert provider_switch.open_offer(db, "worker") is None
    offers = [row[0] for row in db.conn.execute("SELECT id FROM provider_switches ORDER BY id")]
    antigravity.end_turn(db, db.get_agent(reporter), {}, "429 RESOURCE_EXHAUSTED: quota")
    assert [row[0] for row in db.conn.execute("SELECT id FROM provider_switches ORDER BY id")] == offers


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
    db.record_check(db.milestones("root")[0].id, True, "quota check succeeded", "abc")
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "root", "assign", "running", "@2", None, NOW))
    db.add_task(Task("t1", str(repo), None, "root", ws.id, "developer", "write the inbox card", "assign",
                     1, None, None, None, None, "pending", NOW))
    db.enqueue("root", "old message outside the tail", "w1")
    for i in range(10):
        db.enqueue("root" if i % 2 else "w1", f"message-{i:02d}", "w1" if i % 2 else "root")
    db.enqueue("w1", "unrelated message", None)

    note = provider_switch.handoff_note(db, "root", NOW)

    for expected in ("ship the provider switch", "detect the limit", "offer the switch", "w1",
                     "developer", "running", ws.branch, "quota check succeeded",
                     "write the inbox card", "message-00", "message-09", "turn-060", "turn-099"):
        assert expected in note
    # Only the tail of the transcript, not the whole conversation.
    assert "turn-010" not in note
    assert "turn-059" not in note
    assert "old message outside the tail" not in note
    assert "unrelated message" not in note


def test_handoff_switch_pressed_twice_starts_one_supervisor_on_the_target(db, repo, world, spawned, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from brindle.daemon import routes_actions
    from brindle.db import DB

    ws = _session(db, repo)
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **kw: tmux.PaneSnapshot())
    db.add_agent(Agent("other", ws.id, "supervisor", "claude", None, "interactive", "running",
                       "@2", None, NOW))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.id == "other")
    monkeypatch.setattr(agents, "owns_pane", lambda *a: True)
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "root", "assign", "running", "@3", None, NOW))
    autopilot.set_goal(db, "root", "finish the work", [("check", "true", None)])
    db.record_check(db.milestones("root")[0].id, True, "check passed", "abc")
    db.add_task(Task("t1", str(repo), None, "root", ws.id, "developer", "queued work", "assign",
                     1, None, None, None, None, "pending", NOW))
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR)}
    _offer_row(db, "o1", "root", "switch", "claude", "codex", "open", limited_until=NOW + HOUR)

    starting, second_entered, release = Event(), Event(), Event()
    spawn, get = agents.spawn, provider_switch.get
    paused = []

    def blocking_spawn(*args, **kw):
        starting.set()
        assert release.wait(5)
        return spawn(*args, **kw)

    def observe(db_, offer_id):
        offer = get(db_, offer_id)
        if offer and offer.state == "switching":
            second_entered.set()
        return offer

    def pause(db_, root_id, **kw):
        paused.append(root_id)
        db_.update_agent(root_id, status="paused")

    def press(now):
        connection = DB(db.path)
        try:
            return provider_switch.switch(connection, "o1", now)
        finally:
            connection.conn.close()

    monkeypatch.setattr(agents, "spawn", blocking_spawn)
    monkeypatch.setattr(agents, "pause", pause)
    monkeypatch.setattr(provider_switch, "get", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_call = pool.submit(press, NOW)
        first_call.add_done_callback(lambda _: starting.set())
        try:
            assert starting.wait(5)
            if first_call.done():
                first_call.result()
            second_call = pool.submit(press, NOW + 1)
            second_call.add_done_callback(lambda _: second_entered.set())
            assert second_entered.wait(5)
            assert not first_call.done() and not second_call.done()
            assert get(db, "o1").state == "switching" and paused == []
        finally:
            release.set()
        first, second = first_call.result(), second_call.result()

    assert len(spawned) == 1 and second.id == first.id
    # Same workspace and branch, the other provider.
    assert (spawned[0]["ws"].id, spawned[0]["provider_name"], first.provider) == (ws.id, "codex", "codex")
    row = provider_switch.get(db, "o1")
    assert (row.state, row.new_root_id) == ("done", first.id)
    assert paused == ["root"] and db.get_agent("root").status == "paused"
    assert db.get_agent("other").status == "running"
    assert db.get_agent("w1").parent_id == first.id
    assert db.get_task("t1").caller_id == first.id
    assert db.get_autopilot(first.id).goal == "finish the work"
    assert db.milestones(first.id)[0].status == "passed"
    assert provider_switch.switch(db, "o1", NOW + 2).id == first.id
    response = routes_actions.provider_switch(db, {"root": "root"}, {"offer": "o1", "accept": True})
    assert response["id"] == first.id and len(spawned) == 1


def test_handoff_failed_start_is_consumed_and_route_scopes_the_offer(db, repo, world, monkeypatch):
    from brindle.daemon import routes_actions
    from brindle.service.errors import ServiceError

    _session(db, repo)
    _session(db, repo, root_id="other")
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **kw: tmux.PaneSnapshot())
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR)}
    _offer_row(db, "o1", "root", "switch", "claude", "codex", "open", limited_until=NOW + HOUR)
    attempts = []

    def fail(*args, **kw):
        attempts.append(True)
        raise agents.AgentError("launch failed")

    monkeypatch.setattr(agents, "spawn", fail)
    with pytest.raises(ServiceError) as wrong_root:
        routes_actions.provider_switch(db, {"root": "other"}, {"offer": "o1", "accept": True})
    assert wrong_root.value.http_status == 404 and attempts == []
    with pytest.raises(agents.AgentError):
        provider_switch.switch(db, "o1", NOW)
    assert provider_switch.get(db, "o1").state == "failed"
    with pytest.raises(agents.AgentError):
        provider_switch.switch(db, "o1", NOW + 1)
    assert len(attempts) == 1 and db.get_agent("root").status == "running"
    assert provider_switch.maybe_offer(db, "root", NOW + 2) is None
    assert [i.request_id for i in provider_switch.inbox_items(db, str(repo), now=NOW)] == ["o1"]
    routes_actions.provider_switch(db, {"root": "root"}, {"offer": "o1", "accept": False})
    assert provider_switch.inbox_items(db, str(repo), now=NOW) == []
    _offer_row(db, "o2", "other", "switch", "claude", "codex", "open", limited_until=NOW + HOUR)
    routes_actions.provider_switch(db, {"root": "other"}, {"offer": "o2", "accept": False})
    assert provider_switch.get(db, "o2").state == "dismissed"
    with pytest.raises(agents.AgentError):
        provider_switch.switch(db, "o2", NOW)
    assert len(attempts) == 1


def test_handoff_transcripts_use_recorded_paths_and_omit_unreadable_text(db, repo, world, tmp_path, monkeypatch):
    from pathlib import Path

    from brindle import usage

    _session(db, repo, provider="codex")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    db.update_agent("root", session_ref="known-session")
    monkeypatch.setattr(usage, "_codex_sessions", lambda home=None: tmp_path)
    rollout = tmp_path / "2026" / "11" / "16" / "rollout-test-known-session.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("\n".join(json.dumps(entry) for entry in [
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "user handoff context"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "assistant handoff context"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "arguments": "private tool traffic"}},
    ]) + "\n{broken\n[]\n{\"unfinished\"")
    note = provider_switch.handoff_note(db, "root", NOW)
    assert "user handoff context" in note and "assistant handoff context" in note
    assert "private tool traffic" not in note and "unfinished" not in note

    # Antigravity's recorded log can contain usage without conversation text.
    db.update_agent("root", provider="antigravity")
    log = (Path.home() / ".gemini" / "antigravity-cli" / "brain" / "known-session"
           / ".system_generated" / "logs" / "transcript.jsonl")
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"type": "PLANNER_RESPONSE", "input_tokens": 20}) + "\n")
    assert usage.agent_transcripts(db, db.get_agent("root")) == [log]
    without_text = provider_switch.handoff_note(db, "root", NOW)
    assert "user handoff context" not in without_text
    log.write_text(json.dumps({"type": "PLANNER_RESPONSE", "text": "recorded response"}) + "\n")
    assert "recorded response" in provider_switch.handoff_note(db, "root", NOW)
    log.unlink()
    assert provider_switch.handoff_note(db, "root", NOW) == without_text


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

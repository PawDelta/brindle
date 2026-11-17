"""brindle.provider_switch: moving an out-of-quota supervisor to another
provider in the same workspace, and back once the original limit resets.

Fakes only: quota.get answers from a dict, preflight.check says which CLIs
are installed and signed in, agents.spawn adds an Agent row. Every call
gets ``now`` from a fixed clock (NOW), never the wall clock.
"""

import json

import pytest

from brindle import agents, autopilot, config, preflight, provider_switch, quota, tmux, workspaces
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
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: tmux.PaneSnapshot())
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


def test_auto_always_switch_persists_setting_and_get_reads_it(db, repo, world, spawned, monkeypatch):
    from test_daemon_actions import _send

    _session(db, repo)
    monkeypatch.setattr(provider_switch.time, "time", lambda: NOW)
    world["quotas"]["claude"] = _limited("claude", NOW + HOUR)
    offer = provider_switch.maybe_offer(db, "root", NOW)
    code, reply = _send("POST", "/v1/sessions/root/provider-switch",
                        {"offer": offer.id, "accept": True, "always": True})
    assert code == 200 and db.get_agent(reply["id"]).provider == "codex"
    assert config.user_settings().get("quota_switch") == "auto"
    code, settings = _send("GET", "/v1/user-settings")
    assert code == 200 and settings["quota_switch"] == "auto"


@pytest.mark.variant("regression: the settings route rejected quota_switch, so the Settings row couldn't save")
def test_auto_user_settings_patch_accepts_and_validates_quota_switch(db):
    from test_daemon_actions import _send

    code, settings = _send("PATCH", "/v1/user-settings", {"quota_switch": "auto"})
    assert code == 200 and settings["quota_switch"] == "auto"
    assert config.user_settings()["quota_switch"] == "auto"
    assert _send("PATCH", "/v1/user-settings", {"quota_switch": "invalid"})[0] == 400
    assert config.user_settings()["quota_switch"] == "auto"


@pytest.mark.parametrize("return_mode", [
    pytest.param("automatic", marks=pytest.mark.variant("in auto mode the return happens by itself, with no press")),
    "notice_action", "dismiss_notice"])
def test_auto_sweep_switches_posts_notice_and_returns_when_quota_recovers(db, repo, world, spawned, return_mode):
    _session(db, repo)
    config.set_user("quota_switch", "auto")
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR),
                       "codex": quota.Quota("codex", [quota.Window(80, NOW + HOUR)]),
                       "antigravity": quota.Quota("antigravity", [quota.Window(20, NOW + HOUR)])}
    provider_switch.sweep(db, NOW)
    assert [call["provider_name"] for call in spawned] == ["antigravity"]
    new = db.get_agent("new1")
    assert provider_switch.open_offer(db, new.id) is None
    [notice] = provider_switch.inbox_items(db, str(repo), now=NOW)
    assert notice.kind == provider_switch.KIND_BACK and notice.root_id == new.id
    assert notice.action.path == f"/v1/sessions/{new.id}/provider-switch"
    assert notice.detail.startswith(provider_switch.NOTICE_PREFIX + "claude → antigravity")
    assert provider_switch.inbox_items(db, "another-repo", now=NOW) == []
    provider_switch.sweep(db, NOW + 1)
    assert len(spawned) == 1
    if return_mode == "dismiss_notice":
        from brindle.daemon import routes_actions

        routes_actions.provider_switch(db, {"root": new.id}, {"offer": notice.request_id, "accept": False})
        assert provider_switch.inbox_items(db, str(repo), now=NOW + 1) == []
        assert provider_switch.inbox_items(db, str(repo), now=NOW + 2) == []
        provider_switch.sweep(db, NOW + HOUR + 1)
    elif return_mode == "automatic":
        provider_switch.sweep(db, NOW + HOUR + 1)
    else:
        from brindle.daemon import routes_actions

        body = {"offer": notice.request_id, "accept": True}
        reply = routes_actions.provider_switch(db, {"root": new.id}, body)
        assert db.get_agent(reply["id"]).provider == "claude"
        assert routes_actions.provider_switch(db, {"root": new.id}, body)["id"] == reply["id"]
        provider_switch.sweep(db, NOW + 2)
        assert len(spawned) == 2
    assert [call["provider_name"] for call in spawned] == ["antigravity", "claude"]
    assert provider_switch.inbox_items(db, str(repo), now=NOW + HOUR + 1) == []
    provider_switch.sweep(db, NOW + HOUR + 2)
    assert len(spawned) == 2


@pytest.mark.parametrize("billing", [
    pytest.param("quota", marks=pytest.mark.variant("a target with no quota left must not be switched to automatically")),
    "profile",
    pytest.param("environment", marks=pytest.mark.variant("an API key in the environment means per-token billing")),
    pytest.param("org_key_env", marks=pytest.mark.variant("org key metadata overrides a subscription-auth profile")),
    pytest.param("org_key_helper", marks=pytest.mark.variant("org helper metadata must block switching without running the helper")),
])
def test_auto_never_switches_to_key_billed_provider(db, repo, world, spawned, monkeypatch, billing):
    _session(db, repo, provider="codex")
    config.set_user("quota_switch", "auto")
    world["installed"] = {"claude", "codex"}
    world["quotas"]["codex"] = _limited("codex", NOW + HOUR)
    if billing == "quota":
        monkeypatch.setattr(quota, "_key_billed", lambda provider: provider == "claude")
    elif billing == "profile":
        from dataclasses import replace

        from brindle import profiles

        load = profiles.load_profile
        monkeypatch.setattr(profiles, "load_profile", lambda *a, **kw: replace(load(*a, **kw), auth="api_key"))
    elif billing == "environment":
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    else:
        from dataclasses import replace

        from brindle import profiles
        from brindle.pro import managed_models, team_policy
        from brindle.service.routing import RoutingPolicy

        route = team_policy.ProviderConfig("anthropic", **{
            "key_env" if billing == "org_key_env" else "key_helper":
                "REVIEW_ORG_KEY" if billing == "org_key_env" else ("unused-key-helper",)})
        managed = managed_models.Managed("org", route, False)
        monkeypatch.setattr(RoutingPolicy, "load", lambda *a, **kw:
                            RoutingPolicy(team_policy.Access(org_id="org"), managed, str(repo)))
        load = profiles.load_profile
        monkeypatch.setattr(profiles, "load_profile", lambda *a, **kw: replace(load(*a, **kw), auth="subscription"))

        def unexpected_credentials(*args, **kwargs):
            pytest.fail("billing classification must not retrieve credentials")

        monkeypatch.setattr(managed_models, "org_key_env", unexpected_credentials)
    provider_switch.sweep(db, NOW)
    assert spawned == [] and provider_switch.open_offer(db, "root") is None


@pytest.mark.parametrize("change", [
    pytest.param("recovered", marks=pytest.mark.variant("an offer whose source limit has lifted must not switch")),
    pytest.param("new_limit", marks=pytest.mark.variant("a replacement quota window must get its own current-limit record")),
    pytest.param("headroom", marks=pytest.mark.variant("an old ask offer must follow the current target ranking")),
    "recovered_at_dispatch",
])
def test_auto_existing_offer_revalidates_limit_and_target(db, repo, world, spawned, monkeypatch, change):
    _session(db, repo)
    world["quotas"]["claude"] = _limited("claude", NOW + HOUR)
    offer = provider_switch.maybe_offer(db, "root", NOW)
    assert offer.to_provider == "codex"
    if change == "recovered":
        world["quotas"].clear()
    elif change == "new_limit":
        world["quotas"]["claude"] = _limited("claude", NOW + 2 * HOUR)
    elif change == "recovered_at_dispatch":
        dispatch = provider_switch.switch

        def recover_then_dispatch(*args, **kwargs):
            world["quotas"].clear()
            return dispatch(*args, **kwargs)

        monkeypatch.setattr(provider_switch, "switch", recover_then_dispatch)
    else:
        world["quotas"]["codex"] = quota.Quota("codex", [quota.Window(90, NOW + HOUR)])
    config.set_user("quota_switch", "auto")
    provider_switch.sweep(db, NOW + 1)
    if change in ("recovered", "recovered_at_dispatch"):
        assert spawned == [] and provider_switch.open_offer(db, "root") is None
    elif change == "new_limit":
        assert len(spawned) == 1
        row = db.conn.execute("SELECT limited_until FROM provider_switches WHERE state='done'").fetchone()
        assert row["limited_until"] == NOW + 2 * HOUR
        assert provider_switch.get(db, offer.id).state == "dismissed"
    else:
        assert [call["provider_name"] for call in spawned] == ["antigravity"]


@pytest.mark.variant("audit regression: an alternatives suffix was read as the target provider's name")
def test_offer_detail_names_one_target_even_with_alternatives(db, repo, world):
    _session(db, repo)
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR),
                       "codex": quota.Quota("codex", [quota.Window(80, NOW + HOUR)]),
                       "antigravity": quota.Quota("antigravity", [quota.Window(20, NOW + HOUR)])}
    [offer] = provider_switch.sweep(db, NOW)
    [card] = provider_switch.inbox_items(db, str(repo), now=NOW)
    assert card.detail == f"claude → {offer.to_provider}"


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
    pytest.param({"claude", "codex"}, _limited("codex", NOW + HOUR),
                 marks=pytest.mark.variant("the only other CLI is limited too")),
    pytest.param({"claude"}, None, marks=pytest.mark.variant("nothing else is installed or signed in")),
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
def test_offer_never_targets_a_policy_denied_provider(db, repo, world, spawned, monkeypatch, restriction):
    from brindle import policy
    from brindle.config import RepoConfig
    from brindle.pro import team_policy

    _session(db, repo)
    config.set_user("quota_switch", "auto")
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
    provider_switch.sweep(db, NOW)
    assert spawned == []
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
        provider_switch.check_cards(db, now)    # the sweep's look, which the cards show
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


@pytest.mark.variant("a second press while the first switch is in flight must not start a second supervisor")
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
    from datetime import datetime, timezone
    from pathlib import Path

    from brindle import usage

    _session(db, repo, provider="codex")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    source_home = tmp_path / "source-codex"
    db.update_agent("root", session_ref="known-session", config_home=str(source_home))
    monkeypatch.setattr(quota, "codex_home", lambda: source_home)
    monkeypatch.setattr(usage.time, "time", lambda: NOW)
    day = usage.time.strftime("%Y/%m/%d", usage.time.localtime(NOW))
    meta = {"id": "known-session", "cwd": str(repo),
            "timestamp": datetime.fromtimestamp(NOW, timezone.utc).isoformat()}
    rollout = source_home / "sessions" / day / "rollout-test-known-session.jsonl"
    rollout.parent.mkdir(parents=True)
    recorded = "\n".join(json.dumps(entry) for entry in [
        {"type": "session_meta", "payload": meta},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "user handoff context"}]}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
         "content": [{"type": "output_text", "text": "assistant handoff context"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "arguments": "private tool traffic"}},
    ]) + "\n{broken\n[]\n{\"unfinished\""
    rollout.write_text(recorded)
    note = provider_switch.handoff_note(db, "root", NOW)
    assert "user handoff context" in note and "assistant handoff context" in note
    assert "private tool traffic" not in note and "unfinished" not in note

    # Same checkout and start time cannot establish transcript ownership.
    sibling = rollout.with_name("rollout-test-sibling-session.jsonl")
    foreign = "\n".join(json.dumps(entry) for entry in [
        {"type": "session_meta", "payload": {**meta, "id": "sibling-session"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "private sibling conversation"}]}},
    ]) + "\n"
    sibling.write_text(foreign)
    rollout.unlink()
    missing_source = provider_switch.handoff_note(db, "root", NOW)
    assert "private sibling conversation" not in missing_source
    assert "user handoff context" not in missing_source
    # Even a matching filename needs matching session metadata.
    rollout.write_text(foreign)
    assert provider_switch.handoff_note(db, "root", NOW) == missing_source
    rollout.write_text(recorded)

    # The current default home may have changed since the source was launched.
    other_home = tmp_path / "other-codex"
    other_rollout = other_home / "sessions" / day / rollout.name
    other_rollout.parent.mkdir(parents=True)
    other_rollout.write_text(recorded.replace("user handoff context", "private other-home conversation"))
    monkeypatch.setattr(quota, "codex_home", lambda: other_home)
    note = provider_switch.handoff_note(db, "root", NOW)
    assert "user handoff context" in note and "private other-home conversation" not in note
    db.update_agent("root", session_ref=None)
    assert provider_switch.handoff_note(db, "root", NOW) == missing_source
    db.update_agent("root", session_ref="known-session", config_home=None)
    assert provider_switch.handoff_note(db, "root", NOW) == missing_source

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


def test_switch_back_once_original_limit_resets(db, repo, world, spawned):
    ws = _session(db, repo)
    db.update_agent("root", status="paused")
    db.add_agent(Agent("now", ws.id, "supervisor", "codex", None, "interactive", "running", "@3", None, NOW))
    db.add_autopilot("now")
    _offer_row(db, "o1", "root", "switch", "claude", "codex", "done",
               limited_until=NOW + HOUR, new_root_id="now")
    world["quotas"] = {"claude": _limited("claude", NOW + HOUR)}

    assert provider_switch.maybe_offer_back(db, NOW + 60) == []
    autopilot.set_goal(db, "now", "finish after switching", [("check", "true", None)])
    # A reset still offers a back card when the replacement has hit its limit.
    world["quotas"]["codex"] = _limited("codex", NOW + 2 * HOUR)
    [back] = provider_switch.sweep(db, NOW + HOUR + 1)
    assert (back.kind, back.root_id, back.from_provider, back.to_provider, back.back_of) == (
        "back", "now", "codex", "claude", "o1")
    assert provider_switch.maybe_offer_back(db, NOW + HOUR + 2) == []   # offered once

    returned = provider_switch.switch_back(db, back.id, NOW + HOUR + 3)

    assert [(c["ws"].id, c["provider_name"]) for c in spawned] == [(ws.id, "claude")]
    assert returned.provider == "claude"
    assert db.get_autopilot(returned.id).goal == "finish after switching"
    assert "finish after switching" in spawned[0]["prompt"]
    assert provider_switch.switch_back(db, back.id, NOW + HOUR + 4).id == returned.id
    assert len(spawned) == 1
    assert provider_switch.maybe_offer_back(db, NOW + HOUR + 5) == []


@pytest.mark.variant("each loss of session ownership or target eligibility must suppress a back offer")
@pytest.mark.parametrize("change", ["paused", "done", "worker", "handed_away", "dismissed",
                                    "provider", "unavailable", "open_offer"])
def test_switch_back_requires_current_root_and_usable_original_provider(db, repo, world, change):
    from brindle import sessions

    ws = _session(db, repo)
    db.update_agent("root", status="paused")
    db.add_agent(Agent("now", ws.id, "supervisor", "codex", None, "interactive", "running",
                       "@3", None, NOW))
    db.add_autopilot("now")
    _offer_row(db, "o1", "root", "switch", "claude", "codex", "done",
               limited_until=NOW, new_root_id="now")
    if change in ("paused", "done"):
        db.update_agent("now", status=change)
    elif change == "worker":
        db.update_agent("now", parent_id="root")
    elif change == "handed_away":
        db.update_autopilot("now", state="blocked", note=sessions.HANDED_AWAY + "successor")
    elif change == "dismissed":
        db.update_agent("now", dismissed_at=NOW)
    elif change == "provider":
        db.update_agent("now", provider="antigravity")
    elif change == "unavailable":
        world["installed"].remove("claude")
    elif change == "open_offer":
        _offer_row(db, "other", "now", "switch", "codex", "antigravity", "open")

    assert provider_switch.maybe_offer_back(db, NOW + 1) == []
    assert db.conn.execute("SELECT COUNT(*) FROM provider_switches WHERE kind='back'").fetchone()[0] == 0


@pytest.mark.variant("a dismissed switch-back must stay dismissed for that switch")
def test_switch_back_dismissal_is_not_offered_again(db, repo, world):
    ws = _session(db, repo)
    db.update_agent("root", status="paused")
    db.add_agent(Agent("now", ws.id, "supervisor", "codex", None, "interactive", "running",
                       "@3", None, NOW))
    _offer_row(db, "o1", "root", "switch", "claude", "codex", "done",
               limited_until=NOW, new_root_id="now")
    [back] = provider_switch.maybe_offer_back(db, NOW + 1)
    provider_switch.dismiss(db, back.id, NOW + 2)
    assert provider_switch.sweep(db, NOW + 3) == []
    assert provider_switch.open_offer(db, "now") is None
    assert db.conn.execute("SELECT COUNT(*) FROM provider_switches WHERE back_of='o1'").fetchone()[0] == 1

"""The org status poll (pro/status.py): pacing, 404 and air-gap, failing safe,
policy refetch when policy_version moves, and what lands on disk."""
import json

import pytest
from org_status_fixtures import ORG, control, ent, notice, org, poller  # noqa: F401

from brindle import airgap
from brindle.pro import status, team_policy

NOW = 1_000_000.0


# -- pacing -------------------------------------------------------------------------------------


@pytest.mark.parametrize("idle,working,want", [
    (0, False, 60), (14 * 60, False, 60), (15 * 60, False, 300), (3 * 3600, False, 300),
    (3 * 3600, True, 60)])
def test_interval_follows_the_last_activity(idle, working, want):
    assert status.interval(NOW, NOW - idle, working) == want


def test_touch_brings_the_pace_back(monkeypatch):
    monkeypatch.setattr(status, "_activity", NOW - 3600)
    assert status.interval(NOW, status._activity) == 300
    status.touch(NOW)
    assert status.interval(NOW, status._activity) == 60


# -- polling ------------------------------------------------------------------------------------


def test_a_poll_saves_what_the_server_says(org):
    org.body = {"policy_version": 1, "paused": True, "paused_reason": "over budget",
                "seat": {"spent_usd": 12.5, "budget_usd": 40, "budget_source": "member"},
                "notices": [notice()], "org_alert": {"level": "warn", "spent_usd": 1640,
                                                     "total_usd": 2000}}
    st = poller().poll_once()
    assert st.paused and st.paused_reason == "over budget" and len(st.notices) == 1
    saved = status.load(ORG)
    assert saved.status.paused and saved.status.seat["budget_source"] == "member"
    assert saved.status.org_alert["total_usd"] == 2000.0
    assert saved.status.notices[0]["text"].startswith("Over budget")


def test_a_404_disables_polling_for_the_session(org):
    org.status_code = 404
    p = poller()
    assert p.poll_once() is None and p.disabled
    n = len(org.calls)
    assert p.poll_once() is None and len(org.calls) == n


def test_nothing_is_polled_in_air_gap_mode(org, monkeypatch):
    monkeypatch.setattr(airgap, "enabled", lambda: True)
    p = poller()
    assert p.poll_once() is None and not p.start() and not org.calls


def test_no_team_no_poll(org):
    assert poller(entitlement=lambda: ent(features=("learning",))).poll_once() is None
    assert not org.calls


def test_a_failed_poll_keeps_the_last_known_pause(org):
    org.body = {"policy_version": 1, "paused": True, "paused_reason": "x"}
    p = poller()
    p.poll_once()
    org.fail = OSError("offline")
    assert p.poll_once() is None
    assert status.load(ORG).status.paused           # fails closed: still paused
    org.fail, org.status_code, org.body = None, 500, {}
    assert p.poll_once() is None and status.load(ORG).status.paused


def test_garbage_answers_change_nothing(org):
    org.body = {"policy_version": 1, "paused": True}
    p = poller()
    p.poll_once()
    org.body = "not json at all"
    assert p.poll_once() is None and status.load(ORG).status.paused


def test_malformed_notices_and_controls_are_dropped(org):
    org.body = {"policy_version": 1, "notices": [{"id": 1}, "x", notice("ok")],
                "control": {"id": "c", "action": "explode"}}
    st = poller().poll_once()
    assert [n["id"] for n in st.notices] == ["ok"] and st.control is None


def test_the_file_is_private_and_belongs_to_one_org(org, monkeypatch):
    org.body = {"policy_version": 1, "paused": True}
    poller().poll_once()
    assert (status._path().stat().st_mode & 0o777) == 0o600
    assert status.load("org_other").status.paused is False


# -- the policy follows policy_version ------------------------------------------------------------


def test_a_newer_policy_version_refetches_the_policy(org):
    org.body = {"policy_version": 5, "notices": []}
    org.policy = {"org_id": ORG, "version": 5, "policy": {"max_parallel_workers": 2}}
    seen = []

    def refetch(e):
        seen.append(e.org_id)
        team_policy.fetch_policy(ORG, object(), object(), cached_for=(e.role, None))

    poller(refetch=refetch).poll_once()
    assert seen == [ORG]
    assert team_policy.load_cached(ORG).version == 5
    assert org.count("GET", "/policy") == 1


def test_an_unchanged_version_fetches_nothing(org):
    team_policy.save_cached(team_policy.parse_policy(ORG, {"org_id": ORG, "version": 5, "policy": {}}))
    org.body = {"policy_version": 5}
    seen = []
    poller(refetch=lambda e: seen.append(1)).poll_once()
    assert not seen and org.count("GET", "/policy") == 0


def test_a_failing_refetch_does_not_fail_the_poll(org):
    org.body = {"policy_version": 9, "paused": True}

    def boom(e):
        raise RuntimeError("policy down")

    st = poller(refetch=boom).poll_once()
    assert st is not None and status.load(ORG).status.paused


def test_the_default_refetch_uses_the_policy_endpoint(org):
    org.body = {"policy_version": 7}
    org.policy = {"org_id": ORG, "version": 7, "policy": {"max_parallel_workers": 3}}
    poller().poll_once()
    assert team_policy.load_cached(ORG).max_parallel_workers == 3


# -- the thread -------------------------------------------------------------------------------------


def test_the_thread_polls_and_stops(org):
    import time

    org.body = {"policy_version": 1, "paused": True}
    p = poller(activity=lambda: time.time())
    assert p.start()
    for _ in range(100):
        if p.saved and p.saved.status.paused:
            break
        time.sleep(0.02)
    p.stop()
    assert p.saved.status.paused and org.count("GET", "/status") >= 1


def test_the_status_file_is_valid_json(org):
    org.body = {"policy_version": 1, "control": control("throttle", throttle={
        "allowed_models": ["a"], "max_parallel_workers": 1, "budget": {"task_usd": 1}})}
    poller().poll_once()
    d = json.loads(status._path().read_text())
    assert d["org_id"] == ORG and d["status"]["control"]["throttle"]["max_parallel_workers"] == 1

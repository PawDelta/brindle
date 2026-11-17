"""The org status poll (pro/status.py): pacing, 404 and air-gap, failing safe,
policy refetch when policy_version moves, and what lands on disk."""
import json

import pytest

from org_status_fixtures import ORG, control, ent, notice, org, poller  # noqa: F401

from brindle import airgap
from brindle.pro import status

NOW = 1_000_000.0




# -- pacing -------------------------------------------------------------------------------------


# -- polling ------------------------------------------------------------------------------------






def test_nothing_is_polled_in_air_gap_mode(org, monkeypatch):
    monkeypatch.setattr(airgap, "enabled", lambda: True)
    p = poller()
    assert p.poll_once() is None and not p.start() and not org.calls




@pytest.mark.variant("fail closed: network errors and HTTP failures cannot clear a cached organization pause")
def test_a_failed_poll_keeps_the_last_known_pause(org):
    org.body = {"policy_version": 1, "paused": True, "paused_reason": "x"}
    p = poller()
    p.poll_once()
    org.fail = OSError("offline")
    assert p.poll_once() is None
    assert status.load(ORG).status.paused           # fails closed: still paused
    org.fail, org.status_code, org.body = None, 500, {}
    assert p.poll_once() is None and status.load(ORG).status.paused


def test_malformed_notices_and_controls_are_dropped(org):
    org.body = {"policy_version": 1, "notices": [{"id": 1}, "x", notice("ok")],
                "control": {"id": "c", "action": "explode"}}
    st = poller().poll_once()
    assert [n["id"] for n in st.notices] == ["ok"] and st.control is None


# -- the policy follows policy_version ------------------------------------------------------------


def test_a_failing_refetch_does_not_fail_the_poll(org):
    org.body = {"policy_version": 9, "paused": True}

    def boom(e):
        raise RuntimeError("policy down")

    st = poller(refetch=boom).poll_once()
    assert st is not None and status.load(ORG).status.paused


# -- the thread -------------------------------------------------------------------------------------


def test_a_404_disables_polling_for_the_session(org):
    org.status_code = 404
    p = poller()
    assert p.poll_once() is None and p.disabled
    n = len(org.calls)
    assert p.poll_once() is None and len(org.calls) == n


@pytest.mark.variant("regression for #94: main gives both machines one session id")
def test_machines_with_one_hostname_get_distinct_stable_session_ids(monkeypatch, tmp_path):
    """(#94) A hostname alone named two machines the same."""
    monkeypatch.setattr(status.socket, "gethostname", lambda: "MacBook Pro")
    ids = []
    for home in ("a", "b"):
        monkeypatch.setenv("BRINDLE_HOME", str(tmp_path / home))
        ids.append(status.session_id())
        assert status.session_id() == ids[-1]
    assert ids[0] != ids[1]
    assert all(i.startswith("MacBook-Pro-") for i in ids)


def test_garbage_answers_change_nothing(org):
    org.body = {"policy_version": 1, "paused": True}
    p = poller()
    p.poll_once()
    org.body = "not json at all"
    assert p.poll_once() is None and status.load(ORG).status.paused

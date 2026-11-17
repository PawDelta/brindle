"""What the app's start-up and Settings reads keep off the request path.

The Inbox (GET /v1/inbox, read at start and on every change) builds the
provider-switch cards. Whether an offer's target provider is still usable
is checked by the provider-switch sweep (the daemon's, every 30 s), not by
the read: preflight, profile and policy checks run the providers' CLIs
and took the read from ~0.13 s to ~0.7 s warm, ~5 s cold, with four open
offers.

Settings › Storage (GET /v1/storage) measured every worktree of every repo
inside the request: ~35 s on a quiet machine, past the request's deadline on
a busy one. It now answers at once with the last report and measures on a
thread of its own (daemon/storage_reports.py).

Fakes only, no clocks: what is asserted is which calls the read makes.
"""

import threading

import pytest
from starlette.testclient import TestClient

from brindle import disk, preflight, provider_switch, quota, tmux, workspaces
from brindle.daemon.app import build_app
from brindle.db import Agent

NOW = 1_800_000_000.0
TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.mark.variant("regression: the Inbox read ran preflight per provider for every open offer")
def test_inbox_read_shows_switch_card_without_checking_providers(db, repo, monkeypatch):
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("root", ws.id, "supervisor", "claude", None, "interactive", "running", "@1", None, NOW))
    db.add_autopilot("root")
    monkeypatch.setattr(provider_switch.time, "time", lambda: NOW)
    monkeypatch.setattr(quota, "get", lambda provider, **kw: (
        quota.Quota("claude", [], limited_until=NOW + 3600, updated_at=NOW) if provider == "claude" else None))
    monkeypatch.setattr(quota, "_key_billed", lambda provider: False)
    monkeypatch.setattr(tmux, "list_panes", lambda *a, **k: tmux.PaneSnapshot())
    slow = []
    monkeypatch.setattr(preflight, "check", lambda provider, repo_root=None, **kw: slow.append(provider) or [])
    for name in ("refresh_claude_usage", "refresh_antigravity_usage"):
        monkeypatch.setattr(quota, name, lambda *a, _n=name, **k: slow.append(_n))

    [offer] = provider_switch.sweep(db, NOW)     # the daemon's sweep: it checks the providers
    assert slow
    slow.clear()

    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        reply = client.get("/v1/inbox")
    assert reply.status_code == 200
    assert offer.id in [i.get("request_id") for i in reply.json()]
    assert slow == []


def _measurer(monkeypatch):
    """disk.storage_report as a fake that holds until ``release`` is set."""
    state = {"calls": 0, "finished": False, "entered": threading.Event(), "release": threading.Event()}

    def measure(db, here=None, now=None):
        state["calls"] += 1
        state["entered"].set()
        state["release"].wait(5)
        state["finished"] = True
        return {**disk_report(), "reclaimable": 7}

    monkeypatch.setattr(disk, "storage_report", measure)
    return state


def disk_report():
    return {"worktrees": [], "categories": [], "reclaimable": 0, "free_bytes": None,
            "low_disk": None, "dead_branches": []}


def _measured():
    """Wait for the background measurement (its own thread) to be kept."""
    for t in threading.enumerate():
        if t.name == "brindle-storage-report":
            t.join(5)


def test_storage_read_answers_before_measuring_and_joins_the_measurement_under_way(db, monkeypatch):
    m = _measurer(monkeypatch)
    with TestClient(build_app(TOKEN), headers=AUTH) as client:
        first = client.get("/v1/storage")
        assert m["finished"] is False             # answered while the measurement still runs
        assert first.json()["refreshing"] is True and first.json()["measured_at"] is None
        assert m["entered"].wait(5)
        again = client.get("/v1/storage", params={"fresh": "1"})
        assert again.json()["refreshing"] is True and m["calls"] == 1    # joined, not a second walk
        m["release"].set()
        _measured()
        done = client.get("/v1/storage").json()
    assert (done["reclaimable"], done["refreshing"], m["calls"]) == (7, False, 1)
    assert done["measured_at"] is not None


def test_storage_report_begun_before_a_clean_is_not_kept(brindle_home):
    from brindle.daemon.storage_reports import StorageReports

    pending = []

    def measure(repo):
        reports.forget()        # a clean, while this measurement runs
        return {**disk_report(), "reclaimable": 7}

    reports = StorageReports(measure=measure, start=pending.append)
    reports.get(None)
    pending.pop()()
    assert reports.get(None)["measured_at"] is None


def test_storage_reads_for_other_repos_queue_behind_one_measuring_thread(brindle_home, repo):
    from brindle.daemon.storage_reports import StorageReports

    pending, measured = [], []
    reports = StorageReports(measure=lambda r: measured.append(r) or disk_report(), start=pending.append)
    for asked in (None, str(repo), str(repo / "not-a-repo"), "/no/such/place", str(repo)):
        assert reports.get(asked)["refreshing"] is True
    assert len(pending) == 1        # one thread for the home, however many repos are asked for
    pending.pop()()
    assert measured == [None, str(repo.resolve())]      # one after another; made-up repos read as none
    assert reports.get(str(repo))["refreshing"] is False


def test_storage_failure_stops_measuring_until_asked_and_keeps_the_last_report(brindle_home):
    from brindle.daemon.storage_reports import StorageReports

    pending, outcomes = [], [{**disk_report(), "reclaimable": 7}, OSError("disk gone"), {**disk_report(), "reclaimable": 3}]

    def measure(repo):
        got = outcomes.pop(0)
        if isinstance(got, Exception):
            raise got
        return got

    reports = StorageReports(measure=measure, start=pending.append, clock=lambda: NOW)
    reports.get(None)
    pending.pop()()
    reports.get(None, fresh=True)
    pending.pop()()
    failed = reports.get(None)
    assert (failed["reclaimable"], failed["refreshing"], bool(failed["error"])) == (7, False, True)
    assert reports.get(None)["refreshing"] is False and pending == []    # polling doesn't walk again
    assert reports.get(None, fresh=True)["refreshing"] is True
    pending.pop()()
    again = reports.get(None)
    assert (again["reclaimable"], again["error"]) == (3, None)

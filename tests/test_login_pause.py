"""Expired cloud logins: silent refresh, one notice, a held session that resumes."""

import functools
import os
import time
from types import SimpleNamespace

import pytest

from brindle import agents, autopilot, cloud_login, login_pause, tmux, workspaces
from brindle.cloud_login import LoginStatus
from brindle.db import Agent

T0 = 1_800_000_000.0
CMD = "gcloud auth application-default login"
EXPIRED = ("● API Error: Could not load Google Cloud credentials · "
           '{"error":"invalid_grant","error_subtype":"invalid_rapt"}\n\n❯ \n')
BUSY = "● Working on it\n\n❯ \n  ⏵⏵ auto mode on · esc to interrupt\n"
IDLE = "● Done.\n\n❯ \n  ? for shortcuts\n"


class SignIn:
    """A fake `gcloud auth application-default login`: like the real one it
    holds the lock it inherits until it exits."""

    def __init__(self, cmd, pass_fds, **kw):
        self.cmd, self.fd = cmd, os.dup(pass_fds[0])

    def exit(self):
        os.close(self.fd)


@pytest.fixture
def world(db, repo, monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDSDK_CONFIG", str(tmp_path / "gcloud"))
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive",
                       "idle", "@0", None, time.time()))
    db.add_agent(Agent("w1", ws.id, "developer", "claude", "boss", "assign",
                       "idle", "@1", None, time.time()))
    db.add_autopilot("boss")
    autopilot.set_goal(db, "boss", "Goal", [("M1", "true", None)])
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    w = SimpleNamespace(screens={"@0": IDLE, "@1": IDLE}, sent=[], keys=[], alerts=[], pasted=[],
                        refreshed=0, login_ok=True, expires_in=3600.0, active=False, now=T0,
                        signins=[])
    monkeypatch.setattr(cloud_login, "sign_in", functools.partial(
        cloud_login.sign_in, popen=lambda cmd, **kw: w.signins.append(SignIn(cmd, **kw))))
    monkeypatch.setattr(agents, "is_alive", lambda a, panes=None: a.status not in ("paused", "done"))
    monkeypatch.setattr(agents, "send_message",
                        lambda db, to, body, sender_id=None: w.sent.append((to, body)))
    monkeypatch.setattr(agents, "_paste_blocked", lambda a: False)
    monkeypatch.setattr(tmux, "capture", lambda target, **kw: w.screens[target])
    monkeypatch.setattr(tmux, "send_keys", lambda target, *keys, **kw: w.keys.append((target, *keys)))
    monkeypatch.setattr(tmux, "paste", lambda target, text, **kw: w.pasted.append(target))
    monkeypatch.setattr(tmux, "set_alert", lambda session, text, style="warn": w.alerts.append(text))
    monkeypatch.setattr(tmux, "session_attached", lambda session: w.active)
    monkeypatch.setattr(tmux, "client_activity", lambda session: w.now - 10 if w.active else None)

    def refresh(**kw):
        w.refreshed += 1
        return (LoginStatus(True, w.now + 3600, CMD, "logged in") if w.login_ok
                else LoginStatus(False, None, CMD, "login expired (invalid_grant)", expired=True))

    monkeypatch.setattr(cloud_login, "refresh", refresh)
    monkeypatch.setattr(cloud_login, "status",
                        lambda **kw: LoginStatus(True, w.now + w.expires_in, CMD, "logged in"))
    return w


def sweep(db, w, at):
    w.now = at
    return login_pause.sweep(db, now=at)


@pytest.mark.parametrize("expires_in, worker_screen", [
    (300.0, IDLE),        # expiring soon: renewed ahead of time
    (3600.0, EXPIRED),    # a worker stopped on the expired login: renewed, told to go on
])
def test_a_refreshable_login_is_renewed_without_a_notice(db, world, expires_in, worker_screen):
    world.expires_in, world.screens["@1"] = expires_in, worker_screen
    sweep(db, world, T0)
    assert world.refreshed == 1 and world.alerts == []
    assert not login_pause.paused(db, "boss")
    assert [to for to, _ in world.sent] == (["w1"] if worker_screen is EXPIRED else [])


@pytest.mark.parametrize("active", [False, True])
def test_a_login_only_the_person_can_renew_pauses_and_notifies_once(db, world, active, monkeypatch):
    world.active, world.login_ok, world.screens["@1"] = active, False, EXPIRED
    sweep(db, world, T0)
    assert login_pause.paused(db, "boss")
    world.signins.pop().exit()                     # the person closes the sign-in
    monkeypatch.setattr(agents, "_deliver_to_inbox", lambda *a: world.pasted.append("inbox") or True)
    assert agents._send_message(db, db.get_agent("w1"), "next task", "boss") == "queued"
    world.screens["@1"] = BUSY                     # a cron turn starts on its own
    for at in (T0 + 30, T0 + 61, T0 + 181, T0 + 421):   # failed re-checks, backing off
        sweep(db, world, at)
    assert ("@1", "Escape") in world.keys
    db.set_result("w1", "reported")                # no worker left to wait on: a nudge is due
    assert autopilot.on_stop(db, db.get_agent("boss"), {}) is None
    assert agents.flush(db, "w1") is False and db.pending_count("w1") == 1
    notices = [b for to, b in world.sent if to == "boss"] + world.alerts
    assert len(notices) == 1 and CMD in notices[0]
    assert world.refreshed == 5                    # 3 re-checks, 1 more under the sign-in's lock

    world.login_ok = True
    sweep(db, world, T0 + 421 + 480)
    assert not login_pause.paused(db, "boss")
    assert world.alerts == ([] if active else [world.alerts[0], ""])
    assert db.pending_count("w1") == 0 and world.pasted == ["@1"]
    resumed = [b for to, b in world.sent if to == "boss" and "restored" in b]
    assert len(resumed) == 1


@pytest.fixture
def lapsed(db, world):
    """Two sessions on one Vertex login hit its lapse in the same sweep."""
    ws = db.get_agent("boss").workspace_id
    db.add_agent(Agent("boss2", ws, "supervisor", "claude", None, "interactive",
                       "idle", "@2", None, time.time()))
    world.screens["@2"] = IDLE
    world.login_ok, world.expires_in, world.screens["@1"] = False, 300.0, EXPIRED
    sweep(db, world, T0)
    assert login_pause.paused(db, "boss") and login_pause.paused(db, "boss2")
    return world


def test_one_signin_across_sessions(db, lapsed):
    assert [s.cmd for s in lapsed.signins] == [CMD.split()]
    assert len(lapsed.alerts) == 2 and all("open in your browser" in a for a in lapsed.alerts)
    sweep(db, lapsed, T0 + 61)                     # both wait on the one sign-in
    lapsed.signins[0].exit()                       # the person abandons it
    ws = db.get_agent("boss").workspace_id         # a session that hits the lapse later
    db.add_agent(Agent("boss3", ws, "supervisor", "claude", None, "interactive",
                       "idle", "@3", None, time.time()))
    lapsed.screens["@3"] = IDLE
    for at in (T0 + 122, T0 + 400, T0 + 2000):
        sweep(db, lapsed, at)
    assert len(lapsed.signins) == 1 and lapsed.sent == []
    assert len(lapsed.alerts) == 3 and "browser" not in lapsed.alerts[2]   # no sign-in is open
    assert all(login_pause.paused(db, r) for r in ("boss", "boss2", "boss3"))


def test_panes_resume_after_signin(db, lapsed):
    sweep(db, lapsed, T0 + 30)                     # signing in
    lapsed.login_ok = True
    lapsed.signins[0].exit()                       # signed in
    sweep(db, lapsed, T0 + 31)
    assert not login_pause.paused(db, "boss") and not login_pause.paused(db, "boss2")
    assert sorted(to for to, b in lapsed.sent if "restored" in b) == ["boss", "boss2", "w1"]
    assert len(lapsed.signins) == 1

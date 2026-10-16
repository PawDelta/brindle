"""Expired cloud logins: silent refresh, one notice, a held session that resumes."""

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

    def __init__(self, cmd, pid, pass_fds, **kw):
        self.cmd, self.pid, self.fd = cmd, pid, os.dup(pass_fds[0])

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
                        typed_at=T0 - 10, signins=[], ended=[], signin_results=[])
    real_sign_in = cloud_login.sign_in

    def launch(cmd, **kw):
        w.signins.append(SignIn(cmd, len(w.signins), **kw))
        return w.signins[-1]

    def kill(pid):
        w.ended.append(pid)
        w.signins[pid].exit()

    def sign_in(**kw):
        how = real_sign_in(**kw, popen=launch, kill=kill)
        w.signin_results.append(how)
        return how

    monkeypatch.setattr(cloud_login, "sign_in", sign_in)
    monkeypatch.setattr(tmux, "last_input", lambda: w.typed_at)
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
    chat = [b for to, b in world.sent if to == "boss"]
    assert len(chat) == 1 and len(world.alerts) == 1 and CMD in chat[0] and CMD in world.alerts[0]
    assert world.refreshed == 5                    # 3 re-checks, 1 more under the sign-in's lock

    world.login_ok = True
    sweep(db, world, T0 + 421 + 480)
    assert not login_pause.paused(db, "boss")
    assert world.alerts == [world.alerts[0], ""]
    assert db.pending_count("w1") == 0 and world.pasted == ["@1"]
    resumed = [b for to, b in world.sent if to == "boss" and "restored" in b]
    assert len(resumed) == 1


@pytest.fixture
def lapsed(db, world):
    """A supervisor's session and a run started from the CLI (no interactive
    root) hit the lapse of their one Vertex login in the same sweep."""
    ws = db.get_agent("boss").workspace_id
    db.add_agent(Agent("cli", ws, "developer", "claude", None, "assign",
                       "idle", "@2", None, time.time()))
    world.screens["@2"] = EXPIRED
    world.login_ok, world.expires_in, world.screens["@1"] = False, 300.0, EXPIRED
    sweep(db, world, T0)
    assert login_pause.paused(db, "boss") and login_pause.paused(db, "cli")
    return world


def test_one_signin_across_sessions(db, lapsed):
    assert [s.cmd for s in lapsed.signins] == [CMD.split()]
    assert lapsed.signin_results == ["opened", "waiting"]
    assert len(lapsed.alerts) == 2                 # each root's bar says work is paused
    told = len(lapsed.sent)
    sweep(db, lapsed, T0 + 61)                     # both wait on the one sign-in
    lapsed.signins[0].exit()                       # the person abandons it
    ws = db.get_agent("boss").workspace_id         # a session that hits the lapse later
    db.add_agent(Agent("boss3", ws, "supervisor", "claude", None, "interactive",
                       "idle", "@3", None, time.time()))
    lapsed.screens["@3"] = IDLE
    for at in (T0 + 122, T0 + 400, T0 + 2000):     # no input since: nothing reopens
        sweep(db, lapsed, at)
    assert len(lapsed.signins) == 1 and lapsed.signin_results.count("opened") == 1
    assert "tried" in lapsed.signin_results and len(lapsed.sent) == told + 1   # boss3's notice
    assert all(login_pause.paused(db, r) for r in ("boss", "cli", "boss3"))


def test_panes_resume_after_signin(db, lapsed):
    db.add_agent(Agent("w2", db.get_agent("boss").workspace_id, "developer", "claude", "boss",
                       "assign", "idle", "@4", None, time.time()))
    lapsed.screens["@4"] = BUSY                    # a cron turn starts while paused ...
    sweep(db, lapsed, T0 + 30)                     # ... and is escaped
    assert ("@4", "Escape") in lapsed.keys
    lapsed.screens["@4"] = IDLE
    lapsed.login_ok = True
    lapsed.signins[0].exit()                       # signed in
    sweep(db, lapsed, T0 + 31)
    assert not login_pause.paused(db, "boss") and not login_pause.paused(db, "cli")
    assert sorted(to for to, b in lapsed.sent if "restored" in b) == ["boss", "cli", "w1", "w2"]
    assert len(lapsed.signins) == 1 and lapsed.alerts[-2:] == ["", ""]


@pytest.mark.parametrize("exits", [
    False,      # the tab is ignored: its sign-in keeps waiting
    True,       # the sign-in exits (declined, failed, or its code expired)
])
def test_closed_tab_reopens_only_on_input(db, world, exits):
    world.login_ok, world.screens["@1"] = False, EXPIRED
    world.typed_at = T0 - 3600                     # away when it lapses
    sweep(db, world, T0)
    assert login_pause.paused(db, "boss") and world.signins == []
    world.typed_at = T0 + 50                       # back
    sweep(db, world, T0 + 60)
    assert len(world.signins) == 1
    if exits:
        world.signins[0].exit()
    sweep(db, world, T0 + 120)                     # no input since it opened
    if not exits:
        for at in (T0 + 200, T0 + 400, T0 + 600):  # typing on past a tab that still works
            world.typed_at = at - 5
            sweep(db, world, at)
    assert len(world.signins) == 1
    died = T0 + 120 if exits else T0 + 60 + cloud_login.TAB_LIFE
    world.typed_at = died + 10                     # typing after it died: one fresh tab
    sweep(db, world, died + 20)
    world.typed_at = died + 30
    sweep(db, world, died + 40)
    assert len(world.signins) == 2 and world.ended == ([] if exits else [0])

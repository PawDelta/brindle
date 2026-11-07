import pytest
from starlette.testclient import TestClient

from brindle import agents, config, visual_offers as offers, visuals
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.service.errors import ServiceError


@pytest.fixture
def session(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    monkeypatch.delenv("BRINDLE_AGENT_ID", raising=False)
    monkeypatch.setattr(agents, "is_alive", lambda agent, panes=None: True)
    ws = Workspace("ws", str(tmp_path), "main", "main", "main", None, str(tmp_path), None, "tm", 0)
    db.add_workspace(ws)
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "%1", None, 0, headless=1)
    db.add_agent(boss)
    worker = Agent("worker", ws.id, "developer", "claude", boss.id, "assign", "processing", "%2", None, 0)
    db.add_agent(worker)
    config.set_user("visuals", "on")
    return boss, worker


@pytest.mark.parametrize("case,kind,counts,reason", [
    ("open", "layout", {}, "open_offer"),
    ("cooldown", "layout", {}, "cooldown"),
    ("daily", "layout", {}, "daily_cap"),
    ("duplicate", "layout", {}, "duplicate_question"),
    ("dismissals", "layout", {}, "dismissals"),
    ("off", "layout", {}, "setting_off"),
    ("worker", "layout", {}, "only_supervisors"),
    ("threshold", "options", {"options": 2, "attributes": 2}, "below_threshold"),
    ("on", "options", {"options": 3}, None),
    ("two", "options", {"options": 2, "attributes": 3}, None),
    ("small_table", "comparison", {"rows": 4, "columns": 2}, "below_threshold"),
    ("table", "comparison", {"rows": 4, "columns": 3}, None),
    ("short_flow", "flow", {"steps": 4}, "below_threshold"),
    ("flow", "flow", {"steps": 5}, None),
    ("branch", "flow", {"branches": 1}, None),
    ("short_diff", "diff", {"lines": 12}, "below_threshold"),
    ("diff", "diff", {"lines": 13}, None),
    ("unknown", "status", {}, "below_threshold"),
    ("invalid", "options", {"options": -1}, "invalid"),
    ("expired", "layout", {}, None),
    ("old_dismissals", "layout", {}, None),
])
def test_offer_limits(db, session, monkeypatch, case, kind, counts, reason):
    boss, worker = session
    clock = [1000000.0]
    monkeypatch.setattr(offers.time, "time", lambda: clock[0])
    question = "Which layout?"
    if case in ("open", "cooldown", "daily", "duplicate", "dismissals", "expired", "old_dismissals"):
        total = 4 if case == "daily" else 2 if case in ("dismissals", "old_dismissals") else 1
        for i in range(total):
            oid = offers.offer(db, boss, f"Which previous layout {i}?", "layout")["id"]
            if case not in ("open", "expired"):
                offers.resolve(db, boss.id, oid, case not in ("dismissals", "old_dismissals"))
            clock[0] += 901
        if case == "cooldown":
            clock[0] -= 900
        if case == "duplicate":
            question = " WHICH previous LAYOUT 0! "
        if case == "expired":
            clock[0] += offers.FOLD
        if case == "old_dismissals":
            clock[0] += offers.DAY
    if case == "off":
        config.set_user("visuals", "off")
    if reason == "invalid":
        with pytest.raises(ServiceError):
            offers.offer(db, boss, question, kind, **counts)
        return
    result = offers.offer(db, worker if case == "worker" else boss, question, kind, **counts)
    assert result.get("reason") == reason
    assert result["offered"] is (reason is None)
    if reason is None:
        assert offers.current(db, boss.id)["id"] == result["id"]


def test_accept_and_explicit_ask_allow_one_publish(db, session):
    boss, worker = session
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        assert client.patch("/v1/user-settings", json={"visuals": "bad"}).status_code == 400
        assert client.patch("/v1/user-settings", json={"visuals": "on"}).json()["visuals"] == "on"
        oid = offers.offer(db, boss, "Which layout?", "layout")["id"]
        path = f"/v1/sessions/{boss.id}/visual-offer"
        assert client.get(path).json()["id"] == oid
        assert client.post(path, json={"offer": oid, "accept": True}).json()["accepted"]
        assert client.post(path, json={"offer": oid, "accept": True}).status_code == 409
        messages = db.conn.execute("SELECT body FROM inbox WHERE agent_id=?", (boss.id,)).fetchall()
        assert len(messages) == 1 and "Which layout?" in messages[0]["body"]
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Unasked plan", "<p>no</p>", kind="plan")
        visuals.publish(db, worker, "Layout", "<p>layout</p>")
        with pytest.raises(ServiceError, match="accept an offer"):
            visuals.publish(db, boss, "Another", "<p>no</p>")
        assert client.patch("/v1/user-settings", json={"visuals": "off"}).json()["visuals"] == "off"
        assert client.get("/v1/user-settings").json()["visuals"] == "off"
        agents.send_message(db, boss.id, "Please make a visual of this layout", person=True)
        # A failed publication must preserve the grant for a corrected page.
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Bad", "")
        visuals.publish(db, worker, "Requested", "<p>yes</p>")
        agents.send_message(db, boss.id, "Make a visual", sender_id=worker.id)
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Unrequested", "<p>no</p>")
        agents.handle_hook(db, boss.id, "prompt-submit", {"prompt": "Create a planning doc for this change"})
        visuals.publish(db, worker, "Plan", "<p>plan</p>", kind="plan")


def test_answer_folds_offer_and_setting_never_grants(db, session):
    boss, _ = session
    oid = offers.offer(db, boss, "Which layout?", "layout")["id"]
    agents.send_message(db, boss.id, "Use the second layout", person=True)
    assert offers.current(db, boss.id) is None
    with pytest.raises(ServiceError):
        offers.resolve(db, boss.id, oid, True)
    config.set_user("visuals", "off")
    assert offers.current(db, boss.id) is None
    for text in ("Don't make a visual", "What does make a visual mean?", "Make sure no visual is created"):
        agents.send_message(db, boss.id, text, person=True)
        with pytest.raises(ServiceError):
            visuals.publish(db, boss, "No request", "<p>no</p>")

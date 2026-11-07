import pytest
from starlette.testclient import TestClient

from brindle import agents, config, visual_offers as offers, visuals
from brindle.daemon.app import build_app
from brindle.db import Agent, Workspace
from brindle.service.errors import ServiceError


def terminal_input(client, root):
    path = f"/v1/sessions/{root}/visual-input"
    observer = client.post(path, json={"begin": True}).json()["generation"]
    sequence = 0
    def send(text, **fields):
        nonlocal sequence
        sequence += 1
        submitted_at = offers.time.time()
        generation = client.post(path, json={"refresh": observer, "submitted_at": submitted_at}).json()["generation"]
        return client.post(path, json={"text": text, "generation": generation, "sequence": sequence,
                                      "submitted_at": submitted_at, **fields})
    return send


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
        assert terminal_input(client, boss.id)("").status_code == 200
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
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Not a planning request", "<p>no</p>", kind="plan")
        # A failed publication must preserve the grant for a corrected page.
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Bad", "")
        visuals.publish(db, worker, "Requested", "<p>yes</p>")
        agents.send_message(db, boss.id, "Make a visual", sender_id=worker.id)
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Unrequested", "<p>no</p>")
        agents.handle_hook(db, boss.id, "prompt-submit", {"prompt": "Create a planning doc for this change"})
        visuals.publish(db, worker, "Plan", "<p>plan</p>", kind="plan")


@pytest.mark.parametrize("text,scope,allowed", [
    ("Make a visual only if I ask you later.", "visual", False),
    ("Compare these choices. Please make a visual.", "visual", True),
    ("Create a planning document once I approve the approach.", "plan", False),
    ("Please visualize this layout.", "visual", True),
    ("I am quoting a sentence.\nMake a visual.", "visual", False),
    ("can you visualize the changes made that we are planning to release for 0.6.3?", "visual", True),
    ("Show me the release flow visually.", "visual", True),
    ("Visualize the flow and then delete the branch.", "visual", False),
    ("Visualize the flow once I approve the approach.", "visual", False),
    ("Can you visualize the flow tomorrow?", "visual", False),
    ("Can you visualize the approval flow since 0.6.2?", "visual", True),
])
def test_complete_request_intent(db, session, text, scope, allowed):
    boss, _ = session
    agents.send_message(db, boss.id, text, person=True)
    if allowed:
        visuals.publish(db, boss, "Requested", "<p>yes</p>", kind=scope)
    else:
        with pytest.raises(ServiceError):
            visuals.publish(db, boss, "Conditional", "<p>no</p>", kind=scope)
        # Conditional intent requires a fresh host confirmation, even with offers off.
        config.set_user("visuals", "off")
        request = offers.current(db, boss.id)
        assert request["confirmation"] == scope
        offers.resolve(db, boss.id, request["id"], True)
        visuals.publish(db, boss, "Confirmed now", "<p>yes</p>", kind=scope)
        with pytest.raises(ServiceError):
            offers.resolve(db, boss.id, request["id"], True)


def test_codex_terminal_submission_folds_and_authorizes(db, session):
    boss, worker = session
    db.update_agent(boss.id, provider="codex")
    oid = offers.offer(db, db.get_agent(boss.id), "Which layout?", "layout")["id"]
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        path = f"/v1/sessions/{boss.id}/visual-input"
        send = terminal_input(client, boss.id)
        assert send("2").status_code == 200
        assert offers.current(db, boss.id) is None
        with pytest.raises(ServiceError):
            offers.resolve(db, boss.id, oid, True)
        assert send("Make a visual\nonly if I ask later").status_code == 200
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Conditional multiline draft", "<p>no</p>")
        assert send("Make a visual").status_code == 200
        assert send("").status_code == 200
        assert send("y").status_code == 200
        assert send("Do not change permissions").status_code == 200
        visuals.publish(db, worker, "Codex request", "<p>yes</p>")
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "No second request", "<p>no</p>")
        assert send("Make a visual", expires_at=offers.time.time() - 1).status_code == 409
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Late recording", "<p>no</p>")
        assert send("Make a visual").status_code == 200
        assert send("Don't make a visual").status_code == 200
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Cancelled request", "<p>no</p>")


def test_superseded_terminal_observation_cannot_restore_consent(db, session):
    boss, worker = session
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        path = f"/v1/sessions/{boss.id}/visual-input"
        # Cancellation reaches the actual dispatch before the older request.
        assert client.post(path, json={"begin": True, "expires_at": offers.time.time() - 1}).status_code == 409
        generation = client.post(path, json={"begin": True}).json()["generation"]
        newer = {"text": "Don't make a visual", "generation": generation, "sequence": 2, "submitted_at": offers.time.time()}
        older = {"text": "Make a visual", "generation": generation, "sequence": 1, "submitted_at": offers.time.time()}
        assert client.post(path, json=newer).status_code == 200
        assert client.post(path, json=older).status_code == 409
        assert client.post(path, json={"text": "Make a visual"}).status_code == 400
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Superseded", "<p>no</p>")
        # A replaced view's larger sequence cannot override the new view either.
        next_generation = client.post(path, json={"begin": True}).json()["generation"]
        assert client.post(path, json={"refresh": generation, "submitted_at": offers.time.time()}).status_code == 409
        assert client.post(path, json={**older, "generation": next_generation}).status_code == 200
        assert client.post(path, json={**newer, "sequence": 99}).status_code == 409
        visuals.publish(db, worker, "Current view", "<p>yes</p>")
        # Direct person messages invalidate snapshots while the same terminal stays open.
        send = terminal_input(client, boss.id)
        assert send("Make a visual").status_code == 200
        old_context = db.conn.execute("SELECT generation FROM visual_input_state WHERE root_id=?", (boss.id,)).fetchone()[0]
        agents.send_message(db, boss.id, "Use blue for the labels", person=True)
        path = f"/v1/sessions/{boss.id}/visual-input"
        # An observation already holding the old snapshot remains superseded.
        assert client.post(path, json={"text": "Make a visual", "generation": old_context, "sequence": 2,
                                       "submitted_at": offers.time.time()}).status_code == 409
        assert send("Don't make a visual").status_code == 200
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Cancelled after direct message", "<p>no</p>")
        agents.send_message(db, boss.id, "Use red for the labels", person=True)
        offers.offer(db, boss, "Which layout?", "layout")
        assert send("2").status_code == 200
        assert offers.current(db, boss.id) is None
        agents.send_message(db, boss.id, "Use green for the labels", person=True)
        assert send("Make a visual").status_code == 200
        visuals.publish(db, worker, "Fresh ask", "<p>yes</p>")
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "No second ask", "<p>no</p>")


def test_refresh_processed_after_direct_cancellation_cannot_relabel_old_ask(db, session, monkeypatch):
    boss, worker = session
    clock = [1000000.0]
    monkeypatch.setattr(offers.time, "time", lambda: clock[0])
    token = "t" * 43
    with TestClient(build_app(token), headers={"Authorization": f"Bearer {token}"}) as client:
        path = f"/v1/sessions/{boss.id}/visual-input"
        observer = client.post(path, json={"begin": True}).json()["generation"]
        submitted_at = clock[0]
        # Hold refresh before dispatch/processing, rather than holding a response
        # whose snapshot has already been taken.
        delayed = {"refresh": observer, "submitted_at": submitted_at, "expires_at": submitted_at + .25}
        clock[0] += .05
        agents.send_message(db, boss.id, "Don't make a visual", person=True)
        clock[0] += .05
        assert client.post(path, json=delayed).status_code == 409
        current = db.conn.execute("SELECT generation FROM visual_input_state WHERE root_id=?", (boss.id,)).fetchone()[0]
        old_ask = {"text": "Make a visual", "generation": current, "sequence": 1, "submitted_at": submitted_at}
        assert client.post(path, json=old_ask).status_code == 409
        with pytest.raises(ServiceError):
            visuals.publish(db, worker, "Old ask after cancellation", "<p>no</p>")
        fresh = clock[0]
        generation = client.post(path, json={"refresh": observer, "submitted_at": fresh}).json()["generation"]
        assert client.post(path, json={**old_ask, "generation": generation, "submitted_at": fresh}).status_code == 200
        visuals.publish(db, worker, "Actually asked afterward", "<p>yes</p>")


def test_initial_person_prompt_is_observed_before_provider_launch(db, repo, monkeypatch):
    from brindle import workspaces
    from brindle.service import sessions

    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    ws = workspaces.adopt_root(db, str(repo))
    launched = []
    def launch(db, agent, ws, **kwargs):
        # Use the real gate at launch time; a hook after launch is too late.
        with db.tx() as c:
            offers.consume(c, agent.id)
        launched.append(agent.id)
    monkeypatch.setattr(agents, "_launch", launch)
    started = sessions.launch(db, ws, provider="codex", prompt="Make a visual")
    assert launched == [started.agent.id]


def test_legacy_unscoped_grants_are_revoked(db, session):
    from brindle.db import DB

    boss, _ = session
    agents.send_message(db, boss.id, "Make a visual", person=True)
    observer = offers.begin_input(db, boss)
    db.conn.execute("ALTER TABLE visual_grants DROP COLUMN scope")
    db.conn.execute("ALTER TABLE visual_input_state DROP COLUMN observer")
    db.conn.execute("ALTER TABLE visual_input_state DROP COLUMN invalidated_at")
    reopened = DB(db.path)
    try:
        assert offers.refresh_input(reopened, boss, observer, offers.time.time()) == observer
        with pytest.raises(ServiceError):
            with reopened.tx() as c:
                offers.consume(c, boss.id)
    finally:
        reopened.conn.close()


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

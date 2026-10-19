from brindle import knowledge


def test_intents_projection_rebuilds(db):
    key = "github.com/acme/app"
    knowledge.append(db, key, knowledge.INTENT, {"summary": "auth", "paths": ["src/auth/**"]},
                     session="s1", ts=1.0)
    knowledge.append(db, key, knowledge.INTENT, {"summary": "billing", "paths": ["src/bill.py"]},
                     session="s2", ts=2.0)
    knowledge.append(db, key, knowledge.PAUSE, {}, session="s1", ts=3.0)
    before = knowledge.intents(db, key)
    assert [(i["session_root"], i["status"], i["paths"]) for i in before] == [
        ("s2", "live", ["src/bill.py"]), ("s1", "paused", ["src/auth/**"])]

    db.conn.execute("DELETE FROM intents")
    knowledge.rebuild(db, key)
    assert knowledge.intents(db, key) == before

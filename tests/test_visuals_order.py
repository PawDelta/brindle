import time

import pytest

from brindle import plan_docs, plans, visual_offers, visuals
from brindle.db import Agent, Workspace


@pytest.mark.parametrize("edit", [
    "supervisor",
    "person",
    pytest.param("suggestion", marks=pytest.mark.variant(
        "accepting a suggestion must reorder the doc just like a direct edit")),
])
def test_session_docs_sort_by_last_edit(db, tmp_path, monkeypatch, edit):
    now = 100.0
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setenv("BRINDLE_VISUALS", "1")
    monkeypatch.setattr(visual_offers, "consume", lambda *args, **kwargs: None)
    repo = str(tmp_path)
    ws = Workspace("ws1", repo, "main", "main", "main", None, repo, None, "brindle-ws1", now)
    db.add_workspace(ws)
    boss = Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "idle", "%1", None, now,
                 headless=1)
    db.add_agent(boss)

    if edit == "supervisor":
        first = visuals.publish(db, boss, "A", "<p>Original</p>")
    else:
        first = plans.propose(db, boss, "# A\n\nOriginal")
    now += 1
    second = visuals.publish(db, boss, "B", "<p>Another doc</p>")
    now += 1

    if edit == "supervisor":
        visuals.publish(db, boss, "A", "<p>Edited</p>", visual_id=first["id"])
    elif edit == "person":
        plan_docs.edit(db, first["id"], first["rev"], plan_docs.LEDE, 0, "# A", "# Edited")
    else:
        thread = plan_docs.suggest(db, first["id"], first["rev"], plan_docs.LEDE, 0, "# A", "# Edited")
        plan_docs.accept(db, first["id"], thread, first["rev"])

    assert [v["id"] for v in visuals.for_session(db, boss.id)] == [first["id"], second["id"]]

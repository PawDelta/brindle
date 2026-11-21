"""Names survive the Library sync and reach the supervisor without account IDs."""

import pytest

from brindle import plan_docs, plans
from brindle.pro import shared_docs as sd
from test_shared_plan_docs import (  # noqa: F401
    DESIGN, TEAM, backend, fixed_identity, parts, pro_env, session, signing_key,
)


@pytest.fixture
def named_comment(db, parts, backend):
    pid, lib, net = parts
    backend.routes[f"GET {DESIGN}/people"] = lambda f, h: (200, {"people": [
        {"id": "64680428-david", "display_name": "David Chen", "email": "david@example.com",
         "teams": [], "role": "member", "on_plan": True},
        {"id": "reply-account", "display_name": "", "email": "reply@example.com",
         "teams": [], "role": "member", "on_plan": True},
    ]})
    sd.share(db, pid, TEAM, **net)
    lib.threads = [{"id": 1, "kind": "comment", "rev": 1, "author": "64680428-david",
                    "text": "Please clarify this.", "at": 5.0, "status": "open",
                    "anchor": {"kind": "section", "section": "summary"},
                    "replies": [{"id": 2, "author": "reply-account", "text": "Agreed.", "at": 6.0},
                                {"id": 3, "author": "unknown-account", "text": "Me too.", "at": 7.0}]}]
    lib.votes = {"d1": {"repo": ["64680428-david"]}}
    sd.sync(db, pid, **net)
    return pid


@pytest.mark.variant("Synced authors must retain directory names on disk, independently of relay formatting")
def test_synced_comment_keeps_the_directory_name_offline(db, named_comment):
    got = plan_docs.fields(db, plans.get(db, named_comment))
    thread = got["threads"][0]
    assert thread.get("author_name") == "David Chen"
    assert [r.get("author_name") for r in thread["replies"]] == ["reply@example.com", "A teammate"]
    assert got["teammate_names"]["64680428-david"] == "David Chen"
    stored = db.conn.execute("SELECT author_name FROM plan_threads WHERE id=?", (thread["id"],)).fetchone()
    assert stored[0] == "David Chen"


@pytest.mark.variant("The supervisor relay must use names too, independently of the doc DTO's display labels")
def test_supervisor_relay_names_the_teammate(db, named_comment):
    message, record, ids = plan_docs.pending(db, plans.get(db, named_comment))
    assert ids
    for lines in (message, record):
        text = "\n".join(lines)
        assert "David Chen" in text
        assert "reply@example.com" in text
        assert "A teammate" in text
        assert "64680428" not in text
        assert "reply-account" not in text
        assert "unknown-account" not in text

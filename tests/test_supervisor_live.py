"""The supervisor writes live into a shared plan's draft: its changes are
worked out against the text it read (plans.read_draft), never against the
draft as it is when they land (plans.propose with a base)."""

import pytest

from brindle import plans
from brindle.pro import shared_docs as sd
from brindle.service.errors import ServiceError
from pro_fixtures import backend, claims, fixed_identity, pro_env, sign, signing_key  # noqa: F401
from test_plans import session  # noqa: F401 - fixture
from test_shared_plan_docs import DOC, SUMMARY, TEAM, Relay, parts  # noqa: F401 - fixture

HOSTS = [{"id": "summary", "title": "Summary", "markdown": SUMMARY.replace("Workers", "Hosts")}]


@pytest.fixture
def sent_back(db, parts, session, monkeypatch):
    """A shared plan with a live draft, sent back to the supervisor, who has read it."""
    pid, lib, net = parts
    sd.share(db, pid, TEAM, **net)
    lib.open_draft()
    monkeypatch.setattr(sd, "relays", {DOC: Relay(lib)}, raising=False)
    sd.sync(db, pid, **net)
    plans.decide(db, pid, 1, False, note="Say where workers run.", person=True)
    read = plans.read_draft(db, session[0], pid)
    assert read["sections"][0]["markdown"] == SUMMARY
    return pid, lib, net, read["base"]


def _text(db, pid):
    return plans.to_dto(db, plans.get(db, pid))["sections"][0]["markdown"]


def test_a_sentence_added_after_the_supervisor_read_is_still_there_after_its_revision(db, session, sent_back):
    boss, _ = session
    pid, lib, net, base = sent_back
    lib.draft.insert("summary", len(SUMMARY), " It stays on.")     # a teammate types after the read
    sd.sync(db, pid, **net)
    with pytest.raises(ServiceError) as refused:                    # a write must name what it read
        plans.propose(db, boss, "# Cloud workers", sections=HOSTS, plan_id=pid)
    assert refused.value.code == "base_required"

    p = plans.propose(db, boss, "# Cloud workers", sections=HOSTS, plan_id=pid, base=base)
    both = "Hosts run on a machine the customer controls. It stays on."
    assert (_text(db, pid), p["rev"], p["state"], p["draft_write"]["suggested"]) == (both, 1, "open", 0)
    sd.sync(db, pid, **net)
    assert lib.draft.text("summary") == both
    # Its letters are under its own peer id, not this machine's.
    row = db.conn.execute("SELECT peer_id, supervisor_peer FROM plan_drafts").fetchone()
    assert row["supervisor_peer"] not in (None, row["peer_id"])
    assert lib.draft._doc.oplog_vv.get_last(row["supervisor_peer"]) is not None


def test_a_supervisor_change_to_words_someone_also_changed_arrives_as_its_suggestion(db, session, sent_back):
    boss, _ = session
    pid, lib, net, base = sent_back
    lib.draft.replace("summary", 0, len("Workers"), "Agents")      # a teammate rewrites the same word
    sd.sync(db, pid, **net)

    p = plans.propose(db, boss, "# Cloud workers", sections=HOSTS, plan_id=pid, base=base)
    theirs = SUMMARY.replace("Workers", "Agents")
    assert (_text(db, pid), p["draft_write"]["applied"]) == (theirs, 0)
    [s] = [t for t in plans.to_dto(db, plans.get(db, pid))["threads"] if t["kind"] == "suggestion"]
    assert (s["author"], s["before"], s["after"]) == ("supervisor", theirs, HOSTS[0]["markdown"])

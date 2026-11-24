"""Call counts for the plan sharing paths."""

from brindle import plans, visual_offers
from test_plans import session  # noqa: F401 - fixture


def test_plan_dto_looks_up_the_draft_once_when_no_replica_exists(db, session):
    boss, _ = session
    visual_offers.person_spoke(db, boss, "Make a planning doc.")
    plan = plans.propose(db, boss, "# Cloud workers", [])
    statements = []
    db.conn.set_trace_callback(statements.append)
    try:
        dto = plans.to_dto(db, plans.get(db, plan["id"]))
    finally:
        db.conn.set_trace_callback(None)

    assert dto["id"] == plan["id"]
    assert sum("FROM plan_shares WHERE plan_id=" in sql and "doc_id IS NOT NULL" in sql
               for sql in statements) == 1

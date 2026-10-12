"""The supervisor's rules for acting on findings (#44, #45), and the person's
standing ``rules`` from brindle config, in every supervisor's brief."""

import json
import time

from brindle import agents, workspaces
from brindle.db import Agent


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


def brief(db, ws, profile="supervisor", mode="interactive"):
    a = Agent("s1", ws.id, profile, "claude", None, mode, "processing", "%s1", None, time.time())
    db.add_agent(a)
    return agents._profile_for(db, a, ws).prompt


def test_supervisor_brief_carries_the_rules(db, repo):
    write(repo / ".brindle" / "config.json", {"rules": ["Never touch the vendored code"]})
    ws = workspaces.create(db, str(repo), "feature").workspace
    prompt = brief(db, ws)
    assert "- Never touch the vendored code" in prompt

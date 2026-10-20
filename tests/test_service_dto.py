"""The DTOs keep up with what view.snapshot returns: every key the engine
sends for a workspace or an agent has a field on the matching model."""

import time

from brindle import view, workspaces
from brindle.db import Agent
from brindle.service.dto import AgentDTO, WorkspaceDTO


def test_snapshot_keys_all_have_dto_fields(db, repo):
    ws = workspaces.create(db, str(repo), "feat/dto").workspace
    db.add_agent(Agent("w1", ws.id, "developer", "claude", None, "assign", "idle", "",
                       None, time.time()))
    [entry] = view.snapshot(db, str(repo), panes={})
    [agent] = entry["agents"]
    assert set(entry) - set(WorkspaceDTO.model_fields) == set()
    assert set(agent) - set(AgentDTO.model_fields) == set()
    assert WorkspaceDTO.model_validate(entry).to_json()["agents"][0]["id"] == "w1"

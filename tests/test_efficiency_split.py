"""Parallel parts of one file: with ``overlap_granularity`` "hunk", tasks
that declare disjoint regions of a shared file ("big.py:1-10") start side by
side; regions that meet, declared or actually changed, still queue. Runs the
real assign dispatch (declared check before the reply, inspected check at
launch) with only the CLI launch faked."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from conftest import sh
from fast_repo import repo, repo_template  # noqa: F401
from brindle import agents, mcp_server, tasks, workspaces
from brindle.db import Agent
from test_merge_order import fake_spawn

LINES = 100


@pytest.fixture(autouse=True)
def no_real_spawn(monkeypatch):
    monkeypatch.setattr(agents, "spawn", fake_spawn)


@pytest.fixture
def supervisor(db, repo, monkeypatch):
    """A supervisor on the main checkout of a repo whose ``big.py`` the
    parts share; ``set_granularity`` writes the repo's overlap settings."""
    (repo / "big.py").write_text("".join(f"line {n}\n" for n in range(1, LINES + 1)))
    sh("git add big.py && git commit -qm big", repo)
    (repo / ".brindle").mkdir(exist_ok=True)
    ws = workspaces.adopt_root(db, str(repo))
    db.add_agent(Agent("boss", ws.id, "supervisor", "claude", None, "interactive", "processing",
                       "@0", None, time.time()))
    monkeypatch.setenv("BRINDLE_AGENT_ID", "boss")

    def set_granularity(granularity: str) -> None:
        (repo / ".brindle" / "config.json").write_text(json.dumps(
            {"overlap": "queue", "overlap_granularity": granularity, "pipeline": False, "review": False}))
    return set_granularity


def assign(db, repo, task: str, files: list[str]) -> tasks.Task:
    asyncio.run(mcp_server.assign("developer", task, files=files))
    [t] = [t for t in db.list_tasks(str(repo)) if t.task_text == task]
    return t


def edit_line(db, t: tasks.Task, line: int) -> None:
    """Commit a change to ``big.py``'s ``line`` on task ``t``'s branch."""
    path = Path(db.get_workspace(db.get_agent(t.agent_id).workspace_id).path)
    text = (path / "big.py").read_text().splitlines(keepends=True)
    text[line - 1] = f"changed {line}\n"
    (path / "big.py").write_text("".join(text))
    sh(f"git commit -qam edit-{line}", path)


@pytest.mark.parametrize("granularity, second, starts", [
    ("hunk", "big.py:60-70", True),
    ("file", "big.py:60-70", False),
    ("hunk", "big.py:5-20", False),
    pytest.param("file", "big.py:5-20", False, marks=pytest.mark.variant(
        "the brief's matrix: overlapping regions queue under file granularity too")),
    pytest.param("hunk", "big.py", False, marks=pytest.mark.variant(
        "a shared file declared without a region still counts whole under hunk granularity")),
])
def test_parts_of_one_file_start_in_parallel_only_on_disjoint_regions(db, repo, supervisor,
                                                                      granularity, second, starts):
    supervisor(granularity)
    first = assign(db, repo, "part 1", ["big.py:1-10"])
    assert first.state == "started"
    other = assign(db, repo, "part 2", [second])
    if starts:
        assert other.state == "started" and other.agent_id != first.agent_id
    else:
        assert other.state == "pending" and tasks._loads(other.depends_on) == [first.agent_id]


def test_changed_hunks_that_drift_into_a_region_queue_it_and_warn_at_merge(db, repo, supervisor):
    """Declared regions are a promise, the branch's hunks are the fact: a
    worker that strays into another part's lines queues new work on them,
    and its merge warns the part it drifted into."""
    supervisor("hunk")
    first = assign(db, repo, "part 1", ["big.py:1-10"])
    second = assign(db, repo, "part 2", ["big.py:60-70"])
    assert (first.state, second.state) == ("started", "started")
    edit_line(db, first, 65)     # outside its declared 1-10, into part 2's lines
    edit_line(db, first, 85)     # and into lines nobody declared
    edit_line(db, second, 64)
    third = assign(db, repo, "part 3", ["big.py:40-45"])
    assert third.state == "started"      # nothing changed there
    fourth = assign(db, repo, "part 4", ["big.py:84-86"])   # only first's actual hunk meets it
    assert fourth.state == "pending" and tasks._loads(fourth.depends_on) == [first.agent_id]
    first_ws = db.get_workspace(db.get_agent(first.agent_id).workspace_id)
    warning = tasks.merge_hunk_warning(db, first_ws)
    assert warning and second.agent_id in warning and third.agent_id not in warning

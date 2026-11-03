import pytest

from brindle import autopilot
from brindle.config import RepoConfig
from test_autopilot import add_agent, root  # noqa: F401  (the `root` fixture)


def set_goal(db, items):
    autopilot.set_goal(db, "boss", "Goal", items)







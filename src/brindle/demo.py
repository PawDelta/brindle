"""``brindle demo``: a tiny repo with two failing test files and a goal, so a
first run shows the whole loop in a few minutes: two workers in parallel,
a review on each branch, gated merges, and milestones that turn green only
when their check command passes.

The repo is plain Python (unittest, no dependencies) under
``~/.brindle/demo/``; nothing is created where the command runs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from brindle.config import brindle_home

if TYPE_CHECKING:
    from brindle.db import DB

FILES = {
    "README.md": "# brindle demo\n\nA small text toolkit. Run the tests with `python3 -m unittest -q`.\n",
    "textkit/__init__.py": "",
    "textkit/slug.py": '''def slugify(text: str) -> str:
    """Lowercase ``text``, turn runs of anything but letters and digits into
    one hyphen, and trim hyphens from the ends: "Hello, World!" -> "hello-world"."""
    raise NotImplementedError
''',
    "textkit/wrap.py": '''def wrap(text: str, width: int) -> list[str]:
    """Split ``text`` into lines of at most ``width`` characters, breaking only
    between words (a word longer than ``width`` gets a line to itself)."""
    raise NotImplementedError
''',
    "tests/__init__.py": "",
    "tests/test_slug.py": '''import unittest

from textkit.slug import slugify


class SlugTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_runs_and_ends(self):
        self.assertEqual(slugify("  --brindle  demo 2--  "), "brindle-demo-2")

    def test_empty(self):
        self.assertEqual(slugify("!!!"), "")
''',
    "tests/test_wrap.py": '''import unittest

from textkit.wrap import wrap


class WrapTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(wrap("the quick brown fox", 10), ["the quick", "brown fox"])

    def test_long_word(self):
        self.assertEqual(wrap("a supercalifragilistic b", 5), ["a", "supercalifragilistic", "b"])

    def test_empty(self):
        self.assertEqual(wrap("", 5), [])
''',
    ".gitignore": "__pycache__/\n",
}

GOALS = """# Finish the textkit helpers

This is brindle's demo. Show the person how brindle works: assign each milestone
to its own worker at the same time (weight "light"), let brindle review and
merge both branches, then call check_milestone. Name files in task briefs
relative to the repo root: each worker has its own worktree. Narrate briefly
as you go.

## slugify works
check: python3 -m unittest tests.test_slug -q

## wrap works
check: python3 -m unittest tests.test_wrap -q
"""


# The worker and reviewer profiles a demo runs on, by the CLI the person has
# signed in to (the app's demo): every agent of the demo on that one provider.
PROFILES = {
    "claude": ("developer", "reviewer"),
    "codex": ("developer-codex", "reviewer-codex"),
    "antigravity": ("developer-gemini", "reviewer-gemini"),
    "opencode": ("developer-opencode", "reviewer-opencode"),
}


def _config(local: bool, provider: str | None = None) -> dict:
    cfg = {
        # What every branch must pass to merge. Not the whole suite: until both
        # milestones land, each branch still has the other's failing stub, and
        # the demo is about two branches merging on their own. The milestone
        # checks are what verify the behaviour.
        "checks": ["python3 -m compileall -q textkit tests"],
        "max_agents": 2,
        "fetch": False,
        # The demo shows the whole loop, so approved branches merge on their own.
        "auto_merge_default_branch": True,
        # The demo is about parallel workers: "balanced" lets the supervisor
        # sometimes build both milestones itself, and then there are none.
        "delegation": "fast",
    }
    if local:
        cfg.update(default_agent="developer-local", review_profile="reviewer-local",
                   routing={"light": ["developer-local"]})
    elif provider in PROFILES:
        developer, reviewer = PROFILES[provider]
        cfg.update(local_models=False, default_agent=developer, review_profile=reviewer,
                   routing={"light": [developer]})
    else:
        # Fast and predictable: no local model, even when one is configured.
        cfg.update(local_models=False, routing={"light": ["developer"]})
    return cfg


def _config_path(root: Path) -> Path:
    return root / ".brindle" / "config.json"


def is_demo_repo(path: str | Path) -> bool:
    """True when ``path`` is inside the demo folder (symlinks resolved); a repo
    merely named textkit-* elsewhere is not a demo."""
    return (brindle_home() / "demo").resolve() in Path(path).resolve().parents


def unused(db: DB) -> list[Path]:
    """The practice repos no session has run in yet (one a launch's preflight
    held while the person settled it), newest first."""
    return [root for root in sorted((brindle_home() / "demo").glob("textkit-*"), reverse=True)
            if (root / ".brindle" / "goals.md").exists()
            and not any(db.list_agents(ws.id) for ws in db.find_workspaces(os.path.realpath(root)))]


def held(db: DB, provider: str | None = None) -> Path | None:
    """The newest unused practice repo made for ``provider``, None when there is
    none. Every other unused one is discarded: at most one is ever kept."""
    found = None
    for root in unused(db):
        try:
            made = json.loads(_config_path(root).read_text())
        except (OSError, ValueError):
            made = None
        if found is None and made == _config(False, provider):
            found = root
        else:
            discard(db, root)
    return found


def discard(db: DB, root: Path) -> None:
    """Remove an unused practice repo and forget it (its preflight was cancelled)."""
    for ws in db.find_workspaces(os.path.realpath(root)):
        db.delete_workspace(ws.id)
    shutil.rmtree(root, ignore_errors=True)


def create(local: bool = False, provider: str | None = None) -> Path:
    """A fresh demo repo with one commit; returns its path. ``provider``: the
    CLI its workers and reviewers run on (PROFILES); Claude's when None."""
    from brindle import spawn

    spawn.ensure_git()      # before the demo's files exist: the git below would raise Apple's dialog
    base = brindle_home() / "demo"
    base.mkdir(parents=True, exist_ok=True)
    # A new name every time, so no earlier demo's sessions or branches show up.
    stamp = time.strftime("%m%d-%H%M%S")
    root, n = base / f"textkit-{stamp}", 1
    while root.exists():
        n += 1
        root = base / f"textkit-{stamp}-{n}"
    for rel, text in FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (root / ".brindle").mkdir()
    _config_path(root).write_text(json.dumps(_config(local, provider), indent=2) + "\n")
    (root / ".brindle" / "goals.md").write_text(GOALS)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    # In the demo repo's own config (never the person's global one): a new
    # account has no identity, and the workers' and merges' commits need one.
    subprocess.run(["git", "config", "user.name", "brindle demo"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "demo@brindle.invalid"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "textkit: stubs and tests"], cwd=root, check=True)
    return root

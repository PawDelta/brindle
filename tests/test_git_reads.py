from __future__ import annotations

import subprocess

from conftest import sh

from brindle import git, spawn


def test_branch_exists_is_cached(tmp_path, monkeypatch):
    sh("git init -q -b main && git config user.email a@b.c && git config user.name n"
       " && git commit -q --allow-empty -m init", tmp_path)
    calls: list[list[str]] = []
    real = spawn.read

    def counting(argv, **kw):
        calls.append(argv)
        return real(argv, **kw)

    monkeypatch.setattr(spawn, "read", counting)

    assert git.branch_exists(tmp_path, "main")
    assert not git.branch_exists(tmp_path, "nope")
    assert git.branch_exists(tmp_path, "main")
    assert len(calls) == 1

    git.run(["branch", "feat"], tmp_path)           # brindle creates: seen at once
    assert git.branch_exists(tmp_path, "feat")
    git.run(["branch", "-D", "feat"], tmp_path)     # brindle deletes: seen at once
    assert not git.branch_exists(tmp_path, "feat")

    # A change landing between the read and the publish: the stale listing is
    # discarded, the lookup re-reads once, and only the fresh one is cached.
    listings = iter(["refs/heads/main\n", "refs/heads/main\nrefs/heads/feat\n"])
    reads = []

    def racing_run(args, cwd, check=True, **kw):
        reads.append(args)
        stale = next(listings)
        if len(reads) == 1:
            git.invalidate_branches()    # brindle creates "feat" after this read began
        return subprocess.CompletedProcess(args, 0, stale, "")

    git.invalidate_branches()
    monkeypatch.setattr(git, "run", racing_run)
    assert git.branch_exists(tmp_path, "feat")
    assert git.branch_exists(tmp_path, "feat")
    assert len(reads) == 2

from __future__ import annotations

import subprocess
import threading

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

    # A real git.run branch create landing after a lookup took its snapshot but
    # before it published: the stale listing is discarded, the lookup re-reads,
    # and only the fresh listing is cached. spawn is faked; git.run is real.
    names = {"main"}
    listings: list[int] = []
    snapshot_taken, mutation_done = threading.Event(), threading.Event()

    def fake_read(argv, **kw):
        assert argv[:2] == ["git", "for-each-ref"]
        listings.append(1)
        seen = sorted(names)    # the snapshot, taken before any mutation
        if len(listings) == 1:
            snapshot_taken.set()
            assert mutation_done.wait(5)
        return subprocess.CompletedProcess(argv, 0, "".join(f"refs/heads/{n}\n" for n in seen), "")

    def fake_action(argv, **kw):
        assert argv == ["git", "branch", "feat"]
        names.add("feat")
        return subprocess.CompletedProcess(argv, 0, "", "")

    git.invalidate_branches()
    monkeypatch.setattr(spawn, "read", fake_read)
    monkeypatch.setattr(spawn, "action", fake_action)
    seen_by_lookup: list[bool] = []
    lookup = threading.Thread(target=lambda: seen_by_lookup.append(git.branch_exists(tmp_path, "feat")))
    lookup.start()
    assert snapshot_taken.wait(5)
    git.run(["branch", "feat"], tmp_path)
    mutation_done.set()
    lookup.join()
    assert seen_by_lookup == [True]
    assert git.branch_exists(tmp_path, "feat")
    assert len(listings) == 2

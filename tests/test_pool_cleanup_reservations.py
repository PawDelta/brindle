import pytest

from brindle.db import PoolEntry, Workspace


def _entry(path: str, *, created_at: float = 1.0) -> PoolEntry:
    return PoolEntry(
        path=path,
        repo_root="/repo",
        base_branch="main",
        base_sha="sha",
        branch="brindle-pool/test",
        fingerprint="fingerprint",
        port_base=20000,
        ready=1,
        created_at=created_at,
    )


@pytest.mark.variant("atomic transfer preserves pool metadata under durable cleanup ownership")
def test_reserve_pool_entry_cleanup_transfers_entry_to_claim(db):
    entry = _entry("/repo/pool/test")
    db.add_pool_entry(entry)

    reservation = db.reserve_pool_entry_cleanup("/repo", entry.path)

    assert reservation is not None
    assert reservation.path == entry.path
    assert reservation.repo_root == entry.repo_root
    assert reservation.base_branch == entry.base_branch
    assert reservation.base_sha == entry.base_sha
    assert reservation.branch == entry.branch
    assert reservation.fingerprint == entry.fingerprint
    assert reservation.port_base == entry.port_base
    assert reservation.state == "cleanup_pending"
    assert db.pool_entries("/repo", ready_only=False) == []
    assert db.pool_cleanup_reservation("/repo", entry.path) == reservation


def test_reserve_pool_entry_cleanup_refuses_existing_owners(db):
    entry = _entry("/repo/pool/claimed")
    db.add_pool_entry(entry)
    active = db.take_pool_entry("/repo", "main")
    assert active is not None
    db.add_pool_entry(entry)

    assert db.reserve_pool_entry_cleanup("/repo", entry.path) is None
    assert db.pool_entries("/repo", ready_only=False) == [entry]

    workspace_entry = _entry("/repo/pool/workspace", created_at=2.0)
    db.add_pool_entry(workspace_entry)
    db.add_workspace(
        Workspace(
            id="repo/workspace",
            repo_root="/repo",
            name="workspace",
            kind="worktree",
            branch="feature",
            base_branch="main",
            path=workspace_entry.path,
            port_base=None,
            tmux_session="tmux",
            created_at=2.0,
        )
    )

    assert db.reserve_pool_entry_cleanup("/repo", workspace_entry.path) is None
    assert workspace_entry in db.pool_entries("/repo", ready_only=False)


def test_cleanup_reservation_transitions_and_deletion_are_conditional(db):
    entry = _entry("/repo/pool/transition")
    db.add_pool_entry(entry)
    claim = db.take_pool_entry("/repo", "main")
    assert claim is not None

    assert db.reserve_abandoned_pool_claim_cleanup(claim.claim_token)
    assert not db.reserve_abandoned_pool_claim_cleanup(claim.claim_token)
    reservation = db.pool_cleanup_reservation("/repo", entry.path)
    assert reservation is not None
    assert reservation.state == "cleanup_pending"

    assert db.mark_pool_cleanup_remove_pending(claim.claim_token)
    assert not db.mark_pool_cleanup_remove_pending(claim.claim_token)
    reservation = db.pool_cleanup_reservation("/repo", entry.path)
    assert reservation is not None
    assert reservation.state == "remove_pending"

    assert not db.delete_pool_cleanup_reservation(claim.claim_token, "cleanup_pending")
    assert not db.delete_pool_cleanup_reservation(claim.claim_token, "active")
    assert db.delete_pool_cleanup_reservation(claim.claim_token, "remove_pending")
    assert not db.delete_pool_cleanup_reservation(claim.claim_token, "remove_pending")


@pytest.mark.variant("registered workspace prevents abandoned-claim recovery")
def test_abandoned_claim_cleanup_refuses_registered_workspace(db):
    entry = _entry("/repo/pool/registered")
    db.add_pool_entry(entry)
    claim = db.take_pool_entry("/repo", "main")
    assert claim is not None
    workspace = Workspace(
        id="repo/registered",
        repo_root="/repo",
        name="registered",
        kind="worktree",
        branch="feature",
        base_branch="main",
        path=claim.path,
        port_base=None,
        tmux_session="tmux",
        created_at=2.0,
    )
    db.add_workspace(workspace)

    assert not db.reserve_abandoned_pool_claim_cleanup(claim.claim_token)

    [stored_claim] = db.pool_claims("/repo")
    assert stored_claim.claim_token == claim.claim_token
    assert stored_claim.state == "active"
    assert db.workspace_by_path(claim.path) == workspace

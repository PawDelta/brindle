"""Cross-process startup and cleanup serialization for the daemon lifecycle."""

import multiprocessing
import stat

import pytest

from brindle.daemon import lifecycle


def _finish(processes, events):
    for event in events:
        event.set()
    for process in processes:
        if process.pid is None:
            continue
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        if process.is_alive():
            process.kill()
            process.join(10)
        assert not process.is_alive(), f"child process {process.pid} did not exit"


@pytest.mark.variant("Verifies existing lock-file permissions are restricted to 0600; line coverage cannot establish the filesystem permission invariant.")
def test_existing_daemon_lock_permissions_are_restricted(brindle_home):
    lock_path = brindle_home / "daemon.lock"
    brindle_home.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("")
    lock_path.chmod(0o644)

    with lifecycle._lifecycle_lock():
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.variant("Exercises the concurrent startup race and verifies the first daemon's socket inode is preserved while startup is serialized.")
@pytest.mark.slow("spawns two daemon starters and holds the first inside its socket bind")
def test_second_daemon_cannot_replace_socket_during_startup(brindle_home, tmp_path):
    import daemon_lock_procs

    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "d.sock"
    bound, release, first_done = ctx.Event(), ctx.Event(), ctx.Event()
    attempted, bind_attempted, second_done = ctx.Event(), ctx.Event(), ctx.Event()
    first_result, second_result = ctx.Queue(), ctx.Queue()
    first = ctx.Process(target=daemon_lock_procs.prepare_paused_bind,
                        args=(path, bound, release, first_done, first_result))
    second = ctx.Process(target=daemon_lock_procs.prepare_contender,
                         args=(path, attempted, bind_attempted, second_done, second_result))
    try:
        first.start()
        assert bound.wait(15), "first daemon did not reach the paused socket bind"
        original_inode = path.stat().st_ino

        second.start()
        assert attempted.wait(15), "second daemon did not attempt startup"
        assert not bind_attempted.wait(0.5), "second daemon reached socket binding during startup"
        assert not second_done.wait(0.5), "second daemon proceeded while startup was incomplete"
        assert path.stat().st_ino == original_inode, "second daemon replaced the first socket"

        release.set()
        assert first_done.wait(15)
        assert second_done.wait(15)
        assert first_result.get(timeout=5)[0] == "started"
        status, message = second_result.get(timeout=5)
        assert status == "refused" and "already running" in message
        assert not bind_attempted.is_set()
        first.join(10)
        second.join(10)
        assert first.exitcode == second.exitcode == 0
        assert lifecycle.read_info()["pid"] == first.pid
        assert path.stat().st_ino == original_inode
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert (brindle_home / "daemon.lock").exists()
    finally:
        _finish((first, second), (release,))


@pytest.mark.slow("spawns cleanup and replacement processes around a held ownership check")
def test_cleanup_does_not_delete_replacement_daemon_files(brindle_home, tmp_path):
    import daemon_lock_procs

    ctx = multiprocessing.get_context("spawn")
    path = tmp_path / "d.sock"
    owner_sockets, owner = lifecycle.prepare("owner-token", sock_path=path)
    original_inode = path.stat().st_ino
    checked, release, cleanup_done = ctx.Event(), ctx.Event(), ctx.Event()
    attempted, bind_attempted, replacement_done = ctx.Event(), ctx.Event(), ctx.Event()
    cleanup_result, replacement_result = ctx.Queue(), ctx.Queue()
    cleanup = ctx.Process(target=daemon_lock_procs.cleanup_paused_after_check,
                          args=(owner, checked, release, cleanup_done, cleanup_result))
    replacement = ctx.Process(target=daemon_lock_procs.prepare_replacement,
                              args=(path, attempted, bind_attempted, replacement_done, replacement_result))
    replacement_info = None
    try:
        cleanup.start()
        assert checked.wait(15), "cleanup did not reach its ownership check"
        replacement.start()
        assert attempted.wait(15), "replacement daemon did not attempt startup"
        assert not bind_attempted.wait(0.5), "replacement reached socket binding during cleanup"
        assert not replacement_done.wait(0.5), "replacement started during cleanup"
        assert path.stat().st_ino == original_inode

        release.set()
        assert cleanup_done.wait(15)
        assert replacement_done.wait(15)
        assert cleanup_result.get(timeout=5)[0] == "cleaned"
        status, replacement_info = replacement_result.get(timeout=5)
        assert status == "started"
        cleanup.join(10)
        replacement.join(10)
        assert cleanup.exitcode == replacement.exitcode == 0
        assert lifecycle.read_info() == replacement_info
        assert replacement_info["pid"] == replacement.pid
        assert path.exists() and stat.S_ISSOCK(path.stat().st_mode)
        assert (brindle_home / "daemon.lock").exists()
    finally:
        _finish((cleanup, replacement), (release,))
        for sock in owner_sockets:
            sock.close()
        if replacement_info is not None:
            lifecycle.cleanup(replacement_info)

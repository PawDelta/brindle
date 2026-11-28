"""Child-process bodies for daemon lifecycle lock tests (spawned by name)."""

from __future__ import annotations

from pathlib import Path


def prepare_paused_bind(path, bound, proceed, done, result):
    from brindle.daemon import lifecycle

    lifecycle._is_daemon = lambda pid: True
    bind = lifecycle.bind_unix

    def bind_then_wait(sock_path):
        sock = bind(sock_path)
        bound.set()
        if not proceed.wait(30):
            sock.close()
            raise TimeoutError("test did not release paused daemon startup")
        return sock

    lifecycle.bind_unix = bind_then_wait
    socks = []
    try:
        socks, info = lifecycle.prepare("first-token", sock_path=Path(path))
        result.put(("started", info))
    except Exception as exc:
        result.put(("error", type(exc).__name__, str(exc)))
    finally:
        for sock in socks:
            sock.close()
        done.set()


def prepare_contender(path, attempted, bind_attempted, done, result):
    from brindle.daemon import lifecycle

    lifecycle._is_daemon = lambda pid: True
    bind = lifecycle.bind_unix

    def track_bind(sock_path):
        bind_attempted.set()
        return bind(sock_path)

    lifecycle.bind_unix = track_bind
    attempted.set()
    socks = []
    try:
        socks, info = lifecycle.prepare("second-token", sock_path=Path(path))
        result.put(("started", info))
    except lifecycle.DaemonError as exc:
        result.put(("refused", str(exc)))
    except Exception as exc:
        result.put(("error", type(exc).__name__, str(exc)))
    finally:
        for sock in socks:
            sock.close()
        done.set()


def cleanup_paused_after_check(info, checked, proceed, done, result):
    from brindle.daemon import lifecycle

    read_info = lifecycle.read_info

    def read_then_wait():
        current = read_info()
        checked.set()
        if not proceed.wait(30):
            raise TimeoutError("test did not release paused daemon cleanup")
        return current

    lifecycle.read_info = read_then_wait
    try:
        lifecycle.cleanup(info)
        result.put(("cleaned",))
    except Exception as exc:
        result.put(("error", type(exc).__name__, str(exc)))
    finally:
        done.set()


def prepare_replacement(path, attempted, bind_attempted, done, result):
    from brindle.daemon import lifecycle

    lifecycle._is_daemon = lambda pid: False
    bind = lifecycle.bind_unix

    def track_bind(sock_path):
        bind_attempted.set()
        return bind(sock_path)

    lifecycle.bind_unix = track_bind
    attempted.set()
    socks = []
    try:
        socks, info = lifecycle.prepare("replacement-token", sock_path=Path(path))
        result.put(("started", info))
    except Exception as exc:
        result.put(("error", type(exc).__name__, str(exc)))
    finally:
        for sock in socks:
            sock.close()
        done.set()

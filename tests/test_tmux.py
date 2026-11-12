"""A tmux server whose working directory was deleted (and made again at the
same path) starts every new window in the dead copy, whatever ``-c`` says
(tmux 3.8), and Claude Code exits at once there. brindle's panes change
directory themselves, so an agent still runs in its worktree on that server;
`brindle doctor` says the server should be restarted."""

import os
import shlex
import shutil
from types import SimpleNamespace

import pytest

from brindle import doctor, tmux


def test_dead_pane_exits_are_read_as_tmux_prints_them(monkeypatch):
    """#{pane_dead_status} is only set for a program that exited, and
    #{pane_dead_signal} only for one a signal killed: tmux's sig2name prints
    sys_signame's "term" on macOS, the number where there are no names."""
    out = "\n".join([
        "@1\t%1\t1\t143\t\t\t",      # a wrapper shell that exited 128 + SIGTERM
        "@2\t%2\t1\t\tterm\t\t",     # the CLI itself, killed: macOS
        "@3\t%3\t1\t\t15\t\t",       # the same on Linux
        "@4\t%4\t1\t\tzzz\t\t",      # a name Python doesn't know
        "@5\t%5\t0\t\t\tabc\t\t1700000000",      # alive, an agent's, its window's last output
    ]) + "\n"
    monkeypatch.setattr(tmux, "_tmux", lambda *a, **k: SimpleNamespace(returncode=0, stdout=out))
    panes = tmux.list_panes()
    assert panes.exits == {"%1": (143, None), "%2": (None, 15), "%3": (None, 15),
                           "%4": (None, "zzz")}
    assert panes["%5"] and not panes["%1"] and panes.tags == {"%5": {tmux.AGENT_TAG: "abc"}}
    assert panes.activity == {"%5": 1700000000.0}


def test_a_pane_runs_in_its_worktree_on_a_server_whose_directory_was_deleted(tmp_path, monkeypatch):
    doomed, worktree = tmp_path / "started-here", tmp_path / "worktree"
    doomed.mkdir()
    worktree.mkdir()
    # The person's server, started from a directory that is then replaced.
    tmux.kill_server()
    monkeypatch.chdir(doomed)
    tmux._tmux("-f", os.devnull, "new-session", "-d", "-s", "persons", "/bin/sh")
    monkeypatch.chdir(tmp_path)
    shutil.rmtree(doomed)
    doomed.mkdir()

    tmux.ensure_session("brindle_dead_cwd", str(worktree), {})
    out, t = tmp_path / "pane.cwd", shutil.which(tmux._bin())
    tmux.new_window("brindle_dead_cwd", "w", str(worktree),
                    ["/bin/sh", "-c", f'/bin/pwd -P > "$0"; {t} wait-for -S dead-cwd', str(out)], {})
    tmux._tmux("wait-for", "dead-cwd")
    assert out.read_text().strip() == str(worktree.resolve())
    assert tmux.has_session("persons")
    [check] = doctor.tmux_cwd_checks()
    assert check.level == doctor.WARN


def test_sidebar_stays_pinned_on_a_tmux_without_window_resized(monkeypatch):
    """tmux 3.2 (Ubuntu 22.04's) has no window-resized hook and rejects it, so
    the sidebar grew with every window resize. There the pin goes on
    window-layout-changed, and follows the sidebar to another window."""
    hooks: dict[tuple[str, str], str] = {}      # (window, hook) -> its command, as tmux 3.2 keeps them
    window = ["@1"]                             # where the sidebar pane %9 is

    def tmux_3_2(*args, **kwargs):
        if "window-resized" in args:
            return SimpleNamespace(returncode=1, stdout="", stderr="invalid option: window-resized")
        if args[0] == "join-pane":
            window[0] = args[-1]
        elif args[0] == "set-hook" and "-u" in args:
            hooks.pop((args[3], args[-1]), None)
        elif args[0] == "set-hook":
            hooks[(args[3], args[4])] = args[5]
        out = {"split-window": "%9\n", "display-message": window[0]}.get(args[0], "")
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(tmux, "_tmux", tmux_3_2)
    assert tmux.split_left("@1", "/", ["cat"], {}) == "%9"
    [(pinned, command)] = hooks.items()
    assert pinned == ("@1", "window-layout-changed") and "resize-pane -t %9 -x 30" in command
    # Zooming changes the layout too, and resize-pane would undo the zoom that hides the sidebar.
    assert "window_zoomed_flag" in command
    tmux.move_pane("%9", "@2")
    assert hooks == {("@2", "window-layout-changed"): command}


@pytest.mark.variant("real incident 2026-11-12: the hook script's shell behaviour, which Python coverage can't see")
def test_sidebar_hook_stays_silent_when_its_respawn_fails(tmp_path, monkeypatch):
    """A failed restart (the pane already gone) once exited 1, and tmux showed
    "'...' returned 1" over whatever pane the person was looking at."""
    from conftest import sh

    fake = tmp_path / "tmux"
    fake.write_text('#!/bin/sh\ncase "$*" in *respawn-pane*) echo "no pane" >&2; exit 1;; esac\n')
    fake.chmod(0o755)
    monkeypatch.setenv("BRINDLE_TMUX_BIN", str(fake))
    log = tmp_path / "sidebar.log"
    script = tmux.sidebar_respawn_script("%8", str(tmp_path), {}, log)
    assert sh(f"/bin/sh -c {shlex.quote(script)} brindle-sidebar 143", tmp_path) == ""
    assert "no pane" in log.read_text() and "restarting it failed" in log.read_text()

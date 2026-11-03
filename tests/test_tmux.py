"""A tmux server whose working directory was deleted (and made again at the
same path) starts every new window in the dead copy, whatever ``-c`` says
(tmux 3.8), and Claude Code exits at once there. brindle's panes change
directory themselves, so an agent still runs in its worktree on that server;
`brindle doctor` says the server should be restarted."""

import os
import shutil

from brindle import doctor, tmux


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

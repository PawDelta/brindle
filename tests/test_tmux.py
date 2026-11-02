"""A tmux server whose working directory was deleted (and made again at the
same path) starts every new window in the dead copy, whatever ``-c`` says,
and Claude Code exits at once there. brindle moves to a fresh server of its
own, started in the home directory without the person's secrets, and leaves
the old one, with whatever runs on it, alone."""

import os
import shutil

from brindle import tmux


def test_a_server_in_a_deleted_directory_is_left_for_a_fresh_one(tmp_path, monkeypatch):
    doomed, worktree = tmp_path / "started-here", tmp_path / "worktree"
    doomed.mkdir()
    worktree.mkdir()
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    old = tmux.current_server()
    # The person's server, started from their shell in a directory that is then replaced.
    tmux.kill_server()
    monkeypatch.chdir(doomed)
    tmux._tmux("-f", os.devnull, "new-session", "-d", "-s", "persons", "/bin/sh")
    monkeypatch.chdir(tmp_path)
    shutil.rmtree(doomed)
    doomed.mkdir()
    try:
        tmux.ensure_session("brindle_fresh", str(worktree), {})
        out, t = tmp_path / "pane", shutil.which(tmux._bin())
        tmux.new_window("brindle_fresh", "w", str(worktree),
                        ["/bin/sh", "-c", f'/bin/pwd -P > "$0.cwd"; env > "$0.env"; '
                                          f'{t} show-environment -g > "$0.global"; {t} wait-for -S fresh', str(out)],
                        {"BRINDLE_AGENT_ID": "a1"})
        tmux._tmux("wait-for", "fresh")
        assert (tmp_path / "pane.cwd").read_text().strip() == str(worktree.resolve())
        assert tmux.current_server() != old
        assert "SLACK_BOT_TOKEN" not in (tmp_path / "pane.env").read_text()
        assert "SLACK_BOT_TOKEN" not in (tmp_path / "pane.global").read_text()
        # The old server keeps running, untouched.
        assert tmux._tmux("has-session", "-t", "=persons", check=False, server=old).returncode == 0
        assert "SLACK_BOT_TOKEN=xoxb-secret" in tmux._tmux("show-environment", "-g", server=old).stdout
    finally:
        if tmux.current_server() != old:
            tmux.reap_server(tmux.current_server())

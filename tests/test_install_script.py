"""scripts/install.sh on a first run: the PATH note is about the user's own
PATH (not the one the script extends), and the Claude Code hint leads with
the official installer. curl, uv and brindle are stubs."""

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


def _exe(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def box(tmp_path):
    """A home with no ~/.local/bin, and a PATH with stub curl and tmux but no uv."""
    home, stubs = tmp_path / "home", tmp_path / "stubs"
    home.mkdir()
    stubs.mkdir()
    # uv's installer: puts uv and its env file in ~/.local/bin.
    uv = ('case "$1 $2" in\n'
          '  "tool dir") echo "$HOME/.local/bin" ;;\n'
          '  "tool install") printf \'#!/bin/sh\\necho "brindle 0.5.5"\\n\' > "$HOME/.local/bin/brindle";'
          ' chmod +x "$HOME/.local/bin/brindle" ;;\n'
          "esac\n")
    _exe(stubs / "curl", 'mkdir -p "$HOME/.local/bin"\n'
         f"cat > \"$HOME/.local/bin/uv\" <<'EOF'\n#!/bin/sh\n{uv}EOF\n"
         'chmod +x "$HOME/.local/bin/uv"; touch "$HOME/.local/bin/env"\necho "exit 0"\n')
    _exe(stubs / "tmux", "exit 0\n")
    for tool in ("sh", "uname", "mkdir", "chmod", "touch", "cut", "cat", "printf"):
        real = subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True, text=True).stdout.strip()
        if real and not (stubs / tool).exists() and "/" in real:
            os.symlink(real, stubs / tool)
    return home, stubs


def run(home, path):
    return subprocess.run(["sh", str(SCRIPT)], env={"HOME": str(home), "PATH": path},
                          capture_output=True, text=True, timeout=60)


def test_a_fresh_install_ends_by_saying_how_to_get_brindle_on_path(box):
    home, stubs = box
    res = run(home, str(stubs))
    assert res.returncode == 0, res.stderr
    lines = res.stdout.strip().splitlines()
    assert lines[-1] == "Open a new terminal, or run: source ~/.local/bin/env"
    assert "uv added a line to ~/.bashrc, ~/.profile and ~/.zshrc" in res.stdout


def test_no_path_note_when_the_bin_dir_was_already_on_path(box):
    home, stubs = box
    res = run(home, f"{home}/.local/bin:{stubs}")
    assert res.returncode == 0, res.stderr
    assert "isn't on" not in res.stdout and "source ~/.local/bin/env" not in res.stdout


def test_claude_code_hint_leads_with_the_official_installer(box):
    home, stubs = box
    out = run(home, str(stubs)).stdout
    curl = out.index("curl -fsSL https://claude.ai/install.sh | bash")
    assert curl < out.index("npm install -g @anthropic-ai/claude-code")


def test_the_pin_example_names_no_version_that_goes_stale():
    import re

    assert re.search(r"BRINDLE_VERSION=<version>", SCRIPT.read_text())
    assert not re.search(r"BRINDLE_VERSION=\d", SCRIPT.read_text())

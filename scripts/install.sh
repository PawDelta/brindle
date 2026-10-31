#!/bin/sh
# Install brindle: curl -fsSL pawdelta.com/brindle/install | sh
#
# Installs uv (if missing) and tmux (with Homebrew on macOS; prints the
# command elsewhere), then brindle itself with `uv tool install`, then runs
# `brindle doctor`. Safe to run again: it upgrades brindle in place.
# BRINDLE_VERSION=<version> pins a released version (https://pypi.org/project/brindle/#history).
set -eu

# The PATH the user's shell has, before this script adds to its own copy:
# the "not on your PATH" note at the end is about this one.
user_path="$PATH"
installed_uv=no

say() { printf '%s\n' "$*"; }
step() { printf '\033[32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

case "$(uname -s)" in
  Darwin) os=mac ;;
  Linux) os=linux ;;
  *) say "brindle runs on macOS and Linux (on Windows, use WSL)."; exit 1 ;;
esac

if ! have uv; then
  step "installing uv (Python tool installer, https://docs.astral.sh/uv)"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  installed_uv=yes
  PATH="$HOME/.local/bin:$PATH"
  export PATH
fi

if ! have tmux; then
  if [ "$os" = mac ] && have brew; then
    step "installing tmux"
    brew install tmux
  elif have apt-get; then
    warn "tmux is missing. Install it with: sudo apt-get install -y tmux"
  elif have dnf; then
    warn "tmux is missing. Install it with: sudo dnf install -y tmux"
  else
    warn "tmux is missing: brindle runs every agent in a tmux window. Install it with your package manager."
  fi
fi

spec="brindle"
[ -n "${BRINDLE_VERSION:-}" ] && spec="brindle==$BRINDLE_VERSION"
# BRINDLE_SOURCE=/path/to/checkout installs that instead of PyPI (scripts/smoke_install.sh).
[ -n "${BRINDLE_SOURCE:-}" ] && spec="$BRINDLE_SOURCE"
step "installing $spec"
uv tool install --upgrade --python '>=3.11' "$spec"

bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) PATH="$bin_dir:$PATH"; export PATH ;;
esac
on_user_path=yes
case ":$user_path:" in
  *":$bin_dir:"*) ;;
  *) on_user_path=no ;;
esac

if ! have claude; then
  warn "Claude Code isn't installed; the supervisor runs on it by default (or pass --supervisor codex / agy):"
  say "    curl -fsSL https://claude.ai/install.sh | bash"
  say "  (or: npm install -g @anthropic-ai/claude-code)"
fi

say ""
brindle doctor || true
say ""
step "brindle $(brindle --version 2>/dev/null | cut -d' ' -f2) is installed. Next:"
say "    cd your/repo"
say "    brindle init     # detects your setup and test commands"
say "    brindle          # opens the supervisor; tell it what to build"

if [ "$on_user_path" = no ]; then
  say ""
  if [ "$bin_dir" = "$HOME/.local/bin" ] && [ -f "$HOME/.local/bin/env" ]; then
    warn "$bin_dir isn't on this terminal's PATH yet, so \`brindle\` isn't found here."
    [ "$installed_uv" = yes ] &&
      say "  uv added a line to ~/.bashrc, ~/.profile and ~/.zshrc that puts it there in new terminals."
    say "Open a new terminal, or run: source ~/.local/bin/env"
  else
    warn "$bin_dir isn't on your PATH yet, so \`brindle\` isn't found here."
    say "Run \`uv tool update-shell\`, then open a new terminal."
  fi
fi

#!/bin/sh
# Install brindle: curl -fsSL pawdelta.com/brindle/install | sh
#
# Installs uv (if missing) and tmux (with Homebrew on macOS; prints the
# command elsewhere), then brindle itself with `uv tool install`, then runs
# `brindle doctor`. Safe to run again: it upgrades brindle in place.
# BRINDLE_VERSION=0.14.3 pins a version.
set -eu

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
step "installing $spec"
uv tool install --upgrade --python '>=3.11' "$spec"

bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) PATH="$bin_dir:$PATH"; export PATH
     warn "$bin_dir isn't on your PATH yet. Run \`uv tool update-shell\` and open a new terminal." ;;
esac

if ! have claude; then
  warn "Claude Code isn't installed; the supervisor runs on it: npm install -g @anthropic-ai/claude-code"
fi

say ""
brindle doctor || true
say ""
step "brindle $(brindle --version 2>/dev/null | cut -d' ' -f2) is installed. Next:"
say "    cd your/repo"
say "    brindle init     # detects your setup and test commands"
say "    brindle          # opens the supervisor; tell it what to build"

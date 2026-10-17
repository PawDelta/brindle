#!/usr/bin/env bash
# smoke_install.sh: a first-run check on a clean Linux box.
#
# In a fresh ubuntu:24.04 container with no agent CLIs installed or signed in,
# runs scripts/install.sh against this checkout (BRINDLE_SOURCE, not PyPI; uv is
# installed by the script, tmux by apt), then `brindle --version` and
# `brindle doctor`. Fails if a command prints a Python traceback, or if doctor
# crashes (exit status above 1) instead of reporting what is missing.
#
#   scripts/smoke_install.sh
set -uo pipefail

ROOT="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)" || exit 2
docker info >/dev/null 2>&1 || { echo "Docker isn't running" >&2; exit 2; }

IMAGE=brindle-smoke-install
docker build -q -t "$IMAGE" - >/dev/null <<'EOF' || { echo "building the image failed" >&2; exit 1; }
FROM ubuntu:24.04
RUN apt-get update -q && apt-get install -yq --no-install-recommends curl ca-certificates tmux git \
    && rm -rf /var/lib/apt/lists/* && useradd --create-home smoke
USER smoke
WORKDIR /home/smoke
EOF

# The tracked files of the checkout are streamed in as a tar (a worktree's .git
# points outside any mount), so the build can't write into the checkout and
# untracked junk can't hide a problem.
LOG="$(mktemp "${TMPDIR:-/tmp}/brindle-smoke.XXXXXX")" || exit 1
git -C "$ROOT" ls-files -z | COPYFILE_DISABLE=1 tar -C "$ROOT" --no-xattrs --null -T - -cf - | docker run --rm -i "$IMAGE" bash -c '
  set -u
  mkdir src && tar -x -C src || exit 90
  BRINDLE_SOURCE=/home/smoke/src sh src/scripts/install.sh || { echo "SMOKE: install.sh failed"; exit 91; }
  export PATH="$HOME/.local/bin:$PATH"
  echo "== brindle --version"
  brindle --version || { echo "SMOKE: --version failed"; exit 92; }
  echo "== brindle doctor"
  brindle doctor; rc=$?
  echo "SMOKE: doctor exit $rc"
  [ "$rc" -le 1 ] || exit 93
' 2>&1 | tee "$LOG"
status=${PIPESTATUS[2]}   # docker run: after git ls-files and tar

if grep -q "Traceback (most recent call last)" "$LOG"; then
  echo "smoke_install: FAIL: a command printed a Python traceback" >&2
  exit 1
fi
if [ "$status" -ne 0 ]; then
  echo "smoke_install: FAIL (container exit $status; log: $LOG)" >&2
  exit 1
fi
rm -f "$LOG"
echo "smoke_install: ok"

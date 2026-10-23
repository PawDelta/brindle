#!/usr/bin/env bash
# retry.sh — run a command until it succeeds, a few times at most, pausing
# longer after each failure. For the release workflow's steps that go over
# the network (rustup, npm ci, apt-get, a build that downloads its tools or
# talks to Apple): a dropped connection fails one try, not the release.
# A command that fails every try fails the same way the last try did.
#
# Usage: bash app/scripts/retry.sh [-n TRIES] [-d PAUSE] COMMAND [ARG...]
#   -n TRIES  tries in all (default 3)
#   -d PAUSE  seconds after the first failure, then 2x, 3x... (default 10)
set -uo pipefail

tries=3 pause=10
while [ $# -gt 0 ]; do
  case "$1" in
    -n) tries="$2"; shift 2 ;;
    -d) pause="$2"; shift 2 ;;
    --) shift; break ;;
    *) break ;;
  esac
done
[ $# -gt 0 ] || { echo "usage: retry.sh [-n TRIES] [-d PAUSE] COMMAND [ARG...]" >&2; exit 2; }

for attempt in $(seq 1 "$tries"); do
  "$@"
  status=$?
  [ "$status" != 0 ] || exit 0
  if [ "$attempt" -lt "$tries" ]; then
    echo "::warning::$1 failed (exit $status, try $attempt of $tries): trying again in $((pause * attempt))s"
    sleep $((pause * attempt))
  fi
done
echo "::error::$1 failed $tries times (last exit $status)"
exit "$status"

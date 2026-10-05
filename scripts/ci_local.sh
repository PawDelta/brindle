#!/usr/bin/env bash
# ci_local.sh: run the GitHub CI matrix on this machine, for free.
#
# Each leg is what .github/workflows/ci.yml runs: `uv sync && uv run pytest -q`,
# on a clean export of the commit (uncommitted edits can't make it pass).
#
#   scripts/ci_local.sh            host legs (3.11, 3.12, 3.13) + Linux 3.12 in Docker
#   scripts/ci_local.sh --quick    host, the project's default Python only
#   scripts/ci_local.sh --full     host legs + Linux 3.11, 3.12, 3.13
#   scripts/ci_local.sh --report   also post a `local-ci` commit status to GitHub
#                                  (gh api; costs no Actions minutes)
#   --rev REV                      test REV instead of HEAD
#   --jobs N                       legs at once (default 2: a full suite is heavy)
#   --force                        test even a docs-only change
#
# A change that only touches docs (*.md, docs/, assets/, LICENSE, CLA.md),
# compared with where it branched from origin/main, isn't tested: like the
# GitHub workflow's paths-ignore. With --report it still gets a green status.
#
# Logs go to a fresh private directory, printed at the start. Exit 0 only if every leg passed.
set -uo pipefail

ROOT="$(git rev-parse --show-toplevel 2>/dev/null || git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
MODE=default REPORT=0 REV=HEAD JOBS=2 FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --quick) MODE=quick ;;
    --full) MODE=full ;;
    --report) REPORT=1 ;;
    --rev) REV="$2"; shift ;;
    --jobs) JOBS="$2"; shift ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

SHA="$(git -C "$ROOT" rev-parse --verify "$REV^{commit}")" || exit 2
SHORT="${SHA:0:12}"

post_status() {   # post_status STATE DESCRIPTION: the `local-ci` commit status, with --report
  [ "$REPORT" = 1 ] || return 0
  local repo
  repo="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)"
  if [ -n "$repo" ] && gh api -X POST "repos/$repo/statuses/$SHA" -f state="$1" \
       -f context=local-ci -f description="${2:0:140}" >/dev/null 2>&1; then
    echo "==> posted local-ci=$1 on $repo@$SHORT"
  else
    echo "==> couldn't post the local-ci status (is the commit pushed?)" >&2
  fi
}

# Docs-only changes aren't tested. The change is everything since the commit
# branched from origin/main (or, for a commit on main, the commit itself).
is_doc() { case "$1" in *.md|docs/*|assets/*|LICENSE|CLA.md) return 0 ;; *) return 1 ;; esac; }
if [ "$FORCE" = 0 ]; then
  base="$(git -C "$ROOT" merge-base "$SHA" origin/main 2>/dev/null || true)"
  [ -z "$base" ] || [ "$base" = "$SHA" ] && base="$(git -C "$ROOT" rev-parse --verify -q "$SHA^" || true)"
  if [ -n "$base" ]; then
    changed="$(git -C "$ROOT" diff --name-only "$base" "$SHA")"
    code=""
    while IFS= read -r f; do [ -n "$f" ] && ! is_doc "$f" && code="$f" && break; done <<< "$changed"
    if [ -n "$changed" ] && [ -z "$code" ]; then
      echo "==> $SHORT changes only docs: nothing to test (--force to test anyway)"
      post_status success "docs only: not tested"
      exit 0
    fi
  fi
fi
# A fresh private directory per run (mktemp: unpredictable name, mode 0700),
# never a fixed path someone else could have planted a symlink at.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/copse-ci-local-$SHORT.XXXXXX")" || exit 1
mkdir -p "$WORK/src" "$WORK/logs"
echo "==> work and logs: $WORK"
git -C "$ROOT" archive "$SHA" | tar -x -C "$WORK/src"
# A one-commit repo, like a CI checkout: some tests need the project to be one.
(cd "$WORK/src" && git init -q && git add -A && git -c user.name=ci -c user.email=ci@localhost \
   commit -qm "local ci $SHORT") || exit 1

case "$MODE" in
  quick) LEGS=("host:default") ;;
  full) LEGS=("host:3.11" "host:3.12" "host:3.13" "linux:3.11" "linux:3.12" "linux:3.13") ;;
  *) LEGS=("host:3.11" "host:3.12" "host:3.13" "linux:3.12") ;;
esac

if printf '%s\n' "${LEGS[@]}" | grep -q '^linux:'; then
  if ! docker info >/dev/null 2>&1; then
    echo "Docker isn't running: skipping the Linux legs" >&2
    LEGS=($(printf '%s\n' "${LEGS[@]}" | grep -v '^linux:'))
  else
    docker build -q -t copse-ci-linux -f "$ROOT/scripts/ci_local.Dockerfile" "$ROOT/scripts" >/dev/null \
      || { echo "building the Linux CI image failed" >&2; exit 1; }
  fi
fi

leg() {   # leg host:3.12 | linux:3.12 -> runs the suite, exit status is the result
  local where="${1%%:*}" py="${1#*:}" dir="$WORK/${1/:/-}" log="$WORK/logs/${1/:/-}.log"
  cp -R "$WORK/src" "$dir"
  if [ "$where" = host ]; then
    local pyarg=(); [ "$py" != default ] && pyarg=(--python "$py")
    # The tests give tmux sockets of their own; TMUX= keeps them out of the caller's session.
    (cd "$dir" && export UV_PROJECT_ENVIRONMENT="$dir/.venv" TMUX= \
       && uv sync -q ${pyarg[@]+"${pyarg[@]}"} && uv run -q ${pyarg[@]+"${pyarg[@]}"} pytest -q) >"$log" 2>&1
  else
    # The export is copied inside, so the runner-like user owns its checkout;
    # uv's cache (Pythons, wheels) persists in a volume between runs.
    docker run --rm -v "$dir:/src:ro" -v copse-ci-uv-cache:/home/ci/.cache/uv copse-ci-linux \
      sh -c "cp -R /src /home/ci/work && cd /home/ci/work && uv sync -q --python $py && uv run -q --python $py pytest -q" \
      >"$log" 2>&1
  fi
}

echo "==> local CI for $SHORT: ${LEGS[*]} ($JOBS at a time)"
pids=() names=() failed=()
for l in "${LEGS[@]}"; do
  # macOS ships bash 3.2 (no `wait -n`): poll the running jobs instead.
  while [ "$(jobs -rp | wc -l | tr -d ' ')" -ge "$JOBS" ]; do sleep 2; done
  leg "$l" & pids+=($!) names+=("$l")
done
for i in "${!pids[@]}"; do
  if wait "${pids[$i]}"; then
    echo "  ✓ ${names[$i]}  $(tail -1 "$WORK/logs/${names[$i]/:/-}.log")"
  else
    echo "  ✗ ${names[$i]}  (log: $WORK/logs/${names[$i]/:/-}.log)"
    failed+=("${names[$i]}")
  fi
done

STATE=success; DESC="${#LEGS[@]} legs passed: ${LEGS[*]}"
[ ${#failed[@]} -gt 0 ] && STATE=failure DESC="failed: ${failed[*]}"
post_status "$STATE" "$DESC"
echo "==> $STATE"
[ "$STATE" = success ]

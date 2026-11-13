#!/usr/bin/env bash
# Check that the large task kit is sound. Exits 0 only if all of these hold:
#
#   1. the repo clones at the pinned commit and its setup commands succeed;
#   2. every hidden test fails on the base commit;
#   3. reference.patch applies cleanly;
#   4. every hidden test passes with the reference patch applied;
#   5. the repo's own test suite (test_cmd) still passes.
#
# Needs git, python3 and whatever repo.json's setup uses (uv). Works in a fresh
# temporary directory that is removed on exit (KEEP_WORKDIR=1 keeps it).
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
work="$(mktemp -d)"
if [ "${KEEP_WORKDIR:-0}" = "1" ]; then
    echo "work dir: $work" >&2
else
    trap 'rm -rf "$work"' EXIT
fi
repo="$work/repo"

step() { printf '\n== %s\n' "$*" >&2; }
fail() { printf '\nvalidate: FAIL: %s\n' "$*" >&2; exit 1; }

# Read one field of repo.json; for "setup", one command per line.
field() {
    python3 - "$here/repo.json" "$1" <<'PY'
import json, sys
value = json.load(open(sys.argv[1]))[sys.argv[2]]
print("\n".join(value) if isinstance(value, list) else value)
PY
}

# Read "passed"/"failed"/"total" out of grade.sh's JSON.
count() {
    python3 -c 'import json, sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$1" "$2"
}

url="$(field url)"
commit="$(field commit)"
test_cmd="$(field test_cmd)"

step "clone $url at $commit"
git init -q "$repo"
git -C "$repo" remote add origin "$url"
git -C "$repo" fetch -q --depth 1 origin "$commit"
git -C "$repo" checkout -q --detach FETCH_HEAD
[ "$(git -C "$repo" rev-parse HEAD)" = "$commit" ] || fail "checked out the wrong commit"

step "setup"
while IFS= read -r cmd; do
    [ -n "$cmd" ] || continue
    echo "\$ $cmd" >&2
    (cd "$repo" && bash -c "$cmd") || fail "setup command failed: $cmd"
done < <(field setup)

step "hidden tests on the base commit (must all fail)"
base="$(GRADE_PYTEST_ARGS="--tb=no -rN" "$here/grade.sh" "$repo")"
echo "$base"
[ "$(count "$base" total)" -gt 0 ] || fail "no hidden tests found"
[ "$(count "$base" passed)" -eq 0 ] || fail "some hidden tests pass on the base commit"
rm -rf "$repo/bench_hidden_tests"

step "apply reference.patch"
git -C "$repo" apply --check "$here/reference.patch" || fail "reference.patch does not apply"
git -C "$repo" apply "$here/reference.patch"

step "hidden tests with the reference patch (must all pass)"
ref="$("$here/grade.sh" "$repo")"
echo "$ref"
[ "$(count "$ref" failed)" -eq 0 ] || fail "hidden tests fail with the reference patch"
[ "$(count "$ref" passed)" -eq "$(count "$ref" total)" ] || fail "pass count mismatch"
rm -rf "$repo/bench_hidden_tests"

step "repo test suite with the reference patch: $test_cmd"
(cd "$repo" && bash -c "$test_cmd" >&2) || fail "the repo's own test suite fails"

printf '\nvalidate: OK\n' >&2

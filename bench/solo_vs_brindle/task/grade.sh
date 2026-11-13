#!/usr/bin/env bash
# Grade a checkout of the task repo against the hidden acceptance tests.
#
#   grade.sh <checkout_dir>
#
# Copies hidden_tests/ into <checkout_dir>/bench_hidden_tests, runs them with
# the checkout's own interpreter (<checkout_dir>/.venv/bin/python, as created
# by repo.json's setup; override with GRADE_PYTHON), and prints one JSON object
# to stdout:
#
#   {"passed": N, "failed": M, "total": T,
#    "per_part": {"part1_find": {"passed": .., "failed": .., "total": ..}, ...}}
#
# pytest's own output goes to stderr. A test that errors, is skipped, or never
# runs (for example because its module fails to import) counts as failed.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ $# -ne 1 ] || [ ! -d "$1" ]; then
    echo "usage: grade.sh <checkout_dir>" >&2
    exit 2
fi
checkout="$(cd "$1" && pwd)"
python="${GRADE_PYTHON:-$checkout/.venv/bin/python}"

dest="$checkout/bench_hidden_tests"
rm -rf "$dest"
cp -R "$here/hidden_tests" "$dest"
find "$dest" -name '__pycache__' -prune -exec rm -rf {} +

report_dir="$(mktemp -d)"
trap 'rm -rf "$report_dir"' EXIT
report="$report_dir/junit.xml"

# -o addopts= drops the repo's coverage options; importlib mode keeps the
# hidden conftest from clashing with the repo's tests/conftest.py; a part
# whose module fails to import must not stop the other parts from running.
(cd "$checkout" && "$python" -m pytest bench_hidden_tests -q \
    -p no:cacheprovider -o addopts= --import-mode=importlib \
    --continue-on-collection-errors --junitxml="$report" 1>&2)

python3 "$here/summarize.py" "$dest" "$report"

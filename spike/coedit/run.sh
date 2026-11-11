#!/usr/bin/env bash
# T0 spike: Yjs (node) and pycrdt (python) against each other. Throwaway.
# Usage: bash spike/coedit/run.sh   (needs node, npm and uv)
set -u
cd "$(dirname "$0")"
PYCRDT=0.14.13
rm -rf out
mkdir -p out
npm install --no-audit --no-fund --silent

# Which platforms uv can resolve a pycrdt binary wheel for (nothing is installed)
echo "pycrdt==$PYCRDT" > out/requirements.in
: > out/uv_platforms.txt
for plat in aarch64-apple-darwin x86_64-apple-darwin x86_64-manylinux_2_35 aarch64-manylinux_2_35 x86_64-pc-windows-msvc; do
  for pyver in 3.11 3.12 3.13; do
    if uv pip compile out/requirements.in --python-platform "$plat" --python-version "$pyver" --only-binary :all: -q >/dev/null 2>out/uv_err.txt; then
      echo "$plat py$pyver ok" >> out/uv_platforms.txt
    else
      echo "$plat py$pyver FAILED" >> out/uv_platforms.txt
    fi
  done
done

node yjs_side.mjs 1
uv run --no-project --with "pycrdt==$PYCRDT" python py_side.py
node yjs_side.mjs 2
uv run --no-project --with "pycrdt==$PYCRDT" python py_side.py late
uv run --no-project python platforms.py
uv run --no-project python report.py

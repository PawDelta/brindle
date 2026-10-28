"""app/scripts/check_relocated_engine.sh, on fake engine directories."""
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "app" / "scripts" / "check_relocated_engine.sh"

# Stands in for the engine's Python: `-I -c ...` prints the hook command for
# wherever the engine sits now; anything else (-m brindle --version) prints a version.
PYTHON3 = """#!/bin/sh
here=$(cd "$(dirname "$0")/.." && pwd -P)
case "$*" in
  *hook_command*) echo "$here/bin/brindle _hook codex-permission-request" ;;
  *) echo "brindle 0.0.0" ;;
esac
"""
GOOD = "#!/bin/sh\necho 'brindle 0.0.0'\n"


def fake_engine(root, python_brindle):
    for name in ("bin", "python/bin"):
        (root / name).mkdir(parents=True)
    files = {"bin/brindle": GOOD, "python/bin/brindle": python_brindle, "python/bin/python3": PYTHON3}
    for name, text in files.items():
        (root / name).write_text(text)
        (root / name).chmod(0o755)
    return root


@pytest.mark.parametrize(
    "python_brindle, ok",
    [
        (GOOD, True),
        ("#!/Users/runner/work/brindle/engine-build/stage/python/bin/python3\nprint('x')\n", False),
    ],
)
def test_check_relocated_engine(tmp_path, python_brindle, ok):
    engine = fake_engine(tmp_path / "engine", python_brindle)
    done = subprocess.run(["bash", str(SCRIPT), str(engine)], capture_output=True, text=True)
    assert (done.returncode == 0) is ok, done.stderr
    if not ok:
        assert "outside the engine" in done.stderr

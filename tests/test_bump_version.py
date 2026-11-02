import importlib.util
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("bump_version", ROOT / "scripts" / "bump_version.py")
bump_version = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bump_version)


@pytest.fixture
def tree(tmp_path):
    """A copy of the real files a bump edits."""
    for name in bump_version.EDITS:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / name, tmp_path / name)
    return tmp_path


def test_bump_sets_every_file_and_dry_run_writes_nothing(tree):
    before = {n: (tree / n).read_text() for n in bump_version.EDITS}
    assert len(bump_version.bump("9.8.7", tree, dry_run=True)) == len(bump_version.EDITS)
    assert {n: (tree / n).read_text() for n in bump_version.EDITS} == before

    bump_version.bump("9.8.7", tree)
    assert set(bump_version.versions(tree).values()) == {"9.8.7"}
    assert (tree / "app/package-lock.json").read_text().count('"version": "9.8.7"') == 2
    with pytest.raises(SystemExit):
        bump_version.bump("nine", tree)


def test_check_fails_on_a_mismatch_and_passes_on_a_match(tree):
    bump_version.bump("1.2.3", tree)
    assert bump_version.check(tree) is None
    pkg = tree / "app/package.json"
    pkg.write_text(pkg.read_text().replace('"version": "1.2.3"', '"version": "1.2.4"'))
    assert "1.2.4" in bump_version.check(tree)
    assert "1.2.4" in bump_version.check(tree, allow_legacy=True)  # only the old pair is tolerated

    # The old separate numbers: strict check fails, CI's legacy allowance passes.
    py = tree / "pyproject.toml"
    py.write_text(py.read_text().replace('version = "1.2.3"', f'version = "{bump_version.LEGACY_PAIR["engine"]}"'))
    pkg.write_text(pkg.read_text().replace('"version": "1.2.4"', f'"version": "{bump_version.LEGACY_PAIR["app"]}"'))
    assert bump_version.check(tree) and bump_version.check(tree, allow_legacy=True) is None

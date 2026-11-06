"""Release build contracts for assets outside Python coverage."""
from pathlib import Path

import pytest
import yaml


@pytest.mark.variant("release workflow and shell build assets are outside Python coverage")
def test_intel_builds_on_apple_silicon():
    root = Path(__file__).resolve().parents[1]
    job = yaml.safe_load((root / ".github/workflows/app-release.yml").read_text())["jobs"]["macos"]
    matrix = job["strategy"]["matrix"]["include"]
    assert {row["target"] for row in matrix} == {"aarch64-apple-darwin", "x86_64-apple-darwin"}
    assert all(row["runner"] == "macos-latest" for row in matrix)
    steps = {step.get("id", step.get("name")): step for step in job["steps"]}
    assert "--install-rosetta" in steps["Runner architecture"]["run"]
    smoke = steps["Binary architectures"]["run"]
    assert "lipo -archs" in smoke
    for binary in ("Contents/MacOS/brindle-desktop", "engine/bin/tmux", "engine/python/bin/python3",
                   "Library/LoginItems/brindle.app/Contents/MacOS/brindle"):
        assert binary in smoke
    assert '"$expected"' in smoke
    assert steps["Staple the app"]["run"].endswith('--staple-only "$APP"')
    assert steps["Notarize and staple the DMG"]["run"].endswith('notarize_dmg.sh "$DMG"')
    assert steps["collect"]["env"]["NOTARIZED"] == "${{ steps.gatekeeper.outputs.verified == 'true' }}"
    gate = steps["gatekeeper"]["run"]
    assert 'xcrun stapler validate "$packaged/updater/brindle.app"' in gate
    assert 'xcrun stapler validate "$packaged/mount/brindle.app"' in gate
    assert steps["gatekeeper"]["env"]["UPDATER"] == "${{ steps.signing.outputs.updater }}"
    build = (root / "app/scripts/build_engine.sh").read_text()
    assert 'ARCH="${TAURI_ENV_TARGET_TRIPLE%-apple-darwin}"' in build
    assert 'export CC="clang -arch $MACH_ARCH"' in build
    assert build.count('--host="$CONFIGURE_HOST" --build="$CONFIGURE_BUILD"') == 2
    assert '-arch "$MACH_ARCH"' in build
    staple = (root / "app/scripts/notarize_dmg.sh").read_text()
    assert 'tries="${STAPLE_TRIES:-12}"' in staple
    assert 'xcrun stapler staple "$dmg" && xcrun stapler validate "$dmg"' in staple
    assert '"--staple-only"' in staple

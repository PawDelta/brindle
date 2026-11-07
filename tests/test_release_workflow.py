"""Release build contracts for assets outside Python coverage."""
import json
import plistlib
import re
import shlex
from pathlib import Path

import pytest
import yaml
from conftest import sh


@pytest.mark.variant("release approval dependencies are outside Python coverage")
@pytest.mark.parametrize("workflow,gate_condition", [
    ("app-release.yml", "github.event_name == 'push' && github.ref_type == 'tag'"),
    ("app-feed-rollback.yml", "github.ref_type == 'tag' && startsWith(github.ref_name, 'app-v')"),
])
def test_single_release_approval_precedes_credentials(workflow, gate_condition):
    root = Path(__file__).resolve().parents[1]
    jobs = yaml.safe_load((root / ".github/workflows" / workflow).read_text())["jobs"]
    approval_jobs = [name for name, job in jobs.items() if job.get("environment") == "release-gate"]
    assert approval_jobs == ["release-gate"]
    gate = jobs["release-gate"]
    assert gate["if"] == gate_condition
    assert "secrets." not in yaml.safe_dump(gate)

    def ancestors(name):
        needs = jobs[name].get("needs", [])
        needs = [needs] if isinstance(needs, str) else needs
        return set(needs).union(*(ancestors(parent) for parent in needs))

    for name, job in jobs.items():
        environment = job.get("environment", "")
        if "desktop-release" in environment:
            assert "release-gate" in ancestors(name), name
            assert "needs.release-gate.result == 'success'" in job["if"], name
        if "secrets." in yaml.safe_dump(job):
            assert "desktop-release" in environment or environment == "desktop-staff-release", name


@pytest.mark.variant("release workflow and shell build assets are outside Python coverage")
def test_intel_builds_on_apple_silicon():
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/app-release.yml").read_text())
    job = workflow["jobs"]["macos"]
    matrix = job["strategy"]["matrix"]["arch"]
    assert matrix == "${{ fromJSON(needs.staff-gate.outputs.go == 'true' && '[\"aarch64\"]' || '[\"aarch64\",\"x86_64\"]') }}"
    assert 'TARGET: ${{ matrix.arch }}-apple-darwin' in yaml.safe_dump(job)
    publish = workflow["jobs"]["macos-publish"]
    combine = next(step for step in publish["steps"] if step.get("name") == "Combine (latest.json)")["run"]
    assert 'arches=(aarch64)' in combine and 'if $include_x64 == "2"' in combine
    release_publish = (root / "app/scripts/release_publish.mjs").read_text()
    assert 'for (const arch of ["aarch64"])' in release_publish
    steps = {step.get("id", step.get("name")): step for step in job["steps"]}
    assert "--install-rosetta" in steps["Runner architecture"]["run"]
    smoke = steps["Binary architectures"]["run"]
    assert "lipo -archs" in smoke
    for binary in ("Contents/MacOS/brindle-desktop", "engine/bin/tmux", "engine/python/bin/python3",
                   "Library/LoginItems/brindle.app/Contents/MacOS/brindle"):
        assert binary in smoke
    assert '"$expected"' in smoke
    assert steps["Staple the app"]["run"].endswith('--staple-only "$APP"')
    names = [step.get("name") for step in job["steps"]]
    assert names.index("Staple the app") < names.index("Package the stapled app") < names.index("Notarize and staple the DMG")
    assert steps["Package the stapled app"]["run"] == "bash app/scripts/package_macos.sh"
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
    # configure's triples use autoconf's name: tmux 3.8's config.sub rejects arm64.
    triple = re.search(r"^gnu_triple\(\) \{.*\}$", build, re.M).group(0)
    for machine, want in (("arm64", "aarch64"), ("aarch64", "aarch64"), ("x86_64", "x86_64")):
        out = sh(f"bash -c {shlex.quote(f'{triple}; gnu_triple {machine}')}", root)
        assert out == f"{want}-apple-darwin"
    staple =(root / "app/scripts/notarize_dmg.sh").read_text()
    assert 'tries="${STAPLE_TRIES:-12}"' in staple
    assert 'xcrun stapler staple "$dmg" && xcrun stapler validate "$dmg"' in staple
    assert '"--staple-only"' in staple

    # The helper whose architecture we check must also retain its bundle identity
    # and privacy grants across app updates.
    app_root = root / "app"
    info = plistlib.loads((app_root / "scripts/engine/helper/Info.plist").read_bytes())
    app = json.loads((app_root / "src-tauri/tauri.conf.json").read_text())
    mac = json.loads((app_root / "src-tauri/tauri.macos.conf.json").read_text())["bundle"]["macOS"]
    usage = [f"NS{what}UsageDescription" for what in (
        "DesktopFolder", "DocumentsFolder", "DownloadsFolder", "RemovableVolumes", "NetworkVolumes",
        "FileProviderDomain", "LocalNetwork", "AppleEvents", "Microphone")]
    assert info["CFBundlePackageType"] == "APPL" and info["LSBackgroundOnly"] is True
    assert info["CFBundleIdentifier"].startswith(app["identifier"] + ".")
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == app["productName"]
    assert info["LSMinimumSystemVersion"] == mac["minimumSystemVersion"]
    assert [key for key in usage if not info.get(key, "").strip()] == []
    assert set(usage) <= set(plistlib.loads((app_root / "src-tauri/Info.plist").read_bytes()))
    signed = [app_root / "scripts/engine/helper/helper.entitlements", app_root / "src-tauri" / mac["entitlements"]]
    for entitlements in map(plistlib.loads, (path.read_bytes() for path in signed)):
        assert entitlements["com.apple.security.automation.apple-events"] is True
        assert entitlements["com.apple.security.device.audio-input"] is True
    place = f"Library/LoginItems/{info['CFBundleName']}.app"
    assert place in mac["files"]
    executable = f"{place}/Contents/MacOS/{info['CFBundleExecutable']}"
    assert executable in (app_root / "src-tauri/src/engine.rs").read_text()
    assert mac["files"][place] in build and f"Contents/MacOS/{info['CFBundleExecutable']}" in build

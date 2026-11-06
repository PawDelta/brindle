"""The identity macOS shows for brindle's engine: the helper app the daemon's
launchd job runs (app/scripts/engine/helper, built by build_engine.sh), so the
engine, tmux and every agent's pane ask as "brindle" with a reason, not as a
bare python3 (docs/investigations/macos-prompts.md)."""
import json
import plistlib
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"
# What an agent's work can make macOS ask for on the engine's behalf.
USAGE = [f"NS{what}UsageDescription" for what in (
    "DesktopFolder", "DocumentsFolder", "DownloadsFolder", "RemovableVolumes", "NetworkVolumes",
    "FileProviderDomain", "LocalNetwork", "AppleEvents", "Microphone")]


@pytest.mark.variant("macOS bundle identity and privacy declarations are plist/build assets outside Python coverage")
def test_engine_helper_declares_usage_strings():
    info = plistlib.loads((APP / "scripts/engine/helper/Info.plist").read_bytes())
    app = json.loads((APP / "src-tauri/tauri.conf.json").read_text())
    mac = json.loads((APP / "src-tauri/tauri.macos.conf.json").read_text())["bundle"]["macOS"]

    # An app of its own to macOS (privacy grants are kept per bundle id), under the app's name.
    assert info["CFBundlePackageType"] == "APPL" and info["LSBackgroundOnly"] is True
    assert info["CFBundleIdentifier"].startswith(app["identifier"] + ".")
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == app["productName"]
    assert info["LSMinimumSystemVersion"] == mac["minimumSystemVersion"]
    assert [key for key in USAGE if not info.get(key, "").strip()] == []
    # With launchd's job switched off the daemon is the app's child: the app is asked for.
    assert set(USAGE) <= set(plistlib.loads((APP / "src-tauri/Info.plist").read_bytes()))
    # Under the hardened runtime a usage string isn't enough for these two: what is asked
    # for must also be signed with the resource's entitlement, the helper and the app alike.
    signed = [APP / "scripts/engine/helper/helper.entitlements", APP / "src-tauri" / mac["entitlements"]]
    for entitlements in map(plistlib.loads, (path.read_bytes() for path in signed)):
        assert entitlements["com.apple.security.automation.apple-events"] is True
        assert entitlements["com.apple.security.device.audio-input"] is True

    # Bundled where the pieces that are compiled apart expect it, in a folder named as
    # the bundle is: macOS shows a bundle's own name only when its folder's matches.
    place = f"Library/LoginItems/{info['CFBundleName']}.app"
    assert place in mac["files"]
    executable = f"{place}/Contents/MacOS/{info['CFBundleExecutable']}"
    assert executable in (APP / "src-tauri/src/engine.rs").read_text()
    build = (APP / "scripts/build_engine.sh").read_text()
    assert mac["files"][place] in build and f"Contents/MacOS/{info['CFBundleExecutable']}" in build

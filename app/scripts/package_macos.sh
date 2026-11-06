#!/usr/bin/env bash
# Repackage the successfully stapled app without rebuilding or resubmitting it.
# Preserve Tauri's DMG layout, then sign the new DMG and updater bytes.
set -euo pipefail

: "${APP:?}" "${DMG:?}" "${BUNDLE:?}" "${VERSION:?}" "${UPDATER:?}"
: "${APPLE_SIGNING_IDENTITY:?}" "${RUNNER_TEMP:?}"
script_dir="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d "$RUNNER_TEMP/macos-package.XXXXXX")"
mounted=false
cleanup() {
  if [ "$mounted" = true ]; then hdiutil detach "$work/mount"; fi
  rm -rf "$work"
}
trap cleanup EXIT

# A failed staple must never be packaged as a successful release.
xcrun stapler validate "$APP"
mkdir -p "$work/mount" "$work/contents"
hdiutil attach "$DMG" -readonly -nobrowse -mountpoint "$work/mount"
mounted=true
ditto "$work/mount" "$work/contents"
hdiutil detach "$work/mount"
mounted=false
rm -rf "$work/contents/brindle.app"
ditto "$APP" "$work/contents/brindle.app"
hdiutil create -volname brindle -srcfolder "$work/contents" -format UDZO "$work/release.dmg"
codesign --force --timestamp --sign "$APPLE_SIGNING_IDENTITY" "$work/release.dmg"
codesign --verify --strict "$work/release.dmg"

if [ "$UPDATER" = true ]; then
  tar -czf "$work/brindle.app.tar.gz" -C "$(dirname "$APP")" "$(basename "$APP")"
  "${TAURI_CLI:-$script_dir/../node_modules/.bin/tauri}" signer sign --app-version "$VERSION" "$work/brindle.app.tar.gz"
  mv "$work/brindle.app.tar.gz" "$BUNDLE/macos/brindle.app.tar.gz"
  mv "$work/brindle.app.tar.gz.sig" "$BUNDLE/macos/brindle.app.tar.gz.sig"
fi
mv "$work/release.dmg" "$DMG"

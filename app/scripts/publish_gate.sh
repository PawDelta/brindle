#!/usr/bin/env bash
# publish_gate.sh — whether an app-release.yml run publishes to the site's
# downloads. Only a push of an app-v* tag does, with the bucket's role and
# name set, and on macOS only signed and notarized: a manual run (even of an
# existing tag) or the nightly job never overwrites the Mac DMGs / latest.json.
# On macOS it runs once, in macos-publish, for both architectures' builds:
# SIGNED and NOTARIZED are true only if both are (each past its Gatekeeper check).
# The Linux bundles (AppImage, .deb) have no OS signing to wait for.
# CHANNEL=release (release-publish) is the release-branch build instead: only
# a push of the release/desktop branch (REF), signed and notarized, never a tag.
#
# Reads PLATFORM (macos, the default, or linux), CHANNEL (release, or empty),
# EVENT (github.event_name), REF_TYPE (github.ref_type), REF (github.ref;
# release only), SIGNED and NOTARIZED ("true" or not; macOS only), ROLE and
# BUCKET (empty when unset). Prints go=true or go=false, appended to
# $GITHUB_OUTPUT when that is set.
set -euo pipefail

go=true
if [ "${CHANNEL:-}" = release ]; then
  [ "${EVENT:-}" = push ] && [ "${REF_TYPE:-}" = branch ] && [ "${REF:-}" = refs/heads/release/desktop ] \
    || { echo "not a pushed release/desktop (${EVENT:-?} of ${REF:-?}): not publishing"; go=false; }
else
  [ "${EVENT:-}" = push ] && [ "${REF_TYPE:-}" = tag ] \
    || { echo "not a pushed tag (${EVENT:-?} of a ${REF_TYPE:-?}): not publishing"; go=false; }
fi
if [ "${PLATFORM:-macos}" != linux ]; then
  [ "${SIGNED:-}" = true ] && [ "${NOTARIZED:-}" = true ] \
    || { echo "::warning::not signed and notarized: not publishing"; go=false; }
fi
[ -n "${ROLE:-}" ] && [ -n "${BUCKET:-}" ] \
  || { echo "::warning::the release role secret or DESKTOP_DOWNLOADS_BUCKET variable not set: not publishing"; go=false; }
echo "go=$go"
[ -z "${GITHUB_OUTPUT:-}" ] || echo "go=$go" >> "$GITHUB_OUTPUT"

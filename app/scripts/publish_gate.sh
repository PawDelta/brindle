#!/usr/bin/env bash
# publish_gate.sh — whether an app-release.yml run publishes to the site's
# downloads. Only a push of an app-v* tag does, with the bucket's role and
# name set, and on macOS only signed and notarized: a manual run (even of an
# existing tag) or the nightly job never overwrites the Mac DMGs / latest.json.
# On macOS it runs once, in macos-publish, for both architectures' builds:
# SIGNED and NOTARIZED are true only if both are (each past its Gatekeeper check).
# The Linux bundles (AppImage, .deb) have no OS signing to wait for. The Windows
# installer publishes only once its Authenticode signature has verified
# (SIGNED), with no notarization.
# CHANNEL=release (release-publish) is the staff build instead: only a push
# of main (REF), signed and notarized, never a tag.
#
# Reads PLATFORM (macos, the default, windows or linux), CHANNEL (release, or empty),
# EVENT (github.event_name), REF_TYPE (github.ref_type), REF (github.ref;
# release only), SIGNED ("true" or not; macOS and Windows) and NOTARIZED
# (macOS only), ROLE and BUCKET (empty when unset). Prints go=true or go=false,
# appended to $GITHUB_OUTPUT when that is set.
set -euo pipefail

go=true
must_publish=false
if [ "${CHANNEL:-}" = release ]; then
  if [ "${EVENT:-}" = push ] && [ "${REF_TYPE:-}" = branch ] && [ "${REF:-}" = refs/heads/main ]; then
    must_publish=true
  else
    echo "not a pushed main (${EVENT:-?} of ${REF:-?}): not publishing"
    go=false
  fi
else
  if [ "${EVENT:-}" = push ] && [ "${REF_TYPE:-}" = tag ]; then
    must_publish=true
  else
    echo "not a pushed tag (${EVENT:-?} of a ${REF_TYPE:-?}): not publishing"
    go=false
  fi
fi
case "${PLATFORM:-macos}" in
  macos)
    [ "${SIGNED:-}" = true ] && [ "${NOTARIZED:-}" = true ] \
      || { echo "::warning::not signed and notarized: not publishing"; go=false; } ;;
  windows)
    [ "${SIGNED:-}" = true ] \
      || { echo "::warning::not signed (or the signature did not verify): not publishing"; go=false; } ;;
esac
[ -n "${ROLE:-}" ] && [ -n "${BUCKET:-}" ] || {
  if [ "$must_publish" = true ]; then
    echo "::error::the release role secret or DESKTOP_DOWNLOADS_BUCKET variable not set: cannot publish"
    exit 1
  fi
  echo "::warning::the release role secret or DESKTOP_DOWNLOADS_BUCKET variable not set: not publishing"
  go=false
}
echo "go=$go"
[ -z "${GITHUB_OUTPUT:-}" ] || echo "go=$go" >> "$GITHUB_OUTPUT"

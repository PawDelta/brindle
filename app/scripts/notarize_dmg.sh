#!/usr/bin/env bash
# notarize_dmg.sh DMG — notarize the release DMG and staple its ticket, for
# app-release.yml's macos job. Tauri notarizes only the .app; the DMG
# around it needs its own submission and ticket, or Gatekeeper rejects the
# download itself ("Unnotarized Developer ID").
#
# Every call to Apple is retried, as Apple drops connections now and then
# (rc.5 and rc.8's Intel builds: "The network connection was lost", error 68):
# the upload until it has a submission id (a lost answer only means a second
# submission), the wait for that submission until Apple has decided, and the
# staple until the ticket validates. Only Apple's own verdict, Invalid or
# Rejected, fails at once, with Apple's log for it.
#
# Credentials as the workflow's signing setup exports them: APPLE_API_KEY_PATH,
# APPLE_API_KEY and APPLE_API_ISSUER, else APPLE_ID, APPLE_PASSWORD and
# APPLE_TEAM_ID. NOTARY_TRIES (default 5) and NOTARY_PAUSE (seconds, default
# 30, times the try) set the retries.
set -uo pipefail

dmg="${1:?usage: notarize_dmg.sh DMG}"
name="$(basename "$dmg")"
tries="${NOTARY_TRIES:-5}" pause="${NOTARY_PAUSE:-30}"

codesign --verify --strict --verbose=2 "$dmg" || { echo "::error::$name isn't validly signed"; exit 1; }
if [ -n "${APPLE_API_KEY_PATH:-}" ]; then
  auth=(--key "$APPLE_API_KEY_PATH" --key-id "$APPLE_API_KEY" --issuer "$APPLE_API_ISSUER")
else
  auth=(--apple-id "$APPLE_ID" --password "$APPLE_PASSWORD" --team-id "$APPLE_TEAM_ID")
fi
field() { jq -r ".$1 // empty" <<<"$2" 2>/dev/null || true; }
again() {  # what, attempt, detail
  echo "::warning::$1 (try $2 of $tries): ${3:-no answer}"
  [ "$2" = "$tries" ] || sleep $((pause * $2))
}

id=""
for attempt in $(seq 1 "$tries"); do
  out="$(xcrun notarytool submit "$dmg" "${auth[@]}" --output-format json)"
  echo "$out"
  id="$(field id "$out")"
  [ -z "$id" ] || break
  again "notarytool submit of $name got no submission id" "$attempt" "$(field message "$out")"
done
[ -n "$id" ] || { echo "::error::couldn't submit $name for notarization after $tries tries"; exit 1; }

status=""
for attempt in $(seq 1 "$tries"); do
  out="$(xcrun notarytool wait "$id" "${auth[@]}" --timeout 20m --output-format json)"
  echo "$out"
  status="$(field status "$out")"
  case "$status" in
    Accepted) break ;;
    Invalid | Rejected)
      echo "::error::notarization of $name ended '$status'"
      xcrun notarytool log "$id" "${auth[@]}" || true
      exit 1 ;;
  esac
  again "notarytool wait for $name ($id) ended '${status:-no answer}'" "$attempt"
done
if [ "$status" != Accepted ]; then
  echo "::error::notarization of $name ($id) ended '${status:-no answer}' after $tries tries"
  xcrun notarytool log "$id" "${auth[@]}" || true
  exit 1
fi

for attempt in $(seq 1 "$tries"); do
  xcrun stapler staple "$dmg" && xcrun stapler validate "$dmg" && exit 0
  again "stapling $name failed" "$attempt"
done
echo "::error::couldn't staple $name after $tries tries"
exit 1

#!/usr/bin/env bash
# Build and submit the iOS app to App Store Connect / TestFlight.
#
# Credentials come from .easrc, which is gitignored and holds the App Store
# Connect key id, issuer id, and the path to the .p8. The .p8 itself lives
# outside the repository at mode 600 and is never committed.
#
# Every step runs on a bare exit path: no pipes, so a failure cannot be
# swallowed by the exit code of something downstream.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .easrc ]; then
  echo "Missing .easrc. It should export EXPO_ASC_API_KEY_PATH, EXPO_ASC_KEY_ID"
  echo "and EXPO_ASC_ISSUER_ID."
  exit 1
fi
# shellcheck disable=SC1091
source .easrc

if [ ! -f "$EXPO_ASC_API_KEY_PATH" ]; then
  echo "The App Store Connect key is not at $EXPO_ASC_API_KEY_PATH."
  echo "Apple allows exactly one download, so if it is lost, generate a new key."
  exit 1
fi
echo "key present, issuer ${EXPO_ASC_ISSUER_ID:0:8}..., key id $EXPO_ASC_KEY_ID"

npx eas-cli build --platform ios --profile production --non-interactive
npx eas-cli submit --platform ios --latest --non-interactive

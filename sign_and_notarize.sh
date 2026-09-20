#!/usr/bin/env bash
# Sign, notarize and staple the AesApp Radio Updater .app with a Developer ID.
#
# Prerequisites (one-time):
#   1. A "Developer ID Application" certificate in your login keychain
#      (Xcode ▸ Settings ▸ Accounts ▸ Manage Certificates ▸ + ▸ Developer ID Application).
#      Check with:  security find-identity -v -p codesigning
#   2. A notarytool keychain profile with your credentials, e.g.:
#      xcrun notarytool store-credentials aesapp-notary \
#          --apple-id "you@example.com" --team-id "TEAMID" --password "app-specific-pw"
#      (an App Store Connect API key via --key/--key-id/--issuer also works)
#
# Usage:
#   CODESIGN_IDENTITY="Developer ID Application: Your Name (TEAMID)" \
#   NOTARY_PROFILE=aesapp-notary \
#   ./sign_and_notarize.sh ["dist/AesApp Radio Updater.app"]
set -euo pipefail

APP="${1:-dist/AesApp Radio Updater.app}"
IDENTITY="${CODESIGN_IDENTITY:?Set CODESIGN_IDENTITY to your 'Developer ID Application: ... (TEAMID)' identity}"
PROFILE="${NOTARY_PROFILE:-aesapp-notary}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENTITLEMENTS="$HERE/entitlements.plist"

[ -d "$APP" ] || { echo "app not found: $APP (build it first: pyinstaller bt_ota_gui.spec)"; exit 1; }

echo "==> Signing nested Mach-O binaries (hardened runtime)…"
# Sign every embedded Mach-O (dylibs, .so, helper exes) first, then the bundle.
find "$APP" -type f | while IFS= read -r f; do
  if file "$f" | grep -qi 'mach-o'; then
    codesign --force --timestamp --options runtime \
      --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$f"
  fi
done

echo "==> Signing the app bundle…"
codesign --force --timestamp --options runtime \
  --entitlements "$ENTITLEMENTS" --sign "$IDENTITY" "$APP"

echo "==> Verifying signature…"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> Zipping for notarization…"
ZIP="${APP%.app}-notarize.zip"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

echo "==> Submitting to Apple notary service (this waits for the result)…"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait

echo "==> Stapling the ticket…"
xcrun stapler staple "$APP"
xcrun stapler validate "$APP"

echo "==> Gatekeeper assessment…"
spctl -a -vvv -t exec "$APP" || true

echo "==> Done. Package for distribution:"
DIST_ZIP="AesApp-Radio-Updater-macos-arm64.zip"
( cd "$(dirname "$APP")" && rm -f "$DIST_ZIP" && ditto -c -k --sequesterRsrc --keepParent "$(basename "$APP")" "$DIST_ZIP" && echo "   wrote $(dirname "$APP")/$DIST_ZIP" )

#!/bin/bash
# AJ's real release keys (plan Task 7). The fixture is the test signature
# `pilnk-sign --init` made on AJ's laptop with the YubiKey (20 Nov 2026): a fixed
# statement (version 0.0.0, zero commit), its signature, and the two signer lines.
# Everything here is public. This proves the real key works on the fleet's OpenSSH
# floor (the sandbox runs 8.9) and that release_signers pins exactly that key.
. "$(dirname "$0")/lib.sh"
F="$REPO/tests/image/fixtures"
mkroot
TAMPERED="$ROOT/tampered"
sed 's/^version 0\.0\.0$/version 0.0.1/' "$F/release-yk.statement" > "$TAMPERED" 2>/dev/null

ok "real YubiKey signature verifies on this OpenSSH (8.9 floor)" 'ssh-keygen -Y verify -f "$F/release-yk.signers" -I release -n pilnk-release -s "$F/release-yk.sig" < "$F/release-yk.statement"'
ok "one changed byte fails"                                     '[ -s "$TAMPERED" ] && ! cmp -s "$TAMPERED" "$F/release-yk.statement" && ! ssh-keygen -Y verify -f "$F/release-yk.signers" -I release -n pilnk-release -s "$F/release-yk.sig" < "$TAMPERED" 2>/dev/null'
ok "release_signers: two lines, namespaces option only"         '[ "$(grep -c . "$REPO/release_signers")" = 2 ] && ! grep -v "^release namespaces=\"pilnk-release\" [a-z0-9@.-]* [A-Za-z0-9+/=]* pilnk-release-" "$REPO/release_signers"'
fixkey() { cut -d" " -f3- "$F/release-yk.signers" 2>/dev/null | head -1; }
ok "fixture key is the release_signers YubiKey key"             '[ -n "$(fixkey)" ] && grep -qF "$(fixkey)" "$REPO/release_signers"'

# Ruling S2: nodes refuse a FIDO signature without the signed touch flag, so the
# real one must carry it (same parser as release-check.sh's touch_check).
ok "real YubiKey signature carries the touch flag"              'bash -c '\''source "$1"; touch_ok "$2"'\'' _ "$REPO/tools/pilnk-sign.sh" "$F/release-yk.sig"'
# Task 1: the YubiKey line is a FIDO key bound to application ssh:pilnk-release,
# and the backup line is a plain ed25519 key.
yk_app() { grep pilnk-release-yubikey "$REPO/release_signers" | cut -d" " -f4 | base64 -d 2>/dev/null | tail -c 17; }
ok "YubiKey line: sk-ssh-ed25519 bound to ssh:pilnk-release"    '[ "$(grep pilnk-release-yubikey "$REPO/release_signers" | cut -d" " -f3)" = sk-ssh-ed25519@openssh.com ] && [ "$(yk_app)" = ssh:pilnk-release ]'
ok "backup line: plain ssh-ed25519"                             '[ "$(grep pilnk-release-backup "$REPO/release_signers" | cut -d" " -f3)" = ssh-ed25519 ]'

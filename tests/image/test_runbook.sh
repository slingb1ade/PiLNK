#!/bin/bash
# shellcheck disable=SC2034  # the S* section texts are read by eval in ok()
# image/RUNBOOK.md says what the scripts do (final review: I5, I6, M1, M11,
# M12, Inkpot M-2). Static checks on the text.
. "$(dirname "$0")/lib.sh"
RB="$REPO/image/RUNBOOK.md"
sec() { awk -v n="$1" '/^## /{p = ($2 == n".")} p' "$RB"; }   # section N's text
has() { grep -qiE -- "$2" <<< "$1"; }

S1=$(sec 1); S2=$(sec 2); S3=$(sec 3); S5=$(sec 5); S6=$(sec 6); S7=$(sec 7); S8=$(sec 8)
ok "I5: section 1 is Ethernet only, no Wi-Fi in Imager" \
   'has "$S1" "ethernet" && has "$S1" "no wi-fi|do not set wi-fi|leave wi-fi" && ! grep -qi "Wi-Fi set here is fine" "$RB"'
ok "M1: full-upgrade then reboot before the build" \
   'has "$S2$S3" "sudo apt update && sudo apt full-upgrade -y" && has "$S2$S3" "sudo reboot"'
ok "M1: the upgrade comes before pilnk-image-build.sh" \
   'a=$(grep -n "full-upgrade" "$RB" | head -1 | cut -d: -f1); b=$(grep -n "bash ~/pilnk/image/pilnk-image-build.sh" "$RB" | head -1 | cut -d: -f1); [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]'
ok "M11: the build refusals list dnsmasq-base" 'has "$S3" "dnsmasq-base"'
ok "I6: zero-fill timing given for 16, 32 and 64 GB cards" 'has "$S5" "16 ?GB" && has "$S5" "32 ?GB" && has "$S5" "64 ?GB"'
ok "I6: says how to tell it is still working (activity LED)" 'has "$S5" "(activity|green) (LED|light)" && has "$S5" "flicker"'
ok "I6: no 10-minute power-pull rule; pull only after 2 hours" '! has "$S5" "after 10 minutes" && has "$S5" "2 hours"'
ok "I6: points at the console / failure file for a stopped Pi" 'has "$S5" "pilnk-sterilise-failed.txt" && has "$S5" "screen|console|HDMI"'
ok "M12: first boot reboots once to grow the card" 'grep -qiE "first boot.*reboots? once|reboots? once.*(grow|fill)" "$RB"'
ok "Inkpot M-2: save name ends .img (Inkpot adds .xz)" \
   'has "$S6" "save it as \`~/pilnk-images/pilnk-sterile\.img\`" && ! has "$S6" "save it as \`[^\`]*\.img\.xz\`" && has "$S6" "pilnk-sterile\.img\.xz"'
ok "C2: test matrix checks the setup page with tar1090 installed" 'has "$S8" "lighttpd|tar1090"'
ok "run 2: section 1 has the write-build-userdata.sh fallback when Imager leaves the stock template" \
   'has "$S1" "write-build-userdata\.sh" && has "$S1" "user-data"'
ok "run 2: section 7 says denylist words and .pyc files can SKIP as stock Debian files" \
   'has "$S7" "unmodified Debian package file" && has "$S7" "denylist" && has "$S7" "\.pyc"'
ok "12 Nov: section 8 points at the Wi-Fi setup event log and how to read it" \
   'has "$S8" "pilnk-wifi-setup\.log" && has "$S8" "card-identity\.sh" && has "$S8" "never a network name or password"'
ok "12 Nov: section 8 power-cut row uses cut-timer.sh; two-cards row says the hostname is per Pi" \
   'has "$S8" "cut-timer\.sh" && has "$S8" "same hostname by design"'
ok "run4: section 5 says a dark green light does not prove sterilise finished; the mark does" \
   'has "$S5" "does not (prove|mean)" && has "$S5" "pilnk-sterile\.ok"'
ok "run4: section 7 explains FAIL sterilise-unfinished and that the card is rebuilt" \
   'has "$S7" "sterilise-unfinished" && has "$S7" "rebuild|build .*again"'
# 14 Nov, after v1.6.24 shipped: the image work is on main, so the image is built
# from a plain clone of main. No patches, no pinned base, no branch.
ok "ship: section 2 clones main, with no patches and no branch" \
   'has "$S2" "git clone https://github.com/slingb1ade/PiLNK.git ~/pilnk" && ! has "$S2" "git am|sterile-patches|switch -c"'
ok "ship: section 2 shows which release the image is" \
   'has "$S2" "git -C ~/pilnk log --oneline -1" && has "$S2" "cat ~/pilnk/VERSION"'
ok "ship: section 2 says a main-built first boot logs update: ok; only a branch build skips it" \
   'has "$S2" "update: ok" && has "$S2" "update skipped"'
ok "ship: no patches or pinned base left anywhere in the runbook" \
   '! grep -qiE "\.patch|git am|switch -c|fba3ccd" "$RB"'
ok "ship: section 1 runs write-build-userdata.sh from an up-to-date clone of main" \
   'has "$S1" "git clone https://github.com/slingb1ade/PiLNK.git ~/pilnk-main" && has "$S1" "git -C ~/pilnk-main pull --ff-only" && has "$S1" "bash ~/pilnk-main/image/write-build-userdata.sh"'
ok "ship: section 7 verifies from the same clone of main, updated first" \
   'has "$S7" "git -C ~/pilnk-main pull --ff-only" && has "$S7" "cd ~/pilnk-main" && ! has "$S7" "pilnk-verify"'
ok "ship: section 8 says a main-built card updates itself like any node" \
   'has "$S8" "updates itself like any node" && ! has "$S8" "replace the branch"'
ok "ship: section 8 lets the whole-house cut wait for another day on the same cards" \
   'has "$S8" "whole-house" && has "$S8" "same cards"'

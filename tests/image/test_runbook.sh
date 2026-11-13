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
ok "run 2: section 2 removes ~/sterile-patches on the Pi once the patches are applied" \
   'a=$(grep -n "am ~/sterile-patches" "$RB" | head -1 | cut -d: -f1); b=$(grep -n "^rm -rf ~/sterile-patches\$" "$RB" | head -1 | cut -d: -f1); [ -n "$a" ] && [ -n "$b" ] && [ "$a" -lt "$b" ]'
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

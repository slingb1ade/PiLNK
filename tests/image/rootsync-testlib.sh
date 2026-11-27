# Shared helpers for test_rootsync*.sh (Part B). Source after lib.sh and
# sigtest-lib.sh.
#
#   setup        fake GitHub $ROOT/up + node clone, fake root $P with root's
#                pinned signers; RSB=$P/var/lib/pilnk, LIB=$P/usr/local/lib/pilnk
#   rel V [KEY]  commit VERSION V on up, sign it, write vp.json; C = the commit
#   RS [ARGS]    run rootsync.sh under $P; output in $ROOT/rs.out, exit in $RC
#   st_field K =|~ V   check a status.json field
#   mksvc KIND   pilnk.service + passwd under $P; config.json auto|manual|...
RS_BIN="$REPO/rootsync.sh"

# A fake GitHub ($ROOT/up) carrying what a real release carries for rootsync:
# release-check.sh and rootside/MANIFEST. A fake root ($P) with root's pinned
# signers. curl and git are logged to $CALLS and then run for real.
setup() {
  mkroot; mkrepo
  P="$ROOT/fs"; RSB="$P/var/lib/pilnk"; LIB="$P/usr/local/lib/pilnk"
  mkdir -p "$P/run" "$RSB/trust"
  signers_line aj > "$RSB/trust/release_signers"
  ( cd "$ROOT/up" && cp "$REPO/release-check.sh" . && mkdir -p rootside \
    && echo release-check.sh > rootside/MANIFEST && G add -A && G commit -qm "rootsync bits" )
  "$REALGIT" -C "$ROOT/up" config uploadpack.allowReachableSHA1InWant true
  fakebin curl 'exec /usr/bin/curl "$@"'
  fakebin git 'exec "$REALGIT" "$@"'
}
# version.php as pilnk.io serves it, from $ROOT/st + $ROOT/st.sig
vp() {
  python3 -I - "$ROOT/st" "$ROOT/st.sig" "$ROOT/vp.json" <<'PY'
import json, sys
st = open(sys.argv[1]).read(); sig = open(sys.argv[2]).read()
json.dump({'version': st.split('\n')[1].split(' ')[1], 'release': {'statement': st, 'sig': sig}}, open(sys.argv[3], 'w'))
PY
}
rel() { release_to "$@"; vp; C=$("$REALGIT" -C "$ROOT/up" rev-parse HEAD); }
RS() {
  PILNK_ROOTSYNC_PREFIX="$P" PILNK_RELEASE_URL="${URL:-file://$ROOT/vp.json}" \
  PILNK_ROOTSYNC_GIT_URL="$ROOT/up" PILNK_ROOTSYNC_UID="$(id -u)" PILNK_ROOTSYNC_LOCK_WAIT=1 \
    bash "$RS_BIN" "$@" > "$ROOT/rs.out" 2>&1; RC=$?
}
st_field() {  # KEY OP VALUE   (OP: = or ~)
  python3 -I - "$RSB/state/status.json" "$@" <<'PY'
import json, re, sys
try:
    v = str(json.load(open(sys.argv[1])).get(sys.argv[2], ''))
except Exception:
    sys.exit(1)
sys.exit(0 if (v == sys.argv[4] if sys.argv[3] == '=' else re.search(sys.argv[4], v)) else 1)
PY
}
cur() { readlink "$RSB/current" 2>/dev/null; }
tree_sum() { ( cd "$RSB" && find signed current previous -exec ls -ld --time-style=+%s {} + 2>/dev/null; \
               find signed -type f -exec sha256sum {} + 2>/dev/null ) | sort | sha256sum; }

# pilnk.service + passwd under $P say who the PiLNK user is and where the app lives.
mksvc() {  # auto|manual|missing|fifo|huge|symlink
  local u; u=$(id -un)
  mkdir -p "$P/etc/systemd/system"
  printf '[Service]\nUser=%s\nExecStart=/usr/bin/python3 %s/pilnk/app.py\n' "$u" "$HOME" > "$P/etc/systemd/system/pilnk.service"
  printf '%s:x:%s:%s::%s:/bin/bash\n' "$u" "$(id -u)" "$(id -g)" "$HOME" > "$P/etc/passwd"
  rm -f "$HOME/pilnk/config.json"
  case "$1" in
    auto)    echo '{"auto_update": true}' > "$HOME/pilnk/config.json" ;;
    manual)  echo '{"auto_update": false}' > "$HOME/pilnk/config.json" ;;
    fifo)    mkfifo "$HOME/pilnk/config.json" ;;
    huge)    { printf '{"auto_update": false, "pad": "'; head -c 1100000 /dev/zero | tr '\0' a; printf '"}'; } > "$HOME/pilnk/config.json" ;;
    symlink) echo '{"auto_update": false}' > "$ROOT/elsewhere.json"; ln -s "$ROOT/elsewhere.json" "$HOME/pilnk/config.json" ;;
  esac
}


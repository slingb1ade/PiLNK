#!/bin/bash
# Helpers for the signed-release tests. Source AFTER lib.sh (needs mkroot).
#   mkrepo            fake GitHub $ROOT/up (VERSION 1.0.0, release_signers trusting
#                     test key "aj") + node clone $HOME/pilnk
#   testkey NAME      throwaway ed25519 key $ROOT/keys/NAME (never a real key)
#   signers_line NAME the allowed-signers line for NAME (namespaces option only)
#   statement V C     the exact release statement text
#   sign KEY FILE [NS] write FILE.sig (namespace pilnk-release unless NS given)
#   release_to V [KEY] commit VERSION=V on up, write $ROOT/st + $ROOT/st.sig for it
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
REALGIT="$(command -v git)"; export REALGIT
G() { "$REALGIT" -c user.name=t -c user.email=t@t "$@"; }

testkey() {
  mkdir -p "$ROOT/keys"
  [ -f "$ROOT/keys/$1" ] || ssh-keygen -q -t ed25519 -N '' -C "$1" -f "$ROOT/keys/$1"
}
signers_line() { printf 'release namespaces="pilnk-release" %s\n' "$(cut -d' ' -f1,2 "$ROOT/keys/$1.pub")"; }
statement() { printf 'pilnk-release 1\nversion %s\ncommit %s\n' "$1" "$2"; }
sign() {
  rm -f "$2.sig"
  ssh-keygen -q -Y sign -f "$ROOT/keys/$1" -n "${3:-pilnk-release}" "$2" >/dev/null 2>&1
}
mkrepo() {
  export HOME="$ROOT/home"; mkdir -p "$HOME"
  testkey aj
  G init -q -b main "$ROOT/up"
  ( cd "$ROOT/up" && echo 1.0.0 > VERSION && echo 'print(1)' > app.py \
    && printf 'config.json\nrecordings/\nrelease_state.json\nupdate.log\n' > .gitignore \
    && signers_line aj > release_signers && G add -A && G commit -qm one )
  "$REALGIT" clone -q "$ROOT/up" "$HOME/pilnk"
}
release_to() {
  ( cd "$ROOT/up" && echo "$1" > VERSION && G commit -qam "v$1" )
  statement "$1" "$("$REALGIT" -C "$ROOT/up" rev-parse HEAD)" > "$ROOT/st"
  sign "${2:-aj}" "$ROOT/st"
}
node_pull() { G -C "$HOME/pilnk" pull -q --ff-only 2>/dev/null; }
node_fetch() { G -C "$HOME/pilnk" fetch -q origin main 2>/dev/null; }

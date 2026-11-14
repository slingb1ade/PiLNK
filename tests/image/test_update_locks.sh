#!/bin/bash
# shellcheck disable=SC2034  # RC and OUT are read by eval in ok() / kept for debugging
# update.sh after a power cut in the middle of a git command (run 6, 15 Nov).
# A cut can leave git's lock files (.git/index.lock, .git/refs/.../main.lock)
# or a half-written .git/index behind. git then refuses every later fetch or
# reset, so the node would never update again until someone fixed it by hand.
# Real git against a local "GitHub" repo; sudo/systemctl are never reached
# because every run uses PILNK_UPDATE_NO_RESTART=1 (the first-boot path).
. "$(dirname "$0")/lib.sh"

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
G() { git -c user.name=t -c user.email=t@t "$@"; }

# setup VERSION_ON_GITHUB: "GitHub" at 1.0.0 + a node clone in $HOME/pilnk;
# GitHub then moves to VERSION_ON_GITHUB (same version = nothing new).
setup() {
  mkroot
  export HOME="$ROOT/home"; mkdir -p "$HOME"
  fakebin pgrep 'exit 1'                     # no git process running
  G init -q -b main "$ROOT/up"
  ( cd "$ROOT/up" && echo 1.0.0 > VERSION && echo 'print(1)' > app.py \
    && printf 'config.json\nrecordings/\n' > .gitignore \
    && G add -A && G commit -qm one )
  git clone -q "$ROOT/up" "$HOME/pilnk"
  echo '{"mine":1}' > "$HOME/pilnk/config.json"            # per-node, gitignored
  mkdir -p "$HOME/pilnk/recordings" && : > "$HOME/pilnk/recordings/keep.lock"
  if [ "$1" != 1.0.0 ]; then ( cd "$ROOT/up" && echo "$1" > VERSION && G commit -qam two ); fi
}
run_update() {
  OUT=$(cd "$HOME" && PILNK_UPDATE_NO_RESTART=1 bash "$REPO/update.sh" 2>&1); RC=$?
}
ver() { cat "$HOME/pilnk/VERSION"; }

# --- baseline: no leftovers, update works (and keeps working after the fix)
setup 1.0.1; run_update
ok "baseline: updates to the new VERSION" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ]'

# --- stale index lock (cut during reset --hard)
setup 1.0.1; : > "$HOME/pilnk/.git/index.lock"; run_update
ok "index.lock left by a power cut: update still succeeds" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ]'
ok "index.lock left by a power cut: the lock is gone" '[ ! -e "$HOME/pilnk/.git/index.lock" ]'
ok "index.lock left by a power cut: the log says it removed a stale lock" \
   'grep -q "stale lock .git/index.lock" "$HOME/pilnk/update.log"'

# --- stale ref lock (cut during fetch)
setup 1.0.1; mkdir -p "$HOME/pilnk/.git/refs/remotes/origin"
: > "$HOME/pilnk/.git/refs/remotes/origin/main.lock"; run_update
ok "origin/main ref lock left by a power cut: update still succeeds" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ]'
ok "origin/main ref lock left by a power cut: the lock is gone" '[ ! -e "$HOME/pilnk/.git/refs/remotes/origin/main.lock" ]'

# --- half-written index (zero-length or garbage after a cut)
setup 1.0.1; head -c 40 /dev/urandom > "$HOME/pilnk/.git/index"; run_update
ok "corrupt .git/index: update still succeeds" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ]'
ok "corrupt .git/index: the index is readable again" 'git -C "$HOME/pilnk" ls-files >/dev/null 2>&1'
ok "corrupt .git/index: tracked files match GitHub exactly" \
   'st=$(git -C "$HOME/pilnk" status --porcelain --untracked-files=no 2>&1) && [ -z "$st" ]'
ok "corrupt .git/index: the log says the index was rebuilt" 'grep -qi "index" "$HOME/pilnk/update.log" && grep -qi "rebuil" "$HOME/pilnk/update.log"'
setup 1.0.1; : > "$HOME/pilnk/.git/index"; run_update
ok "zero-length .git/index: update still succeeds" '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.1 ]'

# --- per-node files survive the repair
setup 1.0.1; : > "$HOME/pilnk/.git/index.lock"; head -c 40 /dev/urandom > "$HOME/pilnk/.git/index"; run_update
ok "repair keeps config.json" '[ "$(cat "$HOME/pilnk/config.json")" = "{\"mine\":1}" ]'
ok "repair only touches .git: a .lock file in recordings/ stays" '[ -e "$HOME/pilnk/recordings/keep.lock" ]'

# --- first boot with nothing new on GitHub (the run 6 card path)
setup 1.0.0; : > "$HOME/pilnk/.git/index.lock"; run_update
ok "first boot, unchanged VERSION, stale lock: clean no-op exit 0" \
   '[ "$RC" = 0 ] && [ "$(ver)" = 1.0.0 ] && grep -q "nothing to do" "$HOME/pilnk/update.log"'

# --- never pull a lock out from under a live git
setup 1.0.1; fakebin pgrep 'exit 0'          # a git process is running
: > "$HOME/pilnk/.git/index.lock"; run_update
ok "git running: its index.lock is left alone" '[ -e "$HOME/pilnk/.git/index.lock" ]'
ok "git running: update fails safe (exit 1, tree unchanged)" '[ "$RC" = 1 ] && [ "$(ver)" = 1.0.0 ]'
ok "git running: the check asked for a process named exactly git" 'grep -q "^pgrep -x git" "$CALLS"'
setup 1.0.1; fakebin pgrep 'exit 0'; head -c 40 /dev/urandom > "$HOME/pilnk/.git/index"
cp "$HOME/pilnk/.git/index" "$ROOT/index.before"; run_update
ok "git running: a broken index is left alone too" 'cmp -s "$HOME/pilnk/.git/index" "$ROOT/index.before"'

#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  refresh-aircraft-db.sh
#  Downloads the latest aircraft type/registration database for
#  PiLNK's /flights enrichment. Called by the systemd timer
#  (pilnk-aircraft-db-refresh.timer) and safe to run manually.
#
#  Source: https://github.com/wiedehopf/tar1090-db (csv branch)
#  Maintained by: Mictronics + wiedehopf community
#  Format: gzipped CSV, columns icao24,r,t,...
#  Size: ~10 MB compressed, ~500K aircraft
#  Refresh frequency: weekly is plenty (DB changes slowly)
#
#  After a successful download, the script restarts pilnk so the
#  in-memory AIRCRAFT_DB picks up the new data. If pilnk isn't
#  running (first install), the restart is a no-op.
# ─────────────────────────────────────────────────────────────

set -euo pipefail

DB_DIR="/usr/local/share/pilnk-aircraft-db"
DB_FILE="$DB_DIR/aircraft.csv.gz"
DB_URL="https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz"
TMP_FILE="$DB_FILE.tmp"

log() { echo "[$(date -Iseconds)] [pilnk-db] $*"; }

# Create the destination dir if missing (idempotent)
if [[ ! -d "$DB_DIR" ]]; then
    log "Creating $DB_DIR"
    mkdir -p "$DB_DIR"
    chmod 755 "$DB_DIR"
fi

# Download to temp file first so we never corrupt an existing DB
log "Downloading aircraft DB from $DB_URL"
if ! curl -fsSL --connect-timeout 30 --max-time 300 -o "$TMP_FILE" "$DB_URL"; then
    log "ERROR: Download failed, keeping existing DB if present"
    rm -f "$TMP_FILE"
    exit 1
fi

# Sanity-check the download: must be non-empty and look like a gzip
if [[ ! -s "$TMP_FILE" ]]; then
    log "ERROR: Downloaded file is empty"
    rm -f "$TMP_FILE"
    exit 1
fi
if ! file "$TMP_FILE" | grep -q gzip; then
    log "ERROR: Downloaded file is not gzip — refusing to install"
    rm -f "$TMP_FILE"
    exit 1
fi

# Atomic move into place
mv "$TMP_FILE" "$DB_FILE"
chmod 644 "$DB_FILE"
log "Aircraft DB updated: $(stat -c '%s bytes' "$DB_FILE")"

# Restart pilnk if it's running so the new DB loads into memory.
# If pilnk isn't installed yet (first run), this is a silent no-op.
if systemctl is-active --quiet pilnk; then
    log "Restarting pilnk to reload the new DB"
    systemctl restart pilnk
fi

log "Done."

#!/bin/bash
# ─────────────────────────────────────────────────────────────
# PiLNK — RTL-SDR Dongle Check
# Verifies both dongles are present before PiLNK starts.
# Serial 00000001 = FlightAware Pro Stick (ADS-B / 1090MHz)
# Serial 00000002 = RTL-SDR Blog V4 (VHF Audio / 118-137MHz)
# ─────────────────────────────────────────────────────────────

SERIAL_ADSB="00000001"
SERIAL_AUDIO="00000002"
MAX_WAIT=30
RETRY_INTERVAL=5
LOG="/var/log/pilnk-dongles.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

dongle_present() {
  rtl_test -d "$1" -t 2>&1 | grep -q "Found"
}

check_by_serial() {
  # Use rtl_eeprom to find dongle by serial number
  for i in 0 1 2 3; do
    serial=$(rtl_eeprom -d $i 2>&1 | grep "Serial number" | awk '{print $NF}' 2>/dev/null)
    if [ "$serial" = "$1" ]; then
      echo $i
      return 0
    fi
  done
  return 1
}

log "=== PiLNK Dongle Check Starting ==="

ELAPSED=0
ADSB_OK=false
AUDIO_OK=false

while [ $ELAPSED -lt $MAX_WAIT ]; do

  # Check ADS-B dongle (serial 00000001)
  if ! $ADSB_OK; then
    idx=$(check_by_serial "$SERIAL_ADSB")
    if [ $? -eq 0 ]; then
      log "✓ ADS-B dongle found (serial $SERIAL_ADSB) at index $idx"
      ADSB_OK=true
    fi
  fi

  # Check Audio dongle (serial 00000002)
  if ! $AUDIO_OK; then
    idx=$(check_by_serial "$SERIAL_AUDIO")
    if [ $? -eq 0 ]; then
      log "✓ Audio dongle found (serial $SERIAL_AUDIO) at index $idx"
      AUDIO_OK=true
    fi
  fi

  # Both found — all good
  if $ADSB_OK && $AUDIO_OK; then
    log "✓ Both dongles verified — starting PiLNK"
    exit 0
  fi

  # Still waiting
  if ! $ADSB_OK; then
    log "⏳ Waiting for ADS-B dongle (serial $SERIAL_ADSB)... ${ELAPSED}s elapsed"
  fi
  if ! $AUDIO_OK; then
    log "⏳ Waiting for Audio dongle (serial $SERIAL_AUDIO)... ${ELAPSED}s elapsed"
  fi

  sleep $RETRY_INTERVAL
  ELAPSED=$((ELAPSED + RETRY_INTERVAL))
done

# Timeout — log what's missing and continue anyway
log "⚠ Dongle check timed out after ${MAX_WAIT}s"
if ! $ADSB_OK; then
  log "✗ ADS-B dongle (serial $SERIAL_ADSB) NOT FOUND — flight tracking may be unavailable"
fi
if ! $AUDIO_OK; then
  log "✗ Audio dongle (serial $SERIAL_AUDIO) NOT FOUND — ATC audio may be unavailable"
fi
log "⚠ Starting PiLNK anyway with available hardware"

# Exit 0 so systemd still starts PiLNK
exit 0

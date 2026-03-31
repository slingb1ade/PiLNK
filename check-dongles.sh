#!/bin/bash
# ─────────────────────────────────────────────────────────────
# PiLNK — RTL-SDR Dongle Check
# Verifies both dongles are present before PiLNK starts.
# FlightAware Pro Stick  = 0bda:2838 (ADS-B / 1090MHz)
# RTL-SDR Blog V4        = 0bda:2832 (VHF Audio / 118-137MHz)
# ─────────────────────────────────────────────────────────────

USB_ADSB="0bda:2838"
USB_AUDIO="0bda:2832"
MAX_WAIT=30
RETRY_INTERVAL=5
LOG="/var/log/pilnk-dongles.log"

log() {
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG"
}

log "=== PiLNK Dongle Check Starting ==="

ELAPSED=0
ADSB_OK=false
AUDIO_OK=false

while [ $ELAPSED -le $MAX_WAIT ]; do

  # Check ADS-B dongle
  if ! $ADSB_OK; then
    if lsusb | grep -q "$USB_ADSB"; then
      log "✓ ADS-B dongle found (FlightAware Pro Stick $USB_ADSB)"
      ADSB_OK=true
    fi
  fi

  # Check Audio dongle
  if ! $AUDIO_OK; then
    if lsusb | grep -q "$USB_AUDIO"; then
      log "✓ Audio dongle found (RTL-SDR Blog V4 $USB_AUDIO)"
      AUDIO_OK=true
    fi
  fi

  # Both found — all good
  if $ADSB_OK && $AUDIO_OK; then
    log "✓ Both dongles verified — starting PiLNK"
    exit 0
  fi

  # Still waiting
  [ "$ELAPSED" -gt 0 ] && {
    ! $ADSB_OK && log "⏳ Waiting for ADS-B dongle ($USB_ADSB)... ${ELAPSED}s elapsed"
    ! $AUDIO_OK && log "⏳ Waiting for Audio dongle ($USB_AUDIO)... ${ELAPSED}s elapsed"
  }

  sleep $RETRY_INTERVAL
  ELAPSED=$((ELAPSED + RETRY_INTERVAL))
done

# Timeout — log what's missing and continue anyway
log "⚠ Dongle check timed out after ${MAX_WAIT}s"
! $ADSB_OK && log "✗ ADS-B dongle ($USB_ADSB) NOT FOUND — flight tracking may be unavailable"
! $AUDIO_OK && log "✗ Audio dongle ($USB_AUDIO) NOT FOUND — ATC audio may be unavailable"
log "⚠ Starting PiLNK anyway with available hardware"
exit 0

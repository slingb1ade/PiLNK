#!/bin/bash
# ─────────────────────────────────────────────────────────────
# PiLNK — Service Installer
# Sets up PiLNK to start automatically on boot
# Run once as: bash install-service.sh
# ─────────────────────────────────────────────────────────────

set -e

PILNK_DIR="/home/aj/pilnk"
SERVICE_NAME="pilnk"

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║     PiLNK Boot Service Installer      ║"
echo "╚═══════════════════════════════════════╝"
echo ""

# ── Check running as correct user ─────────────────────────
if [ "$EUID" -ne 0 ]; then
  echo "Please run with sudo: sudo bash install-service.sh"
  exit 1
fi

# ── Copy dongle check script ───────────────────────────────
echo "→ Installing dongle check script..."
cp "$PILNK_DIR/check-dongles.sh" "$PILNK_DIR/check-dongles.sh"
chmod +x "$PILNK_DIR/check-dongles.sh"
chown aj:aj "$PILNK_DIR/check-dongles.sh"
echo "  ✓ check-dongles.sh installed"

# ── Install systemd service ────────────────────────────────
echo "→ Installing systemd service..."
cp "$PILNK_DIR/pilnk.service" /etc/systemd/system/pilnk.service
chmod 644 /etc/systemd/system/pilnk.service
echo "  ✓ pilnk.service installed"

# ── Create log file ────────────────────────────────────────
echo "→ Setting up log file..."
touch /var/log/pilnk-dongles.log
chown aj:aj /var/log/pilnk-dongles.log
echo "  ✓ /var/log/pilnk-dongles.log created"

# ── Enable and start service ───────────────────────────────
echo "→ Enabling PiLNK service..."
systemctl daemon-reload
systemctl enable pilnk.service
echo "  ✓ PiLNK enabled on boot"

echo ""
echo "→ Starting PiLNK now..."
systemctl start pilnk.service
sleep 3

# ── Check status ───────────────────────────────────────────
STATUS=$(systemctl is-active pilnk.service)
if [ "$STATUS" = "active" ]; then
  echo "  ✓ PiLNK is running!"
else
  echo "  ✗ PiLNK failed to start — check: sudo journalctl -u pilnk -n 50"
fi

echo ""
echo "╔═══════════════════════════════════════╗"
echo "║           Install Complete!           ║"
echo "╠═══════════════════════════════════════╣"
echo "║  PiLNK will now start on every boot   ║"
echo "║                                       ║"
echo "║  Useful commands:                     ║"
echo "║  sudo systemctl status pilnk          ║"
echo "║  sudo systemctl restart pilnk         ║"
echo "║  sudo systemctl stop pilnk            ║"
echo "║  sudo journalctl -u pilnk -f          ║"
echo "║  cat /var/log/pilnk-dongles.log       ║"
echo "╚═══════════════════════════════════════╝"
echo ""

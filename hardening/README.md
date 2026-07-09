# Radio hardening (deployed on Pi5 2026-07-09, fire-drill verified)

Self-healing for the SDR radio stack. Requires pilnk_bridge >= v0.5.1
(audioSps flow counter in /sdr/status).

Install:
    sudo cp pilnk-radio-watchdog.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/pilnk-radio-watchdog.sh
    sudo cp pilnk-radio-watchdog.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now pilnk-radio-watchdog
    # crash auto-restart for sdrpp itself:
    printf '[Service]\nRestart=on-failure\nRestartSec=5\n' | sudo tee /etc/systemd/system/sdrpp.service.d/restart.conf >/dev/null && sudo systemctl daemon-reload

CRITICAL fleet-installer lesson (2026-07-09): the RTL-SDR Blog V4 NEEDS the
rtl-sdr-blog librtlsdr fork. Stock Debian librtlsdr half-works (enumerates,
streams) but is ~10 dB DESENSITIZED — the node ships deaf and nothing errors.
Force-link it for sdrpp and verify at runtime:
    printf '[Service]\nEnvironment=LD_LIBRARY_PATH=/usr/local/lib\n' | sudo tee /etc/systemd/system/sdrpp.service.d/v4lib.conf >/dev/null
    sudo systemctl daemon-reload && sudo systemctl restart sdrpp
    grep librtlsdr /proc/$(pidof sdrpp)/maps | head -1   # must show /usr/local/lib
Also pin the V4 by serial in ~/.config/sdrpp/rtl_sdr_config.json (dual-dongle
nodes) and set per-crystal ppm (this V4: -7).

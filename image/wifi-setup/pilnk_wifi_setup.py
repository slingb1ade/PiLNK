#!/usr/bin/env python3
"""PiLNK Wi-Fi setup hotspot. Stdlib only; talks to NetworkManager through nmcli.

Run at every boot. If the Pi is online (nm-online AND a default route) it exits 0.
Otherwise: scan, open the hotspot PiLNK-Setup-XXXX, serve a one-page form on
10.42.0.1:80, and join the network the user picks. Exits 0 once online.

Rules: nmcli/raspi-config get argv lists (never a shell); the Wi-Fi password is
never logged, printed or put in an error message; saved connections are never read.
"""
import html, http.server, os, re, shutil, signal, subprocess, sys, threading, time
import urllib.parse

HOST, PORT = "10.42.0.1", 80
HOTSPOT_CON = "PiLNK-Hotspot"
IDLE_RESTART = 1800          # seconds with no POST before the hotspot restarts
JOIN_WAIT = 30               # seconds to wait for a join
BOOT_WAIT = 90               # seconds to wait for a network at boot
MAX_BODY = 4096
POLL = 3
DEFAULT_COUNTRY = "NZ"
DNSMASQ_DIR = "/etc/NetworkManager/dnsmasq-shared.d"
DNSMASQ_NAME = "pilnk-captive.conf"
DNSMASQ_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dnsmasq-captive.conf")

COUNTRIES = frozenset((
    "AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ "
    "CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR "
    "GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP "
    "KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT "
    "MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW "
    "SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG "
    "UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW").split())


def _log(msg):
    # Never pass user-supplied secrets here.
    print("pilnk-wifi-setup: " + msg, file=sys.stderr, flush=True)


def _b(s):
    """str (surrogateescape) -> exact bytes."""
    return s.encode("utf-8", "surrogateescape")


def _show(s):
    """SSID str -> displayable text; invalid bytes become U+FFFD."""
    return _b(s).decode("utf-8", "replace")


# ---------------------------------------------------------------- identity
def host_suffix(cpuinfo="/proc/cpuinfo", machine_id="/etc/machine-id"):
    """Same rule as host_suffix() in pilnk-firstboot.sh: last 4 hex of cpuinfo
    Serial, else of machine-id, lowercased. Never from the hostname."""
    s = ""
    try:
        with open(cpuinfo, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^Serial\s*:\s*(.*)$", line)
                if m:
                    s = re.sub(r"[^0-9a-fA-F]", "", m.group(1)); break
    except OSError:
        pass
    if not s:
        try:
            with open(machine_id, encoding="utf-8", errors="replace") as fh:
                s = re.sub(r"[^0-9a-fA-F]", "", fh.read())
        except OSError:
            pass
    return s[-4:].lower() if s else "0000"


# ---------------------------------------------------------------- network state
def is_online(run=subprocess.run, timeout=1):
    """Online = nm-online succeeds AND there is an IPv4 default route (the hotspot
    alone leaves NetworkManager CONNECTED_LOCAL with no default route)."""
    try:
        if run(["nm-online", "-q", "-t", str(timeout)], capture_output=True).returncode != 0:
            return False
        r = run(["ip", "-4", "route", "show", "default"], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def parse_scan(raw):
    """Parse `nmcli -t -f SSID device wifi list` output (bytes or str).
    Terse mode escapes ':' and '\\' with a backslash. Returns unique non-empty
    SSIDs as str (surrogateescape keeps invalid bytes exact)."""
    if isinstance(raw, str):
        raw = _b(raw)
    out, seen = [], set()
    for line in raw.split(b"\n"):
        ssid = re.sub(rb"\\(.)", rb"\1", line.rstrip(b"\r"))
        if not ssid or ssid == b"--" or ssid in seen or len(ssid) > 32:
            continue
        seen.add(ssid)
        out.append(ssid.decode("utf-8", "surrogateescape"))
    return out


def scan(run=subprocess.run):
    try:
        r = run(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "yes"], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_scan(r.stdout or b"") if r.returncode == 0 else []


# ---------------------------------------------------------------- validation
def validate(form):
    errors = []
    country = (form.get("country") or DEFAULT_COUNTRY).strip().upper()
    ssid = form.get("ssid") or ""
    psk = form.get("psk") or ""
    if country not in COUNTRIES:
        errors.append("Pick a valid two-letter country code, for example NZ, AU, US or GB.")
    n = len(_b(ssid))
    if n == 0:
        errors.append("Choose a Wi-Fi network, or type its name.")
    elif n > 32:
        errors.append("That network name is too long (Wi-Fi names are 32 bytes at most).")
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        errors.append("The network name contains characters that can't be used.")
    if psk and not (re.fullmatch(r"[\x20-\x7e]{8,63}", psk) or re.fullmatch(r"[0-9a-fA-F]{64}", psk)):
        errors.append("The Wi-Fi password must be 8 to 63 plain characters (or 64 hex digits), with no special control characters.")
    if errors:
        return None, errors
    return {"country": country, "ssid": ssid, "psk": psk}, []


# ---------------------------------------------------------------- page
_CSS = ("body{font:16px/1.4 system-ui,sans-serif;margin:0;background:#0f172a;color:#e2e8f0}"
        "main{max-width:26rem;margin:0 auto;padding:1.2rem}h1{font-size:1.4rem}"
        "label{display:block;margin:1rem 0 .3rem}select,input,button{width:100%;box-sizing:border-box;"
        "font:inherit;padding:.7rem;border-radius:.5rem;border:1px solid #475569;background:#1e293b;color:inherit}"
        "button{background:#0ea5e9;border:0;color:#fff;margin-top:1.2rem;font-weight:600}"
        ".err{background:#7f1d1d;padding:.7rem;border-radius:.5rem}.note{color:#94a3b8;font-size:.9rem}")


def _doc(body):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>PiLNK Wi-Fi setup</title><style>' + _CSS + '</style></head><body><main>'
            + body + '</main></body></html>').encode("utf-8")


def _switch_text(suffix):
    return ("PiLNK-Setup will disappear. In a minute, open http://pilnk-%s.local:5000 "
            "from your home Wi-Fi to get your pairing code." % html.escape(suffix, quote=True))


def render_page(networks, suffix, error):
    opts = []
    for n in networks:
        raw = n if isinstance(n, bytes) else _b(n)
        opts.append('<option value="%s">%s</option>' % (
            raw.hex(), html.escape(raw.decode("utf-8", "replace"), quote=True)))
    err = '<p class="err">%s</p>' % html.escape(error, quote=True) if error else ""
    body = (
        "<h1>Connect your PiLNK to Wi-Fi</h1>" + err +
        '<form method="post" action="/join">'
        '<label for="country">Country (two-letter code)</label>'
        '<input id="country" name="country" value="NZ" maxlength="2" autocapitalize="characters" required>'
        '<label for="network">Wi-Fi network</label>'
        '<select id="network" name="network"><option value="">Choose a network</option>' + "".join(opts) + "</select>"
        '<label for="ssid_manual">Or type a network name (hidden network)</label>'
        '<input id="ssid_manual" name="ssid_manual" maxlength="32" autocapitalize="none" autocomplete="off">'
        '<label for="psk">Password (leave empty for an open network)</label>'
        '<input id="psk" name="psk" type="password" maxlength="64" autocomplete="off">'
        '<button type="submit">Connect</button></form>'
        '<p class="note">' + _switch_text(suffix) + "</p>")
    return _doc(body)


def render_done(suffix):
    return _doc("<h1>Connecting…</h1><p>" + _switch_text(suffix) + "</p>")


# ---------------------------------------------------------------- join
def join(country, ssid, psk, run=subprocess.run, wait=JOIN_WAIT, hidden=False):
    """Set the Wi-Fi country, then join. Returns (ok, message). The PSK is only ever
    an argv element; it is not logged and not in any message."""
    fail = (False, "Couldn't join %s — check the password" % _show(ssid))
    try:
        run(["raspi-config", "nonint", "do_wifi_country", country], capture_output=True)
        argv = ["nmcli", "-w", str(wait), "device", "wifi", "connect", ssid]
        if psk:
            argv += ["password", psk]
        if hidden:
            argv += ["hidden", "yes"]
        r = run(argv, capture_output=True, timeout=wait + 15)
        good = r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        good = False
    if good:
        return True, "Connected to %s" % _show(ssid)
    try:
        run(["nmcli", "connection", "delete", "id", ssid], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        pass
    return fail


# ---------------------------------------------------------------- server
class State:
    def __init__(self, suffix, networks=(), now=time.monotonic):
        self.suffix, self.networks, self.now = suffix, list(networks), now
        self.error = None
        self.last_post = now()
        self._pending = None
        self._lock = threading.Lock()

    def submit(self, country, ssid, psk, hidden):
        with self._lock:
            self._pending = (country, ssid, psk, hidden)
            self.last_post = self.now()

    def take_pending(self):
        with self._lock:
            p, self._pending = self._pending, None
            return p


def _parse_form(body):
    q = urllib.parse.parse_qs(body.decode("latin-1"), keep_blank_values=True,
                              encoding="utf-8", errors="surrogateescape")
    return {k: v[0] for k, v in q.items()}


def make_handler(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "pilnk-wifi"

        def log_message(self, *a):
            return

        def _send(self, code, body=b"", extra=()):
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _home(self):
            self._send(302, b"", [("Location", "/")])

        def do_GET(self):
            if self.path.split("?", 1)[0] == "/":
                self._send(200, render_page(state.networks, state.suffix, state.error))
            else:
                self._home()

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/join":
                return self._home()
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                n = -1
            if n < 0:
                return self._send(400, b"bad request", [("Connection", "close")])
            if n > MAX_BODY:
                self.close_connection = True
                return self._send(413, b"too large", [("Connection", "close")])
            form = _parse_form(self.rfile.read(n))
            manual = form.get("ssid_manual", "")
            hidden = bool(manual)
            ssid, errs = manual, []
            if not hidden:
                try:
                    ssid = bytes.fromhex(form.get("network", "")).decode("utf-8", "surrogateescape")
                except ValueError:
                    ssid, errs = "", ["Choose a Wi-Fi network, or type its name."]
            clean, verrs = validate({"country": form.get("country"), "ssid": ssid, "psk": form.get("psk", "")})
            if clean is None or errs:
                return self._send(200, render_page(state.networks, state.suffix, " ".join(errs or verrs)))
            state.error = None
            state.submit(clean["country"], clean["ssid"], clean["psk"], hidden)
            self._send(200, render_done(state.suffix))
    return Handler


def make_server(state, host=HOST, port=PORT):
    srv = http.server.ThreadingHTTPServer((host, port), make_handler(state))
    srv.daemon_threads = True
    return srv


# ---------------------------------------------------------------- hotspot / main
def _quiet(run, argv):
    try:
        return run(argv, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return None


def start_hotspot(state, run):
    """Delete any old hotspot, scan while the radio is free, then bring the hotspot up."""
    _quiet(run, ["nmcli", "connection", "delete", "id", HOTSPOT_CON])
    state.networks = scan(run)
    _quiet(run, ["nmcli", "device", "wifi", "hotspot", "ifname", "wlan0", "con-name", HOTSPOT_CON,
                 "ssid", "PiLNK-Setup-" + state.suffix])
    # `hotspot` without a password makes up a WPA key; the setup network must be open.
    _quiet(run, ["nmcli", "connection", "modify", HOTSPOT_CON, "remove", "802-11-wireless-security"])
    _quiet(run, ["nmcli", "connection", "up", HOTSPOT_CON])
    state.last_post = state.now()


def main(run=subprocess.run, sleep=time.sleep, now=time.monotonic,
         cpuinfo="/proc/cpuinfo", machine_id="/etc/machine-id",
         dnsmasq_dir=DNSMASQ_DIR, dnsmasq_src=DNSMASQ_SRC,
         host=HOST, port=PORT, on_ready=None):
    if is_online(run, BOOT_WAIT):
        return 0
    suffix = host_suffix(cpuinfo, machine_id)
    state = State(suffix, now=now)
    dropin = os.path.join(dnsmasq_dir, DNSMASQ_NAME)
    server = thread = None
    try:
        os.makedirs(dnsmasq_dir, exist_ok=True)
        shutil.copyfile(dnsmasq_src, dropin)
        start_hotspot(state, run)
        server = make_server(state, host, port)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if on_ready:
            on_ready(server, state)
        while True:
            sleep(POLL)
            req = state.take_pending()
            if req:
                country, ssid, psk, hidden = req
                ok_, msg = join(country, ssid, psk, run=run, hidden=hidden)
                del psk, req
                if ok_ and is_online(run, JOIN_WAIT):
                    return 0
                state.error = msg if not ok_ else "Joined, but there was no internet. Check the router and try again."
                start_hotspot(state, run)
            elif is_online(run, 1):
                return 0
            elif now() - state.last_post >= IDLE_RESTART:
                start_hotspot(state, run)
    finally:
        if server:
            server.shutdown()
            server.server_close()
            thread.join(5)
        try:
            os.remove(dropin)
        except OSError:
            pass
        _quiet(run, ["nmcli", "connection", "delete", "id", HOTSPOT_CON])


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    sys.exit(main())

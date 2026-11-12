#!/usr/bin/env python3
"""PiLNK Wi-Fi setup hotspot. Stdlib only; talks to NetworkManager through nmcli.

Run at every boot. If the Pi is online (nm-online AND a default route) it exits 0.
Otherwise: scan, open the hotspot PiLNK-Setup-XXXX, serve a one-page form on
10.42.0.1:80, and join the network the user picks. Exits 0 once online.
tar1090's lighttpd holds *:80, so it is stopped while the page runs and started
again on exit. Every 30 idle minutes the hotspot steps aside so NetworkManager
can rejoin a saved Wi-Fi network (a router that came back after a power cut).

Rules: nmcli/raspi-config get argv lists (never a shell); the Wi-Fi password is
never logged, printed or put in an error message; saved connections are never read.
Events (never an SSID, a password or nmcli's own text) also go to EVENT_LOG, which
survives a reboot; the journal on the image does not.
"""
import errno
import html, http.server, os, re, shutil, signal, subprocess, sys, threading, time
import urllib.parse

HOST, PORT = "10.42.0.1", 80
HOTSPOT_CON = "PiLNK-Hotspot"
IDLE_RESTART = 1800          # seconds with no POST before trying saved Wi-Fi, then the hotspot again
AUTOCONNECT_WAIT = 45        # seconds for NetworkManager to bring up a saved Wi-Fi network
HOTSPOT_RETRIES = 3   # times to bring the hotspot up again when the bind says the address is not there
BIND_BACKOFF = (2, 4, 8, 15, 30, 60)   # seconds between tries when port 80 is busy
JOIN_WAIT = 30               # seconds to wait for a join
BOOT_WAIT = 90               # seconds to wait for a network at boot
MAX_BODY = 4096
POLL = 3
SETTLE = 3                   # seconds for the radio to leave AP mode and rescan before a join
REQ_TIMEOUT = 10             # per-connection socket timeout so a short body can't hang a thread
DEFAULT_COUNTRY = "NZ"
DNSMASQ_DIR = "/etc/NetworkManager/dnsmasq-shared.d"
DNSMASQ_NAME = "pilnk-captive.conf"
DNSMASQ_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dnsmasq-captive.conf")
EVENT_LOG = "/var/log/pilnk-wifi-setup.log"
EVENT_LOG_MAX = 256 * 1024     # bytes; then it rolls to .1

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


_UNSAFE = re.compile(r"[^A-Za-z0-9_./:-]")


def _event(name, **kv):
    """One line in EVENT_LOG: time, pid, event name, key=value pairs. Values are
    numbers or short fixed words, cleaned to [A-Za-z0-9_./:-] and 40 chars. Never
    pass an SSID, a password, form fields or nmcli output here. Never raises."""
    parts = [time.strftime("%Y-%m-%d %H:%M:%S"), "pid=%d" % os.getpid(), name]
    for k, v in kv.items():
        if isinstance(v, bool):
            v = int(v)
        if isinstance(v, float):
            v = "%.1f" % v
        parts.append("%s=%s" % (k, _UNSAFE.sub("?", str(v))[:40]))
    line = " ".join(parts)
    try:
        try:
            if os.path.getsize(EVENT_LOG) > EVENT_LOG_MAX:
                os.replace(EVENT_LOG, EVENT_LOG + ".1")
        except OSError:
            pass
        fd = os.open(EVENT_LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _join_reason(err):
    """nmcli's error text -> one fixed word. The text itself is never kept: it can
    quote the network name."""
    t = (err or b"")
    if isinstance(t, str):
        t = t.encode("utf-8", "replace")
    t = t.lower()
    if b"secrets were required" in t or b"802-11-wireless-security" in t or b"psk" in t:
        return "secrets"
    if b"no network with ssid" in t:
        return "not_found"
    if b"timeout" in t or b"timed out" in t:
        return "nm_timeout"
    return "other"


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
    """Parse `nmcli -t -f SSID-HEX device wifi list` output (bytes or str): one hex
    string per line (no escaping to undo). Returns unique SSIDs of 1-32 bytes as str
    (surrogateescape keeps invalid bytes exact); hidden (empty) SSIDs are skipped."""
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "replace")
    out, seen = [], set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or not re.fullmatch(r"(?:[0-9a-fA-F]{2})+", line):
            continue
        ssid = bytes.fromhex(line)
        if len(ssid) > 32 or ssid in seen:
            continue
        seen.add(ssid)
        out.append(ssid.decode("utf-8", "surrogateescape"))
    return out


def scan(run=subprocess.run):
    try:
        r = run(["nmcli", "-t", "-f", "SSID-HEX", "device", "wifi", "list", "--rescan", "yes"], capture_output=True)
    except (OSError, subprocess.SubprocessError):
        return []
    return parse_scan(r.stdout or b"") if r.returncode == 0 else []


# ---------------------------------------------------------------- validation
def validate(form):
    errors = []
    country = (form.get("country") or DEFAULT_COUNTRY).strip().upper()
    if country == "UK":              # the common name; ISO 3166 says GB
        country = "GB"
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


def render_page(networks, suffix, error, country=DEFAULT_COUNTRY):
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
        '<input id="country" name="country" value="%s" maxlength="2" autocapitalize="characters" required>'
        % html.escape(country, quote=True) +
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
def join(country, ssid, psk, run=subprocess.run, wait=JOIN_WAIT, hidden=False, detail=None):
    """Set the Wi-Fi country, then join. Returns (ok, message). The PSK is only ever
    an argv element; it is not logged and not in any message. If detail is a dict it
    gets rc, reason (a fixed word) and secs, for the event log."""
    fail = (False, "Couldn't join %s — check the password" % _show(ssid))
    info = detail if detail is not None else {}
    info.update(rc=-1, reason="error", secs=0.0)
    t0 = time.monotonic()
    try:
        run(["raspi-config", "nonint", "do_wifi_country", country], capture_output=True)
        argv = ["nmcli", "-w", str(wait), "device", "wifi", "connect", ssid]
        if psk:
            argv += ["password", psk]
        if hidden:
            argv += ["hidden", "yes"]
        r = run(argv, capture_output=True, timeout=wait + 15)
        good = r.returncode == 0
        info["rc"] = r.returncode
        info["reason"] = "ok" if good else _join_reason(r.stderr)
    except subprocess.TimeoutExpired:
        good = False
        info["reason"] = "timeout"
    except (OSError, subprocess.SubprocessError):
        good = False
    info["secs"] = round(time.monotonic() - t0, 1)
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
        self.country = DEFAULT_COUNTRY
        self.last_post = now()
        self.hotspots = 0
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
        timeout = REQ_TIMEOUT

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

        def _who(self):
            ua = self.headers.get("User-Agent", "") or ""
            if re.search(r"CaptiveNetworkSupport|wispr|Dalvik|captive|NetworkProbe", ua, re.I):
                kind = "probe"
            elif ua.startswith("Mozilla"):
                kind = "browser"
            else:
                kind = "other"
            ip = str(self.client_address[0]) if self.client_address else ""
            return kind, ip.rsplit(".", 1)[-1]

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            ua, client = self._who()
            if path == "/":
                _event("page", path="/", error=bool(state.error), ua=ua, client=client)
                self._send(200, render_page(state.networks, state.suffix, state.error, state.country))
            else:
                _event("redirect", path=path, ua=ua, client=client)
                self._home()

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/join":
                return self._home()
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                n = -1
            if n < 0:
                _event("post_join", result="bad_request")
                return self._send(400, b"bad request", [("Connection", "close")])
            if n > MAX_BODY:
                _event("post_join", result="too_large")
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
                _event("post_join", result="invalid", errors=len(errs or verrs))
                return self._send(200, render_page(state.networks, state.suffix, " ".join(errs or verrs), state.country))
            _event("post_join", result="accepted", hidden=hidden, ssid_bytes=len(_b(clean["ssid"])),
                   has_psk=bool(clean["psk"]), country=clean["country"])
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


def saved_country(run):
    """The Wi-Fi country already set on this card (raspi-config, else the global
    regulatory domain from `iw reg get`), or None when none is set ("00")."""
    r = _quiet(run, ["raspi-config", "nonint", "get_wifi_country"])
    if r is not None and r.returncode == 0:
        c = (r.stdout or b"").decode("ascii", "replace").strip().upper()
        if c in COUNTRIES:
            return c
    r = _quiet(run, ["iw", "reg", "get"])
    if r is not None and r.returncode == 0:
        for line in (r.stdout or b"").decode("ascii", "replace").splitlines():
            m = re.match(r"^country ([A-Z0-9]{2}):", line)
            if m:                       # the first one is the global domain
                return m.group(1) if m.group(1) in COUNTRIES else None
    return None


def prepare_radio(run):
    """A fresh card has wlan0 rfkill-blocked until a country is set. Keeps the
    country already set on the card; NZ only when there is none. Failures are
    ignored. Returns the country used."""
    country = saved_country(run) or DEFAULT_COUNTRY
    _quiet(run, ["rfkill", "unblock", "wifi"])
    _quiet(run, ["raspi-config", "nonint", "do_wifi_country", country])
    return country


def start_hotspot(state, run):
    """Delete any old hotspot, scan while the radio is free, then bring up an open AP."""
    _quiet(run, ["nmcli", "connection", "delete", "id", HOTSPOT_CON])
    state.networks = scan(run)
    # Created directly as an open AP (no wifi-sec setting), so there is never a WPA key.
    _quiet(run, ["nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", HOTSPOT_CON,
                 "autoconnect", "no", "ssid", "PiLNK-Setup-" + state.suffix,
                 "802-11-wireless.mode", "ap", "802-11-wireless.band", "bg", "ipv4.method", "shared"])
    r = _quiet(run, ["nmcli", "connection", "up", HOTSPOT_CON])
    state.hotspots += 1
    _event("hotspot_up", n=state.hotspots, networks=len(state.networks), rc=r.returncode if r is not None else -1)
    state.last_post = state.now()


def _service_active(run, unit):
    r = _quiet(run, ["systemctl", "is-active", "--quiet", unit])
    return r is not None and r.returncode == 0


def free_port80(run):
    """tar1090's lighttpd listens on *:80, and a wildcard listener makes our bind
    to 10.42.0.1:80 fail. Stop it while the page runs. True if it was stopped."""
    if _service_active(run, "lighttpd"):
        _log("stopping lighttpd (tar1090) while the setup page needs port 80")
        _quiet(run, ["systemctl", "stop", "lighttpd"])
        return True
    return False


def try_saved_wifi(run, settle):
    """Idle cycle: remove the hotspot so wlan0 is free, rescan, and ask
    NetworkManager to bring up the best saved network for wlan0 itself (saved
    connections are never read here). True once online."""
    _quiet(run, ["nmcli", "connection", "delete", "id", HOTSPOT_CON])
    _quiet(run, ["nmcli", "device", "wifi", "rescan"])
    settle(SETTLE)
    r = _quiet(run, ["nmcli", "-w", str(AUTOCONNECT_WAIT), "device", "connect", "wlan0"])
    if r is not None and r.returncode == 0:
        return is_online(run, JOIN_WAIT)
    return is_online(run, 1)        # nothing saved or in range; maybe a cable came up


def _cleanup(run, dropin):
    try:
        os.remove(dropin)
    except OSError:
        pass
    _quiet(run, ["nmcli", "connection", "delete", "id", HOTSPOT_CON])


def main(run=subprocess.run, sleep=time.sleep, now=time.monotonic,
         cpuinfo="/proc/cpuinfo", machine_id="/etc/machine-id",
         dnsmasq_dir=DNSMASQ_DIR, dnsmasq_src=DNSMASQ_SRC,
         host=HOST, port=PORT, on_ready=None, settle=time.sleep):
    dropin = os.path.join(dnsmasq_dir, DNSMASQ_NAME)
    _event("start")
    if is_online(run, BOOT_WAIT):
        _cleanup(run, dropin)        # a power cut can leave a stale drop-in / profile behind
        _event("online", via="boot")
        return 0
    country = prepare_radio(run)
    _event("radio", country=country)
    suffix = host_suffix(cpuinfo, machine_id)
    state = State(suffix, now=now)
    state.country = country
    server = thread = None
    lighttpd_stopped = False
    try:
        os.makedirs(dnsmasq_dir, exist_ok=True)
        shutil.copyfile(dnsmasq_src, dropin)
        start_hotspot(state, run)
        lighttpd_stopped = free_port80(run)
        tries = 0
        hotspot_tries = 0
        while server is None:
            try:
                server = make_server(state, host, port)
            except OSError as e:
                if e.errno != errno.EADDRINUSE:
                    # e.g. EADDRNOTAVAIL: the AP (10.42.0.1) never came up. Waiting
                    # cannot fix that: raise the hotspot again, a few times, then
                    # re-raise so Restart=on-failure starts over.
                    hotspot_tries += 1
                    _event("bind_error", errno=e.errno, tries=hotspot_tries)
                    if hotspot_tries > HOTSPOT_RETRIES:
                        raise
                    wait = BIND_BACKOFF[min(hotspot_tries - 1, len(BIND_BACKOFF) - 1)]
                    _log("cannot serve the setup page on %s:%s (%s); bringing the hotspot up again in %ss"
                         % (host, port, e.strerror or e, wait))
                    sleep(wait)
                    start_hotspot(state, run)
                    continue
                # loud, and the hotspot stays up: no tear-down / restart loop
                wait = BIND_BACKOFF[min(tries, len(BIND_BACKOFF) - 1)]
                _log("cannot serve the setup page on %s:%s (%s); retrying in %ss"
                     % (host, port, e.strerror or e, wait))
                tries += 1
                _event("bind_busy", tries=tries)
                sleep(wait)
                if is_online(run, 1):
                    _event("online", via="bind_wait")
                    return 0
                lighttpd_stopped = free_port80(run) or lighttpd_stopped
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        if on_ready:
            on_ready(server, state)
        while True:
            sleep(POLL)
            req = state.take_pending()
            if req:
                country, ssid, psk, hidden = req
                _event("join_start", hidden=hidden)
                # wlan0 can't scan or join while it is the AP: drop it, rescan, then join.
                _quiet(run, ["nmcli", "connection", "down", HOTSPOT_CON])
                _quiet(run, ["nmcli", "device", "wifi", "rescan"])
                settle(SETTLE)
                info = {}
                ok_, msg = join(country, ssid, psk, run=run, hidden=hidden, detail=info)
                del psk, req
                _event("join_result", ok=ok_, rc=info.get("rc"), reason=info.get("reason"), secs=info.get("secs"))
                if ok_ and is_online(run, JOIN_WAIT):
                    _event("online", via="join")
                    return 0
                state.error = msg if not ok_ else "Joined, but there was no internet. Check the router and try again."
                _event("error_set", kind="join_failed" if not ok_ else "no_internet")
                start_hotspot(state, run)
            elif is_online(run, 1):
                _event("online", via="poll")
                return 0
            elif now() - state.last_post >= IDLE_RESTART:
                _event("idle_cycle")
                if try_saved_wifi(run, settle):
                    _event("online", via="saved_wifi")
                    return 0
                start_hotspot(state, run)
    finally:
        _event("stop")
        if server:
            server.shutdown()
            server.server_close()
            thread.join(5)
        _cleanup(run, dropin)
        if lighttpd_stopped:
            _quiet(run, ["systemctl", "--no-block", "start", "lighttpd"])


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    sys.exit(main())

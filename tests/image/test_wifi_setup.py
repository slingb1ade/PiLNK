#!/usr/bin/env python3
"""Task 4: Wi-Fi setup hotspot (image/wifi-setup/pilnk_wifi_setup.py). Stdlib only;
nmcli / nm-online / ip / raspi-config are always faked via the injected run=."""
import http.client, importlib.util, io, logging, os, re, subprocess, sys, tempfile, threading, urllib.parse
from html.parser import HTMLParser

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WS = os.path.join(REPO, "image", "wifi-setup")
FAILS = 0

def ok(name, cond):
    global FAILS
    if not cond: FAILS += 1
    print(("PASS: " if cond else "FAIL: ") + name)

try:
    spec = importlib.util.spec_from_file_location("pilnk_wifi_setup", os.path.join(WS, "pilnk_wifi_setup.py"))
    W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)
except Exception as e:
    print("FAIL: module image/wifi-setup/pilnk_wifi_setup.py loads (%s)" % type(e).__name__)
    sys.exit(1)

# Event log (#549): every test run writes to a temp file, never /var/log.
EVLOG_DIR = tempfile.mkdtemp()
W.EVENT_LOG = os.path.join(EVLOG_DIR, "wifi-setup.log")

def rd(path):
    with open(path, encoding="utf-8") as fh: return fh.read()

def wr(path, text):
    with open(path, "w", encoding="utf-8") as fh: fh.write(text)

def cp(args, rc=0, out=b"", err=b""):
    return subprocess.CompletedProcess(args, rc, out, err)

class Fake:
    """Records argv + kwargs; `rules` is a function(argv)->CompletedProcess|None."""
    def __init__(self, rules=None): self.calls = []; self.kw = []; self.rules = rules
    def __call__(self, argv, **kw):
        self.calls.append(list(argv)); self.kw.append(kw)
        r = self.rules(list(argv)) if self.rules else None
        if r is None and list(argv[:2]) == ["systemctl", "is-active"]:
            return cp(argv, 3)      # the default fake machine runs no lighttpd
        return r if r is not None else cp(argv)
    def has(self, *prefix): return any(c[:len(prefix)] == list(prefix) for c in self.calls)
    def idx(self, *prefix):
        for i, c in enumerate(self.calls):
            if c[:len(prefix)] == list(prefix): return i
        return -1

# ---------------- validate ----------------
good, errs = W.validate({"country": "NZ", "ssid": "Home", "psk": "password1"})
ok("validate accepts a normal form", good == {"country": "NZ", "ssid": "Home", "psk": "password1"} and errs == [])
g, e = W.validate({"ssid": "Home", "psk": ""})
ok("validate: country defaults to NZ, empty PSK (open) ok", g and g["country"] == "NZ" and g["psk"] == "" and not e)
g, e = W.validate({"country": "au", "ssid": "x" * 32, "psk": "a" * 63})
ok("validate: 32-byte SSID, 63-char PSK, lowercase country ok", g and g["country"] == "AU")
g, e = W.validate({"ssid": "a", "psk": "ab" * 32})
ok("validate: 64 hex PSK ok", g is not None and not e)
g, e = W.validate({"ssid": "a", "psk": "g" * 64})
ok("validate: 64 non-hex PSK rejected", g is None and e)
g, e = W.validate({"ssid": "é" * 16, "psk": ""})
ok("validate: SSID length counted in bytes (16 x 2-byte = 32 ok)", g is not None)
g, e = W.validate({"ssid": "é" * 17, "psk": ""})
ok("validate: 34-byte SSID rejected", g is None and e)
for label, form in [("empty SSID", {"ssid": "", "psk": ""}),
                    ("33-byte SSID", {"ssid": "x" * 33, "psk": ""}),
                    ("7-char PSK", {"ssid": "a", "psk": "1234567"}),
                    ("64-char PSK", {"ssid": "a", "psk": "z" * 65}),
                    ("control char in PSK", {"ssid": "a", "psk": "pass\x07word1"}),
                    ("control char in SSID", {"ssid": "a\nb", "psk": ""}),
                    ("NUL in SSID", {"ssid": "a\x00b", "psk": ""}),
                    ("country XX", {"country": "XX", "ssid": "a", "psk": ""}),
                    ("country too long", {"country": "NZL", "ssid": "a", "psk": ""})]:
    g, e = W.validate(form)
    ok("validate rejects " + label + " with a plain-English error",
       g is None and len(e) >= 1 and all(isinstance(m, str) and len(m) > 8 for m in e))
g, e = W.validate({"ssid": "x" * 33, "psk": "1234567", "country": "XX"})
ok("validate reports every problem", g is None and len(e) == 3)
g, e = W.validate({"ssid": "a", "psk": "12345678\xe9"})
ok("validate: non-ASCII PSK rejected", g is None and e)
secret = "Sup3rSecretPSK!"
g, e = W.validate({"ssid": "a", "psk": secret + "\x01"})
ok("validate errors never contain the PSK", all(secret not in m for m in e))

# ---------------- render_page ----------------
hostile = ["<script>alert(1)</script>", "a\"b'c", b"\xff\xfe".decode("utf-8", "surrogateescape"),
           "x" * 32, "we:ird\\name"]
page = W.render_page(hostile, "7f3a", None).decode("utf-8")
ok("render_page: no raw <script", "<script" not in page.lower())
ok("render_page: script SSID shown escaped", "&lt;script&gt;alert(1)&lt;/script&gt;" in page)
ok("render_page: quotes escaped (&quot; and &#x27;)", "a&quot;b&#x27;c" in page)
ok("render_page: invalid bytes shown as U+FFFD", "��" in page)
class Opts(HTMLParser):
    def __init__(s): super().__init__(); s.vals = []; s.forms = 0; s.ext = []
    def handle_starttag(s, tag, a):
        a = dict(a)
        if tag == "option" and a.get("value"): s.vals.append(a["value"])
        if tag == "form": s.forms += 1
        for k in ("src", "href"):
            if k in a and tag != "form": s.ext.append(a[k])
        if tag in ("link", "script", "img", "iframe"): s.ext.append(tag)
p = Opts(); p.feed(page)
decoded = [bytes.fromhex(v) for v in p.vals if re.fullmatch(r"[0-9a-f]+", v)]
want = [h.encode("utf-8", "surrogateescape") for h in hostile]
ok("render_page: every option value round-trips to the exact SSID bytes", decoded == want)
ok("render_page: exactly one form, no external assets", p.forms == 1 and p.ext == [])
ok("render_page: PiLNK-Setup will disappear. present", "PiLNK-Setup will disappear." in page)
ok("render_page: dashboard URL for suffix 7f3a", "http://pilnk-7f3a.local:5000" in page)
ok("render_page: inline CSS, viewport meta", "<style>" in page and "viewport" in page)
page2 = W.render_page([], "7f3a", "Couldn't join <b>X</b> — check the password").decode()
ok("render_page: error message escaped", "&lt;b&gt;X&lt;/b&gt;" in page2 and "<b>X</b>" not in page2)
ok("render_page: suffix is escaped", b"<i>" not in W.render_page([], "<i>", None))
ok("render_page returns bytes", isinstance(W.render_page([], "7f3a", None), bytes))
done = W.render_done("7f3a").decode()
ok("render_done has the exact before-switch copy",
   "PiLNK-Setup will disappear. In a minute, open http://pilnk-7f3a.local:5000 from your home Wi-Fi to get your pairing code." in done)

# ---------------- scan parsing (nmcli -t -f SSID-HEX) ----------------
def hx(*items): return b"\n".join(x.hex().encode() if x else b"" for x in items) + b"\n"
raw = hx(b"Home", b"we:ird\\name", b"", b"<script>x", b"Home", b"\xff\xfe", b"A:B:C", b"a\nb", b"x" * 33)
nets = W.parse_scan(raw)
ok("parse_scan decodes hex to exact bytes", "we:ird\\name" in nets and "A:B:C" in nets and "a\nb" in nets)
ok("parse_scan drops empty/duplicate/over-32-byte, keeps invalid bytes",
   nets.count("Home") == 1 and "" not in nets and "x" * 33 not in nets
   and b"\xff\xfe".decode("utf-8", "surrogateescape") in nets)
f = Fake(lambda a: cp(a, 0, raw) if a[:2] == ["nmcli", "-t"] else None)
ok("scan runs nmcli -t -f SSID-HEX device wifi list as argv",
   W.scan(run=f) == nets and f.calls[0][:7] == ["nmcli", "-t", "-f", "SSID-HEX", "device", "wifi", "list"] and f.kw[0].get("shell") is not True)
ok("scan tolerates a failing nmcli", W.scan(run=Fake(lambda a: cp(a, 1))) == [])

# ---------------- join ----------------
f = Fake()
res = W.join("NZ", "Home", "password1", run=f)
ok("join returns (True, ...) on success", res[0] is True)
ok("join: raspi-config country first", f.calls[0] == ["raspi-config", "nonint", "do_wifi_country", "NZ"])
ci = f.idx("nmcli"); conn = [c for c in f.calls if "connect" in c][0]
ok("join: nmcli device wifi connect <ssid> password <psk> as argv",
   conn[0] == "nmcli" and conn[conn.index("device"):conn.index("device") + 4] == ["device", "wifi", "connect", "Home"]
   and conn[-2:] == ["password", "password1"])
ok("join never uses shell=True", all(k.get("shell") is not True for k in f.kw))
ok("join waits 30 s by default", "30" in conn)
f = Fake(); W.join("NZ", "OpenCafe", "", run=f)
conn = [c for c in f.calls if "connect" in c][0]
ok("join: open network omits password", "password" not in conn and conn[-1] == "OpenCafe")
f = Fake(lambda a: cp(a, 4) if "connect" in a and a[0] == "nmcli" else None)
logbuf = io.StringIO(); h = logging.StreamHandler(logbuf); logging.getLogger().addHandler(h)
old_out, old_err = sys.stdout, sys.stderr; sys.stdout = so = io.StringIO(); sys.stderr = se = io.StringIO()
res = W.join("NZ", "Home", secret, run=f)
sys.stdout, sys.stderr = old_out, old_err; logging.getLogger().removeHandler(h)
ok("join failure returns (False, exact copy)", res == (False, "Couldn't join Home — check the password"))
ok("join failure deletes that connection", f.has("nmcli", "connection", "delete", "id", "Home"))
ok("failed join: PSK is in no log/stdout/stderr output and not in the message",
   secret not in logbuf.getvalue() + so.getvalue() + se.getvalue() + res[1])
def boom(a, **kw):
    if "connect" in a and a[0] == "nmcli": raise subprocess.TimeoutExpired(a, 30)
    return cp(a)
f2 = Fake(); f2.rules = None
calls = []
def run_boom(argv, **kw):
    calls.append(list(argv)); return boom(argv)
res = W.join("NZ", "Home", secret, run=run_boom)
ok("join: a timeout is a failure with the same copy, no PSK",
   res == (False, "Couldn't join Home — check the password") and secret not in repr(res))
ok("join: country failure does not leak or crash",
   W.join("NZ", "Home", secret, run=Fake(lambda a: cp(a, 1) if a[0] == "raspi-config" else None))[0] in (True, False))
res = W.join("NZ", "<b>" + b"\xff".decode("utf-8", "surrogateescape"), "", run=Fake(lambda a: cp(a, 1) if "connect" in a else None))
ok("join failure message shows invalid SSID bytes as U+FFFD", res[1] == "Couldn't join <b>\ufffd \u2014 check the password")

# ---------------- suffix ----------------
d = tempfile.mkdtemp()
cpu = os.path.join(d, "cpuinfo"); mid = os.path.join(d, "machine-id")
wr(cpu, "processor\t: 0\nHardware\t: BCM2711\nSerial\t\t: 10000000abcd7f3a\nModel\t\t: Pi 4\n")
wr(mid, "0123456789abcdef0123456789abCDEF\n")
ok("suffix from cpuinfo Serial 10000000abcd7f3a is 7f3a", W.host_suffix(cpu, mid) == "7f3a")
wr(cpu, "Serial : 10000000ABCD7F3A\n")
ok("suffix is lowercased", W.host_suffix(cpu, mid) == "7f3a")
wr(cpu, "processor : 0\n")
ok("suffix falls back to machine-id last 4 hex, lowercased", W.host_suffix(cpu, mid) == "cdef")
ok("suffix falls back when cpuinfo is missing", W.host_suffix(os.path.join(d, "none"), mid) == "cdef")
ok("suffix never reads hostname (no gethostname/uname use)",
   "gethostname" not in rd(os.path.join(WS, "pilnk_wifi_setup.py")))

# ---------------- online semantics ----------------
def net(nm_rc, route):
    return Fake(lambda a: cp(a, nm_rc) if a[0] == "nm-online" else (cp(a, 0, b"default via 192.168.1.1 dev wlan0\n" if route else b"") if a[0] == "ip" else None))
ok("online: nm-online ok + default route", W.is_online(net(0, True), 5) is True)
ok("online: nm-online ok but NO default route is offline", W.is_online(net(0, False), 5) is False)
ok("online: nm-online fails is offline", W.is_online(net(1, True), 5) is False)
f = net(0, True); W.is_online(f, 90)
ok("online uses nm-online -q -t N and ip -4 route show default",
   f.calls[0] == ["nm-online", "-q", "-t", "90"] and f.calls[1] == ["ip", "-4", "route", "show", "default"])

# ---------------- HTTP server ----------------
class State(W.State):
    pass
def start_server(**kw):
    st = W.State(suffix="7f3a", networks=["Home", "Cafe"], now=kw.get("now", lambda: 0.0))
    srv = W.make_server(st, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    return st, srv, t
def stop(srv, t): srv.shutdown(); srv.server_close(); t.join(5)
def req(srv, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse(); data = r.read(); h = dict(r.getheaders()); c.close()
    return r.status, h, data
st, srv, t = start_server()
s, h, b = req(srv, "GET", "/")
ok("GET / serves the form", s == 200 and b"<form" in b and b"Home" in b)
for path in ("/etc/passwd", "/../../etc/shadow", "/join", "/generate_204", "/hotspot-detect.html"):
    s, h, b = req(srv, "GET", path)
    ok("GET %s -> 302 to /" % path, s == 302 and h.get("Location") == "/")
s, h, b = req(srv, "POST", "/etc/passwd", body=b"x=1")
ok("POST to an unknown path -> 302 to /", s == 302 and h.get("Location") == "/")
s, h, b = req(srv, "POST", "/join", body=b"a" * 5000, headers={"Content-Type": "application/x-www-form-urlencoded"})
ok("body over 4 KB -> 413", s == 413)
ok("413 did not queue a join", st.take_pending() is None)
s, h, b = req(srv, "POST", "/join", body=b"a" * 4096)
ok("body of exactly 4 KB is accepted (not 413)", s != 413)
body = urllib.parse.urlencode({"country": "NZ", "network": b"Home".hex(), "ssid_manual": "", "psk": "password1"})
s, h, b = req(srv, "POST", "/join", body=body.encode(), headers={"Content-Type": "application/x-www-form-urlencoded"})
ok("valid POST -> 200 with the before-switch copy",
   s == 200 and b"PiLNK-Setup will disappear. In a minute, open http://pilnk-7f3a.local:5000" in b)
ok("valid POST queues the join with exact values", st.take_pending() == ("NZ", "Home", "password1", False))
ok("pending is consumed once", st.take_pending() is None)
hexssid = (b"\xff\xfe").hex()
body = urllib.parse.urlencode({"country": "NZ", "network": hexssid, "psk": ""})
req(srv, "POST", "/join", body=body.encode())
p = st.take_pending()
ok("POST decodes hex SSID to exact bytes", p is not None and p[1].encode("utf-8", "surrogateescape") == b"\xff\xfe")
body = urllib.parse.urlencode({"country": "NZ", "network": "", "ssid_manual": "Hid:den", "psk": "password1"})
req(srv, "POST", "/join", body=body.encode())
ok("typed (hidden) SSID is used verbatim and flagged hidden", st.take_pending() == ("NZ", "Hid:den", "password1", True))
body = urllib.parse.urlencode({"country": "XX", "network": b"Home".hex(), "psk": "short"})
s, h, b = req(srv, "POST", "/join", body=body.encode())
ok("invalid POST re-renders the form with errors (200, nothing queued)",
   s == 200 and b"<form" in b and st.take_pending() is None and b"password1" not in b)
body = urllib.parse.urlencode({"country": "NZ", "network": "zz", "psk": ""})
s, h, b = req(srv, "POST", "/join", body=body.encode())
ok("bad hex network value is an error, not a crash", s == 200 and st.take_pending() is None)
s, h, b = req(srv, "POST", "/join", body=b"country=NZ")
ok("missing SSID is an error", s == 200 and st.take_pending() is None and b"<form" in b)
st.error = "Couldn't join Home — check the password"
s, h, b = req(srv, "GET", "/")
ok("GET / after a failed join shows the error", "Couldn&#x27;t join Home".encode() in b or "Couldn't join Home".encode() in b)
stop(srv, t)
src = rd(os.path.join(WS, "pilnk_wifi_setup.py"))
ok("log_message is silenced", re.search(r"def log_message\(self[^)]*\):\s*\n\s+(return|pass)", src) is not None)
ok("server default binds the hotspot address only", W.HOST == "10.42.0.1" and W.PORT == 80)

# ---------------- main ----------------
class Env:
    def __init__(self):
        self.d = tempfile.mkdtemp()
        self.dns_dir = os.path.join(self.d, "dnsmasq-shared.d"); os.makedirs(self.dns_dir)
        self.cpu = os.path.join(self.d, "cpuinfo"); wr(self.cpu, "Serial : 10000000abcd7f3a\n")
        self.mid = os.path.join(self.d, "mid"); wr(self.mid, "00ff\n")
        self.dns_snapshots = []
    def kw(self, run, **extra):
        k = dict(run=run, sleep=lambda s: None, now=extra.pop("now", None) or (lambda: 0.0),
                 cpuinfo=self.cpu, machine_id=self.mid, dnsmasq_dir=self.dns_dir,
                 dnsmasq_src=os.path.join(WS, "dnsmasq-captive.conf"), host="127.0.0.1", port=0,
                 settle=lambda s: None)
        k.update(extra); return k

e = Env()
f = net(0, True)
rc = W.main(**e.kw(f))
ok("main online at boot: exits 0 with nm-online -q -t 90", rc == 0 and f.calls[0] == ["nm-online", "-q", "-t", "90"])
ok("main online at boot: no hotspot, no scan, no raspi-config",
   not any(c[0] in ("raspi-config",) or "hotspot" in c or "list" in c for c in f.calls))
ok("main online at boot: no dnsmasq drop-in", os.listdir(e.dns_dir) == [])

# offline: nm-online rc1 first; becomes online once the hotspot is up and a few polls passed
e = Env()
state = {"polls": 0, "hotspot": False}
def rules(a):
    if a[:3] == ["nmcli", "connection", "add"]: e.dns_snapshots.append(os.listdir(e.dns_dir))
    if a[:3] == ["nmcli", "connection", "up"]: state["hotspot"] = True
    if a[0] == "nmcli" and "list" in a: return cp(a, 0, hx(b"Home", b"Cafe"))
    if a[0] == "nm-online":
        if not state["hotspot"]: return cp(a, 1)
        state["polls"] += 1; return cp(a, 0 if state["polls"] > 3 else 1)
    if a[0] == "ip": return cp(a, 0, b"default via 1.2.3.4 dev eth0\n" if state["polls"] > 3 else b"")
f = Fake(rules)
rc = W.main(**e.kw(f))
hs = [c for c in f.calls if c[:3] == ["nmcli", "connection", "add"]]
ok("main offline: nmcli connection add is called once", len(hs) == 1)
ok("main offline: scan happens before the hotspot", f.idx("nmcli", "-t") < f.idx("nmcli", "connection", "add") and f.idx("nmcli", "-t") >= 0)
ok("main offline: dnsmasq drop-in present when hotspot started", e.dns_snapshots and e.dns_snapshots[0] == ["pilnk-captive.conf"])
ok("main offline: drop-in removed on exit", os.listdir(e.dns_dir) == [])
ok("main offline: exits 0 once online", rc == 0)
ok("main offline: hotspot connection removed on exit", f.has("nmcli", "connection", "delete", "id", W.HOTSPOT_CON))
ok("main never uses shell=True", all(k.get("shell") is not True for k in f.kw))
ok("dnsmasq drop-in source is exactly address=/#/10.42.0.1",
   rd(os.path.join(WS, "dnsmasq-captive.conf")).strip() == "address=/#/10.42.0.1")

# nm-online ok but no default route -> hotspot is still started
e = Env(); n = {"i": 0}
def rules2(a):
    if a[0] == "nm-online": return cp(a, 0)
    if a[0] == "ip":
        n["i"] += 1
        return cp(a, 0, b"" if n["i"] == 1 else b"default via 1.1.1.1 dev wlan0\n")
    if a[0] == "nmcli" and "list" in a: return cp(a, 0, b"")
f = Fake(rules2)
rc = W.main(**e.kw(f))
ok("nm-online ok but no default route is treated as offline (hotspot started)",
   f.has("nmcli", "connection", "add") and rc == 0)

# join success path through main: POST -> join -> online -> exit 0
def run_flow(join_ok, idle_cycles=0, nm_err=b"", extra=None):
    e = Env(); st = {"joined": False, "hotspots": 0}
    holder = {}
    def rules(a):
        if a[:3] == ["nmcli", "connection", "up"]: st["hotspots"] += 1
        if a[0] == "nmcli" and "list" in a: return cp(a, 0, hx(b"Home"))
        if extra:
            r = extra(a)
            if r is not None: return r
        if a[0] == "nmcli" and "connect" in a: 
            st["joined"] = join_ok; return cp(a, 0 if join_ok else 4, b"", nm_err)
        if a[0] == "nm-online": return cp(a, 0 if st["joined"] else 1)
        if a[0] == "ip": return cp(a, 0, b"default via 1.1.1.1 dev wlan0\n" if st["joined"] else b"")
    f = Fake(rules)
    return e, f, st
# drive main in a thread; POST once the server is up
def drive(join_ok, ssid="Home", psk=None, nm_err=b"", extra=None, **mainkw):
    psk = secret if psk is None else psk
    e, f, st = run_flow(join_ok, nm_err=nm_err, extra=extra)
    server_ready = threading.Event(); box = {}
    def on_ready(srv, state): box["srv"] = srv; box["state"] = state; server_ready.set()
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("rc", W.main(**e.kw(f, on_ready=on_ready,
                          settle=lambda s: f.calls.append(["SETTLE", str(s)]),
                          sleep=lambda s: __import__("time").sleep(0.01), **mainkw))), daemon=True)
    th.start(); assert server_ready.wait(5)
    body = urllib.parse.urlencode({"country": "NZ", "network": ssid.encode().hex(), "psk": psk})
    s, h, b = req(box["srv"], "POST", "/join", body=body.encode())
    if join_ok:
        th.join(10)
    else:
        for _ in range(200):
            if st["hotspots"] >= 2: break
            __import__("time").sleep(0.02)
        s2, h2, b2 = req(box["srv"], "GET", "/")
        out["page"] = b2
        box["state"].request_stop() if hasattr(box["state"], "request_stop") else None
        th.join(0.1)
    return e, f, st, out, th, s, b
e, f, st, out, th, s, b = drive(True)
ok("main join success: POST returned the before-switch page", s == 200 and b"will disappear" in b)
ok("main join success: raspi-config country then nmcli connect with the PSK as argv only",
   f.has("raspi-config", "nonint", "do_wifi_country", "NZ") and any(c[0] == "nmcli" and "connect" in c and c[-1] == secret for c in f.calls))
def conn_idx(f): return next(i for i, c in enumerate(f.calls) if c[:4] == ["nmcli", "-w", "30", "device"] and "connect" in c)
i_down, i_resc = f.idx("nmcli", "connection", "down", W.HOTSPOT_CON), f.idx("nmcli", "device", "wifi", "rescan")
i_set, i_conn = f.idx("SETTLE"), conn_idx(f)
ok("I-1: join order is hotspot down -> rescan -> settle -> connect", 0 < i_down < i_resc < i_set < i_conn)
ok("main join success: exits 0", out.get("rc") == 0 and not th.is_alive())
ok("main join success: drop-in removed", os.listdir(e.dns_dir) == [])
ok("main join success: PSK appears only in the nmcli connect argv",
   [c for c in f.calls if secret in " ".join(c)] == [c for c in f.calls if c[0] == "nmcli" and "connect" in c])
e, f, st, out, th, s, b = drive(False)
ok("main join failure: hotspot is brought back", st["hotspots"] >= 2)
ok("main join failure: connection deleted, error copy shown on the page",
   f.has("nmcli", "connection", "delete", "id", "Home") and "Couldn".encode() in out.get("page", b"") and secret.encode() not in out.get("page", b""))
i_conn = conn_idx(f)
ok("I-1: failed join brings the hotspot back up after the connect (and went down before it)",
   0 < f.idx("nmcli", "connection", "down", W.HOTSPOT_CON) < i_conn
   and any(c[:4] == ["nmcli", "connection", "up", W.HOTSPOT_CON] for c in f.calls[i_conn:]))
ok("main join failure: still serving, not exited", th.is_alive() or out.get("rc") is not None)

# idle restart after 30 minutes without a POST
e = Env(); clock = {"t": 0.0}; hs_count = {"n": 0}; polls = {"n": 0}
def rules3(a):
    if a[:3] == ["nmcli", "connection", "add"]: hs_count["n"] += 1
    if a[0] == "nmcli" and "list" in a: return cp(a, 0, b"")
    if a[0] == "nm-online":
        if hs_count["n"] >= 2: return cp(a, 0)
        return cp(a, 1)
    if a[0] == "ip": return cp(a, 0, b"default via 1.1.1.1 dev eth0\n" if hs_count["n"] >= 2 else b"")
def sleep3(s):
    clock["t"] += 600.0; polls["n"] += 1
    assert polls["n"] < 50
f = Fake(rules3)
rc = W.main(**e.kw(f, now=lambda: clock["t"], sleep=sleep3))
ok("idle for 30 min restarts the hotspot (second hotspot start, rescan first)",
   hs_count["n"] == 2 and rc == 0 and len([c for c in f.calls if c[:2] == ["nmcli", "-t"]]) == 2)
ok("idle restart is not earlier than 30 min", W.IDLE_RESTART == 1800)

# ---------------- review fixes, round 1 ----------------
HS_ADD = ["nmcli", "connection", "add", "type", "wifi", "ifname", "wlan0", "con-name", "PiLNK-Hotspot",
          "autoconnect", "no", "ssid", "PiLNK-Setup-7f3a", "802-11-wireless.mode", "ap",
          "802-11-wireless.band", "bg", "ipv4.method", "shared"]
def online_after_up(e):
    st = {"up": False, "polls": 0}
    def rules(a):
        if a[:3] == ["nmcli", "connection", "up"]: st["up"] = True
        if a[0] == "nmcli" and "list" in a: return cp(a, 0, hx(b"Home"))
        if a[0] == "nm-online":
            if not st["up"]: return cp(a, 1)
            st["polls"] += 1; return cp(a, 0 if st["polls"] > 2 else 1)
        if a[0] == "ip": return cp(a, 0, b"default via 1.2.3.4 dev eth0\n" if st["polls"] > 2 else b"")
    return Fake(rules)
e = Env(); f = online_after_up(e); rc = W.main(**e.kw(f))
ok("hotspot: open AP profile created with the exact argv", rc == 0 and f.calls.count(HS_ADD) == 1)
ok("hotspot: order delete old -> scan -> add -> up",
   0 <= f.idx("nmcli", "connection", "delete", "id", W.HOTSPOT_CON) < f.idx("nmcli", "-t")
   < f.idx(*HS_ADD) < f.idx("nmcli", "connection", "up", W.HOTSPOT_CON))
ok("hotspot: no hotspot subcommand, no modify, no wifi-sec/password/psk args",
   not f.has("nmcli", "device", "wifi", "hotspot") and not any("modify" in c for c in f.calls)
   and not any(x == "password" or "wifi-sec" in x or "psk" in x.lower() or "802-11-wireless-security" in x
               for c in f.calls if c[:3] == ["nmcli", "connection", "add"] for x in c))
ok("I-2: rfkill unblock and country NZ happen before the first scan and the hotspot",
   0 <= f.idx("rfkill", "unblock", "wifi") < f.idx("nmcli", "-t")
   and 0 <= f.idx("raspi-config", "nonint", "do_wifi_country", "NZ") < f.idx("nmcli", "-t")
   and f.idx("raspi-config", "nonint", "do_wifi_country", "NZ") < f.idx(*HS_ADD))
ok("I-2: unblock/country failures do not stop the hotspot",
   (lambda f2: W.main(**Env().kw(f2)) == 0 and f2.has("nmcli", "connection", "add"))(
       (lambda e2: (lambda inner: Fake(lambda a: cp(a, 1) if a[0] in ("rfkill", "raspi-config") else inner(a)))(online_after_up(e2).rules))(Env())))

# M-3: online early exit still cleans up
e = Env(); wr(os.path.join(e.dns_dir, "pilnk-captive.conf"), "address=/#/10.42.0.1\n")
f = net(0, True); rc = W.main(**e.kw(f))
ok("M-3: online at boot exits 0 and removes a stale dnsmasq drop-in", rc == 0 and os.listdir(e.dns_dir) == [])
ok("M-3: online at boot deletes a stale PiLNK-Hotspot profile",
   f.has("nmcli", "connection", "delete", "id", W.HOTSPOT_CON) and f.calls[0] == ["nm-online", "-q", "-t", "90"]
   and not f.has("rfkill") and not f.has("raspi-config"))

# M-2: a short body must not hang a handler thread
import socket, time
W.REQ_TIMEOUT = 0.5
st, srv, t = start_server()
t0 = time.monotonic(); sk = socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=3)
sk.sendall(b"POST /join HTTP/1.0\r\nContent-Length: 100\r\nContent-Type: application/x-www-form-urlencoded\r\n\r\nabc")
try:
    got = sk.recv(4096); hung = False
except socket.timeout:
    got, hung = b"", True
sk.close(); dt = time.monotonic() - t0
ok("M-2: declared Content-Length longer than the body is dropped quickly (no hang)", not hung and got == b"" and dt < 2.5)
ok("M-2: nothing queued from the short body", st.take_pending() is None)
stop(srv, t)
ok("M-2: handler class sets a socket timeout", re.search(r"^\s+timeout = ", src, re.M) is not None)

# ---------------- final review fixes ----------------
# M7: UK is accepted as GB
g, e = W.validate({"country": "uk", "ssid": "Home", "psk": "password1"})
ok("M7: country UK (any case) is accepted and becomes GB", g is not None and g["country"] == "GB" and not e)

# M6: a country already set on the card is kept; NZ only when none is set
def country_fake(raspi_out=b"", iw_out=b"", base=None):
    def rules(a):
        if a[:3] == ["raspi-config", "nonint", "get_wifi_country"]: return cp(a, 0 if raspi_out else 1, raspi_out)
        if a[:3] == ["iw", "reg", "get"]: return cp(a, 0, iw_out)
        return base(a) if base else None
    return Fake(rules)
f = country_fake(b"AU\n"); c = W.prepare_radio(f)
ok("M6: saved country from raspi-config (AU) is kept, not reset to NZ",
   c == "AU" and f.has("raspi-config", "nonint", "do_wifi_country", "AU") and not f.has("raspi-config", "nonint", "do_wifi_country", "NZ"))
IW = b"global\ncountry DE: DFS-ETSI\n\t(2400 - 2483 @ 40), (N/A, 20), (N/A)\n\nphy#0\ncountry 99: DFS-UNSET\n"
f = country_fake(b"", IW); c = W.prepare_radio(f)
ok("M6: no raspi-config country -> the iw reg get global country (DE)", c == "DE" and f.has("raspi-config", "nonint", "do_wifi_country", "DE"))
f = country_fake(b"", b"global\ncountry 00: DFS-UNSET\n"); c = W.prepare_radio(f)
ok("M6: none set (00 / empty) -> NZ", c == "NZ" and f.has("raspi-config", "nonint", "do_wifi_country", "NZ"))
f = country_fake(b"zz\n", b""); c = W.prepare_radio(f)
ok("M6: an unknown saved code is not used -> NZ", c == "NZ")
ok("M6: the page's country box defaults to the saved country",
   'name="country" value="AU"' in W.render_page([], "7f3a", None, country="AU").decode())
st6 = W.State("7f3a"); st6.country = "AU"
ok("M6: State carries the country for the page", getattr(st6, "country", None) == "AU")

# C2: tar1090's lighttpd holds :80. Stop it while the hotspot is up, start it
# again on exit; a port that stays busy is retried with backoff, loudly, and the
# hotspot is not torn down.
import socket as _s
def held_port():
    k = _s.socket(_s.AF_INET, _s.SOCK_STREAM); k.bind(("0.0.0.0", 0)); k.listen(1)
    return k, k.getsockname()[1]
def lighttpd_flow(active, frees_port, release_after=None):
    e = Env(); holder, port = held_port(); st = {"up": 0, "served": False, "polls": 0, "sleeps": []}
    def rules(a):
        if a[:4] == ["systemctl", "is-active", "--quiet", "lighttpd"]: return cp(a, 0 if active else 3)
        if a[:3] == ["systemctl", "stop", "lighttpd"] and frees_port: holder.close()
        if a[:3] == ["nmcli", "connection", "up"]: st["up"] += 1
        if a[0] == "nmcli" and "list" in a: return cp(a, 0, b"")
        if a[0] == "nm-online": return cp(a, 0 if st["served"] else 1)
        if a[0] == "ip": return cp(a, 0, b"default via 1.1.1.1 dev eth0\n" if st["served"] else b"")
    f = Fake(rules)
    def on_ready(srv, state): st["served"] = True
    def sleep(x):
        st["sleeps"].append(x)
        if release_after is not None and len(st["sleeps"]) == release_after: holder.close()
        assert len(st["sleeps"]) < 200
    old_err = sys.stderr; sys.stderr = err = io.StringIO()
    try:
        rc = W.main(**e.kw(f, port=port, on_ready=on_ready, sleep=sleep))
    finally:
        sys.stderr = old_err
        try: holder.close()
        except OSError: pass
    return rc, f, st, err.getvalue()
rc, f, st, err = lighttpd_flow(active=True, frees_port=True)
ok("C2: lighttpd active -> stopped before the page is served, server binds, exit 0",
   rc == 0 and st["served"] and 0 <= f.idx("systemctl", "stop", "lighttpd") < f.idx("nmcli", "connection", "up", W.HOTSPOT_CON) + 99
   and f.idx("systemctl", "stop", "lighttpd") >= 0)
ok("C2: lighttpd started again on exit (after the hotspot is deleted)",
   f.has("systemctl", "--no-block", "start", "lighttpd")
   and f.idx("systemctl", "--no-block", "start", "lighttpd") > f.idx("systemctl", "stop", "lighttpd"))
rc, f, st, err = lighttpd_flow(active=False, frees_port=False, release_after=4)
ok("C2: port busy, lighttpd not active -> not stopped, not started", not f.has("systemctl", "stop", "lighttpd") and not f.has("systemctl", "--no-block", "start", "lighttpd"))
ok("C2: a busy port is logged clearly (address + reason)", "cannot serve the setup page on" in err and "in use" in err.lower())
ok("C2: retried with backoff (growing waits) until it binds, then exit 0",
   rc == 0 and st["served"] and st["sleeps"][:4] == sorted(st["sleeps"][:4]) and st["sleeps"][0] < st["sleeps"][3])
ok("C2: the hotspot is not torn down while retrying the bind (one add, one up)",
   len([c for c in f.calls if c[:3] == ["nmcli", "connection", "add"]]) == 1 and st["up"] == 1)

# N1: a bind that fails for any reason but "port busy" (EADDRNOTAVAIL: the AP never came up)
# is not waited out; the hotspot is brought up again, bounded, then the error is re-raised
import errno as _errno
def notavail_flow(fail_times):
    e = Env(); st = {"up": 0, "served": False, "sleeps": [], "binds": 0}
    def rules(a):
        if a[:3] == ["nmcli", "connection", "up"]: st["up"] += 1
        if a[0] == "nmcli" and "list" in a: return cp(a, 0, b"")
        if a[0] == "nm-online": return cp(a, 0 if st["served"] else 1)
        if a[0] == "ip": return cp(a, 0, b"default via 1.1.1.1 dev eth0\n" if st["served"] else b"")
    f = Fake(rules)
    real = W.make_server
    def flaky(state, host=W.HOST, port=W.PORT):
        st["binds"] += 1
        if st["binds"] <= fail_times:
            raise OSError(_errno.EADDRNOTAVAIL, "Cannot assign requested address")
        return real(state, host, port)
    def on_ready(srv, state): st["served"] = True
    def sleep(x):
        st["sleeps"].append(x); assert len(st["sleeps"]) < 200
    W.make_server = flaky
    old_err = sys.stderr; sys.stderr = io.StringIO()
    try:
        try: rc = W.main(**e.kw(f, port=0, on_ready=on_ready, sleep=sleep))
        except OSError as ex: rc = ex
    finally:
        sys.stderr = old_err; W.make_server = real
    return rc, st
rc, st = notavail_flow(1)
ok("N1: EADDRNOTAVAIL -> start_hotspot is attempted again (second 'connection up'), then serves, exit 0",
   rc == 0 and st["up"] == 2 and st["served"])
rc, st = notavail_flow(99)
ok("N1: EADDRNOTAVAIL that never clears -> bounded retries, then the error is re-raised (Restart=on-failure)",
   isinstance(rc, OSError) and rc.errno == _errno.EADDRNOTAVAIL and 2 <= st["up"] <= 5 and st["binds"] == st["up"])

# I4: every idle cycle (30 min, no POST) tries the saved Wi-Fi before raising the hotspot again
def idle_flow(connect_ok):
    e = Env(); clock = {"t": 0.0}; st = {"adds": 0, "joined": False}
    def rules(a):
        if a[:3] == ["nmcli", "connection", "add"]: st["adds"] += 1
        if a[0] == "nmcli" and "list" in a: return cp(a, 0, b"")
        if a[:2] == ["nmcli", "-w"] and a[2:5] == ["45", "device", "connect"]:
            st["joined"] = connect_ok; return cp(a, 0 if connect_ok else 10)
        if a[0] == "nm-online":
            return cp(a, 0 if st["joined"] or st["adds"] >= 2 else 1)
        if a[0] == "ip": return cp(a, 0, b"default via 1.1.1.1 dev wlan0\n" if st["joined"] or st["adds"] >= 2 else b"")
    f = Fake(rules)
    def sleep(x):
        clock["t"] += 600.0
        assert clock["t"] < 100000
    rc = W.main(**e.kw(f, now=lambda: clock["t"], sleep=sleep))
    return rc, f, st
rc, f, st = idle_flow(True)
i_del = min(i for i, c in enumerate(f.calls) if c[:5] == ["nmcli", "connection", "delete", "id", W.HOTSPOT_CON] and i > f.idx("nmcli", "connection", "up", W.HOTSPOT_CON))
i_con = f.idx("nmcli", "-w", "45", "device", "connect", "wlan0")
ok("I4: idle -> hotspot removed, rescan, nmcli device connect wlan0 (saved Wi-Fi), then the online check",
   0 < i_del < f.idx("nmcli", "device", "wifi", "rescan") < i_con
   and any(c[:2] == ["nm-online", "-q"] for c in f.calls[i_con:]))
ok("I4: saved Wi-Fi back -> online -> exit 0, no second hotspot", rc == 0 and st["adds"] == 1)
ok("I4: the saved networks are never read (no connection show / list of profiles)",
   not any(c[:3] == ["nmcli", "connection", "show"] or (c[0] == "nmcli" and "NAME" in " ".join(c)) for c in f.calls))
rc, f, st = idle_flow(False)
ok("I4: saved Wi-Fi not there -> the hotspot is raised again (second add), exit 0 once online", rc == 0 and st["adds"] == 2)
i_con = f.idx("nmcli", "-w", "45", "device", "connect", "wlan0")
ok("I4: a failed connect does not sit in a long wait (nm-online -t 1, not 30/45)",
   i_con > 0 and next(c for c in f.calls[i_con:] if c[0] == "nm-online")[:4] == ["nm-online", "-q", "-t", "1"])

# ---------------- unit + conf files ----------------
unit = rd(os.path.join(WS, "pilnk-wifi-setup.service"))
ok("unit: Type=simple, Restart=on-failure, WantedBy=multi-user.target",
   "Type=simple" in unit and "Restart=on-failure" in unit and "WantedBy=multi-user.target" in unit)
ok("unit: After/Wants NetworkManager.service", re.search(r"^After=.*\bNetworkManager\.service\b", unit, re.M) and re.search(r"^Wants=.*\bNetworkManager\.service\b", unit, re.M))
ok("unit: ExecStart exact", "ExecStart=/usr/bin/python3 /usr/local/lib/pilnk/pilnk_wifi_setup.py" in unit)
ordering = "\n".join(l for l in unit.splitlines() if re.match(r"(After|Before|Requires|Wants|BindsTo|PartOf)=", l))
ok("unit: not ordered after multi-user.target or pilnk-firstboot (deadlock)",
   not re.search(r"^(After|Requires|BindsTo)=.*(multi-user\.target|pilnk-firstboot)", ordering, re.M))
ok("unit: runs as root (no User=)", not re.search(r"^User=", unit, re.M))


# ---------------- event log (#549, 12 Nov 2026) ----------------
# The journal is volatile on the image, so the 6 Nov wrong-password run left no
# trace. A small persistent log of EVENTS only: never an SSID, a password, or
# nmcli's own text (which can quote the SSID).
def evlog_fresh():
    W.EVENT_LOG = os.path.join(tempfile.mkdtemp(), "wifi-setup.log"); return W.EVENT_LOG
def evlog_text(path=None):
    try: return rd(path or W.EVENT_LOG)
    except OSError: return ""
def ev_names(text):
    return [l.split()[4] for l in text.splitlines() if len(l.split()) > 4]

# join() reports why a join failed, as a fixed word
SECRET_SSID = "MyPrivateHomeNet"
d = {}
f = Fake(lambda a: cp(a, 4, b"", b"Error: Connection activation failed: (7) Secrets were required, but not provided.\n")
         if a[0] == "nmcli" and "connect" in a else None)
W.join("NZ", SECRET_SSID, secret, run=f, detail=d)
ok("EV: join detail: wrong password -> reason=secrets, rc=4, secs recorded",
   d.get("reason") == "secrets" and d.get("rc") == 4 and isinstance(d.get("secs"), (int, float)))
d = {}
f = Fake(lambda a: cp(a, 10, b"", ("Error: No network with SSID '%s' found.\n" % SECRET_SSID).encode())
         if a[0] == "nmcli" and "connect" in a else None)
W.join("NZ", SECRET_SSID, "", run=f, detail=d)
ok("EV: join detail: unknown network -> reason=not_found (SSID not copied into the detail)",
   d.get("reason") == "not_found" and SECRET_SSID not in repr(d))
d = {}
W.join("NZ", "Home", secret, run=run_boom, detail=d)
ok("EV: join detail: nmcli timeout -> reason=timeout", d.get("reason") == "timeout")
d = {}
W.join("NZ", "Home", "password1", run=Fake(), detail=d)
ok("EV: join detail: success -> reason=ok, rc=0", d.get("reason") == "ok" and d.get("rc") == 0)
ok("EV: join without detail= still returns the same tuple", W.join("NZ", "Home", "password1", run=Fake()) == (True, "Connected to Home"))

# _event: one line, safe values only, size-capped
p = evlog_fresh()
W._event("page", path="/a b<script>?q=" + "x" * 100, error=True, n=3)
t = evlog_text()
ok("EV: _event writes one timestamped line with the event name",
   len(t.splitlines()) == 1 and re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d pid=\d+ up=\d+\.\d page ", t))
ok("EV: _event values are sanitised and short (no spaces/markup, <= 40 chars)",
   "<" not in t and "script>" not in t and all(len(kv.split("=", 1)[1]) <= 40 for kv in t.split()[5:]))
ok("EV: any string that is not a fixed word becomes ? (user text never reaches the log)", " path=? " in t + " ")
ok("EV: _event writes bools as 0/1", " error=1" in t and " n=3" in t)
wr(p, "x" * (W.EVENT_LOG_MAX + 10))
W._event("start")
ok("EV: over the size cap the log rolls to .1 and starts fresh",
   os.path.exists(p + ".1") and len(evlog_text()) < 200)
W.EVENT_LOG = "/nonexistent-dir-pilnk/x.log"
try:
    W._event("start"); ev_ok = True
except Exception:
    ev_ok = False
ok("EV: an unwritable log never raises", ev_ok)

# online at boot: logged, then exit
p = evlog_fresh(); e = Env(); rc = W.main(**e.kw(net(0, True)))
ok("EV: online at boot -> start + online(via=boot)",
   rc == 0 and ev_names(evlog_text(p))[:2] == ["start", "online"] and "via=boot" in evlog_text(p))

# the wrong-password story, end to end
p = evlog_fresh()
e, f, st, out, th, s, b = drive(False)
t = evlog_text(p); names = ev_names(t)
def first(n, after=-1):
    for i, x in enumerate(names):
        if x == n and i > after: return i
    return -1
i_start, i_hs1 = first("start"), first("hotspot_up")
i_post = first("post_join"); i_js = first("join_start"); i_jr = first("join_result")
i_err = first("error_set"); i_hs2 = first("hotspot_up", i_jr); i_page = first("page", i_hs2)
ok("EV: failed join logged in order: start, hotspot_up, post_join, join_start, join_result, error_set, hotspot_up, page",
   0 <= i_start < i_hs1 < i_post < i_js < i_jr < i_err < i_hs2 < i_page)
jr = t.splitlines()[i_jr]
ok("EV: join_result says ok=0 with an rc and a reason word", " ok=0" in jr and re.search(r" rc=-?\d+", jr) and re.search(r" reason=[a-z_]+", jr))
ok("EV: post_join says accepted, ssid length and whether a password was given (never the password)",
   all(x in t.splitlines()[i_post] for x in (" result=accepted", " ssid_bytes=4", " has_psk=1", " hidden=0")))
ok("EV: the page served after the failure says error=1", " error=1" in t.splitlines()[i_page])
ok("EV: hotspot_up counts the restarts (n=1 then n=2)",
   " n=1" in t.splitlines()[i_hs1] and " n=2" in t.splitlines()[i_hs2])
ok("EV: the log never holds the PSK or the SSID", secret not in t and "Home" not in t and b"Home".hex() not in t)
p = evlog_fresh()
e, f, st, out, th, s, b = drive(False, ssid="ZqSsidMark77", psk="ZqPskMark77",
                                nm_err=b"Error: No network with SSID 'ZqSsidMark77' found.\n")
t = evlog_text(p)
ok("EV: marker SSID/PSK/nmcli text (cleaner-proof markers) never reach the log",
   "ZqSsidMark77" not in t and "ZqPskMark77" not in t and "join_result" in t and "reason=not_found" in t)

# an invalid POST is logged as invalid (and still no SSID/PSK)
p = evlog_fresh()
st2, srv2, t2 = start_server()
body = urllib.parse.urlencode({"country": "XX", "network": b"Home".hex(), "psk": "short"})
req(srv2, "POST", "/join", body=body.encode())
req(srv2, "GET", "/hotspot-detect.html", headers={"User-Agent": "CaptiveNetworkSupport-443 wispr"})
req(srv2, "GET", "/", headers={"User-Agent": "Mozilla/5.0 (iPhone)"})
stop(srv2, t2)
t = evlog_text(p)
ok("EV: invalid POST -> post_join result=invalid with an error count", re.search(r"post_join result=invalid errors=\d", t) is not None)
ok("EV: a captive-portal probe is logged as a redirect kind, never its path",
   re.search(r" redirect kind=hotspot_detect method=GET n=1 ua=probe client=1\b", t) is not None and "hotspot-detect" not in t)
ok("EV: a browser GET / is logged as page kind=root ua=browser with the client's last octet",
   re.search(r" page kind=root error=0 ua=browser client=1\b", t) is not None)
ok("EV: invalid POST never logs the PSK or SSID", "short" not in t and "Home" not in t)

# success: online via=join, then stop
p = evlog_fresh()
e, f, st, out, th, s, b = drive(True)
t = evlog_text(p)
ok("EV: successful join -> join_result ok=1 reason=ok, online via=join, stop",
   re.search(r" join_result ok=1 rc=0 reason=ok", t) and " online via=join" in t and ev_names(t)[-1] == "stop")
W.EVENT_LOG = os.path.join(EVLOG_DIR, "wifi-setup.log")


# ---- review round (12 Nov): fsync, allowlist, path kinds, uptime, exit reason, lock, clients ----
import ast
fsyncs = {"n": 0}; real_fsync = W.os.fsync
def counting_fsync(fd): fsyncs["n"] += 1; return real_fsync(fd)
W.os.fsync = counting_fsync
p = evlog_fresh(); fsyncs["n"] = 0
W._event("join_result", ok=False, rc=4, reason="secrets")
ok("R1: a normal event is fsynced (survives a power pull)", fsyncs["n"] == 1)
st3, srv3, t3 = start_server(); fsyncs["n"] = 0
for _ in range(5):
    req(srv3, "GET", "/generate_204", headers={"User-Agent": "Dalvik/2.1.0"})
stop(srv3, t3)
t = evlog_text(p)
ok("R1/M3: probe redirects are rate-limited (5 GETs -> 1 line) and not fsynced",
   len(re.findall(r" redirect kind=generate_204 ", t)) == 1 and fsyncs["n"] == 0)
W.os.fsync = real_fsync

p = evlog_fresh()
W._event("x", word="accepted", c="NZ", bad="MyHomeNet", n=5, fl=1.26, none=None, lst=[1])
t = evlog_text(p)
ok("R2: allowlist: fixed words and country codes pass, other strings/None/lists become ?",
   " word=accepted c=NZ bad=? n=5 fl=1.3 none=? lst=?" in t)
ok("R4: every line carries seconds since boot (up=)", re.search(r" pid=\d+ up=\d+\.\d x ", t) is not None)

p = evlog_fresh()
st4, srv4, t4 = start_server()
for path in ("/MyHomeNet", "/library/test/success.html", "http://example.com/secretpath"):
    req(srv4, "GET", path, headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"})
req(srv4, "GET", "/", headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14; wv) AppleWebKit/537.36 Chrome/120.0 Mobile Safari/537.36"})
req(srv4, "POST", "/somewhere", body=b"x=1")
stop(srv4, t4)
t = evlog_text(p)
ok("R3: unknown paths log as kind=other; user text never appears",
   " redirect kind=other " in t and "MyHomeNet" not in t and "example" not in t and "secretpath" not in t)
ok("R3: Apple success page is a known kind", " redirect kind=apple_success " in t)
ok("R12: iOS captive sheet (no Safari/) is ua=cna; Android webview is ua=webview",
   re.search(r"kind=apple_success method=GET n=1 ua=cna", t) is not None and re.search(r" page kind=root error=0 ua=webview", t) is not None)
ok("M4: a POST to another path is logged as a redirect with method=POST", " redirect kind=other method=POST " in t)

# exit reasons
p = evlog_fresh()
e, f, st, out, th, s, b = drive(True)
ok("R4: a normal finish logs stop exit=return", " stop exit=return" in evlog_text(p))
p = evlog_fresh(); e = Env()
def explode(a):
    if a[0] == "nm-online": return cp(a, 1)
    if a[:2] == ["nmcli", "-t"]: raise RuntimeError("boom")
try:
    W.main(**e.kw(Fake(explode))); raised = False
except RuntimeError:
    raised = True
ok("R4: a crash logs stop exit=<ExceptionClass> and still raises", raised and " stop exit=RuntimeError" in evlog_text(p))

# rotation lock: concurrent writers never replace .1 with a tiny file
lock_ok = True
old_max = W.EVENT_LOG_MAX
W.EVENT_LOG_MAX = 3000
W.os.fsync = lambda fd: None          # speed only: the race is in rename/append, not fsync
real_replace = W.os.replace
def slow_replace(a, b):                 # widen the check-then-rename window so a race shows every time
    time.sleep(0.003); return real_replace(a, b)
W.os.replace = slow_replace
for trial in range(15):
    p = evlog_fresh()
    def burst():
        for _ in range(60): W._event("x", n=1)
    ths = [threading.Thread(target=burst) for _ in range(8)]
    [x.start() for x in ths]; [x.join() for x in ths]
    if os.path.exists(p + ".1") and os.path.getsize(p + ".1") <= W.EVENT_LOG_MAX:
        lock_ok = False
W.EVENT_LOG_MAX = old_max
W.os.fsync = real_fsync
W.os.replace = real_replace
ok("M1: concurrent events never leave a tiny .1 (rotation is locked)", lock_ok)

# join reason: quoted SSID ignored, NM reason number kept, supplicant = auth
d = {}
W.join("NZ", "xPSKx", "", run=Fake(lambda a: cp(a, 10, b"", b"Error: No network with SSID 'xPSKx' found.\n") if "connect" in a else None), detail=d)
ok("M2: a network name containing PSK is still not_found", d.get("reason") == "not_found")
d = {}
W.join("NZ", "Home", secret, run=Fake(lambda a: cp(a, 4, b"", b"Error: Connection activation failed: (7) Secrets were required, but not provided.\n") if "connect" in a else None), detail=d)
ok("M2: NetworkManager's reason number is kept (nm=7)", d.get("nm") == 7 and d.get("reason") == "secrets")
d = {}
W.join("NZ", "Home", secret, run=Fake(lambda a: cp(a, 4, b"", b"Error: Connection activation failed: (11) 802.1X supplicant took too long to authenticate.\n") if "connect" in a else None), detail=d)
ok("M2: supplicant failures are reason=auth", d.get("reason") == "auth" and d.get("nm") == 11)
d = {}
W.join("NZ", "supplicant-timeout", "", run=Fake(lambda a: cp(a, 2, b"", b"Error: Failed to add/activate new connection: unknown error for 'supplicant-timeout'.\n") if "connect" in a else None), detail=d)
ok("M2: words inside the quoted network name never steer the reason", d.get("reason") == "other")

# clients: station + lease counts, logged when they change (counts only)
p = evlog_fresh()
ld = tempfile.mkdtemp(); wr(os.path.join(ld, "dnsmasq-wlan0.leases"), "1700000000 aa:bb:cc:dd:ee:ff 10.42.0.23 MyPhoneName *\n")
def stations(a):
    if a[:3] == ["iw", "dev", "wlan0"]: return cp(a, 0, b"Station aa:bb:cc:dd:ee:ff (on wlan0)\n\tinactive time: 10 ms\nStation 11:22:33:44:55:66 (on wlan0)\n")
e, f, st, out, th, s, b = drive(False, extra=stations, lease_glob=os.path.join(ld, "dnsmasq-*.leases"))
t = evlog_text(p)
ok("R10: clients stations=2 leases=1 logged (counts only, no MAC or names)",
   " clients stations=2 leases=1" in t and "aa:bb" not in t and "MyPhoneName" not in t)
ok("R11: join_start says whether the hotspot went down (down_rc)", re.search(r" join_start hidden=0 down_rc=-?\d+", t) is not None)

# static: every _event keyword value is a constant, a number-ish call, or a vetted name
tree = ast.parse(src)
BAD = {"ssid", "psk", "form", "manual", "body", "err", "stderr", "e", "msg", "path", "ua_raw"}
def bad_value(node):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("len", "bool", "int"):
        return False
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in BAD: return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value in ("ssid", "psk"): return True
    return False
calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_event"]
ok("R2: static (ast): no _event value is built from ssid/psk/form/nmcli text/raw path",
   len(calls) >= 12 and not any(bad_value(k.value) for c in calls for k in c.keywords))
W.EVENT_LOG = os.path.join(EVLOG_DIR, "wifi-setup.log")

# static: no _event call is handed the SSID or the password
ev_lines = [l for l in src.splitlines() if "_event(" in l]
ok("EV: static: _event is used, and never with =ssid / =psk / =clean[...]",
   len(ev_lines) >= 8 and not any(re.search(r"=\s*(ssid|psk|clean\[\"(ssid|psk)\"\]|form\b)", l) for l in ev_lines))

# ---------------- privacy: static ----------------
ok("source never touches /etc/NetworkManager/system-connections",
   "system-connections" not in src)
ok("source: no shell=True anywhere", "shell=True" not in src)

sys.exit(1 if FAILS else 0)

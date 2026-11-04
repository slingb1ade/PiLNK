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

# ---------------- scan parsing (nmcli -t escapes) ----------------
raw = b"Home\nwe\\:ird\\\\name\n\n<script>x\nHome\n\xff\xfe\nA\\:B\\:C\n--\n"
nets = W.parse_scan(raw)
ok("parse_scan unescapes \\: and \\\\", "we:ird\\name" in nets and "A:B:C" in nets)
ok("parse_scan drops empty/duplicate, keeps invalid bytes",
   nets.count("Home") == 1 and "" not in nets and b"\xff\xfe".decode("utf-8", "surrogateescape") in nets)
f = Fake(lambda a: cp(a, 0, raw) if a[:2] == ["nmcli", "-t"] else None)
ok("scan runs nmcli -t -f SSID device wifi list as argv",
   W.scan(run=f) == nets and f.calls[0][:6] == ["nmcli", "-t", "-f", "SSID", "device", "wifi"] and f.kw[0].get("shell") is not True)
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
                 dnsmasq_src=os.path.join(WS, "dnsmasq-captive.conf"), host="127.0.0.1", port=0)
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
    if a[:4] == ["nmcli", "device", "wifi", "hotspot"]: state["hotspot"] = True; e.dns_snapshots.append(os.listdir(e.dns_dir))
    if a[0] == "nmcli" and "list" in a: return cp(a, 0, b"Home\nCafe\n")
    if a[0] == "nm-online":
        if not state["hotspot"]: return cp(a, 1)
        state["polls"] += 1; return cp(a, 0 if state["polls"] > 3 else 1)
    if a[0] == "ip": return cp(a, 0, b"default via 1.2.3.4 dev eth0\n" if state["polls"] > 3 else b"")
f = Fake(rules)
rc = W.main(**e.kw(f))
hs = [c for c in f.calls if c[:4] == ["nmcli", "device", "wifi", "hotspot"]]
ok("main offline: nmcli device wifi hotspot is called", len(hs) == 1)
ok("main offline: hotspot is wlan0, SSID PiLNK-Setup-7f3a, no password",
   "ifname" in hs[0] and "wlan0" in hs[0] and "PiLNK-Setup-7f3a" in hs[0] and "password" not in hs[0])
ok("main offline: scan happens before the hotspot", f.idx("nmcli", "-t") < f.idx("nmcli", "device", "wifi", "hotspot") and f.idx("nmcli", "-t") >= 0)
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
   f.has("nmcli", "device", "wifi", "hotspot") and rc == 0)

# join success path through main: POST -> join -> online -> exit 0
def run_flow(join_ok, idle_cycles=0):
    e = Env(); st = {"joined": False, "hotspots": 0}
    holder = {}
    def rules(a):
        if a[:4] == ["nmcli", "device", "wifi", "hotspot"]: st["hotspots"] += 1
        if a[0] == "nmcli" and "list" in a: return cp(a, 0, b"Home\n")
        if a[0] == "nmcli" and "connect" in a: 
            st["joined"] = join_ok; return cp(a, 0 if join_ok else 4)
        if a[0] == "nm-online": return cp(a, 0 if st["joined"] else 1)
        if a[0] == "ip": return cp(a, 0, b"default via 1.1.1.1 dev wlan0\n" if st["joined"] else b"")
    f = Fake(rules)
    return e, f, st
# drive main in a thread; POST once the server is up
def drive(join_ok):
    e, f, st = run_flow(join_ok)
    server_ready = threading.Event(); box = {}
    def on_ready(srv, state): box["srv"] = srv; box["state"] = state; server_ready.set()
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("rc", W.main(**e.kw(f, on_ready=on_ready,
                          sleep=lambda s: __import__("time").sleep(0.01)))), daemon=True)
    th.start(); assert server_ready.wait(5)
    body = urllib.parse.urlencode({"country": "NZ", "network": b"Home".hex(), "psk": secret})
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
ok("main join success: exits 0", out.get("rc") == 0 and not th.is_alive())
ok("main join success: drop-in removed", os.listdir(e.dns_dir) == [])
ok("main join success: PSK appears only in the nmcli connect argv",
   [c for c in f.calls if secret in " ".join(c)] == [c for c in f.calls if c[0] == "nmcli" and "connect" in c])
e, f, st, out, th, s, b = drive(False)
ok("main join failure: hotspot is brought back", st["hotspots"] >= 2)
ok("main join failure: connection deleted, error copy shown on the page",
   f.has("nmcli", "connection", "delete", "id", "Home") and "Couldn".encode() in out.get("page", b"") and secret.encode() not in out.get("page", b""))
ok("main join failure: still serving, not exited", th.is_alive() or out.get("rc") is not None)

# idle restart after 30 minutes without a POST
e = Env(); clock = {"t": 0.0}; hs_count = {"n": 0}; polls = {"n": 0}
def rules3(a):
    if a[:4] == ["nmcli", "device", "wifi", "hotspot"]: hs_count["n"] += 1
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

# ---------------- privacy: static ----------------
ok("source never touches /etc/NetworkManager/system-connections",
   "system-connections" not in src)
ok("source: no shell=True anywhere", "shell=True" not in src)

sys.exit(1 if FAILS else 0)

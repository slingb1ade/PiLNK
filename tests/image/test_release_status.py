#!/usr/bin/env python3
"""release_status.py: the app's side of signed releases (spec 5.6, plan Task 4).
Real release-check.sh, real git and ssh-keygen, throwaway keys only."""
import json, os, shutil, subprocess, sys, tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
ENV = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1",
           GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
ROOTS = []

def ok(name, cond):
    print(("PASS: " if cond else "FAIL: ") + name)

def run(*a, cwd=None):
    return subprocess.run(a, cwd=cwd, check=True, capture_output=True, env=ENV).stdout.decode()

def make_node():
    root = tempfile.mkdtemp(prefix="pilnk-rs."); ROOTS.append(root)
    up, node, key = root + "/up", root + "/node", root + "/aj"
    run("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "aj", "-f", key)
    pub = open(key + ".pub").read().split()
    run("git", "init", "-q", "-b", "main", up)
    open(up + "/VERSION", "w").write("1.0.0\n")
    open(up + "/app.py", "w").write("print(1)\n")
    open(up + "/release_signers", "w").write('release namespaces="pilnk-release" %s %s\n' % (pub[0], pub[1]))
    shutil.copy(REPO + "/release-check.sh", up + "/release-check.sh")
    run("git", "add", "-A", cwd=up); run("git", "commit", "-qm", "one", cwd=up)
    run("git", "clone", "-q", up, node)
    return root, up, node, key

def release(up, key, ver):
    open(up + "/VERSION", "w").write(ver + "\n")
    run("git", "commit", "-qam", "v" + ver, cwd=up)
    commit = run("git", "rev-parse", "HEAD", cwd=up).strip()
    st = up + "/../st"
    open(st, "wb").write(("pilnk-release 1\nversion %s\ncommit %s\n" % (ver, commit)).encode())
    if os.path.exists(st + ".sig"): os.remove(st + ".sig")
    run("ssh-keygen", "-q", "-Y", "sign", "-f", key, "-n", "pilnk-release", st)
    return {"version": ver, "commit": commit, "statement": open(st, "rb").read().decode(),
            "sig": open(st + ".sig", "rb").read().decode()}

try:
    import release_status as rs
except Exception as e:  # RED before the module exists
    print("FAIL: import release_status (%s)" % e); sys.exit(1)

# --- check(): the periodic, read-only verification
root, up, node, key = make_node()
rel = release(up, key, "1.0.1"); run("git", "pull", "-q", cwd=node)
r = rs.check(node, rel)
ok("check: good release -> ok/on_signed", r["ota_sig"] == "ok" and r["ota_sig_run"] == "on_signed")
ok("check: carries the signed version + commit", r["signed_version"] == "1.0.1" and r["signed_commit"] == rel["commit"])
r = rs.check(node, None)
ok("check: release None -> missing/none", r["ota_sig"] == "missing" and r["ota_sig_run"] == "none")
r = rs.check(node, {"statement": 7, "sig": None})
ok("check: non-text fields -> missing", r["ota_sig"] == "missing")
seen = {}
def spy(argv, **kw):
    seen["st"] = open(argv[-2], "rb").read(); seen["sig"] = open(argv[-1], "rb").read()
    return subprocess.run(argv, **kw)
rs.check(node, rel, runner=spy)
ok("check writes statement bytes exactly", seen.get("st") == rel["statement"].encode())
ok("check writes signature bytes exactly", seen.get("sig") == rel["sig"].encode())
def slow(argv, **kw): raise subprocess.TimeoutExpired(argv, kw.get("timeout", 0))
r = rs.check(node, rel, runner=slow)
ok("check: script timeout -> error/none", r["ota_sig"] == "error" and r["ota_sig_run"] == "none")
class Fake:
    def __init__(self, out): self.stdout, self.returncode = out, 0
r = rs.check(node, rel, runner=lambda a, **k: Fake("ota_sig=pwned\nota_sig_run=on_signed\nsigned_version=1\nsigned_commit=x\n"))
ok("check: unknown token from the script -> error", r["ota_sig"] == "error" and r["ota_sig_run"] == "none")
r = rs.check(node + "/nope", rel)
ok("check: no release-check.sh -> error", r["ota_sig"] == "error")

# dirty tree (the hub): the periodic check must not touch index or tracked files
open(node + "/app.py", "w").write("print(2)\n"); os.utime(node + "/VERSION", (1577836800, 1577836800))
def status():
    return run("git", "status", "--porcelain", cwd=node) + run("sha256sum", node + "/.git/index")
before = status(); r = rs.check(node, rel); after = status()
ok("periodic check reports dirty", r["ota_sig_run"] == "dirty")
ok("periodic check leaves git status identical", before == after)

# --- state file
d = tempfile.mkdtemp(); ROOTS.append(d)
ok("load_state: no file -> {}", rs.load_state(d) == {})
open(d + "/release_state.json", "w").write('{"ota_sig": "o')
ok("load_state survives garbage", rs.load_state(d) == {})
calls = []
real_replace = os.replace
def replace_spy(a, b): calls.append((a, b)); real_replace(a, b)
os.replace = replace_spy
try:
    rs.save_state(d, {"ota_sig": "ok", "ota_sig_run": "on_signed", "signed_version": "1.0.1", "signed_commit": "a" * 40}, "report")
finally:
    os.replace = real_replace
ok("save_state is atomic (os.replace used)", len(calls) == 1 and calls[0][1].endswith("release_state.json"))
st = rs.load_state(d)
ok("save_state writes the R7 fields", st.get("ota_sig") == "ok" and st.get("mode") == "report" and st.get("by") == "app" and "checked_at" in st)
mt = os.stat(d + "/release_state.json").st_mtime_ns
wrote = rs.save_state(d, {"ota_sig": "ok", "ota_sig_run": "on_signed", "signed_version": "1.0.1", "signed_commit": "a" * 40}, "report")
ok("save_state skips an unchanged write (SD card wear)", wrote is False and os.stat(d + "/release_state.json").st_mtime_ns == mt)
wrote = rs.save_state(d, {"ota_sig": "bad", "ota_sig_run": "none", "signed_version": "", "signed_commit": ""}, "report")
ok("save_state writes when something changed", wrote is True and rs.load_state(d)["ota_sig"] == "bad")

# --- policy
open(d + "/release_policy", "w").write("enforced\n")
ok("policy: garbage -> report", rs.read_policy(d) == "report")
open(d + "/release_policy", "w").write("enforce\n")
ok("policy: enforce", rs.read_policy(d) == "enforce")
ok("policy: missing file -> report", rs.read_policy(d + "/nope") == "report")

# --- availability (R8)
def gt(a, b):
    p = lambda s: [int(x) for x in s.split(".")]
    return p(a) > p(b)
sua = rs.signed_update_available
ok("report mode availability unchanged", sua("report", "1.6.27", "1.6.28", None, {}, gt) == (True, False))
ok("report mode: nothing newer", sua("report", "1.6.28", "1.6.28", None, {}, gt) == (False, False))
ok("enforce: newer signed + ok -> available", sua("enforce", "1.6.27", "1.6.28", {"version": "1.6.28"}, {"ota_sig": "ok"}, gt) == (True, False))
ok("enforce: GitHub ahead, not signed -> awaiting", sua("enforce", "1.6.27", "1.6.28", {"version": "1.6.27"}, {"ota_sig": "ok"}, gt) == (False, True))
ok("enforce: bad sig -> not available", sua("enforce", "1.6.27", "1.6.28", {"version": "1.6.28"}, {"ota_sig": "bad"}, gt)[0] is False)
ok("enforce: the verified signed_version wins over release.version",
   sua("enforce", "1.6.27", "9.9.9", {"version": "9.9.9"}, {"ota_sig": "ok", "signed_version": "1.6.27"}, gt) == (False, True))

# --- svc_sudo (spec 5.11)
class R:
    def __init__(self, rc): self.returncode = rc
def fake(rc_true, rc_scoped):
    def f(argv, **kw):
        return R(rc_true if argv[:3] == ["sudo", "-n", "true"] else rc_scoped)
    return f
non_root = lambda: 1000
ok("svc_sudo full / scoped / none",
   [rs.svc_sudo_probe(fake(0, 0), non_root), rs.svc_sudo_probe(fake(1, 0), non_root), rs.svc_sudo_probe(fake(1, 1), non_root)] == ["full", "scoped", "none"])
ok("svc_sudo: running as root -> full", rs.svc_sudo_probe(fake(1, 1), lambda: 0) == "full")
def boom(argv, **kw): raise OSError("no sudo")
ok("svc_sudo: no sudo binary -> none", rs.svc_sudo_probe(boom, non_root) == "none")

# --- ping fields (Review Focus 5)
WORST = {"ota_sig": "not_on_main", "ota_sig_run": "on_signed", "junk": "x" * 400}
pf = rs.ping_fields(WORST, "enforce", "scoped")
ok("ping fields: exactly the four keys", sorted(pf) == ["ota_sig", "ota_sig_mode", "ota_sig_run", "svc_sudo"])
ok("ping fields fit", len(json.dumps(pf)) <= 110 and all(len(v) <= 16 for v in pf.values()))
ok("ping fields: long values cut to 16", len(rs.ping_fields({"ota_sig": "y" * 99}, "x" * 99, "z" * 99)["ota_sig"]) == 16)
ok("ping fields: empty state -> ''", rs.ping_fields({}, "report", "none")["ota_sig"] == "")

for r_ in ROOTS: shutil.rmtree(r_, ignore_errors=True)

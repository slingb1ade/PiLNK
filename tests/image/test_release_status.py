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

# --- update.sh's fetched verdict survives the app's 5-minute check (final review I1)
# The app checks WITHOUT --fetched, so it cannot see ancestry or VERSION-at-commit;
# its "ok" must not wipe update.sh's not_on_main / mismatch for the same commit.
root2, up2, node2, key2 = make_node()
rel2 = release(up2, key2, "1.0.1"); run("git", "pull", "-q", cwd=node2)
kfv = getattr(rs, "keep_fetched_verdict", None)
ok("keep_fetched_verdict exists", callable(kfv))
if callable(kfv):
    upd = {"ota_sig": "mismatch", "ota_sig_run": "on_signed", "signed_version": "1.0.1",
           "signed_commit": rel2["commit"], "by": "update"}
    app_r = rs.check(node2, rel2)
    ok("app check alone says ok (it cannot see VERSION-at-commit)", app_r["ota_sig"] == "ok")
    ok("app check keeps update.sh's mismatch for the same signed commit", kfv(upd, app_r)["ota_sig"] == "mismatch")
    ok("... and not_on_main", kfv(dict(upd, ota_sig="not_on_main"), app_r)["ota_sig"] == "not_on_main")
    ok("a different signed commit clears it", kfv(dict(upd, signed_commit="f" * 40), app_r)["ota_sig"] == "ok")
    ok("a fresh bad/missing result is never replaced by the old verdict",
       kfv(upd, dict(app_r, ota_sig="bad"))["ota_sig"] == "bad" and kfv(upd, dict(app_r, ota_sig="missing"))["ota_sig"] == "missing")
    json.dump(upd, open(node2 + "/release_state.json", "w"))
    for _ in range(3):   # three 5-minute app checks after update.sh's run
        rs.save_state(node2, kfv(rs.load_state(node2), rs.check(node2, rel2)), "report", by="app")
    ok("after several app checks the state file still says mismatch", rs.load_state(node2).get("ota_sig") == "mismatch")
    ok("... and the ping says so", rs.ping_fields(rs.load_state(node2), "report", "none")["ota_sig"] == "mismatch")
    ok("enforce: a kept mismatch is not an available update",
       rs.signed_update_available("enforce", "1.0.0", "1.0.1", rel2, kfv(upd, app_r), gt)[0] is False)

# --- node_features is never stored as cut (invalid) JSON (final review I2)
# node.php keeps 500 chars of PHP's json_encode ('/' -> '\/', non-ASCII -> \uXXXX),
# after shortening audio_build_err to 60; anything longer is cut mid-JSON.
fit = getattr(rs, "fit_features", None)
ok("fit_features exists", callable(fit))
if callable(fit):
    def php_len(d): return len(json.dumps(d, separators=(",", ":"), ensure_ascii=True).replace("/", "\\/"))
    small = {"ota": "auto", "ota_sig": "ok", "volts": 5.11}
    ok("small features pass through unchanged", fit(dict(small)) == small)
    trouble = {"sdr_audio": "not_listening", "denoise": "off", "audio_build": "failed:2/8",
               "audio_build_err": "E: /usr/lib/aarch64-linux-gnu/libfoo.so: cannot open (dpkg lock held by apt)",
               "atc_stt": "absent", "stt_bench": 12.34, "remote_maintenance": True, "hw_tier": "pi4",
               "power": "was_low", "volts": 4.81, "throttled": "0x50005", "uv_now": True,
               "ota": "auto", "ota_check": "stale", "ota_last_result": "error:TimeoutExpired", "ota_fail": "exception",
               "restart_pending": "1.6.28", "ota_restart": "unknown",
               "ota_sig": "not_on_main", "ota_sig_run": "on_signed", "ota_sig_mode": "enforce", "svc_sudo": "scoped"}
    ok("(the trouble case really is over 500 before fitting)", php_len(trouble) > 500)
    before = dict(trouble)
    out = fit(trouble)
    ok("a node in trouble fits node_features (PHP-measured)", php_len(out) <= 500)
    ok("... keeping the diagnosis fields", all(out.get(k) == trouble[k] for k in
       ("ota_sig", "ota_fail", "ota_last_result", "audio_build", "power", "ota_check")))
    ok("... and never mutating the caller's dict", trouble == before)
    # Just over the line: shortening audio_build_err (as node.php does) is enough,
    # so nothing is dropped.
    near = dict(trouble); near.pop("power"); near.pop("throttled"); near.pop("uv_now"); near.pop("volts")
    near["audio_build_err"] = "x" * (60 + 501 - php_len(dict(near, audio_build_err="x" * 60)))
    ok("(the near case is just over 500)", php_len(near) > 500)
    out2 = fit(near)
    ok("just over 500: audio_build_err cut to 60, every other key kept",
       out2.get("audio_build_err") == "x" * 60 and set(out2) == set(near) and php_len(out2) <= 500)
    huge = {("k%03d" % i): "v/" * 15 for i in range(40)}
    ok("anything at all comes out under 500 as valid JSON", php_len(fit(huge)) <= 500)

for r_ in ROOTS: shutil.rmtree(r_, ignore_errors=True)

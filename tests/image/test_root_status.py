#!/usr/bin/env python3
"""root_status.py: the app's view of root's signed copy (Part B spec 5.6, plan
Task 6). The ping's root / root_match fields, the audio-build state path, and
node_features room for them."""
import ast, json, os, shutil, sys, tempfile, time

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)
ROOTS = []


def check(name, cond):
    print(("PASS: " if cond else "FAIL: ") + name)


def tree(root, files, complete=False):
    for rel, body in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(body)
    if complete:
        open(os.path.join(root, ".complete"), "w").write("1.6.40 " + "a" * 40 + "\n")


try:
    import root_status
except Exception as e:  # the module does not exist yet: every check below fails
    print("FAIL: import root_status (%s)" % e)
    root_status = None

T = tempfile.mkdtemp(prefix="pilnk-rst."); ROOTS.append(T)
FILES = {"rootside/MANIFEST": "# root-run\na.sh\nsub/b.sh\n", "a.sh": "echo a\n", "sub/b.sh": "echo b\n"}
A, C = os.path.join(T, "app"), os.path.join(T, "current")
tree(A, FILES); tree(C, FILES, complete=True)
A2 = os.path.join(T, "app2"); tree(A2, dict(FILES, **{"a.sh": "echo A\n"}))
A3 = os.path.join(T, "app3"); tree(A3, {k: v for k, v in FILES.items() if k != "sub/b.sh"})
NOCOPY = os.path.join(T, "nocopy"); tree(NOCOPY, FILES)
# MANIFEST names a file neither tree has: nothing to compare, so no match
GONE = {"rootside/MANIFEST": "a.sh\ngone.sh\n", "a.sh": "echo a\n"}
A4, C4 = os.path.join(T, "app4"), os.path.join(T, "cur4"); tree(A4, GONE); tree(C4, GONE, complete=True)

pf = (lambda *a: root_status.ping_fields(*a)) if root_status else (lambda *a: None)
check("root_signed", pf({"src": "signed", "version": "1.6.40", "switched": True}, A, C) == {"root": "signed:1.6.40"})
check("root_user_ver_match", pf({"src": "user", "version": "1.6.40", "switched": False}, A, C)
      == {"root": "user:1.6.40", "root_match": "y"})
check("root_user_differs", (pf({"src": "user", "version": "1.6.40", "switched": False}, A2, C) or {}).get("root_match") == "n")
check("root_user_missing_file_differs", (pf({"src": "user", "version": "1.6.40"}, A3, C) or {}).get("root_match") == "n")
check("root_match_both_missing_is_n", (pf({"src": "user", "version": "1.6.40"}, A4, C4) or {}).get("root_match") == "n")
check("root_user_no_copy_no_match", pf({"src": "user", "version": "1.6.40", "switched": False}, A, NOCOPY) == {"root": "user:1.6.40"})
check("root_error_token", pf({"src": "error", "reason": "fetch:28"}, A, C) == {"root": "error:fetch:28"})
check("root_error_underscore_token", pf({"src": "error", "reason": "not_on_main"}, A, C) == {"root": "error:not_on_main"})
check("root_bad_reason_dropped", pf({"src": "error", "reason": "rm -rf /"}, A, C) == {"root": "error"})
check("root_unsafe", pf({"src": "unsafe", "reason": "unsafe"}, A, C) == {"root": "unsafe"})
check("root_missing_status", pf({}, A, C) == {"root": "none"})
check("root_odd_version_dropped", pf({"src": "user", "version": "1.6.40; rm"}, A, NOCOPY) == {"root": "user"})
check("root_odd_src_is_error", pf({"src": "evil"}, A, C) == {"root": "error"})
check("root_not_a_dict", pf("junk", A, C, os.path.join(T, "nounits")) == {"root": "none"})
# no status yet, but a root route exists: root still runs the user's tree (review I-2)
UD = os.path.join(T, "units"); os.makedirs(UD); open(os.path.join(UD, "pilnk-sdr-recover.service"), "w").write("[Service]\n")
check("root_no_status_with_route_is_user", pf({}, A, C, UD) == {"root": "user"})
check("root_no_status_no_route_is_none", pf({}, A, C, os.path.join(T, "nounits")) == {"root": "none"})
# the match follows edits to the app tree (cached per current target, not forever)
if root_status:
    st = {"src": "user", "version": "1.6.40"}
    first = root_status.ping_fields(st, A, C).get("root_match")
    time.sleep(0.02); open(os.path.join(A, "a.sh"), "w").write("echo changed\n")
    check("root_match_sees_later_edit", first == "y" and root_status.ping_fields(st, A, C).get("root_match") == "n")
    open(os.path.join(A, "a.sh"), "w").write("echo a\n")
else:
    check("root_match_sees_later_edit", False)

# load(prefix): status.json read as data, capped, anything odd is {}
P = os.path.join(T, "fs"); SD = os.path.join(P, "var/lib/pilnk/state"); os.makedirs(SD)
ld = (lambda: root_status.load(P)) if root_status else (lambda: None)
check("load_missing_is_empty", ld() == {})
json.dump({"src": "signed", "version": "1.6.40", "switched": True}, open(os.path.join(SD, "status.json"), "w"))
check("load_reads_status", (ld() or {}).get("src") == "signed")
open(os.path.join(SD, "status.json"), "w").write("not json")
check("load_junk_is_empty", ld() == {})
# valid JSON past the 64KB cap (whitespace pad): still refused, not read short
open(os.path.join(SD, "status.json"), "w").write('{"src": "user"}' + " " * 70000)
check("load_huge_is_empty", ld() == {})
open(os.path.join(SD, "status.json"), "w").write("[1, 2]")
check("load_list_is_empty", ld() == {})

# app._audio_build_state: root's state file first, then the old path
SRC = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)


def top(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    return None


fn = top("_audio_build_state")
new_p, old_p = os.path.join(T, "root_abs.json"), os.path.join(T, "old_abs.json")
ns = {"json": json, "os": os, "ROOT_AUDIO_BUILD_STATE_FILE": new_p, "AUDIO_BUILD_STATE_FILE": old_p}
if fn is not None:
    exec(compile(ast.Module([fn], []), "app.py", "exec"), ns)
audio_state = ns.get("_audio_build_state", lambda: None)
json.dump({"result": "ok", "where": "root"}, open(new_p, "w")); json.dump({"result": "failed", "where": "old"}, open(old_p, "w"))
check("state_new_path_first", (audio_state() or {}).get("where") == "root")
os.remove(new_p)
check("state_old_path_fallback", (audio_state() or {}).get("where") == "old")
check("app_root_state_path", "ROOT_AUDIO_BUILD_STATE_FILE = '/var/lib/pilnk/state/audio_build_state.json'" in SRC)


def calls_in(fn_name, dotted):
    f = top(fn_name); mod, attr = dotted.split(".")
    return f is not None and any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == attr
        and isinstance(c.func.value, ast.Name) and c.func.value.id == mod for c in ast.walk(f))


check("ping_merges_root_status", calls_in("_ota_ping_features", "root_status.ping_fields"))
guarded = any(isinstance(n, ast.Try) and any(isinstance(b, ast.Import) and any(a.name == "root_status" for a in b.names)
              for b in n.body) for n in TREE.body)
check("root_status_import_guarded", guarded)

# node_features room: 2000, and root is the last thing to go
import release_status
check("feature_limit_2000", release_status._FEATURE_LIMIT == 2000)
check("fit_keeps_root_2000", "root" in release_status.fit_features({"x": "a" * 1900, "root": "signed:1.6.40"}))
big = {"a": "x" * 1200, "b": "y" * 1200, "stt_bench": "z", "root": "signed:1.6.40", "zz": "w"}
fitted = release_status.fit_features(big)
check("fit_root_outlives_stt_bench", "root" in fitted and "stt_bench" not in fitted
      and release_status._php_json_len(fitted) <= 2000)

for r in ROOTS:
    shutil.rmtree(r, ignore_errors=True)

#!/usr/bin/env python3
"""app.py wiring for signed releases (plan Task 4). app.py starts threads on
import, so the pieces are lifted out with ast, as test_no_receiver.py does."""
import ast, os

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)

def ok(name, cond):
    print(("PASS: " if cond else "FAIL: ") + name)

def top(name):
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == name: return n
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == name for t in n.targets): return n
    return None

def calls_in(fn_name, dotted):
    fn = top(fn_name); mod, attr = dotted.split(".")
    return fn is not None and any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == attr
        and isinstance(c.func.value, ast.Name) and c.func.value.id == mod for c in ast.walk(fn))

ns = {}
pat, fn = top("_OTA_FAIL_PATTERNS"), top("_classify_ota_failure")
exec(compile(ast.Module([pat, fn], []), "app.py", "exec"), ns)
classify = ns["_classify_ota_failure"]
ok("exit 6 text -> sig_refused", classify("[PILNK-OTA] SIGNATURE REFUSED: bad — nothing installed") == "sig_refused")
ok("downgrade refusal -> sig_refused", classify("SIGNATURE REFUSED: downgrade") == "sig_refused")
ok("existing tokens unchanged (fetch)", classify("ERROR: git fetch failed (exit code 1)") == "fetch_failed")
ok("existing tokens unchanged (Rule #28)", classify("ABORT: git pull succeeded but VERSION unchanged") == "version_unchanged")
ok("_perform_ota_check calls release_status.check", calls_in("_perform_ota_check", "release_status.check"))
ok("_perform_ota_check saves the state", calls_in("_perform_ota_check", "release_status.save_state"))
ok("_perform_ota_check decides availability via signed_update_available",
   calls_in("_perform_ota_check", "release_status.signed_update_available"))
ok("_ota_ping_features merges release_status.ping_fields", calls_in("_ota_ping_features", "release_status.ping_fields"))
guarded = any(isinstance(n, ast.Try) and any(isinstance(b, ast.Import) and any(a.name == "release_status" for a in b.names)
              for b in n.body) for n in TREE.body)
ok("release_status import is guarded (a missing module never stops the app)", guarded)
st = top("api_ota_status")
keys = [k.value for d in ast.walk(st) if isinstance(d, ast.Dict) for k in d.keys if isinstance(k, ast.Constant)] if st else []
ok("/api/ota/status reports awaiting_signature (R8)", "awaiting_signature" in keys)

# final review I1: the app's no-fetch check must not wipe update.sh's verdict
ok("_perform_ota_check keeps update.sh's fetched verdict (keep_fetched_verdict)",
   calls_in("_perform_ota_check", "release_status.keep_fetched_verdict"))
# final review I2: the ping's features dict is fitted to node_features (varchar 500)
ok("_fit_features uses release_status.fit_features", calls_in("_fit_features", "release_status.fit_features"))
feat_vals = [v for d in ast.walk(TREE) if isinstance(d, ast.Dict)
             for k, v in zip(d.keys, d.values) if isinstance(k, ast.Constant) and k.value == "features"]
ok("the ping's features go through _fit_features",
   bool(feat_vals) and all(isinstance(v, ast.Call) and getattr(v.func, "id", None) == "_fit_features" for v in feat_vals))
ff = top("_fit_features")
if ff is not None:
    ns2 = {"release_status": None}
    exec(compile(ast.Module([ff], []), "app.py", "exec"), ns2)
    ok("_fit_features without release_status returns the dict unchanged", ns2["_fit_features"]({"a": 1}) == {"a": 1})
else:
    ok("_fit_features without release_status returns the dict unchanged", False)

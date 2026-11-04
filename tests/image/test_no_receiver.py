#!/usr/bin/env python3
"""Dashboard 'no receiver' state: the /api/status field and the banner.
app.py starts threads and pairing on import, so the helper is lifted out of
the source and run on its own; the page is checked statically."""
import ast, os, re, sys, tempfile

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
APP = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
PAGE = open(os.path.join(REPO, "templates", "index.html"), encoding="utf-8").read()

def ok(name, cond):
    print(("PASS: " if cond else "FAIL: ") + name)

# --- the field ---
tree = ast.parse(APP)
fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_no_receiver"), None)
ok("app.py has _no_receiver()", fn is not None)
if fn is not None:
    d = tempfile.mkdtemp()
    ns = {"os": os, "PILNK_DIR": d}
    exec(compile(ast.Module([fn], []), "app.py", "exec"), ns)
    ok("no_receiver false without the flag file", ns["_no_receiver"]() is False)
    open(os.path.join(d, ".no_receiver"), "w").close()
    ok("no_receiver true while .no_receiver exists", ns["_no_receiver"]() is True)
    os.remove(os.path.join(d, ".no_receiver"))
    ok("no_receiver false again once removed", ns["_no_receiver"]() is False)
ok("/api/status route returns no_receiver",
   re.search(r"@app\.route\('/api/status'[^\n]*\)\ndef \w+\(\):\n(?:[^\n]*\n){0,6}?[^\n]*'no_receiver':\s*_no_receiver\(\)", APP) is not None)

# --- the banner ---
COPY = "No receiver found — plug in an RTL-SDR or Airspy and restart the Pi."
ok("banner copy is exact, once", PAGE.count(COPY) == 1)
ok("banner element is hidden by default and uses the OTA banner look",
   re.search(r'<div id="noReceiverBanner"[^>]*display:none[^>]*linear-gradient\(90deg,#1e293b,#0f172a\)', PAGE, re.S) is not None)
ok("page polls /api/status and checks no_receiver",
   "/api/status" in PAGE and re.search(r"\.no_receiver", PAGE) is not None)

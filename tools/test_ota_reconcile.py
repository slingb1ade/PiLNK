"""Boot-time OTA reconcile: a success must clear the old failure reason.

Usage: python3 tools/test_ota_reconcile.py app.py
Pulls _reconcile_pending_ota_result out of app.py with ast (importing app.py
starts the whole node) and runs it against an in-memory OTA state.
"""
import ast, sys

def load(path):
    src = open(path).read()
    fn = next(n for n in ast.parse(src).body
              if isinstance(n, ast.FunctionDef) and n.name == '_reconcile_pending_ota_result')
    mod = ast.Module(body=[fn], type_ignores=[])
    return compile(mod, path, 'exec')

def run(code, state, local_version):
    ns = {}
    ns['_load_ota_state'] = lambda: dict(state)
    ns['_save_ota_state'] = lambda **u: state.update(u)
    ns['_get_local_version'] = lambda: local_version
    exec(code, ns)
    ns['_reconcile_pending_ota_result']()
    return state

def main():
    code = load(sys.argv[1])
    fails = 0
    def check(name, got, want):
        nonlocal fails
        ok = got == want
        fails += not ok
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'  got={got!r} want={want!r}'))

    # 1. MITKURT's case: old fault on file, update moved VERSION -> success, fault cleared
    s = run(code, {'last_result': 'started@1.6.15', 'last_fail': 'restart_blocked'}, '1.6.16')
    check('success clears stale last_fail', s.get('last_fail'), '')
    check('success recorded', s.get('last_result'), 'success')

    # 2. VERSION did not move -> interrupted, and the fault reason is kept
    s = run(code, {'last_result': 'started@1.6.16', 'last_fail': 'restart_blocked'}, '1.6.16')
    check('interrupted keeps last_fail', s.get('last_fail'), 'restart_blocked')
    check('interrupted recorded', s.get('last_result'), 'interrupted')

    # 3. Nothing pending -> state untouched
    s = run(code, {'last_result': 'success', 'last_fail': 'x'}, '1.6.16')
    check('no pending marker leaves state alone', s, {'last_result': 'success', 'last_fail': 'x'})

    # 4. Success with no previous fault -> last_fail is '' (key present, empty)
    s = run(code, {'last_result': 'started@1.6.15'}, '1.6.16')
    check('success with no old fault', s.get('last_fail'), '')

    print(f'{6 - fails}/6')
    sys.exit(1 if fails else 0)

main()

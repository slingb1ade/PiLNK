"""release_status: the app's side of signed releases.

Every OTA check (about every 5 minutes) the app hands the `release` object
from pilnk.io/api/version.php to release-check.sh (read-only: it never touches
the tree), keeps the answer in release_state.json, and reports four short
fields in the ping: ota_sig, ota_sig_run, ota_sig_mode, svc_sudo.

update.sh writes the same state file when it installs (by="update"); this
module writes it between installs (by="app"). Both write a temp file and
rename it, so a reader never sees half a file.

Spec: pilnk-tasks/signed-releases-spec.md 5.5, 5.6, 5.11.
"""
import json
import os
import re
import subprocess
import tempfile
import time

STATE_FILE = 'release_state.json'
POLICY_FILE = 'release_policy'
TOKENS = ('ok', 'missing', 'bad', 'not_on_main', 'mismatch', 'no_tool', 'error')
RUNS = ('on_signed', 'behind', 'ahead', 'dirty', 'none')
_VER = re.compile(r'^[0-9]+(\.[0-9]+){1,3}$')
_HEX40 = re.compile(r'^[0-9a-f]{40}$')
_KEYS = ('ota_sig', 'ota_sig_run', 'signed_version', 'signed_commit')


def _error():
    return {'ota_sig': 'error', 'ota_sig_run': 'none', 'signed_version': '', 'signed_commit': ''}


def _text_bytes(v):
    # Byte-exact: the signature covers these bytes. No newline translation.
    return v.encode('utf-8') if isinstance(v, str) else b''


def parse_output(text):
    """release-check.sh's four lines -> dict. Anything off-vocabulary -> error."""
    got = {}
    for line in (text or '').splitlines():
        k, sep, v = line.partition('=')
        if sep and k in _KEYS:
            got[k] = v.strip()
    if got.get('ota_sig') not in TOKENS or got.get('ota_sig_run') not in RUNS:
        return _error()
    return {
        'ota_sig': got['ota_sig'],
        'ota_sig_run': got['ota_sig_run'],
        'signed_version': got.get('signed_version', '') if _VER.match(got.get('signed_version', '')) else '',
        'signed_commit': got.get('signed_commit', '') if _HEX40.match(got.get('signed_commit', '')) else '',
    }


def check(pilnk_dir, release, timeout=20.0, runner=subprocess.run):
    """Run the running tree's release-check.sh (no --fetched: nothing was fetched)."""
    try:
        rel = release if isinstance(release, dict) else {}
        script = os.path.join(pilnk_dir, 'release-check.sh')
        if not os.path.isfile(script):
            return _error()
        with tempfile.TemporaryDirectory(prefix='pilnk-release-') as d:
            st = os.path.join(d, 'statement')
            sig = os.path.join(d, 'sig')
            with open(st, 'wb') as f:
                f.write(_text_bytes(rel.get('statement')))
            with open(sig, 'wb') as f:
                f.write(_text_bytes(rel.get('sig')))
            env = dict(os.environ, PILNK_DIR=pilnk_dir)
            r = runner(['bash', script, st, sig], capture_output=True, text=True,
                       timeout=timeout, env=env)
        return parse_output(getattr(r, 'stdout', ''))
    except Exception:
        return _error()


def read_policy(pilnk_dir):
    try:
        with open(os.path.join(pilnk_dir, POLICY_FILE)) as f:
            return 'enforce' if f.readline().strip() == 'enforce' else 'report'
    except Exception:
        return 'report'


def load_state(pilnk_dir):
    try:
        with open(os.path.join(pilnk_dir, STATE_FILE)) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(pilnk_dir, result, mode, by='app'):
    """Write release_state.json (temp file + rename). Skips the write when
    nothing but the timestamp would change: this runs every 5 minutes on SD
    cards. Returns True when it wrote."""
    new = {k: str((result or {}).get(k, '')) for k in _KEYS}
    new['mode'] = mode
    old = load_state(pilnk_dir)
    if all(old.get(k) == v for k, v in new.items()):
        return False
    new['checked_at'] = int(time.time())
    new['by'] = by
    path = os.path.join(pilnk_dir, STATE_FILE)
    tmp = '%s.tmp.%d' % (path, os.getpid())
    try:
        with open(tmp, 'w') as f:
            json.dump(new, f)
        os.replace(tmp, path)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def signed_update_available(policy, local_ver, remote_ver, release, last, semver_gt):
    """(available, awaiting_signature).
    report: exactly today's rule (GitHub's VERSION newer than ours).
    enforce: only a VERIFIED signed release newer than ours counts; when GitHub
    is ahead of it, the update is waiting for AJ's signature."""
    github_newer = bool(remote_ver) and semver_gt(remote_ver, local_ver)
    if policy != 'enforce':
        return (github_newer, False)
    last = last if isinstance(last, dict) else {}
    rel = release if isinstance(release, dict) else {}
    # The verified statement's version beats the unsigned JSON field next to it.
    signed_ver = last.get('signed_version') or rel.get('version') or ''
    signed_newer = last.get('ota_sig') == 'ok' and bool(signed_ver) and semver_gt(signed_ver, local_ver)
    return (signed_newer, github_newer and not signed_newer)


def svc_sudo_probe(runner=subprocess.run, geteuid=os.geteuid):
    """full | scoped | none: what root access the PiLNK service account has
    without a password (input for part B, spec 5.11). Never runs anything as
    root: `sudo -n true` and `sudo -n -l <cmd>` only ask."""
    try:
        if geteuid() == 0:
            return 'full'
        if runner(['sudo', '-n', 'true'], capture_output=True, timeout=5).returncode == 0:
            return 'full'
        if runner(['sudo', '-n', '-l', 'systemctl', 'restart', 'pilnk'],
                  capture_output=True, timeout=5).returncode == 0:
            return 'scoped'
        return 'none'
    except Exception:
        return 'none'


def ping_fields(state, mode, svc_sudo):
    """Four low-cardinality strings for the ping's node_features (varchar 500)."""
    s = state if isinstance(state, dict) else {}
    cut = lambda v: str(v or '')[:16]
    return {'ota_sig': cut(s.get('ota_sig')), 'ota_sig_run': cut(s.get('ota_sig_run')),
            'ota_sig_mode': cut(mode), 'svc_sudo': cut(svc_sudo)}


# update.sh checks with --fetched (ancestry + VERSION-at-commit); the app's
# 5-minute check cannot. Its "ok" must not wipe update.sh's not_on_main /
# mismatch for the SAME signed commit (final review I1). A new signed commit,
# or a fresh bad/missing/no_tool/error, always wins.
_FETCH_ONLY = ('not_on_main', 'mismatch')


def keep_fetched_verdict(old, result):
    r = dict(result or {})
    o = old if isinstance(old, dict) else {}
    if (r.get('ota_sig') == 'ok' and o.get('ota_sig') in _FETCH_ONLY
            and o.get('signed_commit') and o.get('signed_commit') == r.get('signed_commit')):
        r['ota_sig'] = o['ota_sig']
    return r


# node.php stores json_encode($features) in a varchar(500) and cuts anything
# longer mid-JSON (final review I2). Measure the way PHP will ('/' -> '\/',
# non-ASCII -> \uXXXX) and drop the least useful keys first; never mutate the
# caller's dict.
_FEATURE_LIMIT = 500
_DROP_FIRST = ('svc_sudo', 'ota_sig_mode', 'stt_bench', 'ota_sig_run', 'audio_build_err')


def _php_json_len(d):
    return len(json.dumps(d, separators=(',', ':'), ensure_ascii=True).replace('/', '\\/'))


def fit_features(feats, limit=_FEATURE_LIMIT):
    d = dict(feats or {})
    if _php_json_len(d) <= limit:
        return d
    if isinstance(d.get('audio_build_err'), str):
        d['audio_build_err'] = d['audio_build_err'][:60]
    for k in _DROP_FIRST:
        if _php_json_len(d) <= limit:
            return d
        d.pop(k, None)
    while d and _php_json_len(d) > limit:
        d.pop(next(reversed(d)))
    return d

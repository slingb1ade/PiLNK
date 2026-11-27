"""root_status.py: the app's view of root's own signed copy (Part B spec 5.6).

rootsync (run by root) writes /var/lib/pilnk/state/status.json, readable by
all. The app only reads it, as data, and reports one compact ping field:

  root:       signed:<ver> | user:<ver> | user | none | unsafe | error[:<reason>]
              (none only when there is no status.json AND no root route unit)
  root_match: y | n   only while the node has a good copy but has not switched
              (src user): do the root-run files in the PiLNK user's tree equal
              the ones in root's copy? (sha256 of every rootside/MANIFEST path)

Nothing here can change what root runs; it is a report.
"""
import hashlib
import json
import os
import re

STATUS = '/var/lib/pilnk/state/status.json'
CURRENT = '/var/lib/pilnk/current'
UNITS = '/etc/systemd/system'
# Root's routes into PiLNK code (spec 5.4). With no status.json, a node that has
# one of these is running root jobs from the user's tree: that is 'user', not 'none'.
ROUTES = ('pilnk-sdr-recover.service', 'pilnk-audio-build.service')
_CAP = 65536
_REASON = re.compile(r'^[a-z][a-z_]{0,11}(:[0-9]{1,3})?$')
_VERSION = re.compile(r'^[0-9]+(\.[0-9]+){1,3}$')
_MAN_PATH = re.compile(r'^[A-Za-z0-9._/-]+$')
_match_cache = {}


def _read_capped(path):
    with open(path, 'rb') as f:
        data = f.read(_CAP + 1)
    if len(data) > _CAP:
        raise ValueError('too big')
    return data


def load(prefix=''):
    """status.json as a dict; {} when missing, unreadable, too big or not an object."""
    try:
        d = json.loads(_read_capped((prefix or '') + STATUS).decode('utf-8'))
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _manifest(current_dir):
    try:
        text = _read_capped(os.path.join(current_dir, 'rootside', 'MANIFEST')).decode('utf-8')
    except Exception:
        return None
    paths = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if not _MAN_PATH.match(line) or '..' in line:
            return None
        paths.append(line)
    return paths


def _sha(path):
    try:
        h = hashlib.sha256()
        with open(path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


def _stamp(path):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except Exception:
        return None


def _match(app_dir, current_dir):
    """'y' when every MANIFEST file is identical in both trees, else 'n'.
    Cached per current target and per the app files' stat, so a ping costs a
    few stat() calls unless something changed."""
    target = os.path.realpath(current_dir)
    paths = _manifest(current_dir)
    if not paths:
        return 'n'
    key = (target, os.path.realpath(app_dir), tuple(_stamp(os.path.join(app_dir, p)) for p in paths))
    hit = _match_cache.get(target)
    if hit and hit[0] == key:
        return hit[1]
    def _same(p):
        mine = _sha(os.path.join(app_dir, p))
        return mine is not None and mine == _sha(os.path.join(current_dir, p))
    same = all(_same(p) for p in paths)
    res = 'y' if same else 'n'
    _match_cache.clear()
    _match_cache[target] = (key, res)
    return res


def ping_fields(status, app_dir, current_dir=CURRENT, units_dir=UNITS):
    s = status if isinstance(status, dict) else {}
    if not s:
        has_route = any(os.path.isfile(os.path.join(units_dir, u)) for u in ROUTES)
        return {'root': 'user' if has_route else 'none'}
    src = s.get('src')
    ver = str(s.get('version') or '')
    ver = ver if _VERSION.match(ver) else ''
    if src in ('signed', 'user'):
        out = {'root': src + (':' + ver if ver else '')}
    elif src == 'unsafe':
        out = {'root': 'unsafe'}
    else:
        reason = str(s.get('reason') or '')
        out = {'root': 'error' + (':' + reason if src == 'error' and _REASON.match(reason) else '')}
    if src == 'user' and not s.get('switched') and os.path.isfile(os.path.join(current_dir, '.complete')):
        out['root_match'] = _match(app_dir, current_dir)
    return out

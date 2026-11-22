"""Hub tile store for the 3D views (21 Nov 2026).

Why: 3D Approach and 3D Sky each kept their map tiles in the browser that fetched them, so every new
area, every new zoom and every other device (phone, laptop) started from nothing. The slow part was
the terrain: AWS's elevation tiles live in Virginia, ~0.9 s each from New Zealand. Now the node keeps
its own copy on disk and answers every browser in the house, and overnight it fetches the areas the
camera is most likely to need (see the pre-warm section below).

Only two sources, both open data: AWS terrain ('dem') and OpenFreeMap vector tiles ('ofm'). Satellite
(Esri) is never stored - its terms don't allow it - so those requests keep going straight to Esri.

Disk: ~/.cache/pilnk/tiles/<src>/<z>/<x>/<y>.<ext>, outside the git tree. Each file's mtime is when it
was fetched (30 days = stale, refreshed behind the scenes); its atime is when it was last used (set
by us, at most once a day, so it works on noatime cards too). Over the cap, the least recently USED
go first, down to 90%. It never writes when the disk has less than the floor free.
"""
import json
import logging
import math
import os
import queue
import re
import stat
import threading
import time

log = logging.getLogger('tiles')

MB = 1024 * 1024
GB = 1024 * MB
DAY = 86400
STALE = 30 * DAY

SOURCES = {
    'dem': {'maxz': 15, 'ext': 'png', 'ctype': 'image/png',
            'url': 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png'},
    'ofm': {'maxz': 14, 'ext': 'pbf', 'ctype': 'application/x-protobuf',
            'url': 'https://tiles.openfreemap.org/planet/{b}/{z}/{x}/{y}.pbf'},
}
OFM_TILEJSON = 'https://tiles.openfreemap.org/planet'
BUILD_RE = re.compile(r'^[0-9]{8}_[0-9]{6}_[a-z]+$')


PNG_SIG = b'\x89PNG\r\n\x1a\n'


def looks_like_tile(src, data):
    """A captive-portal page, an error body or a file cut short by a power cut is not a tile.
    Terrain is a PNG; a vector tile is a protobuf whose first field is 'layers' (0x1a), or empty."""
    if data is None:
        return False
    if src == 'dem':
        return data[:8] == PNG_SIG and len(data) > 8
    return len(data) == 0 or data[:1] == b'\x1a'


class UpstreamError(Exception):
    """The tile's real home said no (404) or could not be reached (502)."""
    def __init__(self, status, why=''):
        Exception.__init__(self, '%s %s' % (status, why))
        self.status = status


def valid(src, z, x, y):
    s = SOURCES.get(src)
    if not s or not isinstance(z, int) or not isinstance(x, int) or not isinstance(y, int):
        return False
    if z < 0 or z > s['maxz']:
        return False
    n = 1 << z
    return 0 <= x < n and 0 <= y < n


_SESSION = None
_SESSION_LOCK = threading.Lock()


def _http_session():
    """One pooled, keep-alive session for every upstream fetch. A fresh connection per tile meant a new TLS
    handshake across the Pacific each time: measured 849 ms a tile against 244 ms with keep-alive."""
    global _SESSION
    with _SESSION_LOCK:
        if _SESSION is None:
            import requests
            from requests.adapters import HTTPAdapter
            sess = requests.Session()
            sess.mount('https://', HTTPAdapter(pool_connections=4, pool_maxsize=8))
            _SESSION = sess
        return _SESSION


def _http_get(url, headers=None, timeout=None):
    r = _http_session().get(url, headers=headers or {}, timeout=timeout or 10)
    return r.status_code, r.content


class TileStore(object):
    def __init__(self, root, cap_bytes=600 * MB, floor_bytes=2 * GB, fetch=None, now=time.time,
                 disk_free=None, background=True, ua='PiLNK-node', timeout=10, refresh_queue=500):
        self.root = root
        self.cap = int(cap_bytes)
        self.floor = int(floor_bytes)
        self._fetch = fetch or _http_get
        self._now = now
        self._disk_free = disk_free or self._statvfs_free
        self.background = background
        self.ua = ua
        self.timeout = timeout
        self.count = 0
        self.bytes = 0
        self.disk_low = False
        self.writable = True
        self.last_use = 0.0
        self._lock = threading.Lock()          # count/bytes/inflight
        self._inflight = {}
        self._evicting = False
        self._build = (None, 0.0)              # (OFM build, when resolved)
        self._refreshing = set()
        # Stale tiles are re-fetched one at a time by ONE worker; when the queue is full a tile simply waits for
        # its next use. (A thread per stale hit put hundreds of downloads in flight at once on a 1 GB Pi.)
        self._rq = queue.Queue(maxsize=refresh_queue)
        self._rworker = None
        os.makedirs(root, exist_ok=True)

    # ── plumbing ─────────────────────────────────────────────
    def _statvfs_free(self):
        st = os.statvfs(self.root)
        return st.f_bavail * st.f_frsize

    def path(self, src, z, x, y):
        return os.path.join(self.root, src, str(z), str(x), '%d.%s' % (y, SOURCES[src]['ext']))

    def _headers(self):
        return {'User-Agent': '%s (+https://pilnk.io)' % self.ua}

    def _spawn(self, fn, *a):
        if self.background:
            threading.Thread(target=fn, args=a, daemon=True).start()
        else:
            fn(*a)

    def scan(self):
        """Count what is already on disk (startup). Leftover .tmp files are not tiles."""
        n = b = 0
        now = time.time()
        for d, _, files in os.walk(self.root):
            for f in files:
                if f.endswith('.tmp'):
                    try:                                   # a write cut off by a crash or power cut
                        if now - os.stat(os.path.join(d, f)).st_mtime > 3600:
                            os.remove(os.path.join(d, f))
                    except OSError:
                        pass
                    continue
                if f == 'state.json':
                    continue
                try:
                    b += os.stat(os.path.join(d, f)).st_size
                    n += 1
                except OSError:
                    pass
        with self._lock:
            self.count, self.bytes = n, b
        return n, b

    # ── upstream ─────────────────────────────────────────────
    def ofm_build(self):
        """OpenFreeMap rebuilds the planet weekly; the live build name comes from its TileJSON."""
        b, at = self._build
        if b and self._now() - at < 12 * 3600:
            return b
        try:
            st, body = self._fetch(OFM_TILEJSON, headers=self._headers(), timeout=self.timeout)
            if st != 200:
                raise ValueError('tilejson %s' % st)
            url = json.loads(body.decode('utf-8'))['tiles'][0]
            m = re.search(r'/planet/([0-9]{8}_[0-9]{6}_[a-z]+)/', url)
            if not m:
                raise ValueError('no build in %r' % url[:120])
            self._build = (m.group(1), self._now())
            return m.group(1)
        except Exception as e:
            if b:
                return b                       # last good build beats no map
            raise UpstreamError(502, 'openfreemap build: %s' % e)

    def upstream_url(self, src, z, x, y, build=None):
        if src == 'ofm':
            build = build or self.ofm_build()
            return SOURCES['ofm']['url'].format(b=build, z=z, x=x, y=y)
        return SOURCES[src]['url'].format(z=z, x=x, y=y)

    def _download(self, src, z, x, y, build=None):
        url = self.upstream_url(src, z, x, y, build)
        try:
            st, body = self._fetch(url, headers=self._headers(), timeout=self.timeout)
        except Exception as e:
            raise UpstreamError(502, str(e)[:120])
        if st == 404:
            raise UpstreamError(404, 'not found upstream')
        if st != 200 or body is None:
            raise UpstreamError(502, 'upstream %s' % st)
        if not looks_like_tile(src, body):
            raise UpstreamError(502, 'upstream sent something that is not a tile')
        return body

    # ── store ────────────────────────────────────────────────
    def put(self, src, z, x, y, data):
        """Write one tile atomically. False (not stored) when the disk is below the floor."""
        try:
            free = self._disk_free()
        except Exception:
            free = 0
        if free < self.floor:
            self.disk_low = True
            return False
        self.disk_low = False
        p = self.path(src, z, x, y)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
        except OSError:
            self.writable = False
            return False
        try:
            old = os.stat(p).st_size
            existed = True
        except OSError:
            old, existed = 0, False
        tmp = '%s.%d.%d.tmp' % (p, os.getpid(), threading.get_ident())
        try:
            with open(tmp, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())           # a power cut must not leave a short tile behind a good name
            now = self._now()
            os.utime(tmp, (now, now))
            os.replace(tmp, p)
        except OSError as e:
            # read-only or full card: the tile is still served, just not kept
            if self.writable:
                log.warning('[tiles] cannot write to %s (%s): serving without keeping', self.root, e)
            self.writable = False
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        self.writable = True
        with self._lock:
            self.bytes += len(data) - old
            if not existed:
                self.count += 1
            over = self.bytes > self.cap
        if over:
            self._spawn(self.evict)
        return True

    def touch(self, p, st=None):
        """Mark a tile used (atime), at most once a day; the fetch time (mtime) is kept."""
        try:
            st = st or os.stat(p)
            now = self._now()
            if now - st.st_atime > DAY:
                os.utime(p, (now, st.st_mtime))
        except OSError:
            pass

    def evict(self):
        with self._lock:
            if self._evicting or self.bytes <= self.cap:
                return
            self._evicting = True
        try:
            files = []
            for d, _, fs in os.walk(self.root):
                for f in fs:
                    if f.endswith('.tmp') or f == 'state.json':
                        continue
                    p = os.path.join(d, f)
                    try:
                        st = os.stat(p)
                        files.append((st.st_atime, st.st_size, p))
                    except OSError:
                        pass
            files.sort()
            total = sum(f[1] for f in files)
            target = int(self.cap * 0.9)
            gone_n = gone_b = 0
            for _, size, p in files:
                if total - gone_b <= target:
                    break
                try:
                    os.remove(p)
                    gone_n += 1
                    gone_b += size
                except OSError:
                    pass
            with self._lock:
                self.count, self.bytes = len(files) - gone_n, total - gone_b
            log.info('[tiles] evicted %d tiles (%.1f MB), store now %.1f MB', gone_n, gone_b / MB, (total - gone_b) / MB)
        finally:
            with self._lock:
                self._evicting = False

    def lookup(self, src, z, x, y):
        """(path, stat) of a stored tile, or (path, None)."""
        p = self.path(src, z, x, y)
        try:
            return p, os.stat(p)
        except OSError:
            return p, None

    def fresh(self, st):
        return st is not None and self._now() - st.st_mtime <= STALE

    def fetch_store(self, src, z, x, y, build=None):
        """Download one tile and store it. Returns the bytes."""
        data = self._download(src, z, x, y, build)
        self.put(src, z, x, y, data)
        return data

    def _refresh(self, src, z, x, y, build):
        key = (src, z, x, y)
        try:
            self.fetch_store(src, z, x, y, build)
        except Exception as e:
            log.info('[tiles] refresh %s/%d/%d/%d failed: %s', src, z, x, y, e)
        finally:
            with self._lock:
                self._refreshing.discard(key)

    def _refresh_worker(self):
        while True:
            item = self._rq.get()
            try:
                self._refresh(*item)
            finally:
                self._rq.task_done()

    def _queue_refresh(self, src, z, x, y, build):
        key = (src, z, x, y)
        if not self.background:
            self._refresh(src, z, x, y, build)
            return
        with self._lock:
            if self._rworker is None:
                self._rworker = threading.Thread(target=self._refresh_worker, daemon=True, name='tile-refresh')
                self._rworker.start()
        try:
            self._rq.put_nowait((src, z, x, y, build))
        except queue.Full:
            with self._lock:
                self._refreshing.discard(key)        # it will be queued again the next time someone looks at it

    def get(self, src, z, x, y, build=None, mark_use=True):
        """(bytes, 'hit'|'stale'|'miss'). Raises ValueError for a bad request, UpstreamError when a
        missing tile can't be fetched."""
        if not valid(src, z, x, y):
            raise ValueError('bad tile address')
        if build is not None and (src != 'ofm' or not BUILD_RE.fullmatch(build)):
            raise ValueError('bad build')
        if mark_use:
            self.last_use = self._now()
        p, st = self.lookup(src, z, x, y)
        if st is not None:
            try:
                with open(p, 'rb') as f:
                    data = f.read()
            except OSError:
                data = None
            if data is not None and not looks_like_tile(src, data):
                try:                                   # cut short or damaged: fetch it again below
                    os.remove(p)
                    with self._lock:
                        self.bytes -= st.st_size
                        self.count -= 1
                except OSError:
                    pass
                data = None
            if data is not None:
                self.touch(p, st)
                if self.fresh(st):
                    return data, 'hit'
                key = (src, z, x, y)
                with self._lock:
                    go = key not in self._refreshing
                    self._refreshing.add(key)
                if go:
                    self._queue_refresh(src, z, x, y, build)
                return data, 'stale'
        # a miss: one download per tile, however many browsers ask at once
        key = (src, z, x, y)
        with self._lock:
            ev = self._inflight.get(key)
            mine = ev is None
            if mine:
                ev = self._inflight[key] = {'done': threading.Event(), 'data': None, 'err': None}
        if not mine:
            ev['done'].wait(self.timeout + 5)
            if ev['data'] is not None:
                return ev['data'], 'miss'
            raise ev['err'] or UpstreamError(502, 'timed out waiting')
        try:
            data = self.fetch_store(src, z, x, y, build)
            ev['data'] = data
            return data, 'miss'
        except UpstreamError as e:
            ev['err'] = e
            raise
        except Exception as e:
            ev['err'] = UpstreamError(502, str(e)[:120])
            raise ev['err']
        finally:
            ev['done'].set()
            with self._lock:
                self._inflight.pop(key, None)


# ── the node's service: config, state, routes ───────────────────────────────
# config.json keys (all optional; read fresh, so the owner never has to restart):
#   tile_store     true  - serve and keep tiles for the 3D views
#   tile_store_mb  600   - disk cap in MB (50 .. 20000)
#   tile_prewarm   false - fetch the likely terrain overnight (only if the 3D views were used in 14 days).
#                          OPT-IN (AJ, 22 Nov 2026): off unless the owner switches it on in the dashboard
#                          (Options, 3D Terrain), which saves it here through POST /api/tiles/settings.
#   tile_prewarm_map false - also fetch map tiles overnight (only with OpenFreeMap's blessing)
DEFAULT_CAP_MB = 600
FLOOR_BYTES = 2 * GB


class Service(object):
    def __init__(self, root, config, location, paths, fetch=None, now=time.time, localtime=time.localtime,
                 disk_free=None, background=True, version='', prewarm_fn=None, runways=None, cfg_ttl=60,
                 sleep=time.sleep, scan=True, write=None):
        self.root = root
        self._write = write                     # write(key, value) into config.json; None = this service cannot save
        self.sleep = sleep
        self._config = config
        self.location = location
        self.paths = paths
        self.runways = runways
        self._now = now
        self._localtime = localtime
        self.prewarm_fn = prewarm_fn
        self.cfg_ttl = cfg_ttl
        self._cfg = (None, 0.0)
        self._good = None                       # last config.json that read cleanly
        self.prewarm_running = False
        self.last_report = None
        self._state_saved_use = 0.0
        self._plock = threading.Lock()
        self.reason = ''
        self.store = None
        try:
            self.store = TileStore(root, cap_bytes=DEFAULT_CAP_MB * MB, floor_bytes=FLOOR_BYTES, fetch=fetch, now=now,
                                   disk_free=disk_free, background=background, ua='PiLNK-node/%s' % (version or '?'))
            if scan:
                self.store.scan()
            self._load_state()
        except Exception as e:
            self.store = None
            self.reason = 'tile folder unusable: %s' % e
            log.warning('[tiles] store off: %s', self.reason)
        self.cfg()

    # config, fresh-read with a short cache (every tile request asks)
    def cfg(self):
        c, at = self._cfg
        if c is not None and self._now() - at < self.cfg_ttl:
            return c
        try:
            raw = self._config() or {}
            if not isinstance(raw, dict):
                raise ValueError('config is not an object')
            self._good = raw
        except Exception:
            # a half-written or hand-mangled config.json: keep the owner's last good settings
            raw = self._good if self._good is not None else {}
        cap = raw.get('tile_store_mb', DEFAULT_CAP_MB)
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            cap = DEFAULT_CAP_MB
        cap = max(50, min(20000, cap))
        c = {'enabled': raw.get('tile_store', True) is not False, 'cap_mb': cap,
             'prewarm': raw.get('tile_prewarm', False) is True,      # opt-in: only the owner's own true turns it on
             'prewarm_map': raw.get('tile_prewarm_map', False) is True}
        if self.store is not None:
            self.store.cap = cap * MB
        self._cfg = (c, self._now())
        return c

    def enabled(self):
        return self.store is not None and self.cfg()['enabled']

    # state.json: when the 3D views were last used, and the last pre-warm's report
    def _state_path(self):
        return os.path.join(self.root, 'state.json')

    def _load_state(self):
        try:
            with open(self._state_path()) as f:
                st = json.load(f)
            self.store.last_use = float(st.get('last_use') or 0)
            self._state_saved_use = self.store.last_use
            self.last_report = st.get('prewarm')
        except Exception:
            pass

    def save_state(self):
        if self.store is None:
            return
        try:
            tmp = self._state_path() + '.tmp'
            with open(tmp, 'w') as f:
                json.dump({'last_use': self.store.last_use, 'prewarm': self.last_report}, f)
            os.replace(tmp, self._state_path())
            self._state_saved_use = self.store.last_use
        except Exception as e:
            log.info('[tiles] state not saved: %s', e)

    def note_use(self):
        """After a browser request: persist 'last used' at most every 10 minutes."""
        if self.store is not None and self.store.last_use - self._state_saved_use > 600:
            self.save_state()

    def status(self):
        c = self.cfg()
        s = self.store
        out = {'enabled': bool(s is not None and c['enabled']), 'cap_mb': c['cap_mb'],
               'tiles': s.count if s else 0, 'mb': round((s.bytes if s else 0) / MB, 1),
               'disk_low': bool(s and s.disk_low), 'writable': bool(s and s.writable), 'last_use': int(s.last_use) if s and s.last_use else None,
               'prewarm': {'enabled': c['prewarm'], 'running': self.prewarm_running, 'last': self.last_report,
                           'window': '01:00-05:00 at the receiver, terrain only, only if the 3D views were used in the last 14 days'}}
        if s is None:
            out['reason'] = self.reason
        elif not c['enabled']:
            out['reason'] = 'switched off (config tile_store)'
        try:
            out['free_mb'] = int(s._disk_free() / MB) if s else None
        except Exception:
            out['free_mb'] = None
        return out

    def set_prewarm(self, on):
        """The owner's switch. Returns (saved, why). config.json is only replaced if it was read cleanly."""
        if self._write is None:
            return False, 'this node cannot save settings: not changed'
        try:
            self._write('tile_prewarm', bool(on))
        except Exception as e:
            return False, 'config.json could not be read or written (%s): not changed' % type(e).__name__
        self._cfg = (None, 0.0)                 # take effect now, not after the config cache runs out
        return True, 'saved'

    def start_prewarm(self, manual=False):
        if not self.enabled():
            return False, 'tile store off'
        if not self.cfg()['prewarm']:
            return False, 'prewarm off'
        lat, lon = (self.location() or (None, None))
        if lat is None or lon is None:
            return False, 'no location'
        with self._plock:
            if self.prewarm_running:
                return False, 'running'
            self.prewarm_running = True
        fn = self.prewarm_fn or prewarm

        def go():
            started = self._now()
            try:
                rep = fn(self, manual)
            except Exception as e:
                log.warning('[tiles] prewarm failed: %s', e)
                rep = {'stopped': 'error: %s' % str(e)[:120]}
            rep = dict(rep or {})
            rep.setdefault('started', started)        # a crashed run still counts as tonight's run
            rep.setdefault('manual', manual)
            self.last_report = rep
            self.save_state()
            with self._plock:
                self.prewarm_running = False
        threading.Thread(target=go, daemon=True, name='tile-prewarm').start()
        return True, 'started'


def register(app, svc):
    from flask import Response, jsonify, request

    def tiles_status():
        return jsonify(svc.status())

    def tiles_get(src, z, x, y):
        # Only the node's own pages may make it download. The app-wide guard lets cross-site NAVIGATIONS through
        # (so links to the dashboard work); a tile is never a page, so refuse anything not same-origin here.
        site = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
        if site and site not in ('same-origin', 'none'):
            return jsonify({'error': 'cross-site request refused'}), 403
        if not svc.enabled():
            return jsonify({'error': 'tile store off'}), 503
        if src not in SOURCES:
            return jsonify({'error': 'unknown source'}), 404
        try:
            data, state = svc.store.get(src, z, x, y, build=request.args.get('b'))
        except ValueError as e:
            return jsonify({'error': str(e)}), 400
        except UpstreamError as e:
            return jsonify({'error': 'upstream', 'status': e.status}), (404 if e.status == 404 else 502)
        svc.note_use()
        resp = Response(data, mimetype=SOURCES[src]['ctype'])
        resp.headers['X-Tile'] = state
        resp.headers['Cache-Control'] = 'no-store'      # the page keeps its own copy (IndexedDB); don't keep a third
        return resp

    def tiles_prewarm():
        ok, why = svc.start_prewarm(manual=True)
        return jsonify({'started': ok, 'reason': why}), (202 if ok else 409)

    def tiles_settings():
        # The dashboard's "Fetch 3D terrain overnight" switch. Same-origin only (the app-wide guard says so too).
        site = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
        if site and site not in ('same-origin', 'none'):
            return jsonify({'error': 'cross-site request refused'}), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or not isinstance(body.get('prewarm'), bool):
            return jsonify({'error': 'send {"prewarm": true} or {"prewarm": false}'}), 400
        ok, why = svc.set_prewarm(body['prewarm'])
        if not ok:
            return jsonify({'error': why}), 409
        return jsonify(svc.status())

    app.add_url_rule('/api/tiles/status', 'tiles_status', tiles_status, methods=['GET'])
    app.add_url_rule('/api/tiles/settings', 'tiles_settings', tiles_settings, methods=['POST'])
    app.add_url_rule('/api/tiles/<src>/<int:z>/<int:x>/<int:y>', 'tiles_get', tiles_get, methods=['GET'])
    app.add_url_rule('/api/tiles/prewarm', 'tiles_prewarm', tiles_prewarm, methods=['POST'])


# ── overnight pre-warm ──────────────────────────────────────────────────────
# What the chase camera asks for (measured 20 Nov in the harness, 50 s of an arrival): terrain at
# zoom 15 within ~13 km of the aircraft, 13-14 a little wider, 8-12 out to the horizon; map tiles
# at 13-14 within ~15 km. So the plan is:
#   1. the nearest airport's runways: the extended centreline 30 km out each way, at z15 within
#      2.5 km of it, z14 within 5 km, z13 within 9 km (map tiles stop at z14, their maximum);
#   2. learned arrival paths (Auckland today), the parts within 50 km, same widths;
#   3. a coarse circle: z8-12 within 60 km of the receiver; z13 within 25 km;
#   4. the other airports within 50 km (runways of 3,000 ft or more; at most 4 airports).
# Every node can do this: runways come from pilnk.io's world table (asked at 1 dp, like the rest of
# the node's calls), terrain and map tiles cover the planet.
# Manners: one download at a time with a pause, on one kept-alive connection. The nightly job fetches
# TERRAIN only (AWS open data, 6,000 tiles / 500 MB a night at most): OpenFreeMap allows offline
# downloads a user starts, not unattended ones, so map tiles are only kept as people watch
# (tile_prewarm_map: true adds them, within their 3,000 tiles / 50 MB rule, if they ever agree).
# A tile already here and under 30 days old is not fetched again - it is only marked used, so
# eviction takes the browsing leftovers and leaves the planned areas alone. The plan itself may use
# at most 70% of the cap, so there is always room for what you actually watch. It only runs between
# 01:00 and 05:00 at the receiver (hour from its longitude - the image sets no time zone), at most
# once a day, and only if the 3D views were used in the last 14 days: a node nobody watches
# downloads nothing.
KM_DEG = 111.32
PLAN = {'lane_km': 30.0, 'half': {13: 9.0, 14: 5.0, 15: 2.5}, 'disc_km': 60.0, 'disc_z': (8, 9, 10, 11, 12),
        'near_km': 25.0, 'path_km': 50.0, 'airport_km': 50.0, 'min_ft': 3000, 'max_airports': 4}
BUDGETS = {'dem': {'tiles': 6000, 'bytes': 500 * MB}, 'ofm': {'tiles': 2500, 'bytes': 45 * MB}}
PAUSE = 0.05
RUNWAYS_URL = 'https://pilnk.io/api/runways.php?lat=%.1f&lon=%.1f&limit=8'


def _km(a, b):
    return math.hypot((a[0] - b[0]) * KM_DEG, (a[1] - b[1]) * KM_DEG * math.cos(math.radians(a[0])))


def deg2tile(lat, lon, z):
    lat = max(-85.05112878, min(85.05112878, lat))
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    la = math.radians(lat)
    y = int((1.0 - math.log(math.tan(la) + 1.0 / math.cos(la)) / math.pi) / 2.0 * n)
    return min(max(x, 0), n - 1), min(max(y, 0), n - 1)


def tile_bounds(z, x, y):
    """(north, south, west, east) in degrees."""
    n = float(1 << z)

    def lat(yy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * yy / n))))
    return lat(y), lat(y + 1), x / n * 360.0 - 180.0, (x + 1) / n * 360.0 - 180.0


def tiles_disc(lat, lon, r_km, z):
    dlat = r_km / KM_DEG
    dlon = r_km / (KM_DEG * math.cos(math.radians(lat)))
    x0, y0 = deg2tile(lat + dlat, lon - dlon, z)
    x1, y1 = deg2tile(lat - dlat, lon + dlon, z)
    out = set()
    for x in range(x0, x1 + 1):
        for y in range(y0, y1 + 1):
            n, s, w, e = tile_bounds(z, x, y)
            if _km((lat, lon), (min(max(lat, s), n), min(max(lon, w), e))) <= r_km:
                out.add((x, y))
    return out


def tiles_corridor(line, half_km, z):
    """Tiles within half_km of a polyline [(lat, lon), ...]: sampled finer than a tile."""
    out = set()
    for a, b in zip(line, line[1:]):
        c = math.cos(math.radians(a[0]))
        dn, de = (b[0] - a[0]) * KM_DEG, (b[1] - a[1]) * KM_DEG * c
        seg = math.hypot(dn, de)
        un, ue = (dn / seg, de / seg) if seg > 1e-9 else (1.0, 0.0)
        pn, pe = -ue, un
        tile_km = 40075.017 * c / (1 << z)
        step = max(0.05, min(tile_km / 2.0, half_km / 2.0))
        for i in range(int(seg / step) + 2):
            t = min(seg, i * step)
            for j in range(int(2 * half_km / step) + 2):
                o = -half_km + min(2 * half_km, j * step)
                out.add(deg2tile(a[0] + (un * t + pn * o) / KM_DEG, a[1] + (ue * t + pe * o) / (KM_DEG * c), z))
    return out


def runway_line(r, ext_km=30.0):
    """[30 km out beyond the low end, low end, high end, 30 km out beyond the high end]."""
    le, he = (float(r['le_lat']), float(r['le_lon'])), (float(r['he_lat']), float(r['he_lon']))
    c = math.cos(math.radians(le[0]))
    dn, de = (le[0] - he[0]) * KM_DEG, (le[1] - he[1]) * KM_DEG * c
    L = math.hypot(dn, de) or 1e-9
    un, ue = dn / L, de / L

    def move(p, k):
        return (p[0] + un * k / KM_DEG, p[1] + ue * k / (KM_DEG * c))
    return [move(le, ext_km), le, he, move(he, -ext_km)]


def pick_airports(airports, lat, lon, cfg=PLAN):
    out = []
    for a in airports or []:
        try:
            d = _km((lat, lon), (float(a['lat']), float(a['lon'])))
        except (KeyError, TypeError, ValueError):
            continue
        if d > cfg['airport_km']:
            continue
        rw = []
        for r in a.get('runways') or []:
            try:
                if (r.get('length_ft') or 0) >= cfg['min_ft'] and None not in (r.get('le_lat'), r.get('le_lon'), r.get('he_lat'), r.get('he_lon')):
                    float(r['le_lat']); float(r['he_lon'])
                    rw.append(r)
            except (TypeError, ValueError):
                pass
        if rw:
            b = dict(a); b['runways'] = rw; b['_km'] = d
            out.append(b)
    # The airport that matters is the big one, not the nearest strip: longest runway first, then distance.
    out.sort(key=lambda a: (-max(r.get('length_ft') or 0 for r in a['runways']), a['_km']))
    return out[:cfg['max_airports']]


def plan(lat, lon, airports, paths, cfg=PLAN, sources=('dem', 'ofm')):
    """Every tile to have on disk, most useful first: [(src, z, x, y), ...] without repeats."""
    items, seen = [], set()

    def add(src, z, tiles):
        if src not in sources:
            return
        for t in sorted(tiles, key=lambda t: _km((lat, lon), _centre(z, t[0], t[1]))):
            k = (src, z) + t
            if k not in seen:
                seen.add(k)
                items.append(k)

    def corridors(lines):
        for z in (13, 14, 15):
            tiles = set()
            for ln in lines:
                tiles |= tiles_corridor(ln, cfg['half'][z], z)
            add('dem', z, tiles)
            if z <= SOURCES['ofm']['maxz']:
                add('ofm', z, tiles)

    picked = pick_airports(airports, lat, lon, cfg)
    if picked:
        corridors([runway_line(r, cfg['lane_km']) for r in picked[0]['runways']])
    near = []
    for p in paths or []:
        pts = []
        for q in p:
            try:
                pt = (float(q[0]), float(q[1]))
            except (TypeError, ValueError, IndexError):
                continue
            if _km((lat, lon), pt) <= cfg['path_km']:
                pts.append(pt)
        if len(pts) >= 2:
            near.append(pts)
    if near:
        corridors(near)
    for z in cfg['disc_z']:
        t = tiles_disc(lat, lon, cfg['disc_km'], z)
        add('dem', z, t)
        add('ofm', z, t)
    t = tiles_disc(lat, lon, cfg['near_km'], 13)
    add('dem', 13, t)
    add('ofm', 13, t)
    for a in picked[1:]:
        corridors([runway_line(r, cfg['lane_km']) for r in a['runways']])
    return items


def _centre(z, x, y):
    n, s, w, e = tile_bounds(z, x, y)
    return (n + s) / 2.0, (w + e) / 2.0


def run(store, items, budgets=BUDGETS, should_stop=None, sleep=time.sleep, pause=PAUSE):
    """Fetch what the plan lacks, politely. Returns a report (also shown in /api/tiles/status)."""
    rep = {'started': store._now(), 'planned': len(items), 'present': 0, 'fetched': {'dem': 0, 'ofm': 0},
           'bytes': {'dem': 0, 'ofm': 0}, 'over_budget': {'dem': 0, 'ofm': 0}, 'missing': 0, 'errors': 0,
           'stopped': 'done'}
    build, ofm_ok = None, True
    # The limit is on the PLAN's own size: an LRU store is always nearly full of what was watched, and measuring
    # the whole store would stop the warm-up for good once it filled. Planned tiles are marked used every night,
    # so it is the browsing leftovers that eviction removes.
    plan_bytes, full = 0, False
    for src, z, x, y in items:
        if should_stop and should_stop():
            rep['stopped'] = 'switched off'
            break
        p, st = store.lookup(src, z, x, y)
        if store.fresh(st):
            store.touch(p, st)
            rep['present'] += 1
            plan_bytes += st.st_size
            continue
        if full:
            rep['over_budget'][src] += 1
            continue
        b = budgets[src]
        if rep['fetched'][src] >= b['tiles'] or rep['bytes'][src] >= b['bytes'] or (src == 'ofm' and not ofm_ok):
            rep['over_budget'][src] += 1
            continue
        if plan_bytes >= 0.7 * store.cap:
            rep['stopped'] = 'store 70% full'
            full = True                          # keep walking: tiles already here still get marked used
            rep['over_budget'][src] += 1
            continue
        try:
            low = store._disk_free() < store.floor
        except Exception:
            low = True
        if low:
            rep['stopped'] = 'disk low'
            break
        if src == 'ofm' and build is None:
            try:
                build = store.ofm_build()
            except UpstreamError:
                ofm_ok = False
                rep['ofm'] = 'unavailable'
                rep['over_budget']['ofm'] += 1
                continue
        try:
            data = store.fetch_store(src, z, x, y, build if src == 'ofm' else None)
            rep['fetched'][src] += 1
            rep['bytes'][src] += len(data)
            plan_bytes += len(data)
        except UpstreamError as e:
            if e.status == 404:
                rep['missing'] += 1
            else:
                rep['errors'] += 1
                if rep['errors'] >= 25:
                    rep['stopped'] = 'upstream errors'
                    break
        sleep(pause)
    if rep['stopped'] == 'done' and (rep['over_budget']['dem'] or rep['over_budget']['ofm']):
        rep['stopped'] = 'nightly budget'
    rep['finished'] = store._now()
    return rep


def fetch_runways(store, lat, lon):
    st, body = store._fetch(RUNWAYS_URL % (lat, lon), headers=store._headers(), timeout=store.timeout)
    if st != 200:
        return []
    return (json.loads(body.decode('utf-8')) or {}).get('airports') or []


def prewarm(svc, manual=False):
    lat, lon = svc.location()
    airports = []
    try:
        airports = svc.runways(lat, lon) if svc.runways else fetch_runways(svc.store, lat, lon)
    except Exception as e:
        log.info('[tiles] runways unavailable, warming the circle only: %s', e)
    paths = []
    try:
        paths = [p for p in (svc.paths() or []) if isinstance(p, (list, tuple)) and len(p) >= 2]
    except Exception:
        pass
    picked = pick_airports(airports, lat, lon)
    # OpenFreeMap allows offline downloads that a USER starts; this job runs by itself at night, so it fetches
    # terrain only (AWS open data, the slow part). Map tiles are kept as people watch. tile_prewarm_map: true
    # in config.json adds them back (within their 3,000 tiles / 50 MB rule) - only with their blessing.
    sources = ('dem', 'ofm') if svc.cfg().get('prewarm_map') else ('dem',)
    items = plan(lat, lon, airports, paths, sources=sources)
    rep = run(svc.store, items, BUDGETS, should_stop=lambda: not svc.enabled() or not svc.cfg()['prewarm'],
              sleep=svc.sleep)
    rep['airports'] = [a.get('icao') for a in picked]
    rep['paths'] = len(paths)
    rep['manual'] = manual
    log.info('[tiles] pre-warm %s: %d planned, %d already here, fetched %d terrain (%.1f MB) + %d map (%.1f MB), stopped: %s',
             'manual' if manual else 'nightly', rep['planned'], rep['present'], rep['fetched']['dem'], rep['bytes']['dem'] / MB,
             rep['fetched']['ofm'], rep['bytes']['ofm'] / MB, rep['stopped'])
    return rep


def due(svc):
    c = svc.cfg()
    if not svc.enabled() or not c.get('prewarm') or svc.prewarm_running:
        return False
    now = svc._now()
    lu = getattr(svc.store, 'last_use', 0) or 0
    if not lu or now - lu > 14 * DAY:
        return False
    try:
        lon = (svc.location() or (None, None))[1]
    except Exception:
        lon = None
    if lon is None:
        return False
    # The receiver's own night: PiLNK OS never sets a time zone, so the system clock may say London at an
    # Auckland node. Hour from longitude (UTC + lon/15) is within ~1 h of local clock time almost everywhere.
    hour = ((now % DAY) / 3600.0 + float(lon) / 15.0) % 24
    if not (1 <= hour < 5):
        return False
    last = svc.last_report or {}
    try:
        if last.get('started') and now - float(last['started']) < 20 * 3600:
            return False
    except (TypeError, ValueError):
        pass
    return True


def scheduler(svc, interval=600, sleep=time.sleep):
    while True:
        sleep(interval)
        try:
            if due(svc):
                svc.start_prewarm(manual=False)
        except Exception as e:
            log.warning('[tiles] scheduler: %s', e)


def config_reader(path):
    def config():
        with open(path) as f:
            return json.load(f)
    return config


def config_writer(path):
    """write(key, value): set one key in config.json and keep everything else exactly as it was. The file also
    holds the node's PiLNK Code, so it is only ever replaced after a clean read, in one step (temp file, fsync,
    rename), with its permissions kept."""
    def write(key, value):
        with open(path) as f:
            cfg = json.load(f)                  # unreadable: raise, and change nothing
        if not isinstance(cfg, dict):
            raise ValueError('config.json is not an object')
        cfg[key] = value
        mode = stat.S_IMODE(os.stat(path).st_mode)
        tmp = path + '.tiles.tmp'
        try:
            with open(tmp, 'w') as f:
                json.dump(cfg, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return write


def start(app, config_path, location, paths, version=''):
    """The whole of app.py's part: one call. Never raises into app.py's startup."""
    root = os.path.join(os.path.expanduser('~'), '.cache', 'pilnk', 'tiles')
    svc = Service(root, config_reader(config_path), location, paths, version=version, scan=False,
                  write=config_writer(config_path))
    register(app, svc)

    def background():
        if svc.store is not None:
            try:
                n, b = svc.store.scan()          # count what is on the card without holding up the dashboard
                log.info('[tiles] store holds %d tiles (%.1f MB)', n, b / MB)
            except Exception as e:
                log.warning('[tiles] scan failed: %s', e)
        scheduler(svc)
    threading.Thread(target=background, daemon=True, name='tile-scheduler').start()
    log.info('[tiles] hub tile store %s at %s', 'on' if svc.enabled() else 'OFF (%s)' % svc.reason, root)
    return svc

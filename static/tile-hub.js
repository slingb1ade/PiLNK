/* tile-hub.js - the 3D views fetch their map tiles through this node's tile store (21 Nov 2026).

   3D Approach and 3D Sky keep tiles in the browser (IndexedDB), so a phone, a laptop and the kiosk each
   started from nothing, and every new area waited on AWS's terrain servers in Virginia (~0.9 s a tile from
   New Zealand). The node now keeps one shared copy on its own disk (tile_store.py) and fetches the likely
   areas overnight. This file is the browser half: it sends the terrain (AWS) and map (OpenFreeMap) tile
   requests to the node instead of the internet.

   Fail-safe by design - the node only ever makes things faster, never breaks the map:
     - the node is asked once (/api/tiles/status); an older node, a node with the store switched off,
       or a page not served by a node (pilnk.io /3d/) gets the old behaviour, straight from the source;
     - any tile the node can't give (an error, or no answer within 5 s) is fetched from the source instead,
       and after 3 failures in a row the node is left alone for a minute;
     - satellite (Esri) and anything else that isn't a terrain or map tile always goes straight to its
       source - the node never stores those;
     - ?hubtiles=0 on the page address skips the node completely (for comparing).
   window.pilnkTileNet(url, opts) is a drop-in for fetch(url, opts). */
(function (root) {
  'use strict';
  var DEM = /^https:\/\/s3\.amazonaws\.com\/elevation-tiles-prod\/terrarium\/(\d{1,2})\/(\d{1,6})\/(\d{1,6})\.png$/;
  var OFM = /^https:\/\/tiles\.openfreemap\.org\/planet\/(\d{8}_\d{6}_[a-z]+)\/(\d{1,2})\/(\d{1,6})\/(\d{1,6})\.pbf$/;

  function hubPath(u) {
    var m = DEM.exec(u);
    if (m) return '/api/tiles/dem/' + m[1] + '/' + m[2] + '/' + m[3];
    m = OFM.exec(u);
    if (m) return '/api/tiles/ofm/' + m[2] + '/' + m[3] + '/' + m[4] + '?b=' + m[1];
    return null;
  }

  var cfg = { hubMs: 5000, probeMs: 3000, failLimit: 3, coolMs: 60000, now: function () { return Date.now(); } };
  function configure(o) { for (var k in o) cfg[k] = o[k]; }      // tests only
  var fails = 0, coolUntil = 0;

  // fetch with our own deadline, still honouring the map's own abort
  function timed(url, opts, ms, flag) {
    var ctl = new AbortController(), outer = opts && opts.signal;
    if (outer && typeof outer.addEventListener === 'function') {
      if (outer.aborted) ctl.abort(); else outer.addEventListener('abort', function () { ctl.abort(); }, { once: true });
    }
    var timer = setTimeout(function () { if (flag) flag.v = true; ctl.abort(); }, ms);
    var o = {}; for (var k in (opts || {})) o[k] = opts[k]; o.signal = ctl.signal;
    return fetch(url, o).finally(function () { clearTimeout(timer); });
  }

  var probe = null;
  function hubOn() {
    if (probe) return probe;
    var off = false;
    try { off = new URLSearchParams(location.search).get('hubtiles') === '0'; } catch (e) {}
    probe = off ? Promise.resolve(false)
      : timed('/api/tiles/status', { cache: 'no-store' }, cfg.probeMs)
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (j) { return !!(j && j.enabled === true); })
          .catch(function () { return false; });
    return probe;
  }

  function nodeFailed() {
    if (++fails >= cfg.failLimit) { fails = 0; coolUntil = cfg.now() + cfg.coolMs; }
  }

  async function tileNet(url, opts) {
    var p = hubPath(url);
    if (p && cfg.now() >= coolUntil && await hubOn()) {
      var late = { v: false };
      try {
        var r = await timed(p, opts, cfg.hubMs, late);
        if (r.ok) { fails = 0; return r; }
        if (r.status >= 500) nodeFailed();                      // the node is struggling; a 404 is the tile's own answer
      } catch (e) {
        var outer = opts && opts.signal;
        if ((outer && outer.aborted) || (e && e.name === 'AbortError' && !late.v)) throw e;   // the map no longer wants it
        nodeFailed();
      }
    }
    return fetch(url, opts);
  }

  var api = { hubPath: hubPath, hubOn: hubOn, tileNet: tileNet, configure: configure };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (root) { root.pilnkTileNet = tileNet; root.pilnkTileHub = api; }
})(typeof window !== 'undefined' ? window : null);

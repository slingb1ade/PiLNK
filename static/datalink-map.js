/* PiLNK - datalink on the 2D map (ACARS/VDL2 item 4, 8 Oct 2026).
 *
 * Three extras, all from what the aircraft reported about itself (a.datalink):
 *  - a second label line 'NZAA -> NZWN' (routeLine, used by updateLabel),
 *  - a dashed great-circle line from the SELECTED plane to its destination,
 *  - a ring with 'N min' on planes landing at this node's airport within the hour.
 * skyView() gives the 3D view the same three things, from the same calculation (no drift).
 *
 * Local only (hard rule): nothing here sends anything anywhere. The destination's
 * position comes from the curated airport list or the airport database the
 * dashboard already downloads whole (OurAirports) - never a lookup by code.
 *
 * Every value came over the air: codes must be 4 capital letters and numbers must
 * be finite before they touch the map. ASCII-only, no backslashes (see datalink-card.js).
 */
(function () {
  'use strict';
  var CH = String.fromCharCode, ARROW = CH(0x2192);
  var ICAO = /^[A-Z]{4}$/, RING_MAX_S = 3600, GC_STEPS = 64;
  var R2D = 180 / Math.PI, D2R = Math.PI / 180;

  function ok(v) { return typeof v === 'string' && ICAO.test(v); }
  function num(v) { return typeof v === 'number' && isFinite(v); }
  function landed(d) { return num(d.on_ts) && d.on_ts > 0 || num(d.in_ts) && d.in_ts > 0; }

  function routeLine(d) {
    if (!d || typeof d !== 'object' || !ok(d.dest)) return '';
    return (ok(d.orig) ? d.orig + ' ' : '') + ARROW + ' ' + d.dest;
  }

  // Whole minutes to touchdown (rounded up) for a plane landing at `station` within the hour, else null.
  function arrivalMin(d, station, now) {
    if (!d || typeof d !== 'object' || !ok(station) || d.dest !== station || !num(now)) return null;
    if (!num(d.eta_ts) || landed(d)) return null;
    var s = d.eta_ts - now;
    if (!(s > 0) || s > RING_MAX_S) return null;
    return Math.ceil(s / 60);
  }

  // n+1 points along the great circle. Longitudes are unwrapped so each step is short:
  // a flight over 180 is drawn as lon > 180 (or < -180) instead of jumping across the map.
  function gcPoints(lat1, lon1, lat2, lon2, n) {
    if (!num(lat1) || !num(lon1) || !num(lat2) || !num(lon2)) return [];
    n = (num(n) && n >= 1) ? Math.floor(n) : GC_STEPS;
    var p1 = lat1 * D2R, l1 = lon1 * D2R, p2 = lat2 * D2R, l2 = lon2 * D2R;
    var x1 = Math.cos(p1) * Math.cos(l1), y1 = Math.cos(p1) * Math.sin(l1), z1 = Math.sin(p1);
    var x2 = Math.cos(p2) * Math.cos(l2), y2 = Math.cos(p2) * Math.sin(l2), z2 = Math.sin(p2);
    var dot = Math.max(-1, Math.min(1, x1 * x2 + y1 * y2 + z1 * z2));
    var w = Math.acos(dot), sw = Math.sin(w);
    var out = [[lat1, lon1]], prev = lon1, i;
    if (sw < 1e-9) {                                 // same point (or antipodal): a straight stub is safe
      out.push([lat2, lon2]);
      return out;
    }
    for (i = 1; i <= n; i++) {
      var f = i / n, a = Math.sin((1 - f) * w) / sw, b = Math.sin(f * w) / sw;
      var x = a * x1 + b * x2, y = a * y1 + b * y2, z = a * z1 + b * z2;
      var lat = Math.atan2(z, Math.sqrt(x * x + y * y)) * R2D, lon = Math.atan2(y, x) * R2D;
      if (i === n) { lat = lat2; lon = lon2; }
      while (lon - prev > 180) lon -= 360;
      while (lon - prev < -180) lon += 360;
      out.push([lat, lon]);
      prev = lon;
    }
    return out;
  }

  // icao -> {lat, lon}. A miss is remembered only against the DB it was looked up in (the DB is ~80k rows:
  // never rescan it every second); before the DB loads, or when a new one arrives, codes are looked up again.
  var found = {}, missed = {}, missDb = null;
  function airportLatLon(icao, list, db) {
    if (!ok(icao)) return null;
    if (Object.prototype.hasOwnProperty.call(found, icao)) return found[icao];
    if (Array.isArray(db) && db === missDb && missed[icao]) return null;
    var hit = null, i;
    if (Array.isArray(list)) {
      for (i = 0; i < list.length && !hit; i++) {
        var a = list[i];
        if (a && a.icao === icao && num(a.lat) && num(a.lon)) hit = { lat: a.lat, lon: a.lon };
      }
    }
    if (!hit && Array.isArray(db)) {
      for (i = 0; i < db.length; i++) {
        var r = db[i];
        if (r && (r.ident === icao || r.icao_code === icao || r.gps_code === icao)) {
          var la = parseFloat(r.latitude_deg), lo = parseFloat(r.longitude_deg);
          if (num(la) && num(lo) && Math.abs(la) <= 90 && Math.abs(lo) <= 180) hit = { lat: la, lon: lo };
          break;
        }
      }
    }
    if (hit) found[icao] = hit;
    else if (Array.isArray(db)) {
      if (db !== missDb) { missDb = db; missed = {}; }
      missed[icao] = true;
    }
    return hit;
  }

  // ---- what to show: ONE calculation for the 2D map and the 3D view (8 Oct 2026, AJ: no drift between them) ----
  var dbAskedAt = null, DB_RETRY_S = 300, HEX = /^[0-9a-f~]{6,7}$/;

  // The selected plane's line: {cs, dest, pts: [[lat, lon], ...]} or null. Asks for the airport file when needed.
  function lineFor(o, sel) {
    var a = sel ? o.aircraft[sel] : null, d = a && a.datalink;
    if (!a || !d || typeof d !== 'object' || !ok(d.dest) || landed(d) || !num(a.lat) || !num(a.lon)) return null;
    var ll = airportLatLon(d.dest, o.airports, o.db);
    if (!ll) {
      // The whole public airport file, never a lookup by code. A failed download is tried again after 5 min.
      if (!o.db && typeof o.loadDb === 'function' && num(o.now) && (dbAskedAt === null || o.now - dbAskedAt >= DB_RETRY_S)) {
        dbAskedAt = o.now;
        try { o.loadDb(); } catch (e) { /* no DB: no line */ }
      }
      return null;
    }
    var pts = gcPoints(a.lat, a.lon, ll.lat, ll.lon, GC_STEPS);
    return pts.length < 2 ? null : { cs: sel, dest: d.dest, pts: pts };
  }

  // Planes landing at this node's airport within the hour: [{cs, a, m}]
  function ringsFor(o) {
    var out = [], cs;
    for (cs in o.aircraft) {
      if (!Object.prototype.hasOwnProperty.call(o.aircraft, cs)) continue;
      var a = o.aircraft[cs], m = a ? arrivalMin(a.datalink, o.station, o.now) : null;
      if (m !== null && num(a.lat) && num(a.lon)) out.push({ cs: cs, a: a, m: m });
    }
    return out;
  }

  function hexOf(a) { var h = a && typeof a.hex === 'string' ? a.hex.toLowerCase() : ''; return HEX.test(h) ? h : null; }

  // What the dashboard sends the 3D page: {ac: {hex: {route, min}}, line: {hex, dest, pts: [[lon, lat], ...]} | null}
  function skyView(o) {
    var out = { ac: {}, line: null };
    if (!o || !o.aircraft || typeof o.aircraft !== 'object') return out;
    var cs, h, r5 = function (v) { return Math.round(v * 1e5) / 1e5; };
    for (cs in o.aircraft) {
      if (!Object.prototype.hasOwnProperty.call(o.aircraft, cs)) continue;
      var a = o.aircraft[cs], r = a ? routeLine(a.datalink) : '';
      h = hexOf(a);
      if (h && r) out.ac[h] = { route: r };
    }
    ringsFor(o).forEach(function (x) { h = hexOf(x.a); if (h) { out.ac[h] = out.ac[h] || {}; out.ac[h].min = x.m; } });
    var ln = o.selected ? lineFor(o, o.selected) : null;
    h = ln ? hexOf(o.aircraft[ln.cs]) : null;
    if (ln && h) out.line = { hex: h, dest: ln.dest, pts: ln.pts.map(function (p) { return [r5(p[1]), r5(p[0])]; }) };
    return out;
  }

  // ---- 2D map layers (called every 1 s tick and when the selection changes) ----
  var line = null, lineDest = null, rings = {};

  function clearLine(map) {
    if (line) { map.removeLayer(line.path); map.removeLayer(line.end); }
    line = null; lineDest = null;
  }

  function syncLine(o) {
    var ln = lineFor(o, o.selected);
    if (!ln) { clearLine(o.map); return; }
    var end = ln.pts[ln.pts.length - 1];
    if (line && lineDest === ln.cs + '|' + ln.dest) {
      line.path.setLatLngs(ln.pts);
      line.end.setLatLng(end);
      return;
    }
    clearLine(o.map);
    line = {
      path: o.L.polyline(ln.pts, { color: '#38bdf8', weight: 2, opacity: 0.85, dashArray: '6 6', interactive: false }).addTo(o.map),
      end: o.L.marker(end, { interactive: false, keyboard: false,
        icon: o.L.divIcon({ className: 'dl-dest', html: '<span class="dl-dest-dot"></span><span class="dl-dest-txt">' + ln.dest + '</span>', iconSize: [0, 0] }) }).addTo(o.map)
    };
    lineDest = ln.cs + '|' + ln.dest;
  }

  function syncRings(o) {
    var keep = {}, cs;
    ringsFor(o).forEach(function (x) {
      var a = x.a, m = x.m; cs = x.cs;
      keep[cs] = true;
      var html = '<div class="dl-ring"></div><span class="dl-ring-min">' + m + ' min</span>';
      if (rings[cs]) {
        rings[cs].mk.setLatLng([a.lat, a.lon]);
        if (rings[cs].m !== m) { rings[cs].mk.setIcon(o.L.divIcon({ className: 'dl-ring-wrap', html: html, iconSize: [28, 28], iconAnchor: [14, 14] })); rings[cs].m = m; }
      } else {
        rings[cs] = { m: m, mk: o.L.marker([a.lat, a.lon], { interactive: false, keyboard: false, zIndexOffset: -200,
          icon: o.L.divIcon({ className: 'dl-ring-wrap', html: html, iconSize: [28, 28], iconAnchor: [14, 14] }) }).addTo(o.map) };
      }
    });
    for (cs in rings) {
      if (Object.prototype.hasOwnProperty.call(rings, cs) && !keep[cs]) { o.map.removeLayer(rings[cs].mk); delete rings[cs]; }
    }
  }

  // o: {map, L, aircraft: {cs: a}, selected: cs|null, station: ICAO, now: epoch s, airports, db, loadDb}
  function sync(o) {
    if (!o || !o.map || !o.L || !o.aircraft) return;
    syncLine(o);
    syncRings(o);
  }

  function state() {
    var r = {}, cs;
    for (cs in rings) if (Object.prototype.hasOwnProperty.call(rings, cs)) r[cs] = rings[cs].m;
    return { line: lineDest, points: line ? line.path.getLatLngs().length : 0, rings: r };
  }

  window.dlMap = { routeLine: routeLine, arrivalMin: arrivalMin, gcPoints: gcPoints, airportLatLon: airportLatLon,
                   sync: sync, state: state, skyView: skyView };
})();

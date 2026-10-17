/* PiLNK - Arrival Paths on the dashboard (proof of concept, 17 Oct 2026).
 *
 * pilnk.io learns the arrival paths aircraft really fly into Auckland (NZAA)
 * from PiLNK's own ADS-B history. This file:
 *   - asks the node for them (/api/arrival-paths, which proxies pilnk.io with the
 *     node's own code; off unless config.json has "arrival_paths": true),
 *   - draws them in the Aviation Overlay (colour by runway, width by flights,
 *     the runway in use bright, the other dimmed),
 *   - tells the cinematic card which path a selected arriving aircraft is on.
 * Datalink "next fix" is only read here, in the browser, to add a tick. Nothing
 * goes anywhere from this file except the GET to the node itself.
 * Spec: pilnk-tasks/arrival-paths-spec.md
 */
(function (root) {
  'use strict';

  var C = {
    refLat: -37.0081, refLon: 174.7917,          // NZAA reference point
    rwys: { '05R': 72, '23L': 252 },             // true headings
    colours: { '05R': '#c084fc', '23L': '#fb923c' },
    matchKm: 4,                                  // best path under 4 km mean distance
    lookS: 300,                                  // ...over the last 5 minutes of track
    lockKm: 35,                                  // a match made further out than this sticks (paths share the final)
    keepEveryS: 10,                              // track memory: one point per 10 s
    activeS: 1800,                               // active runway from the last 30 min of landings
    dimOpacity: 0.3, brightOpacity: 0.85,
    minW: 1, maxW: 4,
    refetchMs: 3600000
  };

  function km(lat1, lon1, lat2, lon2) {
    var R = 6371.0088, r = Math.PI / 180;
    var dp = (lat2 - lat1) * r, dl = (lon2 - lon1) * r;
    var a = Math.sin(dp / 2) * Math.sin(dp / 2) +
            Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(a)));
  }
  function esc(t) { return String(t == null ? '' : t).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;'); }
  function angDiff(a, b) { var d = Math.abs(a - b) % 360; return d > 180 ? 360 - d : d; }

  // Distance (km) from a point to a polyline [[lat, lon, alt], ...], local flat projection.
  function kmToLine(lat, lon, line) {
    if (!line || !line.length) return Infinity;
    var kx = 111.32 * Math.cos(lat * Math.PI / 180), ky = 110.57, best = Infinity;
    for (var i = 0; i < line.length; i++) {
      var ax = (line[i][1] - lon) * kx, ay = (line[i][0] - lat) * ky;
      if (i === line.length - 1) { best = Math.min(best, Math.sqrt(ax * ax + ay * ay)); break; }
      var bx = (line[i + 1][1] - lon) * kx, by = (line[i + 1][0] - lat) * ky;
      var dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
      var t = L2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / L2)) : 0;
      var px = ax + t * dx, py = ay + t * dy;
      best = Math.min(best, Math.sqrt(px * px + py * py));
    }
    return best;
  }

  // Track [{lat, lon, alt, t}, ...] (last 5 min) vs paths -> {path, dist_km} or null.
  // Only paths for `rwy` are tried when a runway is known (active one, or the aircraft's own).
  function match(track, paths, rwy) {
    if (!track || track.length < 3 || !paths || !paths.length) return null;
    var best = null;
    paths.forEach(function (p) {
      if (rwy && p.rwy !== rwy) return;
      var sum = 0;
      for (var i = 0; i < track.length; i++) sum += kmToLine(track[i].lat, track[i].lon, p.line);
      var d = sum / track.length;
      if (d < C.matchKm && (!best || d < best.dist_km)) best = { path: p, dist_km: d };
    });
    return best;
  }

  function brg(lat1, lon1, lat2, lon2) {
    var r = Math.PI / 180, y = Math.sin((lon2 - lon1) * r) * Math.cos(lat2 * r);
    var x = Math.cos(lat1 * r) * Math.sin(lat2 * r) - Math.sin(lat1 * r) * Math.cos(lat2 * r) * Math.cos((lon2 - lon1) * r);
    return (Math.atan2(y, x) / r + 360) % 360;
  }
  // Arriving = over the remembered track it came down (>= 300 ft), is below FL200, got closer to the
  // airport and is heading roughly at it (within 45 deg). Overflights and passing traffic are not.
  function isArriving(track) {
    if (!track || track.length < 3) return false;
    var a = track[0], b = track[track.length - 1];
    var dA = km(C.refLat, C.refLon, a.lat, a.lon), dB = km(C.refLat, C.refLon, b.lat, b.lon);
    if (!(dB < 160 && dB < dA - 1)) return false;
    if (a.alt == null || b.alt == null || b.alt > 20000 || a.alt - b.alt < 300) return false;
    return angDiff(brg(a.lat, a.lon, b.lat, b.lon), brg(b.lat, b.lon, C.refLat, C.refLon)) <= 45;
  }

  // Landings seen -> runway in use (most landings in the last 30 min), or null.
  function activeRwy(landings, nowS) {
    var n = {};
    (landings || []).forEach(function (l) { if (nowS - l.t <= C.activeS) n[l.rwy] = (n[l.rwy] || 0) + 1; });
    var best = null;
    Object.keys(n).forEach(function (k) { if (!best || n[k] > n[best]) best = k; });
    return best;
  }

  // An aircraft on short final: inside 8 km, below 2,000 ft, descending, lined up -> runway, else null.
  function finalRwy(a) {
    if (!a || a.lat == null || a.lon == null || typeof a.alt_baro !== 'number') return null;
    if (a.alt_baro >= 2000 || km(C.refLat, C.refLon, a.lat, a.lon) > 8) return null;
    if (!((a.baro_rate || a.geom_rate || 0) < -300) || typeof a.track !== 'number') return null;
    for (var k in C.rwys) if (angDiff(a.track, C.rwys[k]) <= 25) return k;
    return null;
  }

  function lineWidth(flights, maxFlights) {
    if (!maxFlights || maxFlights <= 1) return C.maxW;
    return C.minW + (C.maxW - C.minW) * Math.max(0, Math.min(1, (flights - 1) / (maxFlights - 1)));
  }
  function lineStyle(p, maxFlights, active) {
    return { color: C.colours[p.rwy] || '#fb923c', weight: lineWidth(p.flights, maxFlights),
             opacity: (active && p.rwy !== active) ? C.dimOpacity : C.brightOpacity };
  }

  // Card line (plain text).
  function cardLine(arriving, m, nextWp, windowDays) {
    if (!arriving) return '';
    if (!m) return 'Arrival: off the usual paths';
    var p = m.path, s = 'Arrival: ' + p.name + ' · ' + p.flights + ' flights in ' + (windowDays || 14) + ' days';
    if (nextWp && p.fixes && p.fixes.indexOf(String(nextWp).toUpperCase()) >= 0) s += ' ✓ datalink ' + String(nextWp).toUpperCase();
    return s;
  }

  // ── memory of recent tracks + landings (browser only, never sent anywhere) ──
  var tracks = {}, landings = [], landedBy = {}, locked = {};
  function observe(a, cs, nowS) {
    if (!a || !cs || a.lat == null || a.lon == null) return;
    var t = tracks[cs] || (tracks[cs] = []);
    var last = t[t.length - 1];
    if (!last || nowS - last.t >= C.keepEveryS) {
      t.push({ lat: a.lat, lon: a.lon, alt: typeof a.alt_baro === 'number' ? a.alt_baro : null, t: nowS });
    }
    while (t.length && nowS - t[0].t > C.lookS) t.shift();
    var r = finalRwy(a);
    if (r && (!landedBy[cs] || nowS - landedBy[cs] > 600)) { landedBy[cs] = nowS; landings.push({ t: nowS, rwy: r }); }
    while (landings.length && nowS - landings[0].t > C.activeS) landings.shift();
  }
  function forget(cs) { delete tracks[cs]; delete locked[cs]; }

  // ── state + Leaflet drawing (only when the overlay is on) ──
  var S = { enabled: false, doc: null, unavailable: false, on: false, layer: null, timer: null };

  function statusText() {
    var have = !!(S.doc && S.doc.paths && S.doc.paths.length);
    if (!have && (S.unavailable || (S.doc && S.doc.unavailable))) return 'Arrival paths unavailable right now';
    if (!S.doc) return 'Loading arrival paths…';
    if (S.doc.learning) return 'Still learning — ' + (S.doc.arrivals || 0) + ' arrivals so far';
    var n = (S.doc.paths || []).length;
    return n + ' Auckland arrival path' + (n === 1 ? '' : 's') + ' from ' + (S.doc.arrivals || 0) + ' flights';
  }

  function draw(L, map, nowS) {
    clear(map);
    if (!S.on || !S.doc || !S.doc.paths || !S.doc.paths.length || !L || !map) return;
    var maxF = 0; S.doc.paths.forEach(function (p) { maxF = Math.max(maxF, p.flights || 0); });
    var act = activeRwy(landings, nowS);
    S.layer = L.layerGroup();
    S.doc.paths.forEach(function (p) {
      if (!p.line || p.line.length < 2) return;
      var ll = p.line.map(function (q) { return [q[0], q[1]]; });
      // Leaflet puts tooltip strings in as HTML: escape what came from the server
      var tip = esc(p.name) + ' · ' + (p.flights | 0) + ' flights in ' + ((S.doc.window_days | 0) || 14) + ' days';
      L.polyline(ll, lineStyle(p, maxF, act)).bindTooltip(tip, { sticky: true }).addTo(S.layer);
    });
    S.layer.addTo(map);
  }
  function clear(map) { if (S.layer && map) { try { map.removeLayer(S.layer); } catch (e) {} } S.layer = null; }

  // The followed/selected aircraft's path ({path, dist_km}) or null. Same decision the card line uses.
  function matchFor(a, cs, nowS) {
    if (!S.enabled || !S.doc || !S.doc.paths || !S.doc.paths.length) return null;
    var tr = tracks[cs]; if (!isArriving(tr)) return null;
    var dl = a && a.datalink;
    var own = dl && dl.rwy && C.rwys[String(dl.rwy).toUpperCase()] != null ? String(dl.rwy).toUpperCase() : null;
    var rwy = own || activeRwy(landings, nowS), m = null;
    // All paths share the last few km of final, so a path matched while the aircraft was still
    // further out than lockKm is kept as long as the aircraft stays within matchKm of it.
    var lk = locked[cs] && S.doc.paths.filter(function (p) { return p.id === locked[cs]; })[0];
    if (lk) m = match(tr, [lk], null);
    if (!m) {
      m = match(tr, S.doc.paths, rwy);
      var last = tr[tr.length - 1];
      if (m && km(C.refLat, C.refLon, last.lat, last.lon) > C.lockKm) locked[cs] = m.path.id;
      else if (!m) delete locked[cs];
    }
    return m;
  }

  function lineFor(a, cs, nowS) {
    if (!S.enabled || !S.doc || !S.doc.paths || !S.doc.paths.length) return '';
    if (!isArriving(tracks[cs])) return '';
    var dl = a && a.datalink;
    return cardLine(true, matchFor(a, cs, nowS), dl && dl.next_wp, S.doc.window_days);
  }

  // ── 3D Approach helpers (17 Oct 2026) ──
  // Nearest point on a path to (lat, lon): {alt (ft, interpolated), km_off, seg, t}, or null.
  function profileAt(p, lat, lon) {
    var line = p && p.line; if (!line || !line.length) return null;
    var kx = 111.32 * Math.cos(lat * Math.PI / 180), ky = 110.57, best = null;
    if (line.length === 1) return { alt: line[0][2], km_off: km(lat, lon, line[0][0], line[0][1]), seg: 0, t: 0 };
    for (var i = 0; i < line.length - 1; i++) {
      var ax = (line[i][1] - lon) * kx, ay = (line[i][0] - lat) * ky;
      var bx = (line[i + 1][1] - lon) * kx, by = (line[i + 1][0] - lat) * ky;
      var dx = bx - ax, dy = by - ay, L2 = dx * dx + dy * dy;
      var t = L2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / L2)) : 0;
      var px = ax + t * dx, py = ay + t * dy, d = Math.sqrt(px * px + py * py);
      if (!best || d < best.km_off) best = { alt: line[i][2] + t * (line[i + 1][2] - line[i][2]), km_off: d, seg: i, t: t };
    }
    return best;
  }
  // The path from the aircraft's spot to its end: [[lat, lon, alt], ...], the projected spot first.
  function ribbonAhead(p, lat, lon) {
    var pr = profileAt(p, lat, lon); if (!pr) return [];
    var a = p.line[pr.seg], b = p.line[Math.min(pr.seg + 1, p.line.length - 1)];
    var out = [[a[0] + pr.t * (b[0] - a[0]), a[1] + pr.t * (b[1] - a[1]), pr.alt]];
    for (var i = pr.seg + 1; i < p.line.length; i++) out.push(p.line[i]);
    return out;
  }
  // '400 ft above usual' / '300 ft below usual' / 'on the usual profile' (within 100 ft) / '' when unknown.
  function deltaText(alt, usual) {
    if (typeof alt !== 'number' || typeof usual !== 'number') return '';
    var d = Math.round((alt - usual) / 100) * 100;
    if (Math.abs(alt - usual) < 100 || d === 0) return 'on the usual profile';
    return Math.abs(d) + ' ft ' + (d > 0 ? 'above' : 'below') + ' usual';
  }

  var API = {
    C: C, km: km, kmToLine: kmToLine, match: match, isArriving: isArriving, activeRwy: activeRwy,
    finalRwy: finalRwy, lineWidth: lineWidth, lineStyle: lineStyle, cardLine: cardLine,
    observe: observe, forget: forget, draw: draw, clear: clear, lineFor: lineFor, statusText: statusText,
    matchFor: matchFor, profileAt: profileAt, ribbonAhead: ribbonAhead, deltaText: deltaText,
    _state: S, _tracks: tracks, _landings: landings,
    // Browser glue: fetch, show the toggle row, keep the card line current.
    load: function (fetchFn, onDone) {
      return fetchFn('/api/arrival-paths').then(function (r) { return r.json(); }).then(function (d) {
        if (!d || d.enabled === false) { S.enabled = false; S.doc = null; }
        else { S.enabled = true; S.unavailable = !!d.unavailable; S.doc = d; }
        if (onDone) onDone(S);
      }).catch(function () {
        S.unavailable = true;
        if (onDone) onDone(S);
      });
    }
  };
  root.ArrivalPaths = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : this);

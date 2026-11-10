/* PiLNK - the selected plane's fixes on the 2D map (30 Oct 2026, AJ: "it said via POKOM, then SABAV").
 *
 * The aircraft's own FMS position reports (ACARS/VDL2, decoded on the node into a.datalink) name the
 * fix it last passed (last_wp), the next one (next_wp, with an ETA) and the one after (then_wp).
 * For the SELECTED plane only, this draws:
 *  - a pulsing amber ring on the next fix, labelled with its name and ETA,
 *  - a steady violet ring on the fix after it,
 *  - a small grey mark on the fix it has passed,
 *  - a dashed line plane -> next -> after.
 * Fix positions come from the dashboard's own IFR fix list (IFR_WAYPOINTS: the NZ fix file with the
 * AIRAC 2608 renames, plus the NZ VOR/NDB list). A name not in that list is simply not drawn - never
 * guessed, never looked up anywhere. A fix more than MAX_NM from the plane is treated as a different
 * fix that shares the name, and not drawn either.
 *
 * Local only: nothing here sends anything anywhere. Names came over the air: they must match the
 * waypoint shape before they touch the map, and labels are written with textContent.
 * ASCII-only, no backslashes (see datalink-card.js).
 */
(function () {
  'use strict';
  var WP = /^[A-Z][A-Z0-9]{1,6}$/, MAX_NM = 400;
  var D2R = Math.PI / 180;

  function num(v) { return typeof v === 'number' && isFinite(v); }
  function nm(lat1, lon1, lat2, lon2) {
    var p1 = lat1 * D2R, p2 = lat2 * D2R, dp = p2 - p1, dl = (lon2 - lon1) * D2R;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2) + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 3440.065 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
  }

  // name -> [{lat, lon}] for a fix list of {name, lat, lon}. 'AA VOR' is also found as 'AA'.
  // Rebuilt only when the dashboard hands over a different list.
  var idxList = null, idx = {};
  function index(list) {
    if (list === idxList) return idx;
    idxList = list; idx = {};
    (Array.isArray(list) ? list : []).forEach(function (f) {
      if (!f || typeof f.name !== 'string' || !num(f.lat) || !num(f.lon)) return;
      var key = f.name.split(' ')[0];
      if (!WP.test(key)) return;
      (idx[key] = idx[key] || []).push({ lat: f.lat, lon: f.lon });
    });
    return idx;
  }

  function locate(name, a, ix) {
    if (typeof name !== 'string' || !WP.test(name) || !ix[name]) return null;
    var best = null, bd = Infinity;
    ix[name].forEach(function (p) { var d = nm(a.lat, a.lon, p.lat, p.lon); if (d < bd) { bd = d; best = p; } });
    return best && bd <= MAX_NM ? { name: name, lat: best.lat, lon: best.lon } : null;
  }

  // a: one aircraft from /flights; fixes: [{name, lat, lon}].
  // -> {passed, next, then} (each {name, lat, lon} or null; next also eta_ts) or null when there is nothing to draw.
  function plan(a, fixes) {
    var d = a && a.datalink;
    if (!a || !d || typeof d !== 'object' || !num(a.lat) || !num(a.lon)) return null;
    if ((num(d.on_ts) && d.on_ts > 0) || (num(d.in_ts) && d.in_ts > 0)) return null;   // landed: the route is done
    var ix = index(fixes);
    var out = { passed: locate(d.last_wp, a, ix), next: locate(d.next_wp, a, ix), then: locate(d.then_wp, a, ix) };
    if (out.next && num(d.next_wp_eta_ts) && d.next_wp_eta_ts > 0) out.next.eta_ts = d.next_wp_eta_ts;
    if (out.then && out.next && out.then.name === out.next.name) out.then = null;
    if (out.passed && ((out.next && out.passed.name === out.next.name) || (out.then && out.passed.name === out.then.name))) out.passed = null;
    return (out.passed || out.next || out.then) ? out : null;
  }

  function hhmm(ts) {
    if (!num(ts) || ts <= 0) return '';
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  }

  // ---- 2D map layers (called on every /flights tick and when the selection changes) ----
  var drawn = null;   // {key, line, marks: [marker]}

  function clear(map) {
    if (!drawn) return;
    if (drawn.line) map.removeLayer(drawn.line);
    drawn.marks.forEach(function (m) { map.removeLayer(m); });
    drawn = null;
  }

  function mark(o, kind, f, text) {
    var wrap = document.createElement('div');
    var ring = document.createElement('div'); ring.className = 'dlfx-ring dlfx-' + kind;
    var lbl = document.createElement('span'); lbl.className = 'dlfx-lbl dlfx-lbl-' + kind; lbl.textContent = text;
    wrap.appendChild(ring); wrap.appendChild(lbl);
    var size = kind === 'passed' ? 10 : 26;
    return o.L.marker([f.lat, f.lon], { interactive: false, keyboard: false, zIndexOffset: -150,
      icon: o.L.divIcon({ className: 'dlfx-wrap', html: wrap, iconSize: [size, size], iconAnchor: [size / 2, size / 2] }) }).addTo(o.map);
  }

  // o: {map, L, aircraft: {cs: a}, selected: cs|null, fixes: [{name, lat, lon}]}
  function sync(o) {
    if (!o || !o.map || !o.L) return;
    var a = o.selected && o.aircraft ? o.aircraft[o.selected] : null;
    var p = a ? plan(a, o.fixes) : null;
    if (!p) { clear(o.map); return; }
    var eta = p.next ? hhmm(p.next.eta_ts) : '';
    var key = [o.selected, p.passed && p.passed.name, p.next && p.next.name, eta, p.then && p.then.name].join('|');
    var pts = [[a.lat, a.lon]];
    if (p.next) pts.push([p.next.lat, p.next.lon]);
    if (p.then) pts.push([p.then.lat, p.then.lon]);
    if (drawn && drawn.key === key) {            // same fixes: only the plane end of the line moves
      if (drawn.line) drawn.line.setLatLngs(pts);
      return;
    }
    clear(o.map);
    drawn = { key: key, line: null, marks: [] };
    if (pts.length > 1) {
      drawn.line = o.L.polyline(pts, { className: 'dlfx-line', color: '#f59e0b', weight: 2, opacity: 0.9,
        dashArray: '4 6', interactive: false }).addTo(o.map);
    }
    if (p.passed) drawn.marks.push(mark(o, 'passed', p.passed, p.passed.name));
    if (p.then) drawn.marks.push(mark(o, 'then', p.then, p.then.name));
    if (p.next) drawn.marks.push(mark(o, 'next', p.next, p.next.name + (eta ? ' ' + eta : '')));
  }

  function state() {
    return drawn ? { key: drawn.key, marks: drawn.marks.length, line: drawn.line ? drawn.line.getLatLngs().length : 0 } : null;
  }

  var api = { plan: plan, sync: sync, state: state, MAX_NM: MAX_NM };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else window.dlFixes = api;
})();

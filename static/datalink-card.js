/* PiLNK - "From the aircraft" box on the aircraft card (ACARS/VDL2, 7 Oct 2026).
 *
 * Shows what the aircraft's own datalink said about itself: route, ETA,
 * arrival runway, next waypoint, takeoff/landing times. The node decodes the
 * messages (datalink_decode.py) and /flights carries the facts in a.datalink;
 * this only displays them. Local only: nothing here talks to pilnk.io.
 *
 * Everything comes from the air, so every value is checked against the shape
 * it must have and is written with textContent - never as markup.
 *
 * ASCII-only on purpose: the browser decodes this file with the page's charset.
 */
(function () {
  'use strict';
  var CH = String.fromCharCode;
  var ARROW = CH(0x2192), HEAD_TEXT = ' FROM THE AIRCRAFT ' + CH(0xB7) + ' datalink';
  var ICAO = /^[A-Z]{4}$/, RWY = /^(0[1-9]|[12][0-9]|3[0-6])[LRC]?$/, WP = /^[A-Z][A-Z0-9]{1,6}$/;
  var GATE = /^[A-Z]?[0-9]{1,3}[A-Z]?$/, SID = /^[A-Z]{2,6}[0-9][A-Z]?$/;

  function hhmm(ts) {
    if (typeof ts !== 'number' || !isFinite(ts) || ts <= 0) return '';
    return new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  }
  // Antenna icon as inline SVG (icons are SVG only: emoji show as blank boxes on Chromium/Linux).
  function antenna() {
    var NS = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('class', 'icn');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('aria-hidden', 'true');
    var c = document.createElementNS(NS, 'circle');
    c.setAttribute('cx', '12'); c.setAttribute('cy', '12'); c.setAttribute('r', '2');
    var p = document.createElementNS(NS, 'path');
    p.setAttribute('d', 'M4.9 4.9a10 10 0 0 0 0 14.2M19.1 4.9a10 10 0 0 1 0 14.2M7.8 7.8a6 6 0 0 0 0 8.4M16.2 7.8a6 6 0 0 1 0 8.4');
    svg.appendChild(c); svg.appendChild(p);
    return svg;
  }
  function ok(re, v) { return typeof v === 'string' && re.test(v); }

  function rowsFor(d) {
    var rows = [];
    var orig = ok(ICAO, d.orig) ? d.orig : '', dest = ok(ICAO, d.dest) ? d.dest : '';
    if (orig || dest) rows.push(['Route', (orig ? orig + ' ' : '') + ARROW + (dest ? ' ' + dest : '')]);
    if (ok(RWY, d.dep_rwy)) rows.push(['Departure', d.dep_rwy + (ok(SID, d.sid) ? ' via ' + d.sid : '')]);
    var eta = hhmm(d.eta_ts);
    if (eta) rows.push(['ETA', eta]);
    if (ok(RWY, d.rwy)) rows.push(['Runway', d.rwy]);
    if (ok(GATE, d.arr_gate)) rows.push(['Gate', d.arr_gate]);
    if (ok(WP, d.next_wp)) {
      var nt = hhmm(d.next_wp_eta_ts);
      rows.push(['Next fix', d.next_wp + (nt ? ' ' + nt : '')]);
    }
    [['out_ts', 'Pushback'], ['off_ts', 'Took off'], ['on_ts', 'Landed'], ['in_ts', 'At gate']].forEach(function (e) {
      var t = hhmm(d[e[0]]);
      if (t) rows.push([e[1], t]);
    });
    return rows;
  }

  // One-line summary for the Traffic box (2D tooltip fields / 3D box) and 3D Approach (8 Oct 2026).
  // route: 'NZAA -> NZWN' only when both ends are valid ICAO codes. line: what matters most right now -
  // landed beats arriving beats departed. now = epoch seconds (the caller's clock, like every hhmm here).
  window.datalinkBrief = function (d, now) {
    var out = { route: '', line: '' };
    if (!d || typeof d !== 'object') return out;
    var DOT = ' ' + CH(0xB7) + ' ';
    if (ok(ICAO, d.orig) && ok(ICAO, d.dest)) out.route = d.orig + ' ' + ARROW + ' ' + d.dest;
    var landed = hhmm(d.on_ts) || hhmm(d.in_ts), eta = hhmm(d.eta_ts), off = hhmm(d.off_ts);
    var rwy = ok(RWY, d.rwy) ? d.rwy : '', dep = ok(RWY, d.dep_rwy) ? d.dep_rwy : '';
    if (landed) {
      out.line = 'Landed ' + landed + (ok(GATE, d.arr_gate) ? DOT + 'Gate ' + d.arr_gate : '');
    } else if (eta) {
      var mins = (typeof now === 'number') ? Math.round((d.eta_ts - now) / 60) : 0;
      out.line = 'ETA ' + eta + (mins >= 1 && mins <= 180 ? ' (in ' + mins + ' min)' : '') + (rwy ? DOT + 'RWY ' + rwy : '');
    } else if (rwy) {
      out.line = 'Landing RWY ' + rwy;
    } else if (off) {
      out.line = 'Took off ' + off + (dep ? DOT + 'RWY ' + dep : '');
    } else if (dep) {
      out.line = 'Departing RWY ' + dep;
    }
    return out;
  };

  window.populateDatalink = function (a) {
    var box = document.getElementById('ccDatalink');
    var body = document.getElementById('ccDatalinkBody');
    var head = document.getElementById('ccDatalinkHead');
    if (!box || !body) return;
    var d = (a && a.datalink && typeof a.datalink === 'object') ? a.datalink : {};
    var rows = rowsFor(d);
    while (body.firstChild) body.removeChild(body.firstChild);
    if (!rows.length) { box.style.display = 'none'; return; }
    rows.forEach(function (r) {
      var row = document.createElement('div'); row.className = 'cc-bds-row';
      var k = document.createElement('span'); k.className = 'cc-bds-k'; k.textContent = r[0];
      var v = document.createElement('span'); v.className = 'cc-bds-v'; v.textContent = r[1];
      row.appendChild(k); row.appendChild(v); body.appendChild(row);
    });
    if (head) {
      while (head.firstChild) head.removeChild(head.firstChild);
      head.appendChild(antenna());
      head.appendChild(document.createTextNode(HEAD_TEXT));
    }
    box.style.display = '';
  };
})();

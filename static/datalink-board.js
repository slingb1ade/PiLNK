/* PiLNK - Arrivals / Departures board and ATIS line in the wind chip (ACARS/VDL2, 7-8 Oct 2026).
 *
 * Built from what the aircraft near this node report about themselves over
 * datalink. Local only: everything comes from this node's /api/datalink/*
 * routes and nothing here talks to pilnk.io.
 *
 * Every value came over the air, so each is checked against the shape it must
 * have and is written with textContent - never as markup.
 *
 * ASCII-only on purpose (see datalink-card.js): non-ASCII glyphs are built with
 * String.fromCharCode.
 */
(function () {
  'use strict';
  var CH = String.fromCharCode, DOT = ' ' + CH(0xB7) + ' ';
  var ICAO = /^[A-Z]{4}$/, RWY = /^(0[1-9]|[12][0-9]|3[0-6])[LRC]?$/, GATE = /^[A-Z]?[0-9]{1,3}[A-Z]?$/,
      SID = /^[A-Z]{2,6}[0-9][A-Z]?$/, FLT = /^[A-Z0-9]{2,8}$/, REG = /^[A-Z0-9-]{2,8}$/, HEX = /^[0-9a-f]{6}$/;
  var WORDS = { at_gate: 'At gate', landed: 'Landed', enroute: 'En route', departed: 'Departed',
                pushed_back: 'Pushed back', cleared: 'Cleared', scheduled: 'Scheduled' };
  var EMPTY = 'No reports yet. Flights appear here as aircraft send them.';
  var NO_STATION = "Waiting for this node's nearest airport...";
  var NOTE = 'Datalink reception rules vary by country. Everything here stays on this node.';   // Rule #25 (UK: WTA s48)
  var BOARD_POLL_MS = 15000, MAX_ROWS = 30;

  function has(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
  function ok(re, v) { return typeof v === 'string' && re.test(v); }
  function num(v) { return typeof v === 'number' && isFinite(v) && v > 0; }
  function hhmm(ts) {
    return num(ts) ? new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }) : '';
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function station() {
    var s = (typeof METAR_STATION === 'string') ? METAR_STATION : '';
    return ICAO.test(s) ? s : '';
  }

  // ---- board ------------------------------------------------------------
  function liveCs(hex) {
    if (!ok(HEX, hex) || typeof lastAircraft !== 'object' || !lastAircraft) return null;
    for (var cs in lastAircraft) {
      if (!has(lastAircraft, cs)) continue;
      var a = lastAircraft[cs];
      if (a && String(a.hex || '').toLowerCase() === hex) return cs;
    }
    return null;
  }

  function openCard(cs) {
    window.selectedFlight = cs;
    if (typeof hideTooltip === 'function') hideTooltip();
    if (typeof updateFlights === 'function') updateFlights();
    if (typeof openCinematicCard === 'function') openCinematicCard(cs, false);
  }

  function rowEl(r, arrival) {
    var name = ok(FLT, r.flight) ? r.flight : (ok(REG, r.reg) ? r.reg : '-');
    var place = arrival ? r.from : r.to;
    var extra = arrival ? (ok(GATE, r.gate) ? 'G' + r.gate : '-') : (ok(SID, r.sid) ? r.sid : '-');
    var row = el('div', 'dlb-row');
    row.appendChild(el('span', 'dlb-flight', name));
    row.appendChild(el('span', 'dlb-place', ok(ICAO, place) ? place : '-'));
    row.appendChild(el('span', 'dlb-time', hhmm(r.time) || '-'));
    row.appendChild(el('span', 'dlb-rwy', ok(RWY, r.rwy) ? r.rwy : '-'));
    row.appendChild(el('span', 'dlb-extra', extra));
    row.appendChild(el('span', 'dlb-status', (typeof r.status === 'string' && has(WORDS, r.status)) ? WORDS[r.status] : '-'));
    var cs = liveCs(r.hex);
    if (cs) {
      row.className += ' live';
      row.insertBefore(el('span', 'dlb-dot'), row.firstChild);
      row.title = 'Show on the map';
      row.addEventListener('click', function () { openCard(cs); });
    } else {
      row.appendChild(el('span', 'dlb-notinview', 'not in view'));
    }
    return row;
  }

  function section(title, rows, arrival, ap) {
    var sec = el('div', 'dlb-sec');
    sec.appendChild(el('div', 'dlb-head', title + (ap ? DOT + ap : '')));
    var list = Array.isArray(rows) ? rows.slice(0, MAX_ROWS) : [];
    if (!list.length) sec.appendChild(el('div', 'dlb-empty', EMPTY));
    list.forEach(function (r) { if (r && typeof r === 'object') sec.appendChild(rowEl(r, arrival)); });
    return sec;
  }

  function render(d) {
    var box = document.getElementById('tab-dlboard');
    if (!box || !d || typeof d !== 'object') return;
    var ap = ok(ICAO, d.airport) ? d.airport : '';
    while (box.firstChild) box.removeChild(box.firstChild);
    box.appendChild(section('ARRIVALS', d.arrivals, true, ap));
    box.appendChild(section('DEPARTURES', d.departures, false, ap));
    box.appendChild(el('div', 'dlb-note', NOTE));
  }

  function poll() {
    var box = document.getElementById('tab-dlboard');
    var ap = station();
    if (!box || !box.classList.contains('active')) return;
    if (!ap) {                                     // location not known yet: say so, do not ask
      while (box.firstChild) box.removeChild(box.firstChild);
      box.appendChild(el('div', 'dlb-empty', NO_STATION));
      return;
    }
    board._fetch('/api/datalink/board?airport=' + ap)
      .then(function (r) { return (r && r.ok) ? r.json() : null; })
      .then(function (d) { if (d) render(d); })
      .catch(function () { /* keep the last good board */ });
  }

  var board = window.dlBoard = {
    show: function () { poll(); },
    render: render,
    poll: poll,
    _fetch: function (u) { return window.fetch(u, { cache: 'no-store' }); }
  };
  setInterval(poll, BOARD_POLL_MS);

  // ---- ATIS chip and weather-panel section --------------------------------
  var LETTER = /^[A-Z]$/, APCH = /^[A-Z][A-Z /-]{0,20}$/, TIME = /^[0-9]{4}Z$/;
  var STATUS_POLL_MS = 60000;
  var fails = 0, seq = 0, renderedSeq = 0;

  function pad3(n) { var s = String(Math.round(n)); return s.length >= 3 ? s : ('00' + s).slice(-3); }

  function cleanAtis(a) {
    if (!a || typeof a !== 'object' || !ok(ICAO, a.airport) || !ok(LETTER, a.letter)) return null;
    var c = { airport: a.airport, letter: a.letter };
    if (ok(RWY, a.rwy)) c.rwy = a.rwy;
    if (ok(APCH, a.apch)) c.apch = a.apch;
    var dirOk = a.wind_dir === null || (typeof a.wind_dir === 'number' && a.wind_dir >= 0 && a.wind_dir <= 360);
    if (dirOk && typeof a.wind_kt === 'number' && a.wind_kt >= 0 && a.wind_kt <= 150) {
      c.wind = (a.wind_dir === null ? 'VRB' : pad3(a.wind_dir)) + '/' + Math.round(a.wind_kt) + ' kt';
    }
    if (typeof a.qnh === 'number' && a.qnh >= 900 && a.qnh <= 1100) c.qnh = 'QNH ' + Math.round(a.qnh);
    if (ok(TIME, a.time)) c.time = a.time;
    if (num(a.ts)) c.ts = a.ts;
    return c;
  }

  function shortAtis(a) {
    return [a.airport + ' ' + a.letter, a.rwy, a.apch].filter(Boolean).join(DOT);
  }

  // 8 Oct 2026 (AJ): the ATIS rides inside the wind chip, which already opens the weather panel - a separate
  // pill next to it did the same job twice. A small line in the chip's .wc-data; no click handler of its own
  // (the chip's own click opens the panel). No wind chip on the page: nothing in the header, the panel keeps it.
  function ensureChip() {
    var old = document.getElementById('atisChip');            // a pill from before 8 Oct (cached page)
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var data = document.querySelector('#windCompass .wc-data');
    var chip = document.getElementById('wcAtis');
    if (!data) return null;
    if (!chip) {
      chip = el('span', 'wc-atis');
      chip.id = 'wcAtis';
      chip.style.display = 'none';
    }
    if (chip.parentNode !== data) data.appendChild(chip);
    return chip;
  }

  function wxRow(box, label, value) {
    var r = el('div', 'wx-row');
    r.appendChild(el('span', 'wx-lbl', label));
    r.appendChild(el('span', 'wx-val', value));
    box.appendChild(r);
  }

  function renderWx(mine, others, now) {
    var box = document.getElementById('wxAtis');
    if (!box) return;
    while (box.firstChild) box.removeChild(box.firstChild);
    if (!mine && !others.length) { box.style.display = 'none'; return; }
    box.style.display = '';
    box.appendChild(el('div', 'wx-section-title', 'ATIS (from aircraft)'));
    if (mine) {
      wxRow(box, 'ATIS', mine.airport + ' ' + mine.letter + (mine.time ? ' ' + mine.time : ''));
      if (mine.rwy) wxRow(box, 'Runway', mine.rwy);
      if (mine.apch) wxRow(box, 'Approach', mine.apch);
      if (mine.wind) wxRow(box, 'Wind', mine.wind);
      if (mine.qnh) wxRow(box, 'QNH', mine.qnh.slice(4));
      if (mine.ts) box.appendChild(el('div', 'wx-atis-age', 'received ' + Math.max(0, Math.round((now - mine.ts) / 60)) + ' min ago'));
    }
    if (others.length) box.appendChild(el('div', 'wx-atis-also', 'Also heard: ' + others.map(shortAtis).join(',  ')));
  }

  function renderAtis(s, now) {
    now = num(now) ? now : Date.now() / 1000;
    fails = 0;
    renderedSeq = seq;
    var nav = document.getElementById('navDlBoard');
    if (nav) nav.style.display = (s && s.installed === true) ? '' : 'none';
    var list = (s && Array.isArray(s.atis)) ? s.atis.map(cleanAtis).filter(Boolean) : [];
    var ap = station(), mine = null, others = [];
    list.forEach(function (a) {
      if (ap && a.airport === ap && !mine) mine = a; else others.push(a);
    });
    var chip = ensureChip();
    if (chip) {
      if (mine) {
        chip.textContent = ['ATIS ' + mine.letter, mine.rwy].filter(Boolean).join(DOT);
        chip.title = [mine.time, mine.wind, mine.qnh].filter(Boolean).join(DOT) || 'ATIS from aircraft datalink';
        chip.style.display = '';
      } else {
        chip.style.display = 'none';
      }
    }
    renderWx(mine, others, now);
  }

  function atisPoll() {
    var mySeq = ++seq;
    atis._fetch('/api/datalink/status')
      .then(function (r) { return (r && r.ok) ? r.json() : Promise.reject(new Error('status')); })
      .then(function (d) { if (mySeq < renderedSeq) return; renderAtis(d); })   // a late answer from an older poll changes nothing
      .catch(function () {
        if (mySeq <= renderedSeq) return;          // older than the last good render
        fails += 1;
        var chip = document.getElementById('wcAtis');
        if (fails >= 2 && chip) chip.style.display = 'none';
      });
  }

  var atis = window.dlAtis = {
    poll: atisPoll,
    render: renderAtis,
    _fetch: function (u) { return window.fetch(u, { cache: 'no-store' }); }
  };
  atisPoll();
  setInterval(atisPoll, STATUS_POLL_MS);
})();

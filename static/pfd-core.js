/* PFD core for 3D Approach (AJ, 9 Nov 2026): "build one as an alternative to the data box ... for real plane
   enthusiasts". Spec: pilnk-tasks/pfd-spec.md. Numbers in, "what to draw" out - no DOM, loads under node for the tests.

   The page hands over, every poll, the followed aircraft and the values it has already worked out for the data box
   (update(inp, nowMs)); view(nowMs) answers with every item the display draws, each with a state:
     ok    a measured value (or one held from the last 10 s)
     est   estimated (bank from the turn rate)
     none  never sent by this aircraft - drawn as dashes, as on a real PFD with an unset bug
     fail  sent, then stopped for more than 10 s, or out of its sane range - drawn with a red X
   Each item has a source chain (speed IAS -> GS, heading magnetic -> track, baro crew -> METAR -> STD): the first
   source seen in the last 10 s wins. Only when none is fresh, and one of them was seen for this aircraft, is it an X.
   Memory is per aircraft: a new hex starts clean, so nothing of the last one leaks onto the next. */
(function (root) {
  'use strict';
  const C = {
    STALE_MS: 10000, TA_FT: 13000, CALM_KT: 3, AGL_MAX_FT: 2500, BANK_EST_MAX: 45,
    SANE: { spd: [0, 700], alt: [-1500, 60000], vs: [-10000, 10000], bank: [-90, 90], baro: [900, 1100] },
    STD_HPA: 1013.25, FT_PER_HPA: 27.3, M_PER_FT: 0.3048
  };
  const num = v => (typeof v === 'number' && isFinite(v)) ? v : null;
  const sane = (k, v) => v >= C.SANE[k][0] && v <= C.SANE[k][1];

  // '7700' from '7700', 7700 or ' 7700 '; null for anything that is not four octal digits
  function sqkText(v) {
    if (typeof v === 'number' && isFinite(v)) v = String(Math.round(v)).padStart(4, '0');
    if (typeof v !== 'string') return null;
    v = v.trim();
    return /^[0-7]{4}$/.test(v) ? v : null;
  }

  /* The pressure setting the altitude is read against: the crew's own (BDS 4,0) when sent; otherwise the airport's
     METAR QNH below the transition altitude; otherwise standard. On the ground counts as below. */
  function baroSetting(a, qnh, taFt) {
    const crew = num(a && a.bds40_baro_pressure_setting);
    if (crew != null) return { hpa: crew, src: 'crew' };
    const alt = num(a && a.alt_baro), ta = num(taFt) != null ? taFt : C.TA_FT;
    if (num(qnh) != null && !(alt != null && alt >= ta)) return { hpa: qnh, src: 'met' };
    return { hpa: C.STD_HPA, src: 'std' };
  }
  // pressure altitude corrected to a setting: +27.3 ft per hPa above 1013.25
  function baroAlt(altBaroFt, hpa) { return altBaroFt + (hpa - C.STD_HPA) * C.FT_PER_HPA; }

  const D2R = Math.PI / 180, R2D = 180 / Math.PI, KT = 0.514444, G = 9.81;
  const wrap360 = x => ((x % 360) + 360) % 360;
  const wrap180 = x => ((x % 360) + 540) % 360 - 180;
  const r6 = x => Math.round(x * 1e6) / 1e6 || 0;        // trims float noise (252.8-20.4 = 232.40000000000003); -0 -> 0

  // bank angle for a steady turn: atan(V*omega/g), clamped to +-45 (the estimate is only ever a gentle-turn guess)
  function bankEstimate(rateDps, kt) {
    const b = Math.atan(kt * KT * rateDps * D2R / G) * R2D;
    return r6(Math.max(-C.BANK_EST_MAX, Math.min(C.BANK_EST_MAX, b)));
  }
  // flight-path marker: sideways = drift (track - heading), up/down = flight-path angle
  function driftFpa(trkTrue, hdgTrue, vsFpm, gsKt) {
    return { dx: r6(wrap180(trkTrue - hdgTrue)), dy: r6(Math.atan2((vsFpm || 0) * 0.00508, (gsKt || 0) * KT) * R2D) };
  }
  // wind = ground vector - air vector; returned as where it blows FROM (true), and its speed
  function windOf(gsKt, trkTrue, tasKt, hdgTrue) {
    if ([gsKt, trkTrue, tasKt, hdgTrue].some(v => num(v) == null)) return null;
    const we = gsKt * Math.sin(trkTrue * D2R) - tasKt * Math.sin(hdgTrue * D2R);
    const wn = gsKt * Math.cos(trkTrue * D2R) - tasKt * Math.cos(hdgTrue * D2R);
    return { from: r6(wrap360(Math.atan2(we, wn) * R2D + 180)), kt: r6(Math.hypot(we, wn)) };
  }
  /* The heading box. Magnetic, as on the aircraft's own display: true heading minus declination. With no magnetic
     heading from the aircraft the page steers by its track, and the box says TRK; with no declination model it can
     only show true, and says TRU. */
  function headingView(magH, hdgTrue, decl) {
    if (num(decl) != null) return { v: r6(wrap360(hdgTrue - decl)), trk: num(magH) == null, tru: false };
    if (num(magH) != null) return { v: magH, trk: false, tru: false };
    return { v: hdgTrue, trk: true, tru: true };
  }
  function headingText(deg) { const r = Math.round(wrap360(deg)) % 360; return r === 0 ? '360' : String(r).padStart(3, '0'); }
  // height above the ground under the aircraft, feet; shown only below 2,500 ft, as a radar altimeter would be
  function aglFt(altM, terrainM) {
    if (num(altM) == null || num(terrainM) == null) return null;
    const ft = Math.max(0, (altM - terrainM) / C.M_PER_FT);
    return ft < C.AGL_MAX_FT ? ft : null;
  }
  function brgNm(lat, lon, lat2, lon2) {
    const p1 = lat * D2R, p2 = lat2 * D2R, dl = (lon2 - lon) * D2R;
    const brg = wrap360(Math.atan2(Math.sin(dl) * Math.cos(p2), Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl)) * R2D);
    const h = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
    return { brg, nm: 2 * 6371008.8 * Math.asin(Math.min(1, Math.sqrt(h))) / 1852 };
  }
  /* The next fix: the one the aircraft's datalink names, when we know where it is; otherwise the first fix of the
     matched arrival path that lies on the path (within 2 km) and ahead of the aircraft along it. */
  function nextFix(dl, path, fixes, lat, lon, profileAt) {
    const pos = n => { const p = fixes && fixes[String(n || '').toUpperCase()]; return (p && num(p[0]) != null && num(p[1]) != null) ? p : null; };
    if (dl && dl.next_wp) { const p = pos(dl.next_wp); if (p) return { name: String(dl.next_wp).toUpperCase(), lat: p[0], lon: p[1] }; }
    if (!path || !Array.isArray(path.fixes) || typeof profileAt !== 'function' || num(lat) == null || num(lon) == null) return null;
    const me = profileAt(path, lat, lon); if (!me) return null;
    const at = me.seg + me.t;
    for (const n of path.fixes) {
      const p = pos(n); if (!p) continue;
      const pr = profileAt(path, p[0], p[1]);
      if (!pr || pr.km_off >= 2) continue;
      if (pr.seg + pr.t > at + 1e-9) return { name: String(n).toUpperCase(), lat: p[0], lon: p[1] };
    }
    return null;
  }

  function create(opts) {
    opts = opts || {};
    const taFt = num(opts.taFt) != null ? opts.taFt : C.TA_FT;
    let m = null;            // memory for the followed aircraft: {hex, s:{name:{v,t}}, last, t}
    let waiting = true, lastT = null;

    function put(name, v, now) { if (v != null) m.s[name] = { v, t: now }; }
    function src(name, now) { const s = m && m.s[name]; return s ? { v: s.v, fresh: now - s.t <= C.STALE_MS } : null; }
    // first fresh source of a chain; else 'fail' with the last value if any was seen; else 'none'
    function chain(names, now) {
      let seen = null;
      for (const n of names) { const s = src(n, now); if (!s) continue; if (s.fresh) return { v: s.v, st: 'ok', src: n }; if (!seen) seen = { v: s.v, src: n }; }
      return seen ? { v: seen.v, st: 'fail', src: seen.src } : { v: null, st: 'none', src: null };
    }
    const item = (r, kind) => (r.st === 'ok' && kind && !sane(kind, r.v)) ? { v: r.v, st: 'fail' } : { v: r.v, st: r.st };

    function update(inp, now) {
      lastT = now;
      if (!inp || typeof inp !== 'object' || !inp.a || typeof inp.a !== 'object') { waiting = true; m = null; return; }
      waiting = false;
      const a = inp.a, E = (inp.E && typeof inp.E === 'object') ? inp.E : {};
      const hex = String(a.hex || '').toLowerCase();
      if (!m || m.hex !== hex) m = { hex, s: {} };
      m.last = inp;
      put('ias', num(a.bds60_indicated_airspeed), now);
      put('gs', num(a.gs), now);
      put('tas', num(E.tas), now);
      put('altb', num(a.alt_baro), now);
      if (a.alt_baro === 'ground') put('gnd', true, now);
      put('sel', num(a.bds40_selected_altitude_mcp), now);
      put('crew', num(a.bds40_baro_pressure_setting), now);
      put('mach', num(a.bds60_mach), now);
      put('sqk', sqkText(a.squawk), now);
      put('roll', num(E.roll), now);
      put('rate', num(E.rate), now);
      put('magH', num(E.magH), now);
      put('selhdg', num(a.bds40_selected_heading), now);
      put('navhdg', num(a.nav_heading), now);
      // track history for the bank estimate: the last 10 s
      const trk = num(a.track);
      m.trk = (m.trk || []).filter(s => now - s.t <= C.STALE_MS);
      if (trk != null) m.trk.push({ t: now, v: trk });
      // vertical rate: the raw fields decide "sent" and sanity; the value shown is the page's smoothed one
      const raws = [a.baro_rate, a.bds60_baro_vertical_rate, a.bds60_inertial_vertical_rate].map(num).filter(v => v != null);
      if (raws.length) put('vs', { worst: raws.reduce((w, v) => Math.abs(v) > Math.abs(w) ? v : w, 0), shown: num(inp.vsS) != null ? inp.vsS : raws[0] }, now);
    }

    function view(now) {
      const none = { v: null, st: 'none' };
      if (waiting || !m) {
        return { wait: true, old: false, top: '', spd: { v: null, st: 'none', label: 'GS' }, tas: none, alt: { v: null, st: 'none', gnd: false },
          sel: none, baro: { hpa: C.STD_HPA, src: 'std', st: 'none' }, vs: none, mach: none, sqk: { v: null, st: 'none', emerg: false },
          bank: { v: 0, st: 'none' }, agl: none, hdg: { v: null, st: 'none', trk: false, tru: false }, bug: none, trkMag: none,
          course: { name: null, brgMag: null, nm: null, st: 'none' }, wind: { from: null, kt: null, calm: false, st: 'none' },
          pitch: { v: 0, st: 'none' }, fpm: { dx: 0, dy: 0, st: 'none' },
          acAltM: null, onGround: false, night: false, rwys: [], lat: null, lon: null };
      }
      const inp = m.last, a = inp.a;
      const onGround = a.alt_baro === 'ground';
      const old = (lastT != null && now - lastT > C.STALE_MS) || (num(a.seen) != null && a.seen > 10);

      const sp = chain(['ias', 'gs'], now);
      const spd = Object.assign(item(sp, 'spd'), { label: sp.src === 'ias' ? 'IAS' : 'GS' });

      // baro: crew (held 10 s) -> METAR below the transition altitude -> STD. STD is always there: never an X but by sanity
      const crew = src('crew', now);
      const altNow = onGround ? 'ground' : num(a.alt_baro);
      let baro;
      if (crew && crew.fresh) baro = { hpa: crew.v, src: 'crew', st: sane('baro', crew.v) ? 'ok' : 'fail' };
      else { const b = baroSetting({ alt_baro: altNow }, inp.qnh, taFt); baro = { hpa: b.hpa, src: b.src, st: 'ok' }; }
      const altSet = baro.st === 'ok' ? baro.hpa : baroSetting({ alt_baro: altNow }, inp.qnh, taFt).hpa;

      let alt;
      if (onGround) {
        const gM = num(inp.terrainM) != null ? inp.terrainM : num(inp.groundM);
        alt = gM != null ? { v: gM / C.M_PER_FT, st: 'ok', gnd: true } : { v: null, st: 'none', gnd: true };
      } else {
        const r = chain(['altb'], now);
        if (r.st === 'none') alt = { v: null, st: 'none', gnd: false };
        else if (!sane('alt', r.v)) alt = { v: r.v, st: 'fail', gnd: false };
        else alt = { v: baroAlt(r.v, altSet), st: r.st, gnd: false };
      }

      const vr = chain(['vs'], now);
      const vs = vr.st === 'none' ? none : { v: vr.v.shown, st: (vr.st === 'ok' && !sane('vs', vr.v.worst)) ? 'fail' : vr.st };

      const sq = chain(['sqk'], now);
      const sqk = { v: sq.v, st: sq.st, emerg: sq.v === '7500' || sq.v === '7600' || sq.v === '7700' };

      // speed for the estimates: TAS, else GS (fresh only)
      const tasS = src('tas', now), gsS = src('gs', now);
      const vKt = (tasS && tasS.fresh && sane('spd', tasS.v)) ? tasS.v : ((gsS && gsS.fresh && sane('spd', gsS.v)) ? gsS.v : null);

      // bank: the aircraft's own roll; else estimated from its turn rate (checked rate, or the track over the last 10 s)
      const rl = chain(['roll'], now);
      let bank;
      if (rl.st === 'ok') bank = item(rl, 'bank');
      else {
        const rs = src('rate', now);
        let rate = (rs && rs.fresh) ? rs.v : null;
        if (rate == null && m.trk && m.trk.length >= 2) {
          const f = m.trk[0], l = m.trk[m.trk.length - 1], dt = (l.t - f.t) / 1000;
          if (dt >= 4) rate = wrap180(l.v - f.v) / dt;
        }
        bank = (rate != null && vKt != null) ? { v: bankEstimate(rate, vKt), st: 'est' } : { v: 0, st: 'none' };
      }

      const decl = num(inp.decl), hdgT = num(inp.hdg), trkT = num(inp.trk), stale = !!inp.hdgStale;
      const mh = src('magH', now), magFresh = !!(mh && mh.fresh);
      let hdg;
      if (hdgT == null) hdg = { v: null, st: 'none', trk: false, tru: false };
      else { const h = headingView(magFresh ? mh.v : null, hdgT, decl); hdg = { v: h.v, st: 'ok', trk: h.trk || stale, tru: h.tru }; }
      const trkMag = trkT == null ? none : { v: decl != null ? r6(wrap360(trkT - decl)) : trkT, st: 'ok' };
      const bug = item(chain(['selhdg', 'navhdg'], now));

      const gsK = (gsS && gsS.fresh) ? gsS.v : null;
      const fpm = (trkT != null && hdgT != null && gsK != null) ? Object.assign(driftFpa(trkT, hdgT, num(inp.vsS), gsK), { st: 'ok' }) : { dx: 0, dy: 0, st: 'none' };
      const pitch = num(inp.pitch) != null ? { v: inp.pitch, st: 'ok' } : { v: 0, st: 'none' };

      // wind only from fresh air data with a real (not stale, not track-substitute) magnetic heading
      let wind = { from: null, kt: null, calm: false, st: 'none' };
      if (tasS && tasS.fresh && sane('spd', tasS.v) && magFresh && decl != null && !stale && gsK != null && trkT != null && hdgT != null) {
        const w = windOf(gsK, trkT, tasS.v, hdgT);
        if (w) wind = w.kt < C.CALM_KT ? { from: null, kt: Math.round(w.kt), calm: true, st: 'ok' }
          : { from: (Math.round(wrap360(w.from - decl) / 10) * 10) || 360, kt: Math.round(w.kt), calm: false, st: 'ok' };
      }

      const terr = num(inp.terrainM) != null ? inp.terrainM : num(inp.groundM);
      const ag = aglFt(num(inp.altM), terr);
      const agl = ag == null ? none : { v: ag, st: 'ok' };

      let course = { name: null, brgMag: null, nm: null, st: 'none' };
      const nf = nextFix(inp.dl, inp.path, opts.fixes, num(a.lat), num(a.lon), opts.profileAt);
      if (nf) { const b = brgNm(a.lat, a.lon, nf.lat, nf.lon); course = { name: nf.name, brgMag: decl != null ? r6(wrap360(b.brg - decl)) : b.brg, nm: b.nm, st: 'ok' }; }

      const cs = String(a.flight || '').trim() || String(a.hex || '').toUpperCase();
      const top = [cs, a.t, inp.path && inp.path.name].filter(x => typeof x === 'string' && x.trim()).join(' · ');

      return { wait: false, old, top, spd, tas: item(chain(['tas'], now), 'spd'), alt, sel: item(chain(['sel'], now), 'alt'), baro, vs,
        mach: item(chain(['mach'], now)), sqk, bank, agl, hdg, bug, trkMag, course, wind, pitch, fpm,
        acAltM: num(inp.altM), onGround, night: !!inp.night, rwys: Array.isArray(inp.rwys) ? inp.rwys : [], lat: num(a.lat), lon: num(a.lon) };
    }
    return { update, view };
  }

  const api = { C, num, sqkText, baroSetting, baroAlt, bankEstimate, driftFpa, windOf, headingView, headingText, aglFt, brgNm, nextFix, create };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.PFDCore = api;
})(typeof window !== 'undefined' ? window : this);

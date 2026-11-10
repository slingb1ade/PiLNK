/* PFD for 3D Approach (AJ, 9 Nov 2026): a cockpit-style primary flight display with synthetic vision, as an
   alternative to the data box. Spec: pilnk-tasks/pfd-spec.md. The numbers come from pfd-core.js (fed once a poll by
   approach.html with the values it already worked out for the data box); the terrain view from pfd-svt.js.

   approach.html calls two things: PFD.feed(inp, Date.now()) once a poll, PFD.frame(pose, performance.now()) every
   frame. While the data box is showing, frame() returns before doing anything: no drawing, no terrain mesh, no tile
   requests - the page behaves exactly as before. Shown, it draws at most 30 times a second.
   Choices are kept in this browser: pilnk-pfd-mode (data|pfd), pilnk-pfd-big (0|1), pilnk-pfd-svt (0|1).
   ?pfd=1 opens the page with the PFD showing, without changing the stored choice. */
(function (root) {
  'use strict';
  const KEY = { mode: 'pilnk-pfd-mode', big: 'pilnk-pfd-big', svt: 'pilnk-pfd-svt' };
  const SMALL_W = 380, SMALL_H = 300, BIG_MAX = 900, PHONE_W = 600, MIN_MS = 33;
  const COL = { WHITE: '#ffffff', CYAN: '#00e5ff', MAGENTA: '#ff3df2', GREEN: '#37d67a', RED: '#ff3030' };
  const q = new URLSearchParams(root.location ? root.location.search : '');
  const getS = k => { try { return root.localStorage.getItem(k); } catch (e) { return null; } };
  const setS = (k, v) => { try { root.localStorage.setItem(k, v); } catch (e) {} };

  const S = {
    mode: q.get('pfd') === '1' ? 'pfd' : (getS(KEY.mode) === 'pfd' ? 'pfd' : 'data'),
    big: getS(KEY.big) === '1', svt: getS(KEY.svt) !== '0',
    drawn: 0, lastDraw: -1e9, boxes: {}, view: null, pose: null, w: SMALL_W, h: SMALL_H,
    el: null, ins: null, svtCanvas: null, built: false, core: null, errs: 0
  };
  const taFt = (() => { const v = parseFloat(q.get('ta')); return isFinite(v) && v > 0 ? v : null; })();

  function warn(where, e) { S.errs++; if (S.errs === 1 || S.errs % 30 === 0) console.warn('PFD: ' + where + ' failed (' + S.errs + 'x):', e); }
  function core() {
    if (!S.core && root.PFDCore) {
      const fixes = {};   // the node's fix list (fixes-nz.js, [name, lat, lon]); elsewhere empty: no course line
      for (const f of (Array.isArray(root.IFR_FIXES_NZ) ? root.IFR_FIXES_NZ : [])) if (f && typeof f[0] === 'string') fixes[f[0].toUpperCase()] = [f[1], f[2]];
      const AP = root.ArrivalPaths;
      S.core = root.PFDCore.create({ fixes, profileAt: AP && typeof AP.profileAt === 'function' ? AP.profileAt : null, taFt });
    }
    return S.core;
  }
  const DEBUG = q.get('debug') === '1';
  S.dbg = { frames: 0, draws: 0, el: null };

  const CSS = '#pfd{position:fixed;top:12px;right:12px;z-index:11;display:none;background:#05070d;border:1px solid var(--line,#1f2a44);' +
    'border-radius:8px;overflow:hidden;pointer-events:auto;box-shadow:0 6px 22px rgba(0,0,0,.45)}' +
    'body.pfdon #pfd{display:block} body.pfdon #tr{display:none!important}' +
    '#pfd canvas{position:absolute;left:0;top:0;width:100%;height:100%;display:block}' +
    '#pfd .pb{position:absolute;z-index:3;font:600 10px ui-monospace,Menlo,Consolas,monospace;letter-spacing:.04em;background:rgba(10,14,26,.78);' +
    'color:#dfe7f5;border:1px solid #2a3858;border-radius:5px;padding:3px 6px;cursor:pointer;line-height:1;user-select:none}' +
    '#pfd .pb.off{opacity:.55} #pfd_data{bottom:4px;left:4px} #pfd_svtbtn{bottom:4px;right:30px} #pfd_big{bottom:4px;right:4px;padding:3px 5px}' +
    '.pfdsw{position:absolute;top:-8px;right:10px;font:600 9px ui-monospace,Menlo,Consolas,monospace;letter-spacing:.06em;line-height:1;' +
    'padding:2px 6px;border:1px solid var(--purple,#7c3aed);border-radius:6px;background:var(--panel,#0a0e1a);color:var(--txt,#dfe7f5);cursor:pointer;pointer-events:auto}' +
    '@media (max-width:640px){#pfd{top:calc(8px + env(safe-area-inset-top));right:8px}}';

  function build() {
    if (S.built || !root.document) return S.built;
    const d = root.document, el = d.getElementById('pfd'), sw = d.getElementById('d_pfdsw');
    if (!el) return false;
    const st = d.createElement('style'); st.id = 'pfd_css'; st.textContent = CSS; d.head.appendChild(st);
    S.svtCanvas = d.createElement('canvas'); S.svtCanvas.id = 'pfd_svt';
    S.ins = d.createElement('canvas'); S.ins.id = 'pfd_ins';
    el.appendChild(S.svtCanvas); el.appendChild(S.ins);
    const btn = (id, text, title, fn) => { const b = d.createElement('div'); b.className = 'pb'; b.id = id; b.textContent = text; b.title = title;
      b.addEventListener('click', e => { e.stopPropagation(); fn(); }); el.appendChild(b); return b; };
    btn('pfd_data', 'DATA', 'Back to the data box', () => setMode('data', true));
    S.svtBtn = btn('pfd_svtbtn', 'SVT', 'Synthetic vision on / off', () => setSvt(!S.svt));
    btn('pfd_big', '⤢', 'Bigger / smaller', () => setBig(!S.big));
    if (sw) { sw.hidden = false; sw.addEventListener('click', e => { e.stopPropagation(); setMode('pfd', true); }); }   // hidden in the HTML until this runs
    S.el = el; S.built = true;
    root.addEventListener('resize', layout);
    if (DEBUG) {   // for the live check: the page's own frame rate and the PFD's
      const f = d.createElement('div'); f.id = 'pfd_fps';
      f.style.cssText = 'position:fixed;top:2px;left:50%;transform:translateX(-50%);z-index:20;font:10px ui-monospace,Menlo,monospace;color:#9fe;background:rgba(0,0,0,.55);padding:1px 6px;border-radius:4px;pointer-events:none';
      d.body.appendChild(f); S.dbg.el = f;
      setInterval(() => { f.textContent = 'page ' + S.dbg.frames + ' fps' + (S.mode === 'pfd' ? ' · pfd ' + S.dbg.draws + ' fps' : ''); S.dbg.frames = 0; S.dbg.draws = 0; }, 1000);
    }
    applyMode(); layout();
    return true;
  }
  function applyMode() { if (root.document && root.document.body) root.document.body.classList.toggle('pfdon', S.mode === 'pfd'); if (S.svtBtn) S.svtBtn.classList.toggle('off', !S.svt); }
  function setMode(m, store) { S.mode = m; if (store) setS(KEY.mode, m); applyMode(); layout(); }
  function setBig(b) { S.big = !!b; setS(KEY.big, S.big ? '1' : '0'); layout(); }
  function setSvt(on) { S.svt = !!on; setS(KEY.svt, S.svt ? '1' : '0'); if (!S.svt) dropSvt(); applyMode(); }
  function dropSvt() { if (S.svtObj) { try { S.svtObj.dispose(); } catch (e) {} } S.svtObj = null; S.svtFailed = false; if (S.svtCanvas) S.svtCanvas.style.visibility = 'hidden'; }
  // synthetic vision: made the first time the PFD is shown with SVT on; null (no WebGL) -> the flat horizon, for good
  function ensureSvt() {
    if (!S.svt || S.svtObj || S.svtFailed) return S.svtObj || null;
    if (!root.THREE || !root.PFDSVT || typeof root.tileFetch !== 'function') return null;
    S.svtObj = root.PFDSVT.create(root.THREE, S.svtCanvas, { fetchTile: u => root.tileFetch(u) });
    if (!S.svtObj) S.svtFailed = true; else S.svtObj.setSize(S.w, S.h, S.dpr || 1);
    return S.svtObj;
  }

  // small 380 x 300; big = half the window at 4:3, at most 900 wide; under 600 px wide big = full width
  function layout() {
    if (!S.el) return;
    const W = root.innerWidth || 1024;
    let w = SMALL_W, h = SMALL_H;
    if (S.big) { w = W < PHONE_W ? W : Math.min(BIG_MAX, W / 2); h = w * 3 / 4; }
    const full = S.big && W < PHONE_W;
    S.el.style.width = w + 'px'; S.el.style.height = h + 'px';
    S.el.style.right = full ? '0px' : ''; S.el.style.borderRadius = full ? '0' : '';
    const dpr = root.devicePixelRatio || 1;
    for (const c of [S.ins, S.svtCanvas]) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
    S.w = w; S.h = h; S.dpr = dpr;
    if (S.svtObj) S.svtObj.setSize(w, h, dpr);
  }

  function feed(inp, now) {
    try {
      if (!S.built) build();
      const c = core(); if (!c) return;
      // the terrain under the aircraft, from the SVT tiles once they are in (else the page's own ground)
      if (inp && typeof inp === 'object' && inp.a && typeof inp.a.lat === 'number' && S.svtObj && S.svtObj.ready())
        inp.terrainM = S.svtObj.heightAt(inp.a.lat, inp.a.lon);
      c.update(inp, now);
    } catch (e) { warn('feed', e); }
  }
  function frame(pose, t) {
    try {
      if (DEBUG) S.dbg.frames++;
      if (S.mode !== 'pfd' || (root.document && root.document.hidden)) return;
      if (!S.built && !build()) return;
      if (t - S.lastDraw < MIN_MS) return;
      const dt = Math.min(1, Math.max(0, (t - S.lastDraw) / 1000));
      S.lastDraw = t; S.pose = pose || null;
      const c = core(); S.view = c ? c.view(Date.now()) : null;
      draw(S.view, S.pose, dt);
      S.drawn++; if (DEBUG) S.dbg.draws++;
    } catch (e) { warn('frame', e); }
  }
  function rec(id, text, st, c, x) { S.boxes[id] = { text, st: st || 'ok', x: !!x, c: c || COL.WHITE }; }

  /* ---------- drawing ----------
     Everything moving eases toward its latest reading (time constant 0.2 s, i.e. k = 0.15 a frame at 30 fps); the
     recorded box texts are the readings themselves. Layout follows the photo: speed tape left, altitude tape and
     vertical speed right, compass arc at the bottom, bank scale at the top, the flight-path marker in green. */
  const D2R = Math.PI / 180, R2D = 180 / Math.PI, TAU = 0.2;
  const SKY = '#2f6fc9', GROUND = '#7a4a1e', SYMBOL = '#ffcc00', PANEL = 'rgba(0,0,0,0.5)';
  const wrap180 = x => ((x % 360) + 540) % 360 - 180;
  const hdgTxt = v => (root.PFDCore ? root.PFDCore.headingText(v) : String(Math.round(v)));
  function ease(key, target, dt, angle) {
    const E = S.ease || (S.ease = {});
    if (E[key] == null || !isFinite(E[key])) { E[key] = target; return target; }
    const k = 1 - Math.exp(-dt / TAU);
    E[key] = angle ? ((E[key] + wrap180(target - E[key]) * k) + 360) % 360 : E[key] + (target - E[key]) * k;
    return E[key];
  }
  function xmark(g, x, y, w, h) {
    g.save(); g.strokeStyle = COL.RED; g.lineWidth = 2.5; g.beginPath(); g.moveTo(x, y); g.lineTo(x + w, y + h); g.moveTo(x + w, y); g.lineTo(x, y + h); g.stroke(); g.restore();
  }
  const FONT = px => '600 ' + Math.round(px) + 'px ui-monospace,Menlo,Consolas,monospace';
  // A number box. The text is drawn at opts.px (default the display's base size), shrunk to fit the box if it must
  // (never below 60 %), and only then shortened with an ellipsis: nothing ever spills out of its box.
  function box(g, id, x, y, w, h, text, st, col, align, opts) {
    opts = opts || {};
    let shown = st === 'none' ? '----' : String(text == null ? '' : text);
    const c = col || COL.WHITE, px0 = opts.px || S.fs, room = w - (align === 'left' || align === 'right' ? 8 : 6);
    if (!opts.noBg) { g.fillStyle = 'rgba(0,0,0,0.78)'; g.fillRect(x, y, w, h); g.strokeStyle = 'rgba(255,255,255,0.55)'; g.lineWidth = 1; g.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1); }
    let px = px0; g.font = FONT(px); let tw = g.measureText(shown).width;
    if (tw > room) { px = Math.max(px0 * 0.6, px0 * room / tw); g.font = FONT(px); tw = g.measureText(shown).width; }
    while (tw > room && shown.length > 1) { shown = shown.slice(0, -2) + '…'; tw = g.measureText(shown).width; }
    g.fillStyle = st === 'none' ? 'rgba(255,255,255,0.6)' : c; g.textBaseline = 'middle'; g.textAlign = align || 'center';
    g.fillText(shown, align === 'left' ? x + 4 : align === 'right' ? x + w - 4 : x + w / 2, y + h / 2 + 0.5);
    const fail = st === 'fail' || !!opts.forceX;
    if (fail) xmark(g, x, y, w, h);
    rec(id, shown, st, c, fail);
    S.boxes[id].g = { x, y, w, h, tw };   // where it went, for the layout test
    g.font = FONT(S.fs);
  }
  function draw(v, pose, dt) {
    const g = S.ins.getContext('2d'), dpr = S.dpr || 1, w = S.w, h = S.h;
    g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
    const fs = Math.max(9, Math.round(h * 0.045)); S.fs = fs; g.font = FONT(fs);
    const th = h * 0.07, rowH = fs * 1.1;   // the top strip, and one row of small boxes
    const key = pose && pose.key; if (key !== S.easeKey) { S.ease = {}; S.easeKey = key; }
    if (!v || v.wait) {
      dropFrame(g, w, h); g.fillStyle = 'rgba(0,0,0,0.6)'; g.fillRect(0, 0, w, h);
      g.fillStyle = '#dfe7f5'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText('Waiting for an aircraft', w / 2, h / 2);
      rec('wait', 'Waiting for an aircraft', 'ok'); delete S.boxes.old; S.svtPose = null; return;
    }
    delete S.boxes.wait;
    const old = !!v.old;

    // attitude: pitch from the page's eased pose, bank from the core (measured or estimated), both eased here
    const pitch = ease('pitch', pose && typeof pose.pitch === 'number' ? pose.pitch : (v.pitch.v || 0), dt);
    const bank = ease('bank', v.bank.st === 'none' ? 0 : v.bank.v, dt);
    const vfov = 2 * Math.atan(Math.tan(20 * D2R) / (w / h)) * R2D, ppd = h / vfov, cx = w / 2, cy = h / 2;

    // synthetic vision behind, or the flat horizon
    const svt = ensureSvt();
    let svtOn = false;
    if (svt && pose && typeof pose.lat === 'number') {
      S.svtPose = { lat: pose.lat, lon: pose.lon, altM: pose.altM, hdg: pose.hdg, pitch, bank };
      svt.setPose(S.svtPose); svt.setBands(v.acAltM, v.onGround); svt.setNight(v.night); S.svtNight = !!v.night;
      if (v.rwys !== S.rwysSent || (v.rwys && v.rwys.length !== S.rwysLen)) { svt.setRunways(v.rwys || []); S.rwysSent = v.rwys; S.rwysLen = v.rwys ? v.rwys.length : 0; }
      svtOn = svt.ok() && svt.ready();
      if (svtOn) svt.render();
    } else S.svtPose = pose ? { lat: pose.lat, lon: pose.lon, altM: pose.altM, hdg: pose.hdg, pitch, bank } : null;
    S.svtCanvas.style.visibility = svtOn ? 'visible' : 'hidden';
    S.svtActive = svtOn;
    g.save(); g.translate(cx, cy); g.rotate(-bank * D2R);
    if (!svtOn) {
      const yh = pitch * ppd;
      g.fillStyle = SKY; g.fillRect(-w * 2, -h * 3 + yh, w * 4, h * 3);
      g.fillStyle = GROUND; g.fillRect(-w * 2, yh, w * 4, h * 3);
    }
    rec('horizon', svtOn ? 'svt' : 'flat', 'ok');
    // horizon line + pitch ladder (clipped to the middle)
    g.save(); g.beginPath(); g.rect(-w * 0.31, -h * 0.4, w * 0.62, h * 0.66); g.clip();
    g.strokeStyle = '#ffffff'; g.lineWidth = 1.5; g.beginPath(); g.moveTo(-w * 2, pitch * ppd); g.lineTo(w * 2, pitch * ppd); g.stroke();
    g.fillStyle = '#ffffff'; g.textBaseline = 'middle'; g.font = '600 ' + Math.round(fs * 0.8) + 'px ui-monospace,Menlo,monospace';
    for (let p = -30; p <= 30; p += 5) {
      if (!p) continue;
      const y = (pitch - p) * ppd, len = p % 10 ? w * 0.05 : w * 0.11;
      g.lineWidth = 1.2; g.beginPath(); g.moveTo(-len, y); g.lineTo(len, y); g.stroke();
      if (!(p % 10)) { g.textAlign = 'right'; g.fillText(String(Math.abs(p)), -len - 3, y); g.textAlign = 'left'; g.fillText(String(Math.abs(p)), len + 3, y); }
    }
    g.restore();
    // flight-path marker: drift sideways, flight-path angle up/down
    if (v.fpm.st === 'ok') {
      const fx = v.fpm.dx * ppd, fy = (pitch - v.fpm.dy) * ppd;
      g.strokeStyle = COL.GREEN; g.lineWidth = 2; g.beginPath(); g.arc(fx, fy, fs * 0.45, 0, Math.PI * 2);
      g.moveTo(fx - fs * 1.1, fy); g.lineTo(fx - fs * 0.45, fy); g.moveTo(fx + fs * 0.45, fy); g.lineTo(fx + fs * 1.1, fy); g.moveTo(fx, fy - fs * 0.45); g.lineTo(fx, fy - fs * 0.9); g.stroke();
    }
    rec('fpm', v.fpm.st === 'ok' ? v.fpm.dx.toFixed(1) + ',' + v.fpm.dy.toFixed(1) : '----', v.fpm.st, COL.GREEN);
    g.restore();

    // bank scale (fixed) and pointer (moves with bank)
    const br = h * 0.36;
    g.save(); g.translate(cx, cy); g.strokeStyle = '#ffffff'; g.lineWidth = 1.5;
    g.beginPath(); g.arc(0, 0, br, (-90 - 60) * D2R, (-90 + 60) * D2R); g.stroke();
    for (const a of [-60, -45, -30, -20, -10, 10, 20, 30, 45, 60]) {
      const r2 = br + (Math.abs(a) % 30 ? fs * 0.4 : fs * 0.75), t = (-90 + a) * D2R;
      g.beginPath(); g.moveTo(Math.cos(t) * br, Math.sin(t) * br); g.lineTo(Math.cos(t) * r2, Math.sin(t) * r2); g.stroke();
    }
    g.fillStyle = '#ffffff'; g.beginPath(); g.moveTo(0, -br); g.lineTo(-fs * 0.4, -br - fs * 0.6); g.lineTo(fs * 0.4, -br - fs * 0.6); g.closePath(); g.fill();
    g.rotate(-bank * D2R); g.fillStyle = v.bank.st === 'fail' ? COL.RED : SYMBOL;
    g.beginPath(); g.moveTo(0, -br + 1); g.lineTo(-fs * 0.45, -br + fs * 0.7); g.lineTo(fs * 0.45, -br + fs * 0.7); g.closePath(); g.fill();
    g.restore();
    const bankFail = v.bank.st === 'fail' || old;
    const bx = cx + fs * 1.4, bw = fs * 2.6, bh = rowH * 0.95;
    box(g, 'bank', bx, th + 1, bw, bh, String(Math.round(v.bank.v || 0)), v.bank.st, COL.WHITE, 'center', { noBg: true, px: fs * 0.8, forceX: bankFail && v.bank.st !== 'none' });
    if (v.bank.st === 'est') { g.font = FONT(fs * 0.7); g.fillStyle = SYMBOL; g.textAlign = 'left'; g.textBaseline = 'middle'; g.fillText('est', bx + bw + 2, th + 1 + bh / 2); g.font = FONT(fs); }

    // aircraft symbol (fixed)
    g.save(); g.translate(cx, cy); g.fillStyle = SYMBOL; g.strokeStyle = '#000'; g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, 0); g.lineTo(-w * 0.12, fs * 0.9); g.lineTo(-w * 0.09, fs * 0.9); g.lineTo(0, fs * 0.35); g.lineTo(w * 0.09, fs * 0.9); g.lineTo(w * 0.12, fs * 0.9); g.closePath(); g.fill(); g.stroke();
    g.restore();

    // AGL under the aircraft symbol, only below 2,500 ft
    if (v.agl.st === 'ok') box(g, 'agl', cx - fs * 3, cy + h * 0.17, fs * 6, fs * 1.3, 'AGL ' + Math.round(v.agl.v / 10) * 10, 'ok', COL.WHITE, 'center', { forceX: old });
    else rec('agl', '----', 'none');

    // speed tape (left)
    const tx0 = 0, tw = w * 0.16, ty0 = th + 3 + 2 * rowH + 2, ty1 = h * 0.70, tm = (ty0 + ty1) / 2;   // Mach and the IAS/GS label sit above
    g.fillStyle = PANEL; g.fillRect(tx0, ty0, tw, ty1 - ty0);
    const spdE = v.spd.st === 'none' ? null : ease('spd', v.spd.v, dt), ppk = (ty1 - ty0) / 80;
    if (spdE != null) {
      g.save(); g.beginPath(); g.rect(tx0, ty0, tw, ty1 - ty0); g.clip(); g.strokeStyle = '#fff'; g.fillStyle = '#fff'; g.lineWidth = 1; g.textAlign = 'right'; g.textBaseline = 'middle';
      for (let s = Math.floor((spdE - 45) / 5) * 5; s <= spdE + 45; s += 5) {
        if (s < 0) continue; const y = tm - (s - spdE) * ppk;
        g.beginPath(); g.moveTo(tw - (s % 10 ? 5 : 9), y); g.lineTo(tw, y); g.stroke();
        if (!(s % 20)) g.fillText(String(s), tw - 11, y);
      }
      g.restore();
    }
    box(g, 'spd', tx0 + 2, tm - fs * 0.8, tw - 2, fs * 1.6, String(Math.round(v.spd.v)), v.spd.st, COL.WHITE, 'center', { forceX: old && v.spd.st !== 'none' });
    box(g, 'mach', tx0, th + 2, tw, rowH, v.mach.st === 'none' ? '' : 'M' + Number(v.mach.v).toFixed(3).replace(/^0/, ''), v.mach.st, COL.WHITE, 'center', { px: fs * 0.8, forceX: old && v.mach.st !== 'none' });
    box(g, 'spdlbl', tx0, th + 3 + rowH, tw, rowH, v.spd.label, 'ok', COL.WHITE, 'center', { noBg: true, px: fs * 0.85 });
    box(g, 'tas', tx0, ty1 + 2, tw, rowH, 'TAS ' + Math.round(v.tas.v) + 'KT', v.tas.st, COL.WHITE, 'center', { px: fs * 0.8, forceX: old && v.tas.st !== 'none' });

    // altitude tape + selected altitude + baro box (right), vertical speed beside it
    const ax0 = w * 0.76, aw = w * 0.14, pp100 = (ty1 - ty0) / 8;
    g.fillStyle = PANEL; g.fillRect(ax0, ty0, aw, ty1 - ty0);
    const altE = v.alt.st === 'none' ? null : ease('alt', v.alt.v, dt);
    if (altE != null) {
      g.save(); g.beginPath(); g.rect(ax0, ty0, aw, ty1 - ty0); g.clip(); g.strokeStyle = '#fff'; g.fillStyle = '#fff'; g.lineWidth = 1; g.textAlign = 'left'; g.textBaseline = 'middle';
      for (let a = Math.floor((altE - 500) / 100) * 100; a <= altE + 500; a += 100) {
        const y = tm - (a - altE) / 100 * pp100;
        g.beginPath(); g.moveTo(ax0, y); g.lineTo(ax0 + (a % 200 ? 5 : 9), y); g.stroke();
        if (!(a % 200)) g.fillText(String(a), ax0 + 11, y);
      }
      if (v.sel.st === 'ok') { const ys = tm - (v.sel.v - altE) / 100 * pp100; g.fillStyle = COL.CYAN; g.fillRect(ax0, Math.max(ty0, Math.min(ty1 - 6, ys - 3)), 6, 6); }
      g.restore();
    }
    box(g, 'alt', ax0, tm - fs * 0.8, aw, fs * 1.6, v.alt.v == null ? '' : String(Math.round(v.alt.v / 10) * 10), v.alt.st, COL.WHITE, 'center', { forceX: old && v.alt.st !== 'none' });
    box(g, 'sel', ax0, ty0 - rowH - 1, aw, rowH, v.sel.v == null ? '' : String(Math.round(v.sel.v)), v.sel.st, COL.CYAN, 'center', { px: fs * 0.9, forceX: old && v.sel.st !== 'none' });
    const b = v.baro;
    const btxt = b.src === 'crew' ? Math.round(b.hpa) + ' hPa' : (b.src === 'met' ? 'QNH ' + Math.round(b.hpa) + ' MET' : 'STD');
    box(g, 'baro', ax0, ty1 + 2, w - ax0, rowH, btxt, b.st === 'none' ? 'ok' : b.st, b.src === 'crew' ? COL.CYAN : COL.WHITE, 'center', { px: fs * 0.8 });
    const vx0 = ax0 + aw, vwid = w - vx0;
    g.fillStyle = PANEL; g.fillRect(vx0, ty0, vwid, ty1 - ty0);
    g.strokeStyle = '#fff'; g.lineWidth = 1;
    for (const f of [-2000, -1000, 0, 1000, 2000]) { const y = tm - f / 2000 * (ty1 - ty0) / 2; g.beginPath(); g.moveTo(vx0, y); g.lineTo(vx0 + (f ? 5 : 8), y); g.stroke(); }
    if (v.vs.st !== 'none') {
      const vsE = ease('vs', v.vs.v, dt), yv = tm - Math.max(-2000, Math.min(2000, vsE)) / 2000 * (ty1 - ty0) / 2;
      g.fillStyle = COL.GREEN; g.beginPath(); g.moveTo(vx0 + 2, yv); g.lineTo(vx0 + vwid, yv - fs * 0.4); g.lineTo(vx0 + vwid, yv + fs * 0.4); g.closePath(); g.fill();
    }
    box(g, 'vs', vx0, ty0 - rowH - 1, vwid, rowH, v.vs.v == null ? '' : String(Math.round(v.vs.v / 10) * 10), v.vs.st, COL.WHITE, 'center', { px: fs * 0.75, forceX: old && v.vs.st !== 'none' });   // above its tape, beside the selected altitude

    // compass arc (bottom): heading box, cyan bug, green track, magenta course pointer
    const hd = v.hdg.st === 'none' ? null : ease('hdg', v.hdg.v, dt, true);
    const ccx = cx, ccy = h * 1.2, cr = h * 0.44;
    S.compass = { cx: ccx, cy: ccy, r: cr };
    g.save(); g.beginPath(); g.rect(0, h * 0.74, w, h * 0.26); g.clip();
    g.fillStyle = 'rgba(0,0,0,0.55)'; g.beginPath(); g.arc(ccx, ccy, cr, 0, Math.PI * 2); g.fill();
    if (hd != null) {
      g.strokeStyle = '#fff'; g.fillStyle = '#fff'; g.lineWidth = 1; g.textAlign = 'center'; g.textBaseline = 'middle';
      for (let a = 0; a < 360; a += 5) {
        const t = (wrap180(a - hd) - 90) * D2R, r2 = cr - (a % 10 ? 5 : 9);
        g.beginPath(); g.moveTo(ccx + Math.cos(t) * cr, ccy + Math.sin(t) * cr); g.lineTo(ccx + Math.cos(t) * r2, ccy + Math.sin(t) * r2); g.stroke();
        if (!(a % 30)) { const lbl = { 0: 'N', 90: 'E', 180: 'S', 270: 'W' }[a] || String(a / 10); g.fillText(lbl, ccx + Math.cos(t) * (cr - fs * 1.4), ccy + Math.sin(t) * (cr - fs * 1.4)); }
      }
      const mark = (deg, col, inward) => { const t = (wrap180(deg - hd) - 90) * D2R, x = ccx + Math.cos(t) * cr, y = ccy + Math.sin(t) * cr;
        g.save(); g.translate(x, y); g.rotate(t + Math.PI / 2); g.fillStyle = col; g.beginPath();
        if (inward) { g.moveTo(0, 2); g.lineTo(-fs * 0.35, fs * 0.6); g.lineTo(fs * 0.35, fs * 0.6); } else { g.rect(-fs * 0.45, -fs * 0.2, fs * 0.9, fs * 0.35); }
        g.closePath(); g.fill(); g.restore(); };
      if (v.bug.st === 'ok') mark(v.bug.v, COL.CYAN, false);
      if (v.trkMag.st === 'ok') mark(v.trkMag.v, COL.GREEN, true);
      if (v.course.st === 'ok') { const t = (wrap180(v.course.brgMag - hd) - 90) * D2R;
        g.strokeStyle = COL.MAGENTA; g.lineWidth = 2.5; g.beginPath(); g.moveTo(ccx + Math.cos(t) * cr * 0.55, ccy + Math.sin(t) * cr * 0.55); g.lineTo(ccx + Math.cos(t) * (cr - 2), ccy + Math.sin(t) * (cr - 2)); g.stroke(); }
    }
    g.restore();
    const hlbl = (v.hdg.trk ? ' TRK' : '') + (v.hdg.tru ? ' TRU' : '');
    box(g, 'hdg', cx - fs * 3.4, h * 0.745, fs * 6.8, fs * 1.4, v.hdg.v == null ? '' : hdgTxt(v.hdg.v) + hlbl, v.hdg.st, COL.WHITE, 'center', { forceX: old && v.hdg.st !== 'none' });
    rec('bug', v.bug.st === 'ok' ? hdgTxt(v.bug.v) : '----', v.bug.st, COL.CYAN);
    if (v.course.st === 'ok') box(g, 'course', w * 0.78, h * 0.79 + rowH + 4, w * 0.22 - 2, rowH, v.course.name + ' ' + v.course.nm.toFixed(1) + ' NM', 'ok', COL.MAGENTA, 'center', { noBg: true, px: fs * 0.75 });   // under the squawk, clear of the compass
    else rec('course', '----', 'none', COL.MAGENTA);

    // wind (bottom left), squawk (bottom right), top strip, mark
    const wt = v.wind.st !== 'ok' ? '' : (v.wind.calm ? 'Calm' : v.wind.from + '° / ' + v.wind.kt + ' kt');
    box(g, 'wind', 2, h * 0.79, w * 0.24, rowH, wt, v.wind.st, COL.WHITE, 'center', { px: fs * 0.8 });
    box(g, 'sqk', w * 0.84 - 2, h * 0.79, w * 0.16, rowH, v.sqk.v || '', v.sqk.st, v.sqk.emerg ? COL.RED : COL.WHITE, 'center', { px: fs * 0.85, forceX: old && v.sqk.st !== 'none' });
    g.fillStyle = 'rgba(0,0,0,0.6)'; g.fillRect(0, 0, w, th);
    box(g, 'top', 4, 0, w * 0.8 - 4, th, v.top || '', 'ok', COL.WHITE, 'left', { noBg: true, px: fs * 0.85 });
    g.font = FONT(fs * 0.85); g.fillStyle = 'rgba(200,190,255,0.85)'; g.textAlign = 'right'; g.textBaseline = 'middle'; g.fillText('PiLNK', w - 4, th / 2); g.font = FONT(fs);

    if (old) {
      g.fillStyle = 'rgba(150,0,0,0.85)'; g.fillRect(cx - fs * 4, cy - h * 0.2 - fs, fs * 8, fs * 1.8);
      g.fillStyle = '#fff'; g.textAlign = 'center'; g.textBaseline = 'middle'; g.fillText('DATA OLD', cx, cy - h * 0.2 - fs * 0.1);
      rec('old', 'DATA OLD', 'fail', COL.RED);
    } else delete S.boxes.old;
  }
  function dropFrame() { S.svtActive = false; if (S.svtCanvas) S.svtCanvas.style.visibility = 'hidden'; }

  function state() {
    const s = S.svtObj;
    return { mode: S.mode, big: S.big, svt: S.svt, svtActive: !!S.svtActive, svtNight: !!S.svtNight, svtPose: S.svtPose || null,
      svtStats: s ? s.stats() : null, drawn: S.drawn, w: S.w, h: S.h, boxes: S.boxes, view: S.view, compass: S.compass || null };
  }
  function svtProbe(x, y) { return S.svtObj ? S.svtObj.probe(x, y) : null; }

  const api = { feed, frame, state, svtProbe, COL };
  root.PFD = api;
  if (root.document) {
    if (root.document.readyState === 'loading') root.document.addEventListener('DOMContentLoaded', build); else build();
  }
})(typeof window !== 'undefined' ? window : this);

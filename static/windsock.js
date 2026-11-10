/* Windsock for 3D Approach (Caroline / AJ, 2 Nov 2026): "a windsock at Auckland, so when the view switches to the
   tower it shows as the aircraft crosses the threshold". Built in code, not a downloaded model, and driven by the
   airport METAR, so it is only ever the real wind.

   The real thing (ICAO Annex 14 5.1.1, FAA AC 150/5345-27): a truncated fabric cone at least 3.6 m long with a 0.9 m
   mouth, in five bands, orange at both ends. It swivels on a mast, streams DOWNWIND (a METAR gives where the wind
   comes FROM), and fills about one band per 3 kt, straight out at 15 kt; below that it sags, and in calm air it hangs.
   Gusts swing it between the steady wind and the gust; a variable wind (VRB) wanders; the tail ripples.

   Frame (as the approach lights): x east, y up, -z north, metres, origin at the foot of the mast.
   Pure helpers + build(THREE). No DOM; loads under node for the tests. */
(function (root) {
  'use strict';
  const D2R = Math.PI / 180;
  const MAST_H = 6.0, MAST_D = 0.11, SOCK_LEN = 3.6, MOUTH_D = 0.9, TAIL_D = 0.4, ARM = 0.32;   // metres (MAST_D: mean mast diameter)
  const BANDS = 5, KT_FULL = 15;               // FAA: full at 15 kt, ~3 kt a band
  const ALONG = 60, LEFT = 80;                 // where it stands: 60 m past the threshold, 80 m left of the centreline (in the tower shot as the aircraft crosses the threshold)
  const LIMP = 84;                             // deg below level: a hanging sock
  const RPB = 6, SEG = 14;                     // rings per band, segments round each ring
  const ORANGE = [1.0, 0.13, 0.012], WHITE = [0.9, 0.9, 0.88];   // linear: shows as aviation orange / off-white

  const clamp01 = v => v < 0 ? 0 : v > 1 ? 1 : v;
  const smooth = (a, b, x) => { const t = clamp01((x - a) / (b - a)); return t * t * (3 - 2 * t); };

  /* METAR -> {from, kt, gust, vrb}; null when there is no usable wind (then no sock is drawn). from null = calm or variable. */
  function windOf(m) {
    if (!m || typeof m !== 'object' || Array.isArray(m) || m.error) return null;
    if (m.wspd == null) return null;
    const kt = +m.wspd; if (!isFinite(kt) || kt < 0) return null;
    const g = (m.wgst != null && isFinite(+m.wgst) && +m.wgst > kt) ? +m.wgst : kt;
    const vrb = String(m.wdir).toUpperCase() === 'VRB';
    let from = null;
    if (!vrb && kt > 0 && m.wdir != null && isFinite(+m.wdir)) from = ((+m.wdir % 360) + 360) % 360;
    return { from, kt, gust: g, vrb };
  }
  const downwind = from => (from == null || !isFinite(from)) ? null : ((from + 180) % 360 + 360) % 360;
  const fill = kt => (isFinite(kt) && kt > 0) ? Math.min(1, kt / KT_FULL) : 0;
  const bandsUp = kt => Math.floor(fill(kt) * BANDS + 1e-9);
  const droop = f => 80 * Math.pow(1 - clamp01(f), 1.5);           // deg below level of the part that is filled
  /* Gusts: the speed moves between the steady wind and the gust, on a slow irregular cycle (seconds). */
  function speedAt(w, t) {
    if (!w) return 0;
    if (!(w.gust > w.kt)) return w.kt;
    const n = 0.75 * Math.sin(2 * Math.PI * t / 11) + 0.25 * Math.sin(2 * Math.PI * t / 3.7 + 1);
    return w.kt + (w.gust - w.kt) * (n + 1) / 2;
  }
  /* Where it points right now (deg true), or null = keep the last (calm). A steady wind wobbles a few degrees, more in
     gusts; a variable one wanders slowly round the compass. */
  function dirAt(w, t) {
    if (!w) return null;
    if (w.vrb) return ((180 + 150 * Math.sin(2 * Math.PI * t / 900) + 120 * Math.sin(2 * Math.PI * t / 347 + 1)) % 360 + 360) % 360;
    const to = downwind(w.from); if (to == null) return null;
    const A = 2.5 + 4 * clamp01((w.gust - w.kt) / 10);
    return ((to + A * (0.7 * Math.sin(2 * Math.PI * t / 6.3) + 0.3 * Math.sin(2 * Math.PI * t / 2.1 + 0.5))) % 360 + 360) % 360;
  }
  /* Tail ripple: amplitude (m, at the tail) and frequency (Hz). None in calm air. */
  const flutter = kt => (isFinite(kt) && kt > 0) ? { amp: 0.03 + 0.09 * fill(kt), freq: 0.8 + 0.12 * Math.min(kt, 30) } : { amp: 0, freq: 0 };

  /* The spot by a runway end {lat, lon, land (deg true, the landing direction)}: left of the runway as the pilot sees it. */
  function placement(end) {
    if (!end || !isFinite(end.lat) || !isFinite(end.lon) || end.land == null || !isFinite(end.land)) return null;
    const h = end.land * D2R, sh = Math.sin(h), ch = Math.cos(h), lat = -LEFT;
    const dE = sh * ALONG + ch * lat, dN = ch * ALONG - sh * lat;
    return { lat: end.lat + dN / 110574, lon: end.lon + dE / (111320 * Math.cos(end.lat * D2R)) };
  }

  /* ---- the model ---- */
  function build(THREE, opts) {
    opts = opts || {};
    const group = new THREE.Group(); group.name = 'windsock';
    const metal = new THREE.MeshStandardMaterial({ color: 0xd9dcdf, metalness: 0.35, roughness: 0.55 });
    const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.045, 0.065, MAST_H, 10).translate(0, MAST_H / 2, 0), metal);
    const pad = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.6, 0.12, 16).translate(0, 0.06, 0),
      new THREE.MeshStandardMaterial({ color: 0x9a9a94, roughness: 0.95 }));
    group.add(pad, mast);

    const head = new THREE.Group(); head.position.y = MAST_H; group.add(head);   // swivels; +x = downwind
    const cap = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, 0.16, 10), metal); head.add(cap);
    const R = MOUTH_D / 2;
    const hoop = new THREE.Mesh(new THREE.TorusGeometry(R, 0.018, 6, 28).rotateY(Math.PI / 2).translate(ARM, 0, 0), metal);
    head.add(hoop);
    for (const s of [1, -1]) {   // the frame: two rods from the swivel to the top and bottom of the hoop
      const len = Math.hypot(ARM, R), rod = new THREE.Mesh(new THREE.CylinderGeometry(0.012, 0.012, len, 5), metal);
      rod.position.set(ARM / 2, s * R / 2, 0); rod.rotation.z = -s * Math.atan2(ARM, R); head.add(rod);
    }

    // the sock: rings along the length, duplicated at each band edge so the colours change cleanly
    const ringN = BANDS * (RPB + 1), segM = SEG;
    const pos = new Float32Array(ringN * segM * 3), nrm = new Float32Array(ringN * segM * 3);
    const col = new Float32Array(ringN * segM * 3), uv = new Float32Array(ringN * segM * 2);
    const ringS = new Float32Array(ringN), idx = [];
    for (let k = 0; k < BANDS; k++) for (let j = 0; j <= RPB; j++) {
      const r = k * (RPB + 1) + j, s = (k + j / RPB) / BANDS, c = (k % 2 === 0) ? ORANGE : WHITE;
      ringS[r] = s;
      for (let m = 0; m < segM; m++) { const v = r * segM + m;
        col[v * 3] = c[0]; col[v * 3 + 1] = c[1]; col[v * 3 + 2] = c[2];
        uv[v * 2] = m / segM; uv[v * 2 + 1] = (j === 0 ? s + 1e-4 : j === RPB ? s - 1e-4 : s); }
      if (j < RPB) for (let m = 0; m < segM; m++) {
        const a = r * segM + m, b = r * segM + (m + 1) % segM, c2 = (r + 1) * segM + m, d = (r + 1) * segM + (m + 1) % segM;
        idx.push(a, c2, b, b, c2, d);
      }
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3).setUsage(THREE.DynamicDrawUsage));
    geo.setAttribute('normal', new THREE.BufferAttribute(nrm, 3).setUsage(THREE.DynamicDrawUsage));
    geo.setAttribute('color', new THREE.BufferAttribute(col, 3));
    geo.setAttribute('uv', new THREE.BufferAttribute(uv, 2));
    geo.setIndex(idx);
    /* Floodlit at night: the light comes through the same band pattern (a 1x5 texture down the sock), so orange stays
       orange and white stays white instead of everything lifting to one pink. */
    const bandPx = new Uint8Array(BANDS * 4);
    for (let k = 0; k < BANDS; k++) { const c = (k % 2 === 0) ? [255, 96, 22] : [236, 236, 230]; bandPx.set([c[0], c[1], c[2], 255], k * 4); }
    const bandTex = new THREE.DataTexture(bandPx, 1, BANDS, THREE.RGBAFormat);
    bandTex.magFilter = bandTex.minFilter = THREE.NearestFilter; bandTex.needsUpdate = true;
    const cloth = new THREE.MeshStandardMaterial({ vertexColors: true, side: THREE.DoubleSide, roughness: 0.92, metalness: 0,
      emissive: 0x000000, emissiveMap: bandTex });
    const sock = new THREE.Mesh(geo, cloth); sock.frustumCulled = false; head.add(sock);

    // obstruction light: red, on top of the mast, lit at night (with a screen-size glow so it reads from the tower)
    const beacon = new THREE.Mesh(new THREE.SphereGeometry(0.07, 8, 6), new THREE.MeshBasicMaterial({ color: 0xff2414 }));
    beacon.position.y = MAST_H + 0.14; beacon.visible = false; group.add(beacon);
    const glow = new THREE.Points(new THREE.BufferGeometry().setAttribute('position', new THREE.BufferAttribute(new Float32Array(3), 3)),
      new THREE.PointsMaterial({ color: 0xff3020, size: 7 * (opts.pixelRatio || 1), sizeAttenuation: false, transparent: true,
        opacity: 0.85, depthWrite: false, blending: THREE.AdditiveBlending }));
    glow.frustumCulled = false; beacon.add(glow);

    // centreline scratch (no allocation per frame)
    const cx = new Float32Array(ringN), cy = new Float32Array(ringN), th = new Float32Array(ringN);
    let lastTo = 90;
    function update(st) {
      st = st || {};
      if (st.to != null && isFinite(st.to)) lastTo = st.to;
      head.rotation.y = (90 - lastTo) * D2R;
      const kt = isFinite(st.kt) ? st.kt : 0, t = isFinite(st.t) ? st.t : 0, f = fill(kt), th0 = droop(f), fl = flutter(kt);
      const L = SOCK_LEN, w = 2 * Math.PI * fl.freq * t;
      // walk down the sock: the filled part at the droop angle, then it falls towards hanging; level at the hoop
      let x = ARM, y = 0, sPrev = 0;
      for (let r = 0; r < ringN; r++) {
        const s = ringS[r];
        let a = th0 + (LIMP - th0) * smooth(f, f + 0.32, s);
        a *= smooth(0, 0.15, s);          // the hoop holds the mouth square to the wind
        th[r] = a * D2R;
        if (r > 0) { const ds = (s - sPrev) * L, am = 0.5 * (th[r] + th[r - 1]); x += Math.cos(am) * ds; y -= Math.sin(am) * ds; }
        cx[r] = x; cy[r] = y; sPrev = s;
      }
      for (let r = 0; r < ringN; r++) {
        const s = ringS[r], tx = Math.cos(th[r]), ty = -Math.sin(th[r]);   // tangent, in the x-y plane
        const nx = -ty, ny = tx;                                          // in-plane normal
        const full = Math.max(1 - smooth(f, f + 0.15, s), 1 - smooth(0, 0.12, s));   // 1 = filled, 0 = collapsed cloth
        const rad = (MOUTH_D + (TAIL_D - MOUTH_D) * s) / 2;
        const rn = rad * (0.55 + 0.45 * full), rb = rad * (0.82 + 0.18 * full);   // a limp sock hangs as a flattened tube
        const k = Math.pow(s, 1.5), dz = fl.amp * k * Math.sin(w - s * 7), dy = 0.4 * fl.amp * k * Math.sin(1.3 * w - s * 9);
        for (let m = 0; m < segM; m++) {
          const p = (m / segM) * 2 * Math.PI, c = Math.cos(p), sn = Math.sin(p), v = (r * segM + m) * 3;
          pos[v] = cx[r] + nx * c * rn; pos[v + 1] = cy[r] + ny * c * rn + dy; pos[v + 2] = sn * rb + dz;
          nrm[v] = nx * c; nrm[v + 1] = ny * c; nrm[v + 2] = sn;
        }
      }
      geo.attributes.position.needsUpdate = true; geo.attributes.normal.needsUpdate = true;
      // st.minW: the narrowest the mast may be drawn (local metres, ~1 px at this range). Up close it is true size; from the
      // tower a 0.1 m mast is sub-pixel, breaks up, and the sock seems to float - so it is widened just enough to stay a line.
      const mw = (isFinite(st.minW) && st.minW > MAST_D) ? st.minW / MAST_D : 1;
      mast.scale.x = mast.scale.z = mw; pad.scale.x = pad.scale.z = Math.max(1, mw * 0.5);
      const night = !!st.night;
      cloth.emissive.setHex(night ? 0x8c8c8c : 0x000000);   // floodlit at night (through the band texture)
      beacon.visible = night;
    }
    update({ to: 90, kt: 0, t: 0 });
    return { group, head, sock, mast, hoop, beacon, ringN, segM, update };
  }

  const api = { MAST_H, MAST_D, SOCK_LEN, MOUTH_D, TAIL_D, BANDS, KT_FULL, ALONG, LEFT,
    windOf, downwind, fill, bandsUp, droop, speedAt, dirAt, flutter, placement, build };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.WindSock = api;
})(typeof window !== 'undefined' ? window : this);

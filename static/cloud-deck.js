/* Cloud deck for 3D Approach (AJ 29 Oct 2026): "watch an approach in clear air, enter the cloud and not
   see anything until it descends through the bottom of the cloud layer". It only has to look right.

   Data: the airport METAR gives each layer's cover (FEW/SCT/BKN/OVC) and base in feet above the field.
   Tops come from the forecast model's cloud cover by height (/api/cloudtops, Open-Meteo), or a default
   thickness when the model is unavailable or disagrees. Coverage becomes holes in a soft noise pattern:
   the same pattern draws the deck AND decides whether the camera is inside cloud, so what you see and
   the white-out always agree.

   Pure logic, no DOM and no three.js: approach.html draws it. Also loads under node for the tests. */
(function (root) {
  'use strict';
  const FILL = { FEW: 0.2, SCT: 0.45, BKN: 0.75, OVC: 1 };
  const DEFAULT_THICK = { FEW: 1000, SCT: 1000, BKN: 2000, OVC: 2000 };
  const MIN_THICK = 300, MAX_THICK = 6000, MAX_LAYERS = 3, CLOUDY = 30;   // ft, ft, layers, % cover that counts as cloud
  const EXTENT_M = 48000;   // one tile of the pattern, metres; the deck is drawn this wide and repeats as it drifts
  const SOFT = 0.035;       // half-width of the soft edge, in noise units
  const KT = 1852 / 3600;   // knots to m/s

  function coverOf(c) {
    c = String(c || '').toUpperCase();
    if (c === 'VV' || c === 'OVX') return 'OVC';   // sky obscured: vertical visibility reads as overcast at that height
    return FILL[c] != null ? c : null;
  }
  /* METAR (aviationweather.gov JSON) -> [{cover, baseFt}], the lowest three, sorted. [] = clear sky, null = no report. */
  function layersFromMetar(m) {
    if (!m || typeof m !== 'object' || m.error) return null;
    const out = [];
    for (const c of (Array.isArray(m.clouds) ? m.clouds : [])) {
      const cover = coverOf(c && c.cover), base = c && +c.base;
      if (!cover || !isFinite(base) || base < 0 || c.base == null) continue;
      out.push({ cover, baseFt: Math.round(base) });
    }
    out.sort((a, b) => a.baseFt - b.baseFt);
    return out.slice(0, MAX_LAYERS);
  }
  /* The top of the layer whose base is baseFt, from the model profile [{ft (above the field), cover %}].
     Walk up from the first level at or above the base while it is cloudy; the top is half way to the next
     clear level. null when the model has no cloud there (it disagrees with the METAR): use the default. */
  function topFromProfile(baseFt, profile) {
    if (!Array.isArray(profile) || !profile.length) return null;
    const p = profile.filter(q => q && isFinite(q.ft) && isFinite(q.cover)).sort((a, b) => a.ft - b.ft);
    let i = p.findIndex(q => q.ft >= baseFt);
    if (i < 0 || p[i].cover < CLOUDY) return null;
    while (i + 1 < p.length && p[i + 1].cover >= CLOUDY) i++;
    return i + 1 < p.length ? Math.round((p[i].ft + p[i + 1].ft) / 2) : p[i].ft;
  }

  // ---- the pattern: periodic value noise, so the tile repeats with no seam ----
  function hash(ix, iy, seed) {
    let h = (ix * 374761393 + iy * 668265263 + seed * 2246822519) | 0;
    h = Math.imul(h ^ (h >>> 13), 1274126177); h ^= h >>> 16;
    return (h >>> 0) / 4294967296;
  }
  const smooth = t => t * t * (3 - 2 * t);
  function vnoise(u, v, period, seed) {   // u, v in cells; wraps every `period` cells
    const x0 = Math.floor(u), y0 = Math.floor(v), fx = smooth(u - x0), fy = smooth(v - y0);
    const w = k => ((k % period) + period) % period;
    const a = hash(w(x0), w(y0), seed), b = hash(w(x0 + 1), w(y0), seed), c = hash(w(x0), w(y0 + 1), seed), d = hash(w(x0 + 1), w(y0 + 1), seed);
    return a + (b - a) * fx + (c - a) * fy + (a - b - c + d) * fx * fy;
  }
  function field(xM, yM, seed) {   // 0..1-ish, blobs of a few km with ragged edges
    const u = xM / EXTENT_M, v = yM / EXTENT_M;
    return 0.55 * vnoise(u * 12, v * 12, 12, seed) + 0.3 * vnoise(u * 32, v * 32, 32, seed + 101) + 0.15 * vnoise(u * 80, v * 80, 80, seed + 202);
  }
  /* The threshold that fills `fill` of the sky: the (1-fill) quantile of the field over one tile. */
  function threshold(fill, seed) {
    if (fill >= 1) return -Infinity;
    const N = 96, s = [];
    for (let r = 0; r < N; r++) for (let c = 0; c < N; c++) s.push(field((c + 0.5) * EXTENT_M / N, (r + 0.5) * EXTENT_M / N, seed));
    s.sort((a, b) => a - b);
    return s[Math.min(s.length - 1, Math.max(0, Math.round((1 - fill) * s.length)))];
  }
  /* 0..1: how much cloud there is at (x east, y north) metres from the field, soft at the edges. >= 0.5 is cloud. */
  function mask(layer, xM, yM) {
    if (!layer) return 0;
    if (layer.thr === -Infinity) return 1;
    const n = field(xM, yM, layer.seed), t = (n - (layer.thr - SOFT)) / (2 * SOFT);
    return t <= 0 ? 0 : t >= 1 ? 1 : smooth(t);
  }

  /* METAR + profile -> the deck. null when there is no report; layers [] when the sky is clear. */
  function build(metar, profile) {
    const L = layersFromMetar(metar);
    if (L === null) return null;
    const layers = L.map((l, i) => {
      let top = topFromProfile(l.baseFt, profile), src = 'model';
      if (top == null) { top = l.baseFt + DEFAULT_THICK[l.cover]; src = 'default'; }
      top = Math.min(l.baseFt + MAX_THICK, Math.max(l.baseFt + MIN_THICK, top));
      const seed = 17 + i * 7919;
      return { cover: l.cover, fill: FILL[l.cover], baseFt: l.baseFt, topFt: top, topSrc: src, seed, thr: threshold(FILL[l.cover], seed) };
    });
    const dir = +metar.wdir, kt = +metar.wspd;
    return { layers, wind: { dir: isFinite(dir) ? dir : 0, kt: isFinite(kt) ? kt : 0 } };
  }
  /* Index of the layer the point is inside (height in ft above the field, x/y metres from the field), or -1. */
  function inside(layers, ftAboveField, xM, yM) {
    if (!Array.isArray(layers)) return -1;
    for (let i = 0; i < layers.length; i++) {
      const l = layers[i];
      if (ftAboveField >= l.baseFt && ftAboveField <= l.topFt && mask(l, xM, yM) >= 0.5) return i;
    }
    return -1;
  }
  /* How thick the cloud is at a point, 0 (clear) .. 1 (can't see a thing). AJ 30 Oct: breaking out of the base was
     too sudden. Real bases and tops are ragged: the fog thins over the bottom RAMP_IN_FT of a layer and a few wisps
     hang RAMP_OUT_FT below it (the same at the top), so on a 3 degree final it clears over 10-15 s, not at once.
     Across the layer the pattern's own soft edges (mask) thin it the same way. Overlapping layers add up. */
  const RAMP_IN_FT = 150, RAMP_OUT_FT = 60;
  function density(layers, ftAboveField, xM, yM) {
    if (!Array.isArray(layers) || !isFinite(ftAboveField)) return 0;
    let clear = 1;
    for (const l of layers) {
      const rin = Math.min(RAMP_IN_FT, (l.topFt - l.baseFt) / 3), w = rin + RAMP_OUT_FT;
      const lo = (ftAboveField - (l.baseFt - RAMP_OUT_FT)) / w, hi = ((l.topFt + RAMP_OUT_FT) - ftAboveField) / w;
      if (lo <= 0 || hi <= 0) continue;
      const v = smooth(Math.min(1, lo)) * smooth(Math.min(1, hi));
      clear *= 1 - v * mask(l, xM, yM);
    }
    return 1 - clear;
  }
  /* How far the deck has moved after `sec` seconds: downwind (a METAR gives the direction the wind is FROM). */
  function drift(wind, sec) {
    if (!wind || !(wind.kt > 0)) return { e: 0, n: 0 };
    const d = wind.kt * KT * sec, to = (wind.dir + 180) * Math.PI / 180;
    return { e: d * Math.sin(to), n: d * Math.cos(to) };
  }
  /* N x N alpha bytes (0..255) over one tile: row 0 is the north edge, column 0 the west edge. */
  function alphaGrid(layer, N) {
    const a = new Uint8Array(N * N);
    for (let r = 0; r < N; r++) for (let c = 0; c < N; c++) {
      const x = -EXTENT_M / 2 + (c + 0.5) * EXTENT_M / N, y = EXTENT_M / 2 - (r + 0.5) * EXTENT_M / N;
      a[r * N + c] = Math.round(mask(layer, x, y) * 255);
    }
    return a;
  }

  const api = { FILL, EXTENT_M, RAMP_IN_FT, RAMP_OUT_FT, layersFromMetar, topFromProfile, build, mask, inside, density, drift, alphaGrid };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.CloudDeck = api;
})(typeof window !== 'undefined' ? window : this);

/* PiLNK city lights (10 Oct 2026) - shared by the 3D Sky (static/sky3d.html) and the 3D Approach
   (static/approach.html). After dark the towns glow: NASA's Black Marble night photo from height, and the
   map's own streets lit sodium orange close in. Each page decides WHEN (its own sun and theme logic) and
   hands this file a level 0..1; this file decides WHAT is drawn and puts every property it touched back
   exactly when the level returns to 0. Two looks: 'sky' (top-down, v1.6.5 Night VFR) and 'approach'
   (a chase camera ~300 m behind the aircraft at high pitch, where blur makes lines vanish and fixed-pixel
   widths shrink streets to hairlines). Spec: pilnk-tasks/approach-city-lights.md. ASCII only, no backslashes. */
(function (root, factory) {
  const CL = factory();
  if (typeof module === 'object' && module.exports) module.exports = CL; else root.CityLights = CL;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';
  const NASA = 'pilnk://gibs.earthdata.nasa.gov/wmts/epsg3857/best/VIIRS_Black_Marble/default/2016-01-01/GoogleMapsCompatible_Level8/{z}/{y}/{x}.png';
  const NASA_STOPS = [6, 0.95, 9, 0.6, 10.5, 0.22, 12, 0.1, 13, 0];
  const ROAD = /^(road|bridge|tunnel)_/, DIM = /casing|rail|path_pedestrian/, MAJOR = /motorway|trunk|primary/;
  const PROFILES = {
    sky: { exp: 1.5, major: [9, 0.6, 13, 2.2, 17, 9], minor: [10, 0.25, 13, 0.9, 17, 5], blurMajor: 1.2, blurMinor: 0.6,
      opMajor: [9, 0, 10.5, 0.95, 13, 1], opMinor: [9, 0, 10.5, 0.5, 13, 0.75], bldg: '#4a3522', dimPatterns: false, glow: [] },
    approach: { exp: 2, major: [9, 0.6, 13, 2.2, 17, 12, 22, 384], minor: [10, 0.25, 13, 0.9, 17, 7, 22, 224], blurMajor: 0, blurMinor: 0,
      opMajor: [9, 0, 10.5, 0.95, 13, 1], opMinor: [9, 0, 10.5, 0.5, 13, 0.9], bldg: '#1c1814', dimPatterns: true,
      glow: ['road_minor', 'road_secondary_tertiary', 'road_trunk_primary', 'road_motorway', 'road_link', 'road_service_track'] },
  };
  const prof = p => PROFILES[p] || PROFILES.sky;
  // scale the VALUES of zoom stops: a zoom expression must stay top-level, so it can never be wrapped in ['*', ...]
  const sc = (stops, k) => stops.map((v, i) => i % 2 ? Math.round(v * k * 1000) / 1000 : v);
  const lamp = L => 0.5 + 0.5 * L;   // street lamps come on at full strength: never below half, so streets never vanish at dusk

  function level(elev, q) {
    if (q === 'on') return 1; if (q === 'off') return 0;
    if (typeof elev !== 'number' || !isFinite(elev)) return 0;
    return Math.min(1, Math.max(0, -elev / 6));
  }
  const isPattern = l => l.type === 'fill' && l.paint && l.paint['fill-pattern'] != null;
  /* The style's own values of every paint property the lights may touch, read from the style JSON before
     any repaint (a live layer's paint is MapLibre's internal store). null = the style never set it. */
  function captureOrig(style) {
    const orig = {};
    for (const l of (style && style.layers) || []) {
      const p = l.paint || {}, rec = {}, get = k => (p[k] !== undefined ? p[k] : null);
      if (l.type === 'line' && ROAD.test(l.id)) for (const k of ['line-color', 'line-width', 'line-blur', 'line-opacity']) rec[k] = get(k);
      else if (l.type === 'fill-extrusion') rec['fill-extrusion-color'] = get('fill-extrusion-color');
      else if (isPattern(l)) rec['fill-opacity'] = get('fill-opacity');
      else continue;
      orig[l.id] = { type: l.type, pattern: isPattern(l), p: rec };
    }
    return orig;
  }
  function origValue(orig, id, prop) {
    const o = orig && orig[id]; const v = o ? o.p[prop] : null; return v === null || v === undefined ? undefined : v;
  }
  /* Every (layer, property) the lights set, with its lit value at level k. Glow: road and bridge lines that are
     not casings, rails or footpaths (tunnels stay as they are). Dim: casings, rails, footpaths - their pale
     lines are what made a lit street look grey. Buildings: warm and dark. Approach: fill patterns dimmed. */
  function targets(orig, profile) {
    const P = prof(profile), out = [];
    for (const id in orig || {}) {
      const o = orig[id];
      if (o.type === 'fill-extrusion') { out.push({ id, prop: 'fill-extrusion-color', val: () => P.bldg }); continue; }
      if (o.pattern) { if (P.dimPatterns) out.push({ id, prop: 'fill-opacity', val: () => 0.06 }); continue; }
      if (DIM.test(id)) { out.push({ id, prop: 'line-opacity', val: () => 0.08 }); continue; }
      if (/^tunnel_/.test(id)) continue;
      const major = MAJOR.test(id);
      out.push({ id, prop: 'line-color', val: () => major ? '#ffbe5c' : '#ff9d3d' });
      out.push({ id, prop: 'line-width', val: () => ['interpolate', ['exponential', P.exp], ['zoom']].concat(major ? P.major : P.minor) });
      out.push({ id, prop: 'line-blur', val: () => major ? P.blurMajor : P.blurMinor });
      out.push({ id, prop: 'line-opacity', val: k => ['interpolate', ['linear'], ['zoom']].concat(sc(major ? P.opMajor : P.opMinor, lamp(k))) });
    }
    return out;
  }
  /* Approach only: a wide, faint copy under each main street layer. A soft glow done with line-blur vanishes
     at chase-camera pitch, so the glow is its own layer instead. */
  function glowSpecs(style, profile) {
    const P = prof(profile), out = [];
    for (const id of P.glow) {
      const l = ((style && style.layers) || []).find(x => x.id === id && x.type === 'line'); if (!l) continue;
      const major = MAJOR.test(id), base = major ? 0.22 : 0.16;
      const layer = { id: 'cl-glow-' + id, type: 'line', source: l.source, 'source-layer': l['source-layer'], minzoom: 12,
        layout: { 'line-cap': 'round', 'line-join': 'round', visibility: 'none' },
        paint: { 'line-color': major ? '#ffb347' : '#ff9a3c', 'line-opacity': base,
          'line-width': ['interpolate', ['exponential', 2], ['zoom']].concat(major ? [12, 6, 17, 48, 22, 1536] : [12, 3, 17, 28, 22, 896]) } };
      if (l.filter !== undefined) layer.filter = l.filter;
      out.push({ layer, before: id, opacity: k => Math.round(base * lamp(k) * 1000) / 1000 });
    }
    return out;
  }
  /* The NASA photo is a glow on the ground: above land and water, under every street, label, 3D building and
     overlay (PiLNK overlays are named av-* or rings). */
  function before(layers) {
    const t = (layers || []).find(l => l.type === 'symbol' || l.type === 'fill-extrusion' || /^av-/.test(l.id) || ROAD.test(l.id) || l.id === 'rings');
    return t ? t.id : undefined;
  }
  /* A page's lights. opts: map, profile, orig (captureOrig of the style JSON), base(id, prop) = what a property
     goes back to when the lights are off (colours follow the page's theme; default origValue), style (the
     style JSON, for the approach glow). set(level, force): same level twice is a no-op unless forced. */
  function create(opts) {
    const map = opts.map, profile = opts.profile || 'sky';
    const base = opts.base || ((id, prop) => origValue(opts.orig, id, prop));
    const glows = glowSpecs(opts.style, profile);
    const C = { level: 0, orig: opts.orig, profile };
    C.targets = () => targets(C.orig, profile);
    C.set = function (lv, force) {
      const L = Math.round(Math.min(1, Math.max(0, +lv || 0)) * 100) / 100;
      if (!map) return;
      if (!force && L === C.level) return;
      C.level = L;
      const sp = (id, prop, v) => { if (map.getLayer(id)) try { map.setPaintProperty(id, prop, v); } catch (e) {} };
      try {
        if (L > 0) {
          if (!map.getSource('citylights')) map.addSource('citylights', { type: 'raster', tiles: [NASA], tileSize: 256, maxzoom: 8, attribution: 'Night lights: NASA Black Marble' });
          if (!map.getLayer('citylights')) map.addLayer({ id: 'citylights', type: 'raster', source: 'citylights',
            paint: { 'raster-saturation': 0.3, 'raster-contrast': 0.25, 'raster-resampling': 'linear' } }, before(map.getStyle().layers));
          map.setPaintProperty('citylights', 'raster-opacity', ['interpolate', ['linear'], ['zoom']].concat(sc(NASA_STOPS, L)));
          map.setLayoutProperty('citylights', 'visibility', 'visible');
          for (const g of glows) {
            if (!map.getLayer(g.layer.id) && map.getLayer(g.before)) try { map.addLayer(JSON.parse(JSON.stringify(g.layer)), g.before); } catch (e) {}
            if (map.getLayer(g.layer.id)) { map.setPaintProperty(g.layer.id, 'line-opacity', g.opacity(L)); map.setLayoutProperty(g.layer.id, 'visibility', 'visible'); }
          }
          for (const t of C.targets()) sp(t.id, t.prop, t.val(L));
        } else {
          if (map.getLayer('citylights')) map.setLayoutProperty('citylights', 'visibility', 'none');
          for (const g of glows) if (map.getLayer(g.layer.id)) map.setLayoutProperty(g.layer.id, 'visibility', 'none');
          for (const t of C.targets()) sp(t.id, t.prop, base(t.id, t.prop));
        }
      } catch (e) { try { console.warn('city lights:', e && e.message); } catch (_) {} }
      map.triggerRepaint();
    };
    return C;
  }
  return { level, sc, captureOrig, origValue, targets, glowSpecs, before, create, NASA, NASA_STOPS, PROFILES };
});

/* Signal Border - range bins to map points and words.
 *
 * A node keeps how far it hears in every direction as a list of ranges in nautical miles, one per
 * equal slice of the compass starting at true north (360 one-degree bins; older nodes: 36 ten-degree
 * sectors). 0 means nothing heard that way. This file turns such a list into things to draw and say.
 * No map library, no network, no page state: the same file, unchanged, serves the node dashboard and
 * the pilnk.io network map.
 *
 *   SignalBorder.has(d)                 does this /api/coverage answer carry the 1-degree outlines?
 *   SignalBorder.outline(lat, lon, bins)  [[lat, lon], ...] one point per heard bin, in bearing order
 *   SignalBorder.rays(lat, lon, bins)     [{lat, lon, t}, ...] spoke ends; t = range / longest
 *   SignalBorder.furthest(bins)           {nm, deg} or null
 *   SignalBorder.readout(d, view)         {main, sub} - the words under the buttons
 *   SignalBorder.colorFor(id, dark)       a steady colour per node for the "All nodes" view
 */
(function (root) {
  'use strict';

  var EARTH_M = 6371000, NM_M = 1852;

  function heard(v) {              // a bin's range in nm, or 0 for nothing / junk
    var n = (typeof v === 'number') ? v : parseFloat(v);
    return (isFinite(n) && n > 0) ? n : 0;
  }

  // Great-circle point `nm` out from (lat, lon) on true bearing `deg`. The longitude is kept within
  // 180 degrees of the receiver's own, never wrapped into -180..180, so an outline that crosses the
  // date line stays one shape instead of jumping across the map.
  function dest(lat, lon, deg, nm) {
    var b = deg * Math.PI / 180, d = nm * NM_M / EARTH_M;
    var la1 = lat * Math.PI / 180, lo1 = lon * Math.PI / 180;
    var la2 = Math.asin(Math.sin(la1) * Math.cos(d) + Math.cos(la1) * Math.sin(d) * Math.cos(b));
    var lo2 = lo1 + Math.atan2(Math.sin(b) * Math.sin(d) * Math.cos(la1), Math.cos(d) - Math.sin(la1) * Math.sin(la2));
    var outLon = lo2 * 180 / Math.PI;
    while (outLon - lon > 180) outLon -= 360;
    while (outLon - lon < -180) outLon += 360;
    return [la2 * 180 / Math.PI, outLon];
  }

  function has(d) {
    return !!(d && Array.isArray(d.border_24h) && d.border_24h.length === 360);
  }

  function outline(lat, lon, bins) {
    if (!Array.isArray(bins) || !bins.length) return [];
    var step = 360 / bins.length, pts = [];
    for (var i = 0; i < bins.length; i++) {
      var r = heard(bins[i]);
      if (r > 0) pts.push(dest(lat, lon, i * step + step / 2, r));   // empty bins are skipped: no spike back to the receiver
    }
    return pts.length >= 3 ? pts : [];                                // fewer than 3 points is not a shape
  }

  function rays(lat, lon, bins) {
    if (!Array.isArray(bins) || !bins.length) return [];
    var step = 360 / bins.length, longest = 0, out = [], i;
    for (i = 0; i < bins.length; i++) longest = Math.max(longest, heard(bins[i]));
    if (longest <= 0) return [];
    for (i = 0; i < bins.length; i++) {
      var r = heard(bins[i]);
      if (r <= 0) continue;
      var p = dest(lat, lon, i * step + step / 2, r);
      out.push({ lat: p[0], lon: p[1], t: r / longest });
    }
    return out;
  }

  function furthest(bins) {
    if (!Array.isArray(bins) || !bins.length) return null;
    var step = 360 / bins.length, best = 0, at = -1;
    for (var i = 0; i < bins.length; i++) {
      var r = heard(bins[i]);
      if (r > best) { best = r; at = i; }
    }
    return at < 0 ? null : { nm: best, deg: Math.floor(at * step + step / 2) };
  }

  function pad3(n) { n = String(n); return n.length >= 3 ? n : ('00' + n).slice(-3); }

  function readout(d, view) {
    if (!d) return { main: '', sub: '' };
    if (view === 'horizon') return { main: 'Horizon \u00b7 all time, 10\u00b0 slices', sub: '' };
    if (!has(d)) {
      // New page, PiLNK not restarted yet (or a node on older software): only the old outline exists.
      return { main: 'All time, 10\u00b0 slices (restart PiLNK for the 24 h outline)', sub: '' };
    }
    var all = (view === 'all');
    var f = furthest(all ? d.border_all : d.border_24h);
    var hours = (typeof d.border_hours === 'number') ? d.border_hours : null;
    return {
      main: f ? (all ? 'All time' : 'Last 24 h') + ' \u00b7 furthest ' + Math.round(f.nm) + ' nm at ' + pad3(f.deg) + '\u00b0'
              : 'No positions heard yet',
      sub: (!all && hours !== null && hours < 24) ? 'Still filling: ' + hours + ' of 24 hours' : ''
    };
  }

  // One steady colour per node. The golden angle spreads consecutive ids far apart round the wheel;
  // lightness follows the map theme (bright on dark tiles, deep on light) so the line always reads.
  function colorFor(id, dark) {
    var n = Number(id);
    if (!isFinite(n)) {
      n = 0;
      var s = String(id);
      for (var i = 0; i < s.length; i++) n = (n * 31 + s.charCodeAt(i)) % 100003;
    }
    var hue = Math.round((((n * 137.508) % 360) + 360) % 360 * 10) / 10;
    var band = Math.abs(Math.floor(n)) % 3;        // ids five apart land on nearby hues: three lightness bands keep them apart
    return 'hsl(' + hue + ', ' + (dark ? '90%, ' + (58 + band * 8) : '85%, ' + (28 + band * 7)) + '%)';
  }

  var api = { has: has, outline: outline, rays: rays, furthest: furthest, readout: readout, colorFor: colorFor };
  if (typeof module !== 'undefined' && module && module.exports) module.exports = api;
  if (root) root.SignalBorder = api;
})(typeof window !== 'undefined' ? window : (typeof globalThis !== 'undefined' ? globalThis : null));

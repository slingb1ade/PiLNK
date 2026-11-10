/* Traffic tab filters (30 Oct 2026, asked for by Caroline): which group each aircraft in the list belongs to.
 *   MIL   the aircraft is military (the node's own flag)
 *   HELI  helicopters
 *   GA    light aircraft, gliders, and anything flying under its registration rather than an airline callsign
 *   airline flights: jet or prop by type (the regional turboprop class - ATR, Q300, Beech 1900 - is "prop"),
 *   domestic or international by, in order:
 *     1. the route, when the flight's route is known: both ends in the home country = domestic
 *     2. the airline's country: a home airline is domestic, except its widebodies, which fly the long-haul routes
 *     3. the country of registration (the transponder address)
 *   Anything that matches none of these is counted as international: unknown is not home.
 * The home country is the country of the node's nearest airport. Pure functions: no DOM, no fetches. */
(function (root) {
  'use strict';
  // ICAO airport prefix -> ISO country. Two letters first (NF = Fiji, not N...), then one letter.
  var ICAO2 = {
    NZ: 'NZ', NF: 'FJ', NS: 'WS', NT: 'PF', NW: 'NC', NV: 'VU', NC: 'CK', NG: 'KI', NI: 'NU', AY: 'PG', AG: 'SB',
    PH: 'US', PA: 'US', PG: 'US', EG: 'GB', EI: 'IE', EH: 'NL', ED: 'DE', ET: 'DE', LF: 'FR', LE: 'ES', LI: 'IT',
    LS: 'CH', LO: 'AT', EB: 'BE', EK: 'DK', EN: 'NO', ES: 'SE', EF: 'FI', EP: 'PL', LP: 'PT', LG: 'GR', LT: 'TR',
    WS: 'SG', WM: 'MY', WB: 'MY', WI: 'ID', WA: 'ID', VH: 'HK', VT: 'TH', VV: 'VN', VI: 'IN', VA: 'IN', VO: 'IN',
    VE: 'IN', RJ: 'JP', RO: 'JP', RK: 'KR', RC: 'TW', RP: 'PH', OM: 'AE', OT: 'QA', OE: 'SA', FA: 'ZA', HK: 'KE',
    SB: 'BR', SA: 'AR', SC: 'CL', MM: 'MX'
  };
  var ICAO1 = { Y: 'AU', K: 'US', C: 'CA', Z: 'CN', U: 'RU' };
  function icaoCountry(icao) {
    if (!icao || typeof icao !== 'string' || icao.length < 2) return null;
    var u = icao.toUpperCase();
    return ICAO2[u.slice(0, 2)] || ICAO1[u[0]] || null;
  }
  // ICAO 24-bit address blocks -> country of registration (the common ones in this part of the world and beyond)
  var HEX = [
    [0xC80000, 0xC87FFF, 'NZ'], [0xC88000, 0xC88FFF, 'FJ'], [0x7C0000, 0x7FFFFF, 'AU'], [0xA00000, 0xAFFFFF, 'US'],
    [0xC00000, 0xC3FFFF, 'CA'], [0x400000, 0x43FFFF, 'GB'], [0x4CA000, 0x4CAFFF, 'IE'], [0x3C0000, 0x3FFFFF, 'DE'],
    [0x380000, 0x3BFFFF, 'FR'], [0x480000, 0x487FFF, 'NL'], [0x840000, 0x87FFFF, 'JP'], [0x768000, 0x76FFFF, 'SG'],
    [0x780000, 0x7BFFFF, 'CN'], [0x896000, 0x896FFF, 'AE'], [0x06A000, 0x06AFFF, 'QA'], [0x008000, 0x00FFFF, 'ZA'],
    [0xE40000, 0xE7FFFF, 'BR'], [0x04C000, 0x04CFFF, 'KE'], [0x750000, 0x757FFF, 'MY'], [0x880000, 0x887FFF, 'TH']
  ];
  function hexCountry(hex) {
    if (!hex || typeof hex !== 'string' || !/^[0-9a-f]{6}$/i.test(hex)) return null;
    var n = parseInt(hex, 16);
    for (var i = 0; i < HEX.length; i++) if (n >= HEX[i][0] && n <= HEX[i][1]) return HEX[i][2];
    return null;
  }
  // a flag emoji (two regional-indicator letters) -> ISO country
  function flagIso(flag) {
    if (!flag || typeof flag !== 'string') return null;
    var cps = Array.from(flag).map(function (c) { return c.codePointAt(0); });
    if (cps.length !== 2 || cps.some(function (c) { return c < 0x1F1E6 || c > 0x1F1FF; })) return null;
    return String.fromCharCode(cps[0] - 0x1F1E6 + 65, cps[1] - 0x1F1E6 + 65);
  }
  // An airline (ICAO) callsign: three letters then a digit. Registrations (ZKMCJ, N123AB) are not.
  function isAirlineCallsign(cs) { return !!cs && /^[A-Z]{3}[0-9]/.test(String(cs).trim().toUpperCase()); }

  var GA_CATS = { lightsingle: 1, lighttwin: 1, glider: 1 };
  var LONG_HAUL = { widebody: 1, superheavy: 1 };
  /* a: the aircraft ({flight, hex, is_military}); ctx: {home, cat (the dashboard's getCat), airlineIso, route:{o,d}}
     -> {group: mil|heli|ga|domjet|domprop|intljet|intlprop, src: route|airline|registration|none|type} */
  function classify(a, ctx) {
    ctx = ctx || {};
    var cat = ctx.cat, home = ctx.home || null;
    if (a && a.is_military) return { group: 'mil', src: 'type' };
    if (cat === 'heli') return { group: 'heli', src: 'type' };
    if (GA_CATS[cat] || !isAirlineCallsign(a && a.flight)) return { group: 'ga', src: 'type' };
    var kind = cat === 'heavytwin' ? 'prop' : 'jet', dom, srcName;
    var r = ctx.route;
    if (r && r.o && r.d) { dom = !!home && r.o === home && r.d === home; srcName = 'route'; }
    else if (ctx.airlineIso) { dom = !!home && ctx.airlineIso === home && !LONG_HAUL[cat]; srcName = 'airline'; }
    else {
      var hc = hexCountry(a && a.hex);
      if (hc) { dom = !!home && hc === home && !LONG_HAUL[cat]; srcName = 'registration'; }
      else { dom = false; srcName = 'none'; }
    }
    return { group: (dom ? 'dom' : 'intl') + kind, src: srcName };
  }
  var FILTERS = [
    { id: 'all', label: 'ALL' }, { id: 'domjet', label: 'DOM JETS' }, { id: 'domprop', label: 'DOM PROPS' },
    { id: 'intl', label: 'INTL' }, { id: 'ga', label: 'GA' }, { id: 'heli', label: 'HELI' }, { id: 'mil', label: 'MIL' }
  ];
  var KNOWN = {}; FILTERS.forEach(function (f) { KNOWN[f.id] = 1; });
  function matches(filter, group) {
    if (!KNOWN[filter] || filter === 'all') return true;
    if (filter === 'intl') return group === 'intljet' || group === 'intlprop';
    return filter === group;
  }
  function counts(groups) {
    var c = {}; FILTERS.forEach(function (f) { c[f.id] = 0; });
    (groups || []).forEach(function (g) { FILTERS.forEach(function (f) { if (matches(f.id, g)) c[f.id]++; }); });
    return c;
  }
  var api = { icaoCountry: icaoCountry, hexCountry: hexCountry, flagIso: flagIso, isAirlineCallsign: isAirlineCallsign,
              classify: classify, matches: matches, counts: counts, FILTERS: FILTERS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.TrafficFilter = api;
})(this);

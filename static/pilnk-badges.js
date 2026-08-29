/* ─────────────────────────────────────────────────────────────
 * pilnk-badges.js — the radar-scope badge renderer (Phase 2 rev B)
 *
 * ONE renderer, TWO surfaces: pilnk.io profile page and the node
 * dashboard Badges tab both load THIS file. Single source in the
 * repo; myHost copy synced at push time (see SOP).
 *
 * Layout (rev B, 28 Aug — per AJ's Ingress profile reference):
 *  - The wall is BADGES ALONE: a dense grid of scopes, no card
 *    boxes, no text. The bezel arc still shows live progress to
 *    the next tier, so the wall reads at a glance without words.
 *  - CLICK a badge → detail overlay: big scope, current stat in
 *    lights, name + description, and the full tier ladder — five
 *    mini-scopes with threshold and earn date, greyed if unearned.
 *
 * Design (locked with AJ):
 *  - Badge = radar scope. Tier bezel + compass notches, dark face,
 *    range rings, family glyph as the CONTACT. Locked = empty
 *    scope, "no contact yet".
 *  - Tiers = airframe materials: Fabric, Aluminium, Titanium,
 *    Carbon, Stealth (near-black + phosphor rim).
 *  - Military honours: amber phosphor + olive bezel + ribbon bar.
 *  - Serials always shown in detail: "#0007 / 0025".
 *  - NO tier count hardcoded — tier 6 someday is just a new row.
 * All SVG inline, zero dependencies, no emoji.
 * ───────────────────────────────────────────────────────────── */
(function (global) {
  'use strict';

  /* Rev C (28 Aug, AJ): green was drowning the metals. The glyph and range
   * rings now wear the TIER colour; phosphor green is reserved for the
   * progress arc alone — green means "living signal", metal means rank.
   * Stealth keeps the phosphor glyph: on near-black, the joke needs light. */
  var TIERS = {
    1: { name: 'Fabric',    bezel: '#c9a87c', face: '#141008', ring: '#574a38', glyph: '#c9a87c' },
    2: { name: 'Aluminium', bezel: '#c8ccd2', face: '#0f1215', ring: '#4c5158', glyph: '#c8ccd2' },
    3: { name: 'Titanium',  bezel: '#7e9ab8', face: '#0c1118', ring: '#39485c', glyph: '#7e9ab8' },
    4: { name: 'Carbon',    bezel: '#4a5058', face: '#0d0f12', ring: '#2c3138', glyph: '#8b939e' },
    5: { name: 'Stealth',   bezel: '#191c20', face: '#0b1410', rim: '#39e58c', ring: '#1e3a2c', glyph: '#39e58c' }
  };
  var PHOSPHOR = '#39e58c', PHOSPHOR_DIM = '#1e3a2c';
  var AMBER = '#f0a832', AMBER_DIM = '#4a3512', OLIVE = '#6b7245';
  var LOCKED_BEZEL = '#3a3f46', LOCKED_GLYPH = '#3a4149', LOCKED_FACE = '#12151a', LOCKED_RING = '#232830';

  var GLYPHS = {
    tracker: 'M0,-20 L4,-8 L18,2 L18,7 L4,3 L4,12 L9,17 L9,20 L0,17 L-9,20 L-9,17 L-4,12 L-4,3 L-18,7 L-18,2 L-4,-8 Z',
    collector: 'M-11,-13 L-8,-6 L-1,-1 L-1,2 L-8,0 L-8,5 L-5,8 L-5,10 L-11,8 L-17,10 L-17,8 L-14,5 L-14,0 L-21,2 L-21,-1 L-14,-6 Z M11,-3 L14,4 L21,9 L21,12 L14,10 L14,15 L17,18 L17,20 L11,18 L5,20 L5,18 L8,15 L8,10 L1,12 L1,9 L8,4 Z M0,-21 L2,-16 L7,-12 L7,-10 L2,-11 L2,-8 L4,-6 L4,-4 L0,-6 L-4,-4 L-4,-6 L-2,-8 L-2,-11 L-7,-10 L-7,-12 L-2,-16 Z',
    fastmover: 'M0,-21 L3,-12 L4,-2 L14,8 L14,12 L4,7 L3,13 L7,17 L7,20 L0,17 L-7,20 L-7,17 L-3,13 L-4,7 L-14,12 L-14,8 L-4,-2 L-3,-12 Z',
    longeyes: 'M6,-14 L9,-6 L18,0 L18,4 L9,1 L9,8 L13,12 L13,15 L6,12 L-1,15 L-1,12 L3,8 L3,1 L-6,4 L-6,0 L3,-6 Z M-12,-8 A 16 16 0 0 0 -12,8 L-15,8 A 19 19 0 0 1 -15,-8 Z M-18,-12 A 22 22 0 0 0 -18,12 L-21,12 A 25 25 0 0 1 -21,-12 Z',
    highflyer: 'M-2,-4 L1,4 L10,12 L10,16 L1,10 L0,17 L4,21 L-3,19 L-9,21 L-9,18 L-5,15 L-6,9 L-15,13 L-15,9 L-6,0 Z M4,-20 L20,-20 L20,-17 L4,-17 Z M8,-13 L20,-13 L20,-10 L8,-10 Z M12,-6 L20,-6 L20,-3 L12,-3 Z',
    tower: 'M-3,14 L-1,-10 L1,-10 L3,14 L7,18 L-7,18 Z M0,-14 L2,-10 L-2,-10 Z M-8,-16 A 11 11 0 0 1 8,-16 L6,-13 A 8 8 0 0 0 -6,-13 Z M-13,-19 A 18 18 0 0 1 13,-19 L11,-16 A 15 15 0 0 0 -11,-16 Z',
    interceptor: 'M0,-20 L2,-10 L3,0 L12,6 L12,10 L3,7 L3,12 L8,15 L8,18 L2,16 L2,19 L-2,19 L-2,16 L-8,18 L-8,15 L-3,12 L-3,7 L-12,10 L-12,6 L-3,0 L-2,-10 Z',
    ghosthunter: 'M-10,-4 A 10 10 0 0 1 10,-4 L10,14 L6,10 L3,14 L0,10 L-3,14 L-6,10 L-10,14 Z M-5,-4 A 2 2 0 1 0 -5,-4.01 M5,-4 A 2 2 0 1 0 5,-4.01',
    photographer: 'M0,-16 L5,-6 L16,-8 L9,0 L16,8 L5,6 L0,16 L-5,6 L-16,8 L-9,0 L-16,-8 L-5,-6 Z M0,-5 A 5 5 0 1 0 0,5 A 5 5 0 1 0 0,-5',
    crowdfav: 'M0,-18 L5,-6 L18,-6 L8,3 L12,16 L0,8 L-12,16 L-8,3 L-18,-6 L-5,-6 Z',
    watchstander: 'M-18,0 Q0,-16 18,0 Q0,16 -18,0 Z M0,-7 A 7 7 0 1 0 0,7 A 7 7 0 1 0 0,-7 M0,-2.5 A 2.5 2.5 0 1 1 0,2.5 A 2.5 2.5 0 1 1 0,-2.5',
    firstresponder: 'M-4,-18 L4,-18 L4,-4 L18,-4 L18,4 L4,4 L4,18 L-4,18 L-4,4 L-18,4 L-18,-4 L-4,-4 Z'
  };

  /* Commendations stop being a wall of identical stars: known slugs get a
   * matching family glyph, and each badge's own DB colour drives its bezel
   * and glyph — colour without a single emoji (Rev C, AJ's note). */
  var SPECIAL_GLYPHS = {
    af1_spotter: 'tracker', vvip_spotted: 'tracker',
    mach_buster: 'fastmover', mach_2_club: 'fastmover',
    founder: 'tower', beta_tester: 'ghosthunter',
    squawk_7700: 'firstresponder', squawk_7600: 'firstresponder',
    ghost_1: 'ghosthunter', ghost_10: 'ghosthunter',
    military_5: 'interceptor',
    mil_first_strike: 'interceptor', mil_squadron: 'interceptor',
    mil_wing: 'interceptor', mil_air_marshal: 'interceptor',
    mil_pioneer: 'interceptor'
  };

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(n) { n = parseInt(n, 10) || 0; return n.toLocaleString('en-NZ'); }
  function dateStr(iso) {
    if (!iso) return '';
    var d = new Date(String(iso).replace(' ', 'T'));
    if (isNaN(d)) return '';
    return d.toLocaleDateString('en-NZ', { year: 'numeric', month: '2-digit', day: '2-digit' }).replace(/\//g, '.');
  }

  function arcPath(r, frac) {
    if (frac <= 0) return '';
    if (frac >= 1) frac = 0.9999;
    var a = (frac * 360 - 90) * Math.PI / 180;
    var x = r * Math.cos(a), y = r * Math.sin(a);
    return 'M0,' + (-r) + ' A' + r + ',' + r + ' 0 ' + (frac > 0.5 ? 1 : 0) + ',1 ' + x.toFixed(2) + ',' + y.toFixed(2);
  }

  function sanitizeColor(c, fallback) {
    return (typeof c === 'string' && /^#[0-9a-fA-F]{3,8}$/.test(c)) ? c : fallback;
  }

  function scopeSVG(opts) {
    var size = opts.size || 96;
    var tier = opts.tier || 0;
    var t = TIERS[tier];
    var mil = !!opts.military;
    var locked = tier === 0;
    var bezel = locked ? LOCKED_BEZEL : (mil ? OLIVE : (opts.bezelColor || t.bezel));
    var face = locked ? LOCKED_FACE : (mil ? '#1d1608' : t.face);
    var ringC = locked ? LOCKED_RING : (mil ? AMBER_DIM : (t.ring || PHOSPHOR_DIM));
    var glyphC = locked ? LOCKED_GLYPH : (mil ? AMBER : (opts.glyphColor || t.glyph || PHOSPHOR));
    var accent = mil ? AMBER : PHOSPHOR;
    var s = [];
    s.push('<svg width="' + size + '" height="' + size + '" viewBox="-62 -62 124 124" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">');
    if (!locked && t && t.rim) s.push('<circle r="57" fill="none" stroke="' + t.rim + '" stroke-width="1.5"/>');
    s.push('<circle r="52" fill="none" stroke="' + bezel + '" stroke-width="7"' + (locked ? ' stroke-dasharray="4 5"' : '') + '/>');
    if (opts.progressFrac > 0 && opts.progressFrac < 1) {
      s.push('<path d="' + arcPath(52, opts.progressFrac) + '" fill="none" stroke="' + accent + '" stroke-width="7" stroke-linecap="round"/>');
    }
    if (!locked) {
      s.push('<g fill="' + bezel + '">' +
        '<rect x="-2" y="-62" width="4" height="9"/><rect x="-2" y="53" width="4" height="9"/>' +
        '<rect x="-62" y="-2" width="9" height="4"/><rect x="53" y="-2" width="9" height="4"/></g>');
    }
    s.push('<circle r="45" fill="' + face + '"/>');
    s.push('<circle r="32" fill="none" stroke="' + ringC + '" stroke-width="1.5"/>');
    s.push('<circle r="18" fill="none" stroke="' + ringC + '" stroke-width="1.5"/>');
    if (opts.sweep && !locked) {
      s.push('<g><path d="M0,0 L38,-16 A41,41 0 0,1 41,0 Z" fill="' + (mil ? '#3a2c10' : '#1d4232') + '">' +
        '<animateTransform attributeName="transform" type="rotate" from="0" to="360" dur="6s" repeatCount="indefinite"/></path></g>');
    } else if (!locked) {
      s.push('<path d="M0,0 L38,-16 A41,41 0 0,1 41,0 Z" fill="' + (mil ? '#3a2c10' : '#1d4232') + '"/>');
    }
    s.push('<path d="' + (GLYPHS[opts.glyph] || GLYPHS.tracker) + '" fill="' + glyphC + '"/>');
    s.push('</svg>');
    return s.join('');
  }

  function familiesFrom(defs) {
    var fams = {};
    defs.forEach(function (d) {
      if (!d.category || d.category.indexOf('fam_') !== 0) return;
      (fams[d.category] = fams[d.category] || []).push(d);
    });
    Object.keys(fams).forEach(function (k) { fams[k].sort(function (a, b) { return a.tier - b.tier; }); });
    return fams;
  }

  var FAMILY_LABELS = {
    fam_tracker: 'Tracker', fam_collector: 'Collector', fam_fastmover: 'Fast mover',
    fam_longeyes: 'Long eyes', fam_highflyer: 'High flyer', fam_tower: 'Tower',
    fam_interceptor: 'Interceptor', fam_ghosthunter: 'Ghost hunter',
    fam_photographer: 'Photographer', fam_crowdfav: 'Crowd favourite',
    fam_watchstander: 'Watchstander', fam_firstresponder: 'First responder'
  };

  /* One-line purpose statements, Ingress-style — shown in the detail view so
   * every badge explains itself. Tier rows describe thresholds; THIS says
   * what the family is FOR. */
  var FAMILY_DESC = {
    fam_tracker: 'Track aircraft with your node. Every unique airframe counts once, for life.',
    fam_collector: 'Collect unique aircraft types — from C172s to Antonovs.',
    fam_fastmover: 'Catch the fastest thing in your sky. Lifetime ground-speed record, in knots.',
    fam_longeyes: 'Your longest-range catch, in nautical miles. Physics caps this near 250.',
    fam_highflyer: 'Your highest-altitude catch. Airliners stop around 45,000 ft. Other things don’t.',
    fam_tower: 'Keep your node on watch, day after unbroken day. The streak resets — the record doesn’t.',
    fam_interceptor: 'Spot military aircraft — the callsigns airlines don’t use.',
    fam_ghosthunter: 'Identify aircraft flying dark: no callsign, no type, no story until you give it one.',
    fam_photographer: 'Upload aircraft photos to the community gallery.',
    fam_crowdfav: 'Photos of yours the community voted to the top.',
    fam_watchstander: 'Watchlist alerts fired for aircraft you were waiting for.',
    fam_firstresponder: 'Emergency squawks witnessed by your node — 7700, 7600, 7500.'
  };

  function famState(tiers, earned, stats) {
    var statKey = tiers[0].stat_key;
    var current = parseInt((stats && stats[statKey]) || 0, 10);
    var top = null, next = null;
    tiers.forEach(function (t) {
      if (earned[t.slug]) { if (!top || t.tier > top.tier) top = t; }
      else if (!next) next = t;
    });
    var frac = 0;
    if (next) {
      var base = top ? parseInt(top.threshold, 10) : 0;
      var span = parseInt(next.threshold, 10) - base;
      frac = span > 0 ? Math.max(0, Math.min(1, (current - base) / span)) : 0;
    }
    return { current: current, top: top, next: next, frac: frac, tier: top ? top.tier : 0 };
  }

  /* ── Detail overlay ── */
  var overlayEl = null;
  function closeDetail() {
    if (overlayEl && overlayEl.parentNode) overlayEl.parentNode.removeChild(overlayEl);
    overlayEl = null;
    document.removeEventListener('keydown', escClose);
  }
  function escClose(e) { if (e.key === 'Escape') closeDetail(); }

  function openDetail(html) {
    closeDetail();
    overlayEl = document.createElement('div');
    overlayEl.className = 'pb-overlay';
    overlayEl.innerHTML = '<div class="pb-sheet" role="dialog" aria-modal="true">' + html +
      '<button type="button" class="pb-done">Done</button></div>';
    overlayEl.addEventListener('click', function (e) { if (e.target === overlayEl) closeDetail(); });
    overlayEl.querySelector('.pb-done').addEventListener('click', closeDetail);
    document.body.appendChild(overlayEl);
    document.addEventListener('keydown', escClose);
  }

  function familyDetail(famKey, tiers, earned, stats) {
    var st = famState(tiers, earned, stats);
    var label = FAMILY_LABELS[famKey] || famKey.replace('fam_', '');
    var h = [];
    h.push('<div class="pb-d-hero">' + scopeSVG({ glyph: tiers[0].icon, tier: st.tier, progressFrac: st.next ? st.frac : 0, sweep: st.tier > 0, size: 132 }) + '</div>');
    h.push('<div class="pb-d-stat">' + fmt(st.current) + '</div>');
    h.push('<div class="pb-d-name">' + esc(label) + (st.top ? ' — ' + esc(st.top.name) : '') + '</div>');
    h.push('<div class="pb-d-desc">' + esc(FAMILY_DESC[famKey] || tiers[0].description || '') + '</div>');
    h.push('<div class="pb-d-tiers">');
    tiers.forEach(function (t) {
      var e = earned[t.slug];
      h.push('<div class="pb-d-tier' + (e ? '' : ' pb-d-unearned') + '">' +
        scopeSVG({ glyph: t.icon, tier: e ? t.tier : 0, progressFrac: 0, size: 56 }) +
        '<div class="pb-d-thr">' + fmt(t.threshold) + '</div>' +
        '<div class="pb-d-sub">' + esc(t.name) + '</div>' +
        (e && e.awarded_at ? '<div class="pb-d-date">' + dateStr(e.awarded_at) + '</div>' : '') +
        '</div>');
    });
    h.push('</div>');
    if (st.next) h.push('<div class="pb-d-next">' + fmt(st.current) + ' / ' + fmt(st.next.threshold) + ' to ' + esc(TIERS[st.next.tier].name) + ' — ' + esc(st.next.name) + '</div>');
    else h.push('<div class="pb-d-next">Maximum tier held</div>');
    return h.join('');
  }

  function specialGlyph(def) {
    if (def.icon in GLYPHS) return def.icon;
    return SPECIAL_GLYPHS[def.slug] || 'crowdfav';
  }

  function specialDetail(def, earnedRow, remaining, cap) {
    var mil = def.category === 'military';
    var isEarned = !!earnedRow;
    var col = sanitizeColor(def.color, null);
    var h = [];
    h.push('<div class="pb-d-hero">' + scopeSVG({ glyph: specialGlyph(def), tier: isEarned ? (mil ? 3 : 2) : 0, military: mil, bezelColor: mil ? null : col, glyphColor: mil ? null : col, sweep: isEarned, size: 132 }) + '</div>');
    h.push('<div class="pb-d-name">' + esc(def.name) + '</div>');
    h.push('<div class="pb-d-desc">' + esc(def.description || '') + '</div>');
    if (isEarned && earnedRow.serial) {
      var capStr = cap ? ' / ' + ('0000' + cap).slice(-4) : '';
      h.push('<div class="pb-d-serial">#' + ('0000' + earnedRow.serial).slice(-4) + capStr + '</div>');
    }
    if (isEarned && earnedRow.awarded_at) h.push('<div class="pb-d-date">Earned ' + dateStr(earnedRow.awarded_at) + '</div>');
    if (!isEarned && typeof remaining === 'number' && cap) {
      h.push('<div class="pb-d-remaining">' + (remaining > 0 ? remaining + ' of ' + cap + ' serials remaining' : 'All ' + cap + ' serials awarded') + '</div>');
    }
    if (mil) h.push('<div class="pb-ribbon" aria-hidden="true"><span></span><span></span><span></span></div>');
    return h.join('');
  }

  var CSS = '.pb-wall{display:flex;flex-direction:column;gap:8px}' +
    '.pb-grid{display:flex;flex-wrap:wrap;gap:6px}' +
    '.pb-hex{background:none;border:none;padding:2px;cursor:pointer;line-height:0;border-radius:50%}' +
    '.pb-hex:hover{transform:scale(1.07)}.pb-hex:focus-visible{outline:2px solid #39e58c;outline-offset:2px}' +
    '.pb-cat{font-family:"Share Tech Mono",monospace;font-size:0.66rem;letter-spacing:0.18em;color:#7d8590;margin:12px 0 2px}' +
    '.pb-summary{font-family:"Share Tech Mono",monospace;font-size:0.62rem;color:#7d8590}' +
    '.pb-overlay{position:fixed;inset:0;background:rgba(3,6,10,0.82);display:flex;align-items:center;justify-content:center;z-index:9000;padding:16px}' +
    '.pb-sheet{background:#0d1117;border:1px solid #263041;border-radius:16px;max-width:420px;width:100%;max-height:88vh;overflow-y:auto;padding:22px 20px;text-align:center}' +
    '.pb-d-hero{line-height:0;margin-bottom:8px}' +
    '.pb-d-stat{font-family:"Orbitron",sans-serif;font-size:1.4rem;font-weight:900;color:#39e58c;letter-spacing:0.04em}' +
    '.pb-d-name{font-family:"Orbitron",sans-serif;font-size:0.85rem;font-weight:700;color:#e6e8ea;margin-top:4px}' +
    '.pb-d-desc{font-size:0.72rem;color:#9aa4b0;line-height:1.5;margin:8px auto 14px;max-width:320px}' +
    '.pb-d-tiers{display:flex;justify-content:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}' +
    '.pb-d-tier{width:72px}.pb-d-unearned{opacity:0.5}' +
    '.pb-d-thr{font-family:"Share Tech Mono",monospace;font-size:0.66rem;color:#c8ccd2;margin-top:2px}' +
    '.pb-d-sub{font-size:0.55rem;color:#7d8590;line-height:1.25}' +
    '.pb-d-date{font-family:"Share Tech Mono",monospace;font-size:0.55rem;color:#5b6470;margin-top:1px}' +
    '.pb-d-next{font-family:"Share Tech Mono",monospace;font-size:0.7rem;color:#39e58c;margin-top:4px}' +
    '.pb-d-serial{font-family:"Orbitron",sans-serif;font-size:0.8rem;font-weight:900;letter-spacing:0.14em;color:#f0a832;margin-top:6px}' +
    '.pb-d-remaining{font-family:"Share Tech Mono",monospace;font-size:0.62rem;color:#f0a832;margin-top:6px}' +
    '.pb-ribbon{display:flex;gap:2px;margin-top:10px;justify-content:center}' +
    '.pb-ribbon span{width:26px;height:8px;border-radius:1px;background:#6b7245}' +
    '.pb-ribbon span:nth-child(2){background:#f0a832}' +
    '.pb-ribbon span:nth-child(3){background:#8a4b32}' +
    '.pb-done{margin-top:6px;font-family:"Orbitron",sans-serif;font-size:0.72rem;font-weight:700;letter-spacing:0.08em;color:#e6e8ea;background:#1a2230;border:1px solid #2c3a4f;border-radius:10px;padding:9px 34px;cursor:pointer}' +
    '.pb-done:hover{background:#223048}';

  function renderBadgeWall(el, data) {
    if (!el) return;
    if (!document.getElementById('pb-style')) {
      var st = document.createElement('style');
      st.id = 'pb-style'; st.textContent = CSS;
      document.head.appendChild(st);
    }
    var defs = data.defs || [], earned = data.earned || {}, stats = data.stats || {};
    var caps = data.serial_caps || {}, remaining = data.remaining || {};
    var badgeSize = data.badgeSize || 88;
    var fams = familiesFrom(defs);
    var famKeys = Object.keys(FAMILY_LABELS).filter(function (k) { return fams[k]; });
    Object.keys(fams).forEach(function (k) { if (famKeys.indexOf(k) < 0) famKeys.push(k); });

    var earnedTiers = 0, totalTiers = 0;
    famKeys.forEach(function (k) { fams[k].forEach(function (t) { totalTiers++; if (earned[t.slug]) earnedTiers++; }); });

    var frag = document.createElement('div');
    frag.className = 'pb-wall';
    frag.innerHTML = '<div class="pb-summary">' + earnedTiers + ' / ' + totalTiers + ' tiers earned</div>';

    function grid(items, renderBtn) {
      var g = document.createElement('div');
      g.className = 'pb-grid';
      if (data.columns) {
        g.style.display = 'grid';
        g.style.gridTemplateColumns = 'repeat(' + parseInt(data.columns, 10) + ', minmax(0, 1fr))';
        g.style.justifyItems = 'center';
      }
      items.forEach(function (it) { g.appendChild(renderBtn(it)); });
      return g;
    }
    /* tip → native hover tooltip AND the aria-label, so what a badge is for
     * is one hover away without opening the detail. */
    function hexBtn(svg, label, tip, onClick) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'pb-hex';
      b.setAttribute('aria-label', tip || label);
      b.title = tip || label;
      b.innerHTML = svg;
      b.addEventListener('click', onClick);
      return b;
    }
    function cat(text) {
      var c = document.createElement('div');
      c.className = 'pb-cat'; c.textContent = text;
      return c;
    }

    frag.appendChild(cat('SERVICE RECORD'));
    frag.appendChild(grid(famKeys, function (k) {
      var st2 = famState(fams[k], earned, stats);
      var label = (FAMILY_LABELS[k] || k) + (st2.top ? ' — ' + st2.top.name : ' — no contact yet');
      var tip = (FAMILY_LABELS[k] || k) + ': ' + (FAMILY_DESC[k] || '') +
        (st2.next ? ' (' + fmt(st2.current) + ' / ' + fmt(st2.next.threshold) + ' to ' + st2.next.name + ')' : ' (maximum tier)');
      return hexBtn(
        scopeSVG({ glyph: fams[k][0].icon, tier: st2.tier, progressFrac: st2.next ? st2.frac : 0, size: badgeSize }),
        label, tip,
        function () { openDetail(familyDetail(k, fams[k], earned, stats)); }
      );
    }));

    var specials = defs.filter(function (d) { return d.category === 'special' || d.category === 'rare'; });
    if (specials.length) {
      frag.appendChild(cat('COMMENDATIONS'));
      frag.appendChild(grid(specials, function (d) {
        var scol = sanitizeColor(d.color, null);
        return hexBtn(
          scopeSVG({ glyph: specialGlyph(d), tier: earned[d.slug] ? 2 : 0, bezelColor: scol, glyphColor: scol, size: badgeSize }),
          d.name, d.name + ': ' + (d.description || ''),
          function () { openDetail(specialDetail(d, earned[d.slug], remaining[d.slug], caps[d.slug])); }
        );
      }));
    }
    var mil = defs.filter(function (d) { return d.category === 'military'; });
    if (mil.length) {
      frag.appendChild(cat('MILITARY HONOURS'));
      frag.appendChild(grid(mil, function (d) {
        return hexBtn(
          scopeSVG({ glyph: specialGlyph(d), tier: earned[d.slug] ? 3 : 0, military: true, size: badgeSize }),
          d.name, d.name + ': ' + (d.description || ''),
          function () { openDetail(specialDetail(d, earned[d.slug], remaining[d.slug], caps[d.slug])); }
        );
      }));
    }
    el.innerHTML = '';
    el.appendChild(frag);
  }

  global.PilnkBadges = { renderBadgeWall: renderBadgeWall, scopeSVG: scopeSVG, TIERS: TIERS, GLYPHS: GLYPHS };
})(typeof window !== 'undefined' ? window : this);

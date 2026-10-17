/* static/live-pill.js - airport live streams in the navbar (main, 17 Oct 2026, AJ: "make it a little more visible so
   I don't need to open an AC card to see it").
   The dashboard already fetches /api/cams every 5 min (loadLiveCams in index.html): the live streams near this node,
   checked on pilnk.io every 10 min. It hands that list to LivePill.update(). Then:
     - no live stream -> no pill (never a dead button);
     - one   -> "🔴 LIVE · Auckland", tap = play it;
     - more  -> "🔴 LIVE · 2 streams", tap = pick one from a short list.
   A stream plays in a small floating player over the map (drag it by its bar, resize from the corner, × to close).
   It is the channel's own YouTube embed, privacy-enhanced and muted until the viewer unmutes - nothing is recorded,
   re-streamed or proxied (AJ's legal line, same as the card's LIVE STREAM button). A stream that ends while you
   watch is never yanked; you close it. Everything shown comes from pilnk.io as text, never as HTML. */
(function (root) {
  'use strict';
  var VID = /^[A-Za-z0-9_-]{11}$/;
  var NARROW = 600;              // px: phone width -> the pill just says "🔴 LIVE"
  var cams = [];

  function shortName(c) {
    var l = String((c && (c.label || c.icao)) || '').split(/\s+[—–-]\s+/)[0];
    l = l.replace(/\s+(International\s+)?Airport$/i, '').trim();
    return l || String(c.icao || '');
  }
  function valid(list) {
    return (Array.isArray(list) ? list : []).filter(function (c) { return c && VID.test(String(c.video_id || '')); });
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function css() {
    if (document.getElementById('livePillCss')) return;
    var s = document.createElement('style'); s.id = 'livePillCss';
    s.textContent =
      '#livePill{display:none;align-items:center;gap:4px;padding:3px 10px;border-radius:12px;cursor:pointer;white-space:nowrap;' +
      'font-family:"Share Tech Mono",monospace;font-size:0.7rem;letter-spacing:0.05em;color:#fff;' +
      'background:#dc2626;border:1px solid #b91c1c;animation:lpPulse 2.4s ease-in-out infinite;}' +
      '#livePill:hover{background:#b91c1c;}' +
      '@keyframes lpPulse{0%,100%{box-shadow:0 0 0 0 rgba(248,113,113,0.0)}50%{box-shadow:0 0 0 3px rgba(248,113,113,0.25)}}' +
      '#liveMenu{position:fixed;z-index:3001;display:none;min-width:200px;max-width:90vw;background:var(--panel-bg,#111827);' +
      'border:1px solid rgba(248,113,113,0.5);border-radius:6px;padding:4px;box-shadow:0 6px 24px rgba(0,0,0,0.45);}' +
      '#liveMenu .lp-item{display:block;width:100%;text-align:left;background:none;border:0;color:var(--text,#e5e7eb);' +
      'padding:6px 8px;border-radius:4px;cursor:pointer;font-size:0.78rem;}' +
      '#liveMenu .lp-item:hover{background:rgba(248,113,113,0.15);}' +
      '#liveMenu .lp-sub{display:block;font-size:0.66rem;opacity:0.65;}' +
      '#livePlayer{position:fixed;z-index:3000;right:16px;bottom:16px;width:400px;max-width:calc(100vw - 16px);' +
      'background:#000;border:1px solid rgba(248,113,113,0.6);border-radius:8px;overflow:hidden;resize:both;' +
      'min-width:220px;min-height:150px;box-shadow:0 8px 30px rgba(0,0,0,0.55);display:flex;flex-direction:column;}' +
      '#livePlayer .lp-bar{display:flex;align-items:center;gap:6px;padding:4px 6px 4px 10px;background:rgba(127,29,29,0.92);' +
      'color:#fee2e2;font-size:0.72rem;cursor:move;user-select:none;touch-action:none;}' +
      '#livePlayer .lp-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}' +
      '#livePlayer .lp-bar a,#livePlayer .lp-close{color:#fee2e2;text-decoration:none;background:none;border:0;cursor:pointer;' +
      'font-size:0.8rem;padding:0 4px;}' +
      '#livePlayer .lp-frame{position:relative;flex:1;aspect-ratio:16/9;}' +
      '#livePlayer iframe{position:absolute;inset:0;width:100%;height:100%;border:0;}';
    document.head.appendChild(s);
  }

  function pill() {
    var p = document.getElementById('livePill');
    if (p) return p;
    var right = document.querySelector('#header .header-right');
    if (!right) return null;
    css();
    p = el('button'); p.id = 'livePill'; p.type = 'button';
    p.addEventListener('click', function (e) { e.stopPropagation(); onPill(); });
    var before = right.querySelector('.status-bar');
    right.insertBefore(p, before || right.firstChild);
    return p;
  }

  function update(list) {
    cams = valid(list);
    var p = pill(); if (!p) return;
    if (!cams.length) { p.style.display = 'none'; hideMenu(); return; }
    var narrow = (root.innerWidth || 1024) <= NARROW;
    p.textContent = narrow ? '🔴 LIVE' : (cams.length === 1 ? '🔴 LIVE · ' + shortName(cams[0]) : '🔴 LIVE · ' + cams.length + ' streams');
    p.title = cams.map(function (c) {
      return shortName(c) + ' live' + (c.channel_name ? ' (stream by ' + c.channel_name + ')' : '');
    }).join('\n') + '\nTap to watch';
    p.style.display = 'inline-flex';
  }

  function onPill() {
    if (cams.length === 1) { open(cams[0]); return; }
    var m = document.getElementById('liveMenu');
    if (m && m.style.display === 'block') { hideMenu(); return; }
    showMenu();
  }
  function showMenu() {
    var m = document.getElementById('liveMenu');
    if (!m) {
      m = el('div'); m.id = 'liveMenu'; document.body.appendChild(m);
      document.addEventListener('click', function (e) { if (!m.contains(e.target)) hideMenu(); });
    }
    m.textContent = '';
    cams.forEach(function (c) {
      var b = el('button', 'lp-item', '🔴 ' + shortName(c));
      b.type = 'button';
      b.appendChild(el('span', 'lp-sub', (c.channel_name || '') + (c.dist_nm != null ? ' · ' + Math.round(c.dist_nm) + ' nm' : '')));
      b.addEventListener('click', function (e) { e.stopPropagation(); hideMenu(); open(c); });
      m.appendChild(b);
    });
    var r = document.getElementById('livePill').getBoundingClientRect();
    m.style.display = 'block';
    m.style.top = Math.round(r.bottom + 6) + 'px';
    m.style.left = Math.max(8, Math.min(Math.round(r.left), (root.innerWidth || 1024) - m.offsetWidth - 8)) + 'px';
  }
  function hideMenu() { var m = document.getElementById('liveMenu'); if (m) m.style.display = 'none'; }

  function close() {
    var w = document.getElementById('livePlayer'); if (!w) return;
    var f = w.querySelector('iframe'); if (f) f.src = 'about:blank';
    w.remove();
  }
  function open(c) {
    if (!c || !VID.test(String(c.video_id || ''))) return;
    css(); close();
    var w = el('div'); w.id = 'livePlayer';
    var bar = el('div', 'lp-bar');
    bar.appendChild(el('span', 'lp-title', '🔴 ' + shortName(c) + (c.channel_name ? ' · ' + c.channel_name : '')));
    var yt = el('a', null, '↗'); yt.href = 'https://www.youtube.com/watch?v=' + c.video_id; yt.target = '_blank';
    yt.rel = 'noopener noreferrer'; yt.title = 'Open on YouTube';
    var x = el('button', 'lp-close', '✕'); x.type = 'button'; x.title = 'Close';
    x.addEventListener('click', close);
    bar.appendChild(yt); bar.appendChild(x);
    var fw = el('div', 'lp-frame');
    var f = document.createElement('iframe');
    f.src = 'https://www.youtube-nocookie.com/embed/' + c.video_id + '?autoplay=1&mute=1&playsinline=1&rel=0';
    f.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture; fullscreen');
    f.setAttribute('allowfullscreen', '');
    f.setAttribute('referrerpolicy', 'strict-origin-when-cross-origin');
    f.title = 'Live stream — ' + shortName(c);
    fw.appendChild(f);
    w.appendChild(bar); w.appendChild(fw);
    document.body.appendChild(w);
    drag(w, bar);
  }

  // Drag by the bar. Switches the window from right/bottom anchoring to left/top on first move.
  function drag(w, bar) {
    var sx, sy, ox, oy, on = false;
    bar.addEventListener('pointerdown', function (e) {
      if (e.target.closest && e.target.closest('a,button')) return;
      var r = w.getBoundingClientRect();
      w.style.left = r.left + 'px'; w.style.top = r.top + 'px'; w.style.right = 'auto'; w.style.bottom = 'auto';
      sx = e.clientX; sy = e.clientY; ox = r.left; oy = r.top; on = true;
      if (bar.setPointerCapture) bar.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    bar.addEventListener('pointermove', function (e) {
      if (!on) return;
      var W = root.innerWidth, H = root.innerHeight, r = w.getBoundingClientRect();
      w.style.left = Math.max(0, Math.min(W - 60, ox + e.clientX - sx)) + 'px';
      w.style.top = Math.max(0, Math.min(H - 30, oy + e.clientY - sy)) + 'px';
    });
    var end = function () { on = false; };
    bar.addEventListener('pointerup', end); bar.addEventListener('pointercancel', end);
  }

  root.addEventListener && root.addEventListener('resize', function () { if (cams.length) update(cams); });
  root.LivePill = { update: update, open: open, close: close, shortName: shortName };
})(window);

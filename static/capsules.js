
// ── FLIGHT CAPSULES (v1.5.22 Ship 1: track recorder, auto-record, replay;
//    v1.5.23 Ship 2: synced ATC audio, recorded only when the radio is already on;
//    v1.5.26: an auto rule the owner ticks 📻 may switch the radio on for its capsule) ──
// The node records chosen aircraft SERVER-SIDE (app.py capsule_recorder) every
// 2 s until they have been out of coverage for 10 min, so a capsule keeps
// going with this page closed. This block is only the UI: ● REC / ⟳ AUTO on
// the cinematic card, the library in the History tab, and replay on the map
// with a screenshot mode. Keyed by HEX throughout, so a helicopter that
// changes or drops its callsign stays one track. Elapsed times use the Pi's
// clock (capState.skew), never this device's — same lesson as #91.
var capState = { active: {}, rules: [], list: [], skew: 0, woken: false };

(function(){
  var s = document.createElement('style');
  s.textContent = [
    '.cap-row{display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:6px 4px;border-bottom:1px solid var(--border);font-family:"Share Tech Mono",monospace;font-size:0.62rem;color:var(--text);}',
    '.cap-dim{color:var(--text-dim);}',
    '.cap-right{margin-left:auto;display:flex;gap:4px;align-items:center;}',
    '.cap-row button,.cap-row a{background:var(--bg-deep);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:inherit;font-size:0.58rem;padding:2px 6px;cursor:pointer;text-decoration:none;}',
    '.cap-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#ef4444;box-shadow:0 0 6px #ef4444;animation:capblink 1.2s infinite;}',
    '@keyframes capblink{50%{opacity:.25}}',
    '.cap-empty{color:var(--text-dim);font-size:0.62rem;padding:8px;text-align:center;}',
    '#ccRecBtn.cap-rec-on{background:rgba(220,38,38,0.35)!important;color:#fff!important;}',
    '#capBar{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:9000;background:rgba(8,14,26,0.93);border:1px solid rgba(56,189,248,0.45);border-radius:8px;padding:8px 12px;width:min(560px,94vw);font-family:"Share Tech Mono",monospace;font-size:0.66rem;color:#e2e8f0;box-shadow:0 6px 24px rgba(0,0,0,.55);}',
    '#capBar .cap-bar-row{display:flex;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap;}',
    '#capBar button,#capBar select,#capBar a{background:#0f172a;border:1px solid #334155;border-radius:4px;color:#e2e8f0;font-family:inherit;font-size:0.62rem;padding:3px 8px;cursor:pointer;text-decoration:none;}',
    '#capScrub{flex:1;min-width:120px;}',
    '.cap-legend-bar{display:inline-block;width:90px;height:6px;border-radius:3px;vertical-align:middle;margin:0 4px;background:linear-gradient(90deg,hsl(20,90%,55%),hsl(80,90%,55%),hsl(140,90%,55%),hsl(200,90%,55%),hsl(260,90%,55%));}',
    '#capShotExit{position:fixed;top:10px;right:10px;z-index:100001;background:rgba(8,14,26,0.85);border:1px solid #334155;border-radius:6px;color:#e2e8f0;font-family:"Share Tech Mono",monospace;font-size:0.65rem;padding:4px 10px;cursor:pointer;opacity:.2;transition:opacity .2s;}',
    '#capShotExit:hover{opacity:1;}',
    '.cap-plane svg{filter:drop-shadow(0 0 3px rgba(0,0,0,.85));}',
    '#capAddForm select,#capAddForm input[type=text]{background:var(--bg-deep);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:inherit;font-size:0.6rem;padding:3px 5px;}',
    '#capAddForm input[type=text]{flex:1;min-width:90px;text-transform:uppercase;}',
    '#capAddMsg{font-size:0.6rem;padding:2px 4px 6px;min-height:0.8rem;}',
    '#capAddMsg button{background:var(--bg-deep);border:1px solid var(--border);border-radius:4px;color:var(--text);font-family:inherit;font-size:0.58rem;padding:2px 6px;margin:2px 4px 0 0;cursor:pointer;}'
  ].join('\n');
  document.head.appendChild(s);
})();

function capEsc(s){ return String(s == null ? '' : s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function capFmtDur(s){
  s = Math.max(0, Math.round(s || 0));
  var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
}
function capNow(){ return Date.now() / 1000 + capState.skew; }   // the Pi's clock
// Ship 2 (v1.5.23): what the node's audio tap is doing for live capsules. A
// capsule leaves the radio alone (AJ, 24 Sep) — unless its auto rule has 📻
// ticked (v1.5.26), and then the row says the radio was switched on for it.
function capAudioTag(st){
  return st === 'recording' ? (capState.woken ? ' · 🔊 audio (📻 radio switched on for this)' : ' · 🔊 audio')
       : st === 'wake_failed' ? ' · track only (📻 could not switch the radio on)'
       : st === 'radio_off' ? ' · track only (radio off)'
       : st === 'no_radio'  ? ' · track only (no radio)'
       : st === 'no_ffmpeg' ? ' · track only (ffmpeg missing)' : '';
}
function capMHz(hz){ return (typeof hz === 'number' && hz > 0) ? (hz / 1e6).toFixed(3) + ' MHz' : '?'; }
function capAltTxt(v){ return (typeof v === 'number') ? Math.round(v).toLocaleString() + 'ft' : '—'; }
function capPost(url, body){
  return fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body || {})})
    .then(function(r){ return r.json(); });
}

function loadCapsules(){
  return fetch('/api/capsules').then(function(r){ return r.json(); }).then(function(d){
    capState.active = {};
    (d.active || []).forEach(function(c){ capState.active[c.hex] = c; });
    capState.rules = d.rules || [];
    capState.list  = d.capsules || [];
    capState.woken = !!d.radio_woken;
    if (typeof d.now === 'number') capState.skew = d.now - Date.now() / 1000;
    ccCapsuleButtons();
    renderCapsulePanel();
  }).catch(function(){ /* node busy or restarting: keep the last known state */ });
}

// ── Card buttons ─────────────────────────────────────────────
// Hex is read from the LIVE aircraft behind the open card, not ccCurrentHex:
// a hex-less contact opened straight after another card can leave
// ccCurrentHex holding the previous aircraft, and REC must never hit the
// wrong airframe.
function capCardHex(){
  if (!ccVisible || !ccCurrentCS) return '';
  var a = lastAircraft[ccCurrentCS];
  return (a && a.hex) ? String(a.hex).toUpperCase() : '';
}
function capRuleForHex(hx){
  return capState.rules.some(function(r){ return (r.hex || '').toUpperCase() === hx; });
}
function ccCapsuleButtons(){
  var rb = document.getElementById('ccRecBtn'), ab = document.getElementById('ccAutoBtn');
  if (!rb || !ab) return;
  var hx = capCardHex();
  if (!/^[0-9A-F]{6}$/.test(hx)) { rb.style.display = 'none'; ab.style.display = 'none'; return; }
  rb.style.display = 'inline-block'; ab.style.display = 'inline-block';
  var c = capState.active[hx];
  if (c) { rb.textContent = '■ STOP · ' + capFmtDur(capNow() - c.started); rb.classList.add('cap-rec-on'); }
  else   { rb.textContent = '● REC'; rb.classList.remove('cap-rec-on'); }
  var on = capRuleForHex(hx);
  ab.textContent = on ? '⟳ AUTO ✓' : '⟳ AUTO';
  ab.style.opacity = on ? '1' : '0.65';
}
function ccRecToggle(){
  var hx = capCardHex(); if (!hx) return;
  var a = lastAircraft[ccCurrentCS] || {};
  var on = !!capState.active[hx];
  capPost(on ? '/api/capsules/stop' : '/api/capsules/start', {hex: hx, flight: (a.flight || '').trim()})
    .then(loadCapsules).catch(function(){});
}
function ccAutoToggle(){
  var hx = capCardHex(); if (!hx) return;
  var a = lastAircraft[ccCurrentCS] || {};
  var on = capRuleForHex(hx);
  var label = [(a.flight || '').trim(), a.r, a.t].filter(Boolean).join(' ') || hx;
  capPost('/api/capsules/rules', {action: on ? 'remove' : 'add', hex: hx, label: label})
    .then(loadCapsules).catch(function(){});
}

// ── History-tab library ──────────────────────────────────────
function renderCapsulePanel(){
  var act = document.getElementById('capsuleActive'), list = document.getElementById('capsuleList'),
      rl = document.getElementById('capsuleRules'), sum = document.getElementById('capsuleSummary');
  if (!act || !list || !rl) return;
  var hxs = Object.keys(capState.active);
  act.innerHTML = hxs.map(function(hx){
    var c = capState.active[hx];
    return '<div class="cap-row"><span class="cap-dot"></span><b>' + capEsc(c.flight || hx) + '</b>' +
      '<span class="cap-dim">' + capEsc(hx) + (c.auto ? ' · auto' : '') + capAudioTag(c.audio) + '</span>' +
      '<span class="cap-right"><span class="cap-dim cap-el" data-st="' + c.started + '" data-pts="' + c.points + '">' +
        capFmtDur(capNow() - c.started) + ' · ' + c.points + ' pts</span>' +
      '<button onclick="capReplay(\'' + c.id + '\')">▶ View</button>' +
      '<button onclick="capStop(\'' + hx + '\')">■ Stop</button></span></div>';
  }).join('');
  list.innerHTML = capState.list.length ? capState.list.map(function(m){
    var name = [m.flight, m.reg, m.type].filter(Boolean).join(' · ') || m.hex;
    var when = new Date((m.started || 0) * 1000);
    return '<div class="cap-row"><b>' + capEsc(name) + '</b>' +
      '<span class="cap-dim">' + when.toLocaleDateString() + ' ' + when.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) +
      ' · ' + capFmtDur(m.duration) + ' · ' + capAltTxt(m.min_alt) + '–' + capAltTxt(m.max_alt) + (m.auto ? ' · auto' : '') +
      ((m.audio && m.audio.length) ? ' · 🔊' : '') + '</span>' +
      '<span class="cap-right"><button onclick="capReplay(\'' + m.id + '\')">▶ Replay</button>' +
      '<a href="/api/capsules/' + m.id + '/kml" download>KML</a>' +
      '<button onclick="capDelete(\'' + m.id + '\')" title="Delete this capsule">✕</button></span></div>';
  }).join('') : '<div class="cap-empty">No capsules yet. Open a plane and hit ● REC, or tap ⟳ AUTO to record it every time it flies.</div>';
  capEnsureAddForm(rl);
  rl.innerHTML = capState.rules.length ? capState.rules.map(function(r){
    var key = r.hex ? r.hex : ('callsign ' + (r.callsign || '') + '…');
    var hxA = capEsc(r.hex || ''), csA = capEsc(r.callsign || '');
    // v1.5.26 (MME1): per-rule opt-in. Off by default; the tooltip says exactly what it does.
    var wakeTip = r.wake
      ? 'ON: when this aircraft arrives, the radio is switched on to record ATC with it, and switched back off afterwards (unless you pressed LISTEN meanwhile). Pressing STOP cancels it for that visit. Click to turn off.'
      : 'OFF: audio is recorded only if the radio is already on. Click to let this rule switch the radio on for its recordings. Nothing plays out loud unless you press LISTEN.';
    return '<div class="cap-row"><b>' + capEsc(r.label || key) + '</b><span class="cap-dim">' + capEsc(key) + '</span>' +
      '<span class="cap-right">' +
      '<button title="' + capEsc(wakeTip) + '" style="opacity:' + (r.wake ? '1' : '0.6') + '" ' +
        'onclick="capRuleWake(\'' + hxA + '\',\'' + csA + '\',' + (r.wake ? 'false' : 'true') + ')">📻 ' + (r.wake ? 'Wakes radio ✓' : 'Wake radio') + '</button>' +
      // v1.5.28: the frequency a 📻 rule tunes to when IT switches the radio on (only then).
      (r.wake ? '<button title="' + capEsc(r.freq_hz
            ? 'When this rule switches the radio on, it tunes to ' + capMHz(r.freq_hz) + ' first, and puts the old frequency back when it switches off. If someone is already listening it never retunes. Click to change or clear.'
            : 'Records whatever frequency the radio is on. Click to set one for this aircraft, e.g. the Tower frequency it talks on.') +
          '" onclick="capRuleFreq(\'' + hxA + '\',\'' + csA + '\',' + (r.freq_hz || 0) + ')">📡 ' +
          (r.freq_hz ? capMHz(r.freq_hz) : 'any freq') + '</button>' : '') +
      '<button onclick="capRuleRemove(\'' + hxA + '\',\'' + csA + '\')">✕ Remove</button></span></div>';
  }).join('') : '<div class="cap-empty">Nothing on auto. Add an aircraft above, or open a plane’s card and tap ⟳ AUTO.</div>';
  if (sum) sum.textContent = capState.list.length + ' saved' + (hxs.length ? ' · ' + hxs.length + ' recording' : '');
}
function capStop(hx){ capPost('/api/capsules/stop', {hex: hx}).then(loadCapsules).catch(function(){}); }
function capRuleRemove(hx, cs){ capPost('/api/capsules/rules', {action: 'remove', hex: hx, callsign: cs}).then(loadCapsules).catch(function(){}); }
function capRuleWake(hx, cs, on){ capPost('/api/capsules/rules', {action: 'wake', hex: hx, callsign: cs, on: !!on}).then(loadCapsules).catch(function(){}); }
function capRuleFreq(hx, cs, curHz){
  var cur = curHz ? (curHz / 1e6).toFixed(3) : '';
  var v = prompt('Frequency for this aircraft, in MHz (e.g. 118.7 for Auckland Tower).\n' +
                 'Used only when this rule switches the radio on itself. Leave blank to record whatever the radio is on.', cur);
  if (v === null) return;                                   // cancelled
  capPost('/api/capsules/rules', {action: 'freq', hex: hx, callsign: cs, mhz: v.trim()})
    .then(function(j){ if (j && j.ok === false) alert(j.error || 'Could not set the frequency'); loadCapsules(); })
    .catch(function(){});
}

// ── Add an aircraft before it shows up (v1.5.27, MME1 thread 75) ─────────────
// ⟳ AUTO on a card needs the aircraft on screen. This adds it by registration
// (looked up to a hex on the node), by hex, or by callsign prefix. Built once and
// placed above the rules list; renderCapsulePanel only rewrites the list itself,
// so whatever is being typed survives the 10 s refresh.
var CAP_ADD_HINT = { reg: 'e.g. ZK-IPC or G-ABCD', hex: 'e.g. C81D5A', callsign: 'e.g. PLC (matches PLC1, PLC2…)' };
function capEnsureAddForm(rl){
  if (document.getElementById('capAddForm')) return;
  var f = document.createElement('div');
  f.id = 'capAddForm'; f.className = 'cap-row'; f.style.borderBottom = 'none';
  f.innerHTML =
    '<select id="capAddKind" onchange="capAddKindChanged()" title="What you are typing in">' +
      '<option value="reg">Registration</option><option value="hex">Hex code</option><option value="callsign">Callsign</option></select>' +
    '<input type="text" id="capAddVal" maxlength="12" placeholder="' + CAP_ADD_HINT.reg + '" ' +
      'onkeydown="if(event.key===\'Enter\')capRuleAddManual()">' +
    '<label title="Also switch the radio on to record ATC when it arrives (see 📻 on each rule)" style="display:flex;align-items:center;gap:2px;cursor:pointer;">' +
      '<input type="checkbox" id="capAddWake">📻</label>' +
    '<button onclick="capRuleAddManual()">+ Add</button>';
  var m = document.createElement('div'); m.id = 'capAddMsg'; m.className = 'cap-dim';
  rl.parentNode.insertBefore(f, rl);
  rl.parentNode.insertBefore(m, rl);
}
function capAddKindChanged(){
  var k = document.getElementById('capAddKind').value, v = document.getElementById('capAddVal');
  if (v) v.placeholder = CAP_ADD_HINT[k] || '';
  capAddMsg('');
}
function capAddMsg(html){ var m = document.getElementById('capAddMsg'); if (m) m.innerHTML = html; }
function capRuleAddManual(pick){
  var kind = document.getElementById('capAddKind').value;
  var inp = document.getElementById('capAddVal');
  var v = (inp.value || '').trim().toUpperCase();
  var body = { action: 'add', wake: !!document.getElementById('capAddWake').checked };
  if (pick) {
    body.hex = pick.hex;
    body.label = [pick.reg, pick.type].filter(Boolean).join(' ');
  } else if (!v) {
    return capAddMsg('Type a ' + ({reg:'registration', hex:'hex code', callsign:'callsign'}[kind]) + ' first.');
  } else if (kind === 'reg') {
    body.reg = v;
  } else if (kind === 'hex') {
    if (!/^[0-9A-F]{6}$/.test(v)) return capAddMsg('A hex code is 6 characters, 0–9 and A–F.');
    body.hex = v;
  } else {
    v = v.replace(/[^A-Z0-9]/g, '');
    if (!/^[A-Z0-9]{2,8}$/.test(v)) return capAddMsg('A callsign is 2–8 letters and numbers.');
    body.callsign = v; body.label = 'Callsign ' + v + '…';
  }
  capAddMsg('Adding…');
  fetch('/api/capsules/rules', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)})
    .then(function(r){ return r.json().then(function(j){ return {code: r.status, j: j}; }); })
    .then(function(res){
      var j = res.j || {};
      if (j.ok) {
        inp.value = ''; capState.addCands = null;
        capAddMsg('Added. It will be recorded the next time it is in range.');
        return loadCapsules();
      }
      if (j.error === 'several' && j.candidates) {
        // Never guess between airframes that share a registration: show them, let the person pick.
        capState.addCands = j.candidates;
        return capAddMsg('That registration matches ' + j.candidates.length + ' aircraft. Pick one:<br>' +
          j.candidates.map(function(c, i){
            return '<button onclick="capRuleAddManual(capState.addCands[' + i + '])">' +
              capEsc(c.reg) + ' · ' + capEsc(c.hex) + (c.type ? ' · ' + capEsc(c.type) : '') + '</button>';
          }).join(''));
      }
      capAddMsg(capEsc(j.message || (j.error === 'not_found' ? 'Not found.' : ('Could not add: ' + (j.error || res.code)))));
    })
    .catch(function(){ capAddMsg('Could not reach the node. Try again.'); });
}
function capDelete(id){
  if (!confirm('Delete this capsule? This cannot be undone.')) return;
  capPost('/api/capsules/' + id + '/delete', {}).then(loadCapsules).catch(function(){});
}

// ── Replay ───────────────────────────────────────────────────
var capRP = null;
function capEnsurePanes(){
  if (map.getPane('capsuleTrack')) return;
  map.createPane('capsuleTrack');  map.getPane('capsuleTrack').style.zIndex  = 600;
  map.createPane('capsuleMarker'); map.getPane('capsuleMarker').style.zIndex = 610;
}
// Colour is scaled to THIS capsule's own altitude band, not a fixed 0–40,000 ft
// scale — a helicopter working 500–1,500 ft would otherwise be one flat colour.
function capHue(frac){ return 'hsl(' + Math.round(20 + Math.max(0, Math.min(1, frac)) * 240) + ',90%,55%)'; }
function capArrow(){
  return '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M12 2 L19 21 L12 17 L5 21 Z" fill="#fde047" stroke="#111" stroke-width="1.3" stroke-linejoin="round"/></svg>';
}
// The replay marker is the SAME silhouette the live map draws for this type
// (AJ, 24 Sep): a military shape when one exists (C-130, F-16…), else the
// per-type civil shape, else the category shape — drawn in replay yellow so it
// stands out on the dimmed map. The arrow is only the last-resort fallback if
// the dashboard's icon code isn't available.
var CAP_PLANE_COL = '#fde047';
// Capsules recorded before the ADS-B category was stored (pre-v1.5.23) only
// know the type, and a type alone can't say "helicopter". Common rotorcraft
// types, so the police Bell 429 in those capsules is still drawn as one.
var CAP_HELI_TYPES = /^(B06T?|B212|B230|B222|B407|B412|B427|B429|B505|BK17|EC(20|30|35|45|55|75)|H(125|130|135|145|160|175|500)|AS(32|3B|50|55|65)|A1(09|19|39|69|89)|AW09|R22|R44|R66|S76|S92|MD(52|60)|UH1|H60|NH90|EH10|KA32|MI(8|17))$/;
function capPlaneIcon(meta){
  var t = String((meta && meta.type) || '').toUpperCase().trim();
  var cat = (meta && meta.cat) || (CAP_HELI_TYPES.test(t) ? 'A7' : '');
  try {
    if (t && typeof milCanvasKey === 'function' && typeof MIL_PATHS !== 'undefined') {
      var mk = milCanvasKey(t, '');
      if (mk && MIL_PATHS[mk]) {
        // 54, not the live map's 42: the military silhouettes fill less of their
        // box, and at 42 the C-130 read smaller than an A320 beside it.
        return {size: 54, html: '<svg xmlns="http://www.w3.org/2000/svg" width="54" height="54" viewBox="0 0 240 240">' +
          '<g transform="translate(0,240) scale(0.1,-0.1)" fill="' + CAP_PLANE_COL + '" stroke="#111" stroke-width="12">' +
          '<path d="' + MIL_PATHS[mk] + '"/></g></svg>'};
      }
    }
    if (typeof makeIcon === 'function' && typeof getCat === 'function') {
      var hasShape = t && (window.AIRCRAFT_SHAPES || {})[t];
      var ic = makeIcon('', getCat(t, cat), 0, CAP_PLANE_COL, false, hasShape ? t : undefined);
      var m = ((ic && ic.options && ic.options.html) || '').match(/<svg[\s\S]*?<\/svg>/);
      if (m) return {size: (ic.options.iconSize && ic.options.iconSize[0]) || 38, html: m[0]};
    }
  } catch (e) { /* fall through to the arrow */ }
  return {size: 22, html: capArrow()};
}
// Fade (or hide) every map layer that isn't the base tiles or the capsule.
// level === null restores what was there before.
function capDimOthers(level){
  var panes = map.getPanes();
  Object.keys(panes).forEach(function(n){
    if (n === 'mapPane' || n === 'tilePane' || n === 'capsuleTrack' || n === 'capsuleMarker') return;
    var p = panes[n];
    if (level === null) {
      if (p.dataset.capOp !== undefined) { p.style.opacity = p.dataset.capOp; delete p.dataset.capOp; }
    } else {
      if (p.dataset.capOp === undefined) p.dataset.capOp = p.style.opacity || '';
      p.style.opacity = level;
    }
  });
}
function capReplay(id){
  fetch('/api/capsules/' + id).then(function(r){ return r.json(); }).then(function(d){
    var pts = (d && d.points) || [];
    if (pts.length < 2) { alert('This capsule has no track to replay yet.'); return; }
    capExit();
    if (typeof closeCinematicCard === 'function' && ccVisible) closeCinematicCard();
    capEnsurePanes();
    var m = d.meta || {};
    var alts = pts.filter(function(p){ return typeof p.alt === 'number'; }).map(function(p){ return p.alt; });
    var aMin = alts.length ? Math.min.apply(null, alts) : 0, aMax = alts.length ? Math.max.apply(null, alts) : 1;
    var span = (aMax - aMin) || 1;
    var rend = L.canvas({padding: 0.5, pane: 'capsuleTrack'});
    var layer = L.layerGroup().addTo(map);
    var seg = [], segBucket = null, prev = null;
    function flush(bucket){
      if (seg.length > 1) L.polyline(seg, {renderer: rend, pane: 'capsuleTrack', color: capHue(bucket / 23),
        weight: 3.5, opacity: 0.95, lineCap: 'round', lineJoin: 'round', interactive: false}).addTo(layer);
    }
    pts.forEach(function(p){
      var ll = [p.lat, p.lon];
      var b = Math.round(((typeof p.alt === 'number' ? p.alt : aMin) - aMin) / span * 23);
      if (prev && p.t - prev.t > 60) {
        // Coverage dropout (behind a hill, below the horizon): a dashed grey
        // bridge, so the gap is shown honestly rather than drawn as flown.
        flush(segBucket);
        L.polyline([[prev.lat, prev.lon], ll], {renderer: rend, pane: 'capsuleTrack', color: '#94a3b8',
          weight: 1.5, dashArray: '4 6', opacity: 0.8, interactive: false}).addTo(layer);
        seg = [ll]; segBucket = b;
      } else if (segBucket === null || b === segBucket) {
        seg.push(ll); segBucket = b;
      } else {
        seg.push(ll); flush(segBucket); seg = [ll]; segBucket = b;   // shared joint: no visual gap
      }
      prev = p;
    });
    flush(segBucket);
    // Wrapped in a div so capUpdate can rotate one element whatever the icon's own markup.
    var pi = capPlaneIcon(m);
    var mk = L.marker([pts[0].lat, pts[0].lon], {pane: 'capsuleMarker', interactive: false, keyboard: false,
      icon: L.divIcon({className: 'cap-plane', iconSize: [pi.size, pi.size], iconAnchor: [pi.size / 2, pi.size / 2],
        html: '<div style="width:' + pi.size + 'px;height:' + pi.size + 'px;transform-origin:50% 50%;">' + pi.html + '</div>'})}).addTo(layer);
    capRP = {id: id, meta: m, pts: pts, t0: pts[0].t, t1: pts[pts.length - 1].t, t: pts[0].t, i: 0,
             playing: false, speed: 16, layer: layer, rend: rend, marker: mk,
             bounds: L.latLngBounds(pts.map(function(p){ return [p.lat, p.lon]; })),
             aMin: aMin, aMax: aMax, timer: null, shot: false,
             // Ship 2: audio stretches {n, t0, dur, hz, mode}, played in sync at 1×.
             audio: (m.audio || []).slice().sort(function(x, y){ return x.t0 - y.t0; }),
             aud: null, audSeg: null, audOn: true};
    capDimOthers(0.25);
    capBuildBar();
    capFit();
    capUpdate();
    capRP.timer = setInterval(capTick, 100);
  }).catch(function(){ alert('Could not load that capsule.'); });
}
function capBuildBar(){
  var R = capRP, m = R.meta;
  var name = [m.flight, m.reg, m.type].filter(Boolean).join(' · ') || m.hex || R.id;
  var bar = document.createElement('div'); bar.id = 'capBar';
  bar.innerHTML =
    '<div><b>' + capEsc(name) + '</b> <span style="color:#94a3b8;">' + capEsc(m.hex || '') + ' · ' +
      new Date(R.t0 * 1000).toLocaleString() + (m.status === 'recording' ? ' · still recording' : '') + '</span></div>' +
    '<div class="cap-bar-row"><button id="capPlay" onclick="capPlayToggle()">▶</button>' +
      '<select id="capSpeed" onchange="if(capRP){capRP.speed=+this.value;capUpdate();}">' +
        [1, 4, 16, 60, 120].map(function(s){ return '<option value="' + s + '"' + (s === 16 ? ' selected' : '') + '>' + s + '×</option>'; }).join('') +
      '</select><input type="range" id="capScrub" min="0" max="1000" value="0" oninput="capScrubTo(this.value)">' +
      '<span id="capClock"></span></div>' +
    '<div class="cap-bar-row"><span id="capInfo" style="flex:1;"></span>' +
      '<span style="color:#94a3b8;">' + capAltTxt(R.aMin) + '<span class="cap-legend-bar"></span>' + capAltTxt(R.aMax) + '</span></div>' +
    (R.audio.length ?
      // Honest labelling: the tap records the TUNED FREQUENCY, not this aircraft's
      // own calls, and speech only makes sense at real speed.
      '<div class="cap-bar-row"><button id="capAudBtn" onclick="capAudToggle()">🔊 Audio on</button>' +
      '<span style="color:#94a3b8;flex:1;">ATC audio · ' +
        R.audio.map(function(s){ return capMHz(s.hz); }).filter(function(v, k, a){ return a.indexOf(v) === k; }).join(', ') +
        ' · plays at 1× · everything heard on that frequency, not only this aircraft</span></div>' : '') +
    '<div class="cap-bar-row"><button onclick="capFit()">⤢ Fit</button>' +
      '<button onclick="capShot(true)">📷 Screenshot mode</button>' +
      '<a href="/api/capsules/' + R.id + '/kml" download>⬇ KML</a>' +
      '<button onclick="capExit()" style="margin-left:auto;">✕ Close</button></div>';
  document.body.appendChild(bar);
}
function capFit(){ if (capRP) map.fitBounds(capRP.bounds, {padding: [40, 40]}); }
function capPlayToggle(){
  var R = capRP; if (!R) return;
  if (!R.playing && R.t >= R.t1) { R.t = R.t0; R.i = 0; }
  R.playing = !R.playing;
  capUpdate();
}
function capScrubTo(v){
  var R = capRP; if (!R) return;
  R.t = R.t0 + (v / 1000) * (R.t1 - R.t0);
  R.i = 0;
  capUpdate(true);
}
function capTick(){
  var R = capRP; if (!R || !R.playing) return;
  // While a stretch of audio is actually playing, the AUDIO is the clock: the
  // marker follows the sound, so the two can never drift apart. Everywhere
  // else (no audio, gaps, faster speeds) the 100 ms timer drives as before.
  if (R.audSeg && R.aud && !R.aud.paused && R.speed === 1 && R.aud.readyState >= 2) {
    R.t = R.audSeg.t0 + R.aud.currentTime;
  } else {
    R.t += 0.1 * R.speed;
  }
  if (R.t >= R.t1) { R.t = R.t1; R.playing = false; }
  capUpdate();
}
function capUpdate(fromScrub){
  var R = capRP; if (!R) return;
  var pts = R.pts, i = R.i;
  if (pts[i] && pts[i].t > R.t) i = 0;
  while (i < pts.length - 2 && pts[i + 1].t <= R.t) i++;
  R.i = i;
  var a = pts[i], b = pts[Math.min(i + 1, pts.length - 1)], p = a, lat = a.lat, lon = a.lon;
  if (b !== a && b.t - a.t <= 60 && R.t > a.t) {          // no interpolating across a dropout
    var f = Math.min(1, (R.t - a.t) / (b.t - a.t));
    lat = a.lat + (b.lat - a.lat) * f; lon = a.lon + (b.lon - a.lon) * f;
    if (f > 0.5) p = b;
  }
  R.marker.setLatLng([lat, lon]);
  var el = R.marker.getElement();
  if (el && el.firstChild && typeof p.trk === 'number') el.firstChild.style.transform = 'rotate(' + p.trk + 'deg)';
  var pb = document.getElementById('capPlay'); if (pb) pb.textContent = R.playing ? '❚❚' : '▶';
  var sc = document.getElementById('capScrub');
  if (sc && !fromScrub) sc.value = Math.round((R.t - R.t0) / ((R.t1 - R.t0) || 1) * 1000);
  var ck = document.getElementById('capClock');
  if (ck) ck.textContent = new Date(R.t * 1000).toLocaleTimeString() + ' · ' + capFmtDur(R.t - R.t0) + ' / ' + capFmtDur(R.t1 - R.t0);
  var inf = document.getElementById('capInfo');
  if (inf) inf.textContent = [p.gnd ? 'GND' : capAltTxt(p.alt),
    (typeof p.gs === 'number' ? Math.round(p.gs) + 'kt' : ''),
    (typeof p.trk === 'number' ? Math.round(p.trk) + '°' : ''),
    (typeof p.vr === 'number' && p.vr ? (p.vr > 0 ? '+' : '') + Math.round(p.vr) + 'fpm' : ''),
    p.sq ? 'sq ' + p.sq : '', p.fl || ''].filter(Boolean).join(' · ');
  capSyncAudio();
}
// ── Ship 2: audio playback ───────────────────────────────────
// Each stretch is its own file with its own start time, so the position inside
// it is simply (t - t0). A moment with no stretch is genuine silence in the
// record (radio off, restart) and plays as nothing.
function capSegAt(R, t){
  for (var k = 0; k < R.audio.length; k++) {
    var s = R.audio[k];
    if (t >= s.t0 && t < s.t0 + s.dur) return s;
  }
  return null;
}
function capSyncAudio(){
  var R = capRP; if (!R || !R.audio.length) return;
  var btn = document.getElementById('capAudBtn');
  if (btn) btn.textContent = R.audOn ? (R.speed === 1 ? '🔊 Audio on' : '🔊 Audio (1× only)') : '🔇 Audio off';
  var s = (R.playing && R.speed === 1 && R.audOn) ? capSegAt(R, R.t) : null;
  if (!s) {
    if (R.aud && !R.aud.paused) R.aud.pause();
    R.audSeg = null;
    return;
  }
  if (!R.aud) { R.aud = new Audio(); R.aud.preload = 'auto'; }
  var a = R.aud;
  if (R.audSeg !== s) {
    R.audSeg = s;
    a.src = '/api/capsules/' + R.id + '/audio/' + s.n;
    // Seeking before metadata has loaded throws in some browsers — wait for it.
    a.addEventListener('loadedmetadata', function once(){
      a.removeEventListener('loadedmetadata', once);
      if (capRP === R && R.audSeg === s) { try { a.currentTime = Math.max(0, R.t - s.t0); } catch (e) {} }
    });
    a.play().catch(function(){});
    return;
  }
  if (a.readyState >= 1 && Math.abs(a.currentTime - (R.t - s.t0)) > 0.8) {
    try { a.currentTime = Math.max(0, R.t - s.t0); } catch (e) {}   // after a scrub
  }
  if (a.paused && !a.ended) a.play().catch(function(){});
}
function capAudToggle(){
  var R = capRP; if (!R) return;
  R.audOn = !R.audOn;
  if (R.audOn && R.speed !== 1) {                // turning audio on means "let me hear it"
    R.speed = 1;
    var sel = document.getElementById('capSpeed'); if (sel) sel.value = '1';
  }
  capUpdate();
}
// Screenshot mode: the map goes full-window with ONLY the base map and the
// capsule on it — no aircraft, rings, weather, zoom buttons or panels. The
// OpenStreetMap attribution stays (their licence asks for it). Space plays or
// pauses, Esc (or the faint corner button) comes back.
function capShot(on){
  var R = capRP; if (!R) return;
  var mapEl = document.getElementById('map'), bar = document.getElementById('capBar');
  if (on && !R.shot) {
    R.shot = true;
    R.mapCss = mapEl.style.cssText;
    mapEl.style.cssText += ';position:fixed!important;left:0!important;top:0!important;width:100vw!important;height:100vh!important;z-index:100000!important;margin:0!important;';
    capDimOthers(0);
    var zc = mapEl.querySelector('.leaflet-control-zoom'); if (zc) zc.style.display = 'none';
    if (bar) bar.style.display = 'none';
    var x = document.createElement('div'); x.id = 'capShotExit'; x.textContent = '✕ exit screenshot mode (Esc) · Space = play/pause';
    x.onclick = function(){ capShot(false); };
    document.body.appendChild(x);
    setTimeout(function(){ map.invalidateSize(); capFit(); }, 60);
  } else if (!on && R.shot) {
    R.shot = false;
    mapEl.style.cssText = R.mapCss || '';
    capDimOthers(0.25);
    var zc2 = mapEl.querySelector('.leaflet-control-zoom'); if (zc2) zc2.style.display = '';
    if (bar) bar.style.display = '';
    var x2 = document.getElementById('capShotExit'); if (x2) x2.remove();
    setTimeout(function(){ map.invalidateSize(); }, 60);
  }
}
function capExit(){
  var R = capRP; if (!R) return;
  if (R.shot) capShot(false);
  clearInterval(R.timer);
  if (R.aud) { try { R.aud.pause(); R.aud.removeAttribute('src'); R.aud.load(); } catch (e) {} }
  map.removeLayer(R.layer);
  if (R.rend) map.removeLayer(R.rend);
  capDimOthers(null);
  var bar = document.getElementById('capBar'); if (bar) bar.remove();
  capRP = null;
}
document.addEventListener('keydown', function(e){
  if (!capRP || !capRP.shot) return;
  if (e.key === 'Escape') { capShot(false); e.preventDefault(); }
  else if (e.key === ' ') { capPlayToggle(); e.preventDefault(); }
});

setInterval(function(){
  ccCapsuleButtons();
  // Tick the live elapsed counters in place (no re-render, so buttons stay clickable).
  document.querySelectorAll('.cap-el').forEach(function(el){
    el.textContent = capFmtDur(capNow() - parseFloat(el.getAttribute('data-st'))) + ' · ' + el.getAttribute('data-pts') + ' pts';
  });
}, 1000);
setInterval(loadCapsules, 10000);
loadCapsules();

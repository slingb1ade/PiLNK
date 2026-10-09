/* PiLNK node splash: live 3D radar console (spec pilnk-tasks/splash-3d-spec.md, plan splash-3d-plan.md).
 * Today's flat splash always renders first; this only swaps in a three.js scene when it can draw its first
 * frame within the window, and never later. Any failure leaves the flat splash exactly as it was.
 * ASCII only, no backslash characters (MCP write rule). */
(function (W) {
  'use strict';
  var SWEEP_MS = 4000, FLASH_MS = 600, AIR_MS = 1500;

  /* Blips at today's SVG positions (200 box, centre 100,100, radius 90), screen-normalised, y down. */
  function blipAt(x, y, color) { return { x: (x - 100) / 90, y: (y - 100) / 90, color: color }; }
  var BLIPS = [blipAt(148, 58, '#00ff88'), blipAt(72, 130, '#00aaff'), blipAt(125, 148, '#ff8800')];

  /* The sweep starts pointing east and turns clockwise (as the SVG), so a blip is hit when the sweep
   * angle reaches the blip's clockwise angle from east. */
  function hitPhase(b) {
    var a = Math.atan2(b.y, b.x) * 180 / Math.PI;
    return ((a + 360) % 360) / 360 * SWEEP_MS;
  }

  function blipState(tMs, b) {
    var since = tMs - hitPhase(b);
    var none = { visible: false, x: b.x, y: 0, z: b.y, yaw: 0, alpha: 0 };
    if (!(since >= 0)) return { flash: 0, aircraft: none };
    var dt = since % SWEEP_MS;
    var flash = dt < FLASH_MS ? 1 - dt / FLASH_MS : 0;
    if (dt >= AIR_MS) return { flash: flash, aircraft: none };
    var u = dt / AIR_MS, len = Math.sqrt(b.x * b.x + b.y * b.y) || 1, dx = b.x / len, dz = b.y / len;
    return {
      flash: flash,
      aircraft: {
        visible: true,
        x: b.x + dx * 0.5 * u,
        y: 0.9 * u,
        z: b.y + dz * 0.5 * u,
        yaw: Math.atan2(dx, dz) + (Math.PI / 3) * u,
        alpha: u < 0.6 ? 1 : 1 - (u - 0.6) / 0.4
      }
    };
  }


  /* Screen disc of static/splash/console.glb in model space (measured by slim_glb.py --measure, 9 Oct 2026). */
  var SCREEN = { cx: -0.0566, cy: 0.5941, cz: 0.0621, nx: -0.0001, ny: 0.926, nz: 0.3775, r: 0.7194 };
  var TINT = 0x9fb4cc, RISE_MS = 600, ORBIT_MS = 5500, TEX = 256;

  function now() { return (W.performance && W.performance.now) ? W.performance.now() : Date.now(); }

  function loadScript(src) {
    return new Promise(function (ok, bad) {
      var s = document.createElement('script');
      s.src = src; s.async = true;
      s.onload = function () { ok(); };
      s.onerror = function () { bad(new Error('script ' + src)); };
      document.head.appendChild(s);
    });
  }
  /* Vendor code comes in by fetch() and runs by indirect eval: unlike a script element, a fetch never holds
   * back the page's load event, and it can be aborted the moment the splash gives up (review 9 Oct, #3). */
  function loadCode(run, src) {
    if (run.reason) return Promise.resolve();
    if (!W.fetch) return loadScript(src);
    var ac = W.AbortController ? new W.AbortController() : null;
    if (ac) run.aborts.push(ac);
    return W.fetch(src, ac ? { signal: ac.signal } : {}).then(function (r) {
      if (!r.ok) throw new Error('fetch ' + src + ' ' + r.status);
      return r.text();
    }).then(function (code) { if (!run.reason) (0, eval)(code); });
  }
  function ensureThree(run, base) {
    var p = W.THREE ? Promise.resolve() : loadCode(run, base + 'vendor/three-r128.min.js');
    return p.then(function () {
      if (run.reason) return null;
      if (!W.THREE) throw new Error('three.js did not load');
      return W.THREE.GLTFLoader ? null : loadCode(run, base + 'vendor/GLTFLoader-r128.js');
    });
  }
  /* Probe for WebGL, then give the probe's context straight back (review #1). */
  function webglOK() {
    var c = document.createElement('canvas');
    var gl = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!gl) return false;
    try { var lose = gl.getExtension('WEBGL_lose_context'); if (lose) lose.loseContext(); } catch (e) {}
    return true;
  }
  function late(run) { return now() - run.t0 > run.windowMs; }

  /* The radar face, drawn in 2D each frame and used as the screen disc's texture. */
  function drawScreen(g, t) {
    var S = TEX, c = S / 2, R = S / 2 - 2, i;
    g.clearRect(0, 0, S, S);
    var bg = g.createRadialGradient(c, c, 0, c, c, R);
    bg.addColorStop(0, '#0d1f3c'); bg.addColorStop(1, '#060d1a');
    g.fillStyle = bg; g.beginPath(); g.arc(c, c, R, 0, Math.PI * 2); g.fill();
    g.strokeStyle = '#2a5aa0'; g.lineWidth = 1.5;
    [65, 40, 18].forEach(function (k) { g.beginPath(); g.arc(c, c, R * k / 90, 0, Math.PI * 2); g.stroke(); });
    g.beginPath(); g.moveTo(c, c - R); g.lineTo(c, c + R); g.moveTo(c - R, c); g.lineTo(c + R, c); g.stroke();
    var a = (t % SWEEP_MS) / SWEEP_MS * Math.PI * 2;
    for (i = 0; i < 12; i++) {
      g.fillStyle = 'rgba(59,130,246,' + (0.42 * (1 - i / 12)).toFixed(3) + ')';
      g.beginPath(); g.moveTo(c, c); g.arc(c, c, R, a - (i + 1) * Math.PI / 24, a - i * Math.PI / 24); g.closePath(); g.fill();
    }
    g.strokeStyle = '#60a5fa'; g.lineWidth = 2;
    g.beginPath(); g.moveTo(c, c); g.lineTo(c + Math.cos(a) * R, c + Math.sin(a) * R); g.stroke();
    BLIPS.forEach(function (b) {
      var f = blipState(t, b).flash, x = c + b.x * R, y = c + b.y * R;
      g.globalAlpha = 0.35 + 0.65 * f; g.fillStyle = b.color;
      g.beginPath(); g.arc(x, y, 4 + 3 * f, 0, Math.PI * 2); g.fill();
      if (f > 0) { g.globalAlpha = f * 0.6; g.strokeStyle = b.color; g.lineWidth = 1.5; g.beginPath(); g.arc(x, y, 6 + (1 - f) * 14, 0, Math.PI * 2); g.stroke(); }
      g.globalAlpha = 1;
    });
    g.strokeStyle = '#3b82f6'; g.lineWidth = 3; g.beginPath(); g.arc(c, c, R, 0, Math.PI * 2); g.stroke();
  }

  function dartGeometry(T) {
    var v = new Float32Array([0, 0.09, 0, -0.05, -0.05, 0, 0, -0.02, 0.012, 0, 0.09, 0, 0, -0.02, 0.012, 0.05, -0.05, 0,
                              0, 0.09, 0, 0.05, -0.05, 0, -0.05, -0.05, 0]);
    var geo = new T.BufferGeometry(); geo.setAttribute('position', new T.BufferAttribute(v, 3)); return geo;
  }

  var cur = null;

  function teardown(run) {
    if (run.tornDown) return; run.tornDown = true;
    if (run.raf) W.cancelAnimationFrame(run.raf);
    clearTimeout(run.deadline);
    if (run.onResize) W.removeEventListener('resize', run.onResize);
    if (run.scene) run.scene.traverse(function (o) {
      if (o.geometry) o.geometry.dispose();
      if (o.material) { if (o.material.map) o.material.map.dispose(); o.material.dispose(); }
    });
    if (run.renderer) {
      var cv = run.renderer.domElement;
      run.renderer.dispose();
      try { run.renderer.forceContextLoss(); } catch (e) {}
      if (cv && cv.parentNode) cv.parentNode.removeChild(cv);
    }
    (run.aborts || []).forEach(function (ac) { try { ac.abort(); } catch (e) {} });
    if (run.box) run.box.classList.remove('on');
    if (run.el) { run.el.classList.remove('ps-3d-on'); run.el.classList.remove('ps-3d-pending'); }
    run.renderer = run.scene = run.onResize = run.box = null; run.aborts = [];
  }

  function start(el, opts) {
    if (cur) return cur.promise;
    opts = opts || {};
    var base = opts.base || '/static/', windowMs = opts.windowMs || 1200;
    var run = { el: el, frames: 0, aborts: [], windowMs: windowMs, t0: now() }; cur = run;
    run.promise = new Promise(function (res) { run.resolve = res; });
    function finish(reason) {
      if (run.reason) return; run.reason = reason;
      if (reason === 'flat:timeout' || reason === 'flat:error') { try { W.console.warn('PiLNK splash 3D: ' + reason); } catch (e) {} }
      teardown(run); run.resolve(reason);
    }
    run.finish = finish;
    try {
      if (!el) { finish('flat:error'); return run.promise; }
      if (W.matchMedia && W.matchMedia('(prefers-reduced-motion: reduce)').matches) { finish('flat:reduced-motion'); return run.promise; }
      if (!webglOK()) { finish('flat:no-webgl'); return run.promise; }
      /* AJ 9 Oct: no flat radar while the 3D is on its way; teardown brings it back if the 3D never comes */
      el.classList.add('ps-3d-pending');
    } catch (e) { finish('flat:error'); return run.promise; }
    var t0 = run.t0;
    run.deadline = setTimeout(function () { if (!run.shown) finish('flat:timeout'); }, windowMs);
    ensureThree(run, base).then(function () {
      if (run.reason) return null;
      return new Promise(function (ok, bad) { new W.THREE.GLTFLoader().load(base + 'splash/console.glb', ok, undefined, bad); });
    }).then(function (gltf) {
      if (!gltf) return;
      if (run.reason) { gltf.scene.traverse(function (o) { if (o.geometry) o.geometry.dispose(); if (o.material) o.material.dispose(); }); return; }
      if (late(run)) { finish('flat:timeout'); return; }
      build(run, gltf, t0);
    }).catch(function () { finish('flat:error'); });
    return run.promise;
  }

  function build(run, gltf, t0) {
    var T = W.THREE, el = run.el;
    var box = el.querySelector('.ps-3d');
    if (!box) { box = document.createElement('div'); box.className = 'ps-3d'; box.setAttribute('aria-hidden', 'true'); el.insertBefore(box, el.firstChild); }
    run.box = box;
    var renderer = new T.WebGLRenderer({ antialias: true, alpha: true });
    run.renderer = renderer;
    renderer.setPixelRatio(Math.min(W.devicePixelRatio || 1, 1.5));
    renderer.outputEncoding = T.sRGBEncoding;
    var cv = renderer.domElement; cv.style.width = '100%'; cv.style.height = '100%'; cv.style.display = 'block';
    box.appendChild(cv);
    var scene = new T.Scene(); run.scene = scene;
    var cam = new T.PerspectiveCamera(32, 1, 0.1, 50);

    var model = gltf.scene, rig = new T.Group(); rig.add(model); scene.add(rig);
    model.traverse(function (o) {
      if (!o.isMesh) return;
      var old = o.material;
      o.material = new T.MeshStandardMaterial({ map: old.map || null, color: TINT, metalness: 0.35, roughness: 0.42 });
      old.dispose();
    });
    scene.add(new T.HemisphereLight(0xcfe0ff, 0x0a1a33, 0.75));
    var key = new T.DirectionalLight(0xffffff, 1.15); key.position.set(-3, 4, 3); scene.add(key);
    var n = new T.Vector3(SCREEN.nx, SCREEN.ny, SCREEN.nz).normalize();
    var glow = new T.PointLight(0x3b82f6, 1.1, 3); glow.position.set(SCREEN.cx, SCREEN.cy, SCREEN.cz).addScaledVector(n, 0.35); rig.add(glow);

    var frame = new T.Group();
    frame.position.set(SCREEN.cx, SCREEN.cy, SCREEN.cz);
    frame.quaternion.setFromUnitVectors(new T.Vector3(0, 0, 1), n);
    frame.scale.setScalar(SCREEN.r);
    rig.add(frame);
    var c2 = document.createElement('canvas'); c2.width = c2.height = TEX;
    var g2 = c2.getContext('2d'), tex = new T.CanvasTexture(c2); tex.encoding = T.sRGBEncoding;
    var disc = new T.Mesh(new T.CircleGeometry(0.985, 64), new T.MeshBasicMaterial({ map: tex }));
    disc.position.z = 0.006; frame.add(disc);

    var darts = BLIPS.map(function (b) {
      var m = new T.Mesh(dartGeometry(T), new T.MeshBasicMaterial({ color: b.color, transparent: true, side: T.DoubleSide, blending: T.AdditiveBlending, depthWrite: false }));
      var lg = new T.BufferGeometry(); lg.setAttribute('position', new T.BufferAttribute(new Float32Array(6), 3));
      var line = new T.Line(lg, new T.LineBasicMaterial({ color: b.color, transparent: true, blending: T.AdditiveBlending, depthWrite: false }));
      m.scale.setScalar(1.6); m.visible = line.visible = false; frame.add(m); frame.add(line);
      return { b: b, m: m, line: line };
    });

    function layout() {
      var w = el.clientWidth || 1, h = el.clientHeight || 1;
      renderer.setSize(w, h, false);
      cam.aspect = w / h;
      var radar = el.querySelector('.ps-radar'), size = Math.min(w, h) * 0.6, px = w / 2, py = h * 0.4;
      if (radar && radar.offsetWidth > 0) {
        var x = 0, y = 0, e = radar;
        while (e && e !== el) { x += e.offsetLeft; y += e.offsetTop; e = e.offsetParent; }
        size = radar.offsetWidth; px = x + radar.offsetWidth / 2; py = y + radar.offsetHeight / 2;
      }
      W.PilnkSplash3D.fit = { cx: px, cy: py, size: size };
      run.dist = Math.max(3, Math.min(16, (2.9 * h / size) / (2 * Math.tan(cam.fov * Math.PI / 360))));
      py -= size * 0.06;
      cam.setViewOffset(w, h, w / 2 - px, h / 2 - py, w, h);
      cam.updateProjectionMatrix();
    }
    layout();
    run.onResize = layout; W.addEventListener('resize', layout);

    var tShow = null;
    function tick() {
      if (run.reason) return;
      run.raf = W.requestAnimationFrame(tick);
      var t = now() - t0;
      if (tShow === null) tShow = t;
      var e = Math.min(1, (t - tShow) / RISE_MS); e = 1 - Math.pow(1 - e, 3);
      rig.position.y = -0.15 * (1 - e);
      var az = (-5 + 10 * Math.min(1, t / ORBIT_MS)) * Math.PI / 180, el2 = 26 * Math.PI / 180, d = run.dist;
      cam.position.set(Math.sin(az) * Math.cos(el2) * d, 0.05 + Math.sin(el2) * d, Math.cos(az) * Math.cos(el2) * d);
      cam.lookAt(0, 0.05, 0);
      drawScreen(g2, t); tex.needsUpdate = true;
      darts.forEach(function (o) {
        var a = blipState(t, o.b).aircraft;
        o.m.visible = o.line.visible = a.visible;
        if (!a.visible) return;
        o.m.position.set(a.x, -a.z, 0.03 + a.y);
        o.m.rotation.set(0.5 * a.y, 0, a.yaw + Math.PI);
        o.m.material.opacity = a.alpha;
        var p = o.line.geometry.attributes.position;
        p.setXYZ(0, o.b.x, -o.b.y, 0.012); p.setXYZ(1, a.x, -a.z, 0.03 + a.y); p.needsUpdate = true;
        o.line.material.opacity = 0.45 * a.alpha;
      });
      renderer.render(scene, cam);
      run.frames++; W.PilnkSplash3D.frames = run.frames;
      if (!run.shown) {
        /* never swap in late, even when the deadline timer itself was held up (review #2) */
        if (late(run)) { run.finish('flat:timeout'); return; }
        run.shown = true; clearTimeout(run.deadline);
        box.classList.add('on'); el.classList.add('ps-3d-on'); el.classList.remove('ps-3d-pending');
        run.resolve('shown');
      }
    }
    run.raf = W.requestAnimationFrame(tick);
  }

  function stop() {
    if (!cur) return;
    if (!cur.reason) cur.finish('flat:stopped'); else teardown(cur);
  }

  W.PilnkSplash3D = W.PilnkSplash3D || {};
  W.PilnkSplash3D.BLIPS = BLIPS;
  W.PilnkSplash3D.blipState = blipState;
  W.PilnkSplash3D.SWEEP_MS = SWEEP_MS;
  W.PilnkSplash3D.start = start;
  W.PilnkSplash3D.stop = stop;
  W.PilnkSplash3D.frames = 0;
})(window);

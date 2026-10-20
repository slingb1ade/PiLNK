/* PiLNK - aircraft navigation lights at night (AJ, 20 Oct 2026). 3D Approach first; written to be reused by 3D Sky.
 *
 *   left wingtip   red, steady          right wingtip  green, steady
 *   both wingtips  white strobe, double flash every 1.2 s (off on the ground - real aircraft taxi without them)
 *   top of the tail  red beacon, flashing, out of step with the strobes
 *   helicopters    the beacon only (no wings; the widest point is a rotor blade)
 *
 * Placed from each model's OWN vertices, not its bounding box, so every model gets them with no hand-tuning:
 * wingtips are the outermost points left and right, the beacon the highest point on the rear of the aircraft.
 * Frame used throughout (the page's aircraft frame, after MODEL_ORIENT): nose -Z, tail +Z, right wing +X, up +Y.
 */
(function (root) {
  'use strict';

  var HELI = { 'b429.glb': 1, 'as350.glb': 1, 'nh90.glb': 1, 'sh60.glb': 1 };
  function isHeli(key) { return !!HELI[key]; }

  // The beacon is looked for in the rear part of the aircraft only, so a rotodome, a cockpit or a rotor further
  // forward can never take it. Fixed wing: the back quarter. Helicopters: the back fifth (clear of the main rotor).
  var TAIL_FRAC = 0.25, TAIL_FRAC_HELI = 0.2;

  /* points: a flat array/Float32Array [x,y,z, x,y,z, ...] or an array of [x,y,z].
     -> { left: [x,y,z]|null, right: [x,y,z]|null, tail: [x,y,z] } or null when there is nothing to measure. */
  function anchors(points, opts) {
    if (!points || !points.length) return null;
    opts = opts || {};
    var flat = typeof points[0] === 'number';
    var n = flat ? Math.floor(points.length / 3) : points.length;
    if (!n) return null;
    function P(i) { return flat ? [points[3 * i], points[3 * i + 1], points[3 * i + 2]] : points[i]; }
    var zMin = Infinity, zMax = -Infinity, iL = -1, iR = -1, xL = Infinity, xR = -Infinity, i, p;
    for (i = 0; i < n; i++) {
      p = P(i);
      if (!(isFinite(p[0]) && isFinite(p[1]) && isFinite(p[2]))) continue;
      if (p[2] < zMin) zMin = p[2];
      if (p[2] > zMax) zMax = p[2];
      if (p[0] < xL) { xL = p[0]; iL = i; }
      if (p[0] > xR) { xR = p[0]; iR = i; }
    }
    if (iL < 0) return null;
    var zCut = zMax - (opts.heli ? TAIL_FRAC_HELI : TAIL_FRAC) * (zMax - zMin), iT = -1, yT = -Infinity;
    for (i = 0; i < n; i++) {
      p = P(i);
      if (p[2] >= zCut && p[1] > yT && isFinite(p[1])) { yT = p[1]; iT = i; }
    }
    function cp(k) { var q = P(k); return [q[0], q[1], q[2]]; }
    return { left: opts.heli ? null : cp(iL), right: opts.heli ? null : cp(iR), tail: iT < 0 ? null : cp(iT) };
  }

  // Timing. One 1.2 s cycle: strobes flash twice at the start, the beacon once in the middle.
  var CYCLE = 1.2, STROBE = [[0, 0.06], [0.14, 0.2]], BEACON = [0.6, 0.75];
  function state(tSec, ground) {
    var t = Number(tSec);
    if (!isFinite(t)) t = 0;
    var c = ((t % CYCLE) + CYCLE) % CYCLE;
    var strobe = !ground && STROBE.some(function (w) { return c >= w[0] && c < w[1]; });
    return { strobe: strobe, beacon: c >= BEACON[0] && c < BEACON[1] };
  }

  /* Browser only. Every vertex under `model`, in `frame`'s local coordinates (matrixWorld must be current).
     Uses the real vertices through every node's transform: the C-5's parts sit rotated inside its hierarchy and
     its bounding box overhangs the wings by 6%, so a box would put the lights in mid-air.
     Parts that SPIN are left out - anything named like a prop or rotor (the same test the page's findProps uses to
     turn them): a light on a blade tip would hang in the air while the blade goes round. */
  var SPINNING = /prop|rotor/i;
  function spins(o, model) {
    for (var q = o; q && q !== model; q = q.parent) if (q.name && SPINNING.test(q.name)) return true;
    return false;
  }
  function collect(THREE, model, frame) {
    var inv = new THREE.Matrix4().copy(frame.matrixWorld).invert(), m = new THREE.Matrix4(), v = new THREE.Vector3();
    var total = 0;
    function use(o) { return o.isMesh && o.geometry && o.geometry.attributes.position && !spins(o, model); }
    model.traverse(function (o) { if (use(o)) total += o.geometry.attributes.position.count; });
    var out = new Float32Array(total * 3), k = 0;
    model.traverse(function (o) {
      if (!use(o)) return;
      var a = o.geometry.attributes.position;
      m.multiplyMatrices(inv, o.matrixWorld);
      for (var i = 0; i < a.count; i++) {
        v.fromBufferAttribute(a, i).applyMatrix4(m);
        out[k++] = v.x; out[k++] = v.y; out[k++] = v.z;
      }
    });
    return out;
  }

  // A soft round glow, drawn once.
  var _tex = null;
  function glowTexture(THREE) {
    if (_tex) return _tex;
    var c = document.createElement('canvas'); c.width = c.height = 64;
    var g = c.getContext('2d'), gr = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    gr.addColorStop(0, 'rgba(255,255,255,1)'); gr.addColorStop(0.25, 'rgba(255,255,255,0.9)');
    gr.addColorStop(0.5, 'rgba(255,255,255,0.3)'); gr.addColorStop(1, 'rgba(255,255,255,0)');
    g.fillStyle = gr; g.fillRect(0, 0, 64, 64);
    _tex = new THREE.CanvasTexture(c);
    return _tex;
  }

  /* The lights as one Group, in the same frame as the anchors. Sizes are in screen pixels (not metres), so the
     lights stay visible as points when the camera is far back - which is how you see aircraft at night.
     Each light sits a hair outside the surface so it is not half-buried in the wingtip or the fin. */
  var COL = { red: 0xff2a1a, green: 0x2aff5a, white: 0xffffff };
  function build(THREE, a, opts) {
    opts = opts || {};
    var px = (opts.pixelRatio || 1), grp = new THREE.Group(); grp.name = 'navLights';
    if (!a) return { group: grp, set: function () {} };
    var span = (a.left && a.right) ? (a.right[0] - a.left[0]) : 10, nudge = Math.max(0.05, span * 0.01);
    function pts(list, color, size) {
      var g = new THREE.BufferGeometry();
      g.setAttribute('position', new THREE.Float32BufferAttribute([].concat.apply([], list), 3));
      var mt = new THREE.PointsMaterial({ color: color, size: size * px, sizeAttenuation: false, map: glowTexture(THREE),
        transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
      var o = new THREE.Points(g, mt); o.frustumCulled = false; o.renderOrder = 10; grp.add(o); return o;
    }
    var L = a.left && [a.left[0] - nudge, a.left[1], a.left[2]], R = a.right && [a.right[0] + nudge, a.right[1], a.right[2]];
    var red = L ? pts([L], COL.red, 12) : null, green = R ? pts([R], COL.green, 12) : null;
    var strobe = (L && R) ? pts([L, R], COL.white, 26) : null;
    var beacon = a.tail ? pts([[a.tail[0], a.tail[1] + nudge, a.tail[2]]], COL.red, 20) : null;
    function set(st, visible) {
      grp.visible = !!visible;
      if (!visible) return;
      if (strobe) strobe.visible = !!st.strobe;
      if (beacon) beacon.visible = !!st.beacon;
    }
    return { group: grp, set: set, parts: { red: red, green: green, strobe: strobe, beacon: beacon } };
  }

  var API = { isHeli: isHeli, anchors: anchors, state: state, collect: collect, build: build,
              CYCLE: CYCLE, TAIL_FRAC: TAIL_FRAC, TAIL_FRAC_HELI: TAIL_FRAC_HELI };
  root.NavLights = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : this);

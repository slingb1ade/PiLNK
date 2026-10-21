/* PiLNK - runway and approach lights that look like lights (AJ, 20 Oct 2026). 3D Approach first; 3D Sky can reuse it.
 *
 * The old lights were plain WebGL points: squares, sized in metres, so up close a row of them ran together into solid
 * white bars. These are round glows with a bright core, sized on screen (a pinpoint far out, separate dots close in),
 * and they behave like the real thing:
 *   - PAPI      four lights abeam the touchdown zone that read the aircraft's actual angle: 2 white / 2 red on a 3 deg
 *               slope, all white high, all red low, with the short pink blend real units have at each switch angle.
 *   - direction approach lights are aimed down the approach: full strength from the approach, fading off to the side,
 *               so the tower and orbit views no longer see them at full blaze. Edge lights shine along the runway.
 *   - rabbit    the sequenced flashers run from the outermost bar to the threshold about once a second, each one a
 *               short bright burst that dies away fast, so it reads as flashes running in rather than a smear.
 *   - halo      a faint glow round the threshold bar and the flashers only.
 * The pure parts (papi, elevAngle, rabbit, dirGain, pxSize) are what the shader does, in JS, so they can be tested.
 * Frame: the page's light frame - x east, y up, z south, metres.
 */
(function (root) {
  'use strict';
  var D2R = Math.PI / 180;

  // PAPI settings, light nearest the runway first (it is set highest). On a 3 deg path: inner two red, outer two white.
  var ANGLES = [3.5, 3 + 1 / 6, 2 + 5 / 6, 2.5];
  var TRANS = 0.05;   // deg either side of a setting: the pink band (real units: about 3 minutes of arc)

  function elevAngle(heightM, distM) {
    var h = Number(heightM), d = Number(distM);
    if (!isFinite(h) || !isFinite(d) || d < 1) return null;   // passed the PAPI, or sitting on it
    return Math.atan2(h, d) / D2R;
  }
  // -> [w0..w3] whiteness 0 (red) .. 1 (white), inner light first; null when there is no angle
  function papi(angleDeg) {
    if (angleDeg == null) return null;
    var a = Number(angleDeg);
    if (!isFinite(a)) return null;
    return ANGLES.map(function (s) {
      var w = (a - s + TRANS) / (2 * TRANS);
      return w <= 0 ? 0 : w >= 1 ? 1 : w;
    });
  }
  var RED = [1, 0.16, 0.1], WHITE = [1, 1, 1];
  function papiRGB(w) {
    var k = Math.max(0, Math.min(1, Number(w) || 0));
    return [RED[0] + (WHITE[0] - RED[0]) * k, RED[1] + (WHITE[1] - RED[1]) * k, RED[2] + (WHITE[2] - RED[2]) * k];
  }

  // The rabbit. One cycle: a SWEEP from the outermost flasher (index 0) to the innermost, then dark until CYCLE.
  // Each flasher fires once per cycle and decays with time constant TAU: a bright head and a short tail.
  var CYCLE = 1.0, SWEEP = 0.55, TAU = 0.045;
  function rabbit(tSec, n) {
    n = Math.max(0, n | 0);
    var out = new Float32Array(n), t = Number(tSec);
    if (!isFinite(t)) t = 0;
    var c = ((t % CYCLE) + CYCLE) % CYCLE;
    for (var k = 0; k < n; k++) {
      var tk = n > 1 ? SWEEP * k / (n - 1) : 0, dt = c - tk;
      out[k] = (dt >= 0 && dt < 5 * TAU) ? Math.exp(-dt / TAU) : 0;
    }
    return out;
  }

  // Directional strength from the cosine between a light's beam and the way to the eye (horizontal).
  // Full inside ~12 deg, gone to the floor by 70 deg - the GLSL in VS below is the same formula.
  var C_FULL = Math.cos(12 * D2R), C_NONE = Math.cos(70 * D2R);
  function dirGain(cosOff, floor) {
    var x = (Number(cosOff) - C_NONE) / (C_FULL - C_NONE);
    x = x <= 0 ? 0 : x >= 1 ? 1 : x;
    return floor + (1 - floor) * Math.pow(x, 1.5);
  }

  // On-screen size in pixels: `base` at REF metres, shrinking with the square root of distance (lights stay visible far
  // out, which a 1/d point would not), never under MIN_PX and never over base * MAX_GROW. Same formula as the shader.
  var REF = 600, MIN_PX = 2, MAX_GROW = 1.6;   // MIN_PX 2: a whole 2x2 block wherever it lands, so far lights do not twinkle
  function pxSize(base, distM, pixelRatio) {
    var px = pixelRatio || 1, d = Math.max(1, Number(distM) || REF);
    var s = base * Math.sqrt(REF / d);
    return px * Math.max(MIN_PX, Math.min(base * MAX_GROW, s));
  }

  // Kinds: 0 aimed down the approach (approach lights, threshold, PAPI), 1 along the runway both ways (edge), 2 all round
  var KIND = { approach: 0, edge: 1, omni: 2 };
  var FLOOR = [0.15, 0.3, 1];

  var VS = [
    'attribute vec3 color; attribute float aSize; attribute float aKind; attribute float aHalo; attribute float aFlash;',
    'uniform vec3 uCam; uniform float uCamOk; uniform vec2 uBeam; uniform float uPx;',
    'varying vec3 vCol; varying float vGain; varying float vHalo; varying float vFlash; varying float vPx;',
    'const float REF=' + REF.toFixed(1) + '; const float MIN_PX=' + MIN_PX.toFixed(2) + '; const float MAX_GROW=' + MAX_GROW.toFixed(2) + ';',
    'const float C_FULL=' + C_FULL.toFixed(6) + '; const float C_NONE=' + C_NONE.toFixed(6) + ';',
    'float dirGain(float c, float fl){ float x=clamp((c-C_NONE)/(C_FULL-C_NONE),0.0,1.0); return fl+(1.0-fl)*pow(x,1.5); }',
    'void main(){',
    '  vec3 toEye = uCam - position; float d = uCamOk > 0.5 ? max(1.0, length(toEye)) : REF;',
    '  float g = 1.0; vec2 h = toEye.xz; float hl = length(h);',
    '  if (uCamOk > 0.5 && hl > 0.001) { vec2 hn = h / hl; float c = dot(hn, uBeam);',
    '    if (aKind < 0.5) g = dirGain(c, ' + FLOOR[0].toFixed(2) + ');',
    '    else if (aKind < 1.5) g = dirGain(abs(c), ' + FLOOR[1].toFixed(2) + '); }',
    '  float s = max(MIN_PX, min(aSize * MAX_GROW, aSize * sqrt(REF / d)));',
    '  s *= 1.0 + 0.9 * max(aHalo, aFlash);',            // room for the glow round the bright ones
    '  gl_PointSize = s * uPx; vPx = s * uPx;',
    '  vCol = color; vGain = g; vHalo = aHalo; vFlash = aFlash;',
    '  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);',
    '}'].join('\n');
  var FS = [
    'uniform float uOpacity; varying vec3 vCol; varying float vGain; varying float vHalo; varying float vFlash; varying float vPx;',
    'void main(){',
    '  float d = length(gl_PointCoord - vec2(0.5)) * 2.0; if (d > 1.0 && vPx > 3.0) discard;',   // a far pinpoint stays a whole block
    '  float glow = max(vHalo, vFlash);',
    '  float coreR = mix(0.5, 0.3, glow);',                // the core is a smaller share of a haloed sprite
    '  float core = 1.0 - smoothstep(coreR * 0.35, coreR, d);',
    '  float soft = pow(1.0 - d, 2.2) * (0.25 + 0.55 * glow);',
    '  vec3 col = mix(vCol, vec3(0.86, 0.93, 1.0), vFlash) * (1.0 + 1.6 * vFlash);',
    '  float prof = clamp(core + soft, 0.0, 1.0);',
    '  prof = mix(1.0, prof, smoothstep(3.0, 5.0, vPx));',   // a pinpoint a pixel or two across is drawn solid, or it would vanish
    '  float a = prof * vGain * uOpacity;',
    '  gl_FragColor = vec4(col, a);',
    '}'].join('\n');

  /* spec: { pos:[x,y,z...], col:[r,g,b...], size:number|[...], kind:number|[...], halo:number|[...] }
     opts: { pixelRatio, opacity } -> { points, setCamera(eye|null), setBeam(x,z), setFlash(idx[], levels[]),
     setColor(i,[r,g,b]), setOpacity(o) } */
  function build(THREE, spec, opts) {
    opts = opts || {};
    var n = Math.floor((spec.pos || []).length / 3);
    function per(v, dflt) { var a = new Float32Array(n); for (var i = 0; i < n; i++) a[i] = Array.isArray(v) || (v && v.length != null) ? (v[i] != null ? v[i] : dflt) : (v != null ? v : dflt); return a; }
    var g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(spec.pos, 3));
    g.setAttribute('color', new THREE.Float32BufferAttribute(spec.col, 3));
    g.setAttribute('aSize', new THREE.BufferAttribute(per(spec.size, 8), 1));
    g.setAttribute('aKind', new THREE.BufferAttribute(per(spec.kind, 0), 1));
    g.setAttribute('aHalo', new THREE.BufferAttribute(per(spec.halo, 0), 1));
    g.setAttribute('aFlash', new THREE.BufferAttribute(new Float32Array(n), 1));
    var u = {
      uCam: { value: new THREE.Vector3() }, uCamOk: { value: 0 }, uBeam: { value: new THREE.Vector2(0, 1) },
      uPx: { value: opts.pixelRatio || 1 }, uOpacity: { value: opts.opacity != null ? opts.opacity : 1 }
    };
    var m = new THREE.ShaderMaterial({ vertexShader: VS, fragmentShader: FS, uniforms: u, transparent: true,
      depthWrite: false, blending: THREE.AdditiveBlending });
    var p = new THREE.Points(g, m); p.frustumCulled = false;
    var flash = g.getAttribute('aFlash'), colr = g.getAttribute('color');
    return {
      points: p,
      setCamera: function (e) { if (e && isFinite(e[0]) && isFinite(e[1]) && isFinite(e[2])) { u.uCam.value.set(e[0], e[1], e[2]); u.uCamOk.value = 1; } else u.uCamOk.value = 0; },
      setBeam: function (x, z) { var l = Math.hypot(x, z) || 1; u.uBeam.value.set(x / l, z / l); },
      setFlash: function (idx, lv) {   // uploads only when something changed: dark for ~45% of every cycle
        var ch = false;
        for (var k = 0; k < idx.length; k++) { var v = lv[k] || 0; if (flash.array[idx[k]] !== v) { flash.array[idx[k]] = v; ch = true; } }
        if (ch) flash.needsUpdate = true;
      },
      setColor: function (i, c) { colr.array[i * 3] = c[0]; colr.array[i * 3 + 1] = c[1]; colr.array[i * 3 + 2] = c[2]; colr.needsUpdate = true; },
      setOpacity: function (o) { u.uOpacity.value = o; }
    };
  }

  var API = { ANGLES: ANGLES, TRANS: TRANS, CYCLE: CYCLE, SWEEP: SWEEP, TAU: TAU, REF: REF, MIN_PX: MIN_PX, MAX_GROW: MAX_GROW,
              KIND: KIND, FLOOR: FLOOR, elevAngle: elevAngle, papi: papi, papiRGB: papiRGB, rabbit: rabbit, dirGain: dirGain,
              pxSize: pxSize, build: build };
  root.RunwayLights = API;
  if (typeof module !== 'undefined' && module.exports) module.exports = API;
})(typeof window !== 'undefined' ? window : this);

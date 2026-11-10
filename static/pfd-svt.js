/* PFD synthetic vision for 3D Approach (AJ, 9 Nov 2026). Spec: pilnk-tasks/pfd-spec.md section 4.
   Its own small three.js scene in the PFD's own canvas - not a second map. Terrain is a mesh built from the same AWS
   terrarium tiles the page already loads (through the page's tile cache), at DEM zoom 11 on a ~250 m grid, 60 km square
   round the aircraft, rebuilt in the background once the aircraft is 10 km from its centre. Coloured by height, then
   the terrain-alert bands against the aircraft's altitude: red at or above it (or within 100 ft below), yellow within
   1,000 ft below; water (0 m or less) always blue. Drawn at true scale in a flat local frame (x east, y up, z south,
   metres, origin at the mesh centre): over 30 km the earth's curve is ~70 m, inside the fog.
   The pure helpers below load under node for the tests; create() needs a browser. */
(function (root) {
  'use strict';
  const C = { Z: 11, SPAN_M: 60000, STEP_M: 250, REBUILD_M: 10000, HFOV: 40, RED_FT: 100, YELLOW_FT: 1000,
    FOG0_M: 15000, FOG1_M: 29000, ROWS: 20, MAX_FETCH: 4, RWY_W_M: 45, RETRY_MS: 5000, RETRY_MAX_MS: 60000 };
  const D2R = Math.PI / 180, M_PER_DEG = 111320, FT = 0.3048;

  // terrarium: height = R*256 + G + B/256 - 32768 (metres)
  function heightsFromRGBA(data, w, h) {
    const out = new Float32Array(w * h);
    for (let p = 0, i = 0; p < w * h; p++, i += 4) out[p] = data[i] * 256 + data[i + 1] + data[i + 2] / 256 - 32768;
    return out;
  }
  const tileURL = (z, x, y) => 'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/' + z + '/' + x + '/' + y + '.png';

  // Web Mercator, in global pixels at zoom z (256-px tiles); x unwrapped, y clamped to the map's latitude limit
  function gpx(lat, lon, z) {
    const N = 256 * Math.pow(2, z), la = Math.max(-85.0511, Math.min(85.0511, lat)) * D2R;
    return { x: (lon + 180) / 360 * N, y: (1 - Math.log(Math.tan(la) + 1 / Math.cos(la)) / Math.PI) / 2 * N };
  }
  // the tiles covering a square of spanM centred on lat/lon; x wraps across the antimeridian
  function tilesFor(lat, lon, spanM, z) {
    const n = Math.pow(2, z), half = spanM / 2;
    const dLat = half / M_PER_DEG, dLon = half / (M_PER_DEG * Math.cos(lat * D2R));
    const nw = gpx(lat + dLat, lon - dLon, z), se = gpx(lat - dLat, lon + dLon, z);
    const x0 = Math.floor(nw.x / 256), x1 = Math.floor(se.x / 256);
    const y0 = Math.max(0, Math.floor(nw.y / 256)), y1 = Math.min(n - 1, Math.floor(se.y / 256));
    const out = [], seen = new Set();
    for (let y = y0; y <= y1; y++) for (let xi = x0; xi <= x1; xi++) {
      const x = ((xi % n) + n) % n, k = x + '/' + y;
      if (!seen.has(k)) { seen.add(k); out.push({ x, y }); }
    }
    return out;
  }
  // heightAt(lat, lon): bilinear between pixel centres of the loaded tiles; a tile that is not loaded counts as 0 m
  function makeSampler(tiles, z) {
    const n = Math.pow(2, z), byNum = new Map();
    // numeric keys, indexed once: the build samples ~60,000 points x 4 pixels, and a string key per pixel is garbage
    for (const [k, t] of tiles) { const p = String(k).split('/'); if (+p[0] === z) byNum.set(+p[2] * n + +p[1], t); }
    let lastK = -1, lastT = null;
    function px(gx, gy) {
      const tx = Math.floor(gx / 256), ty = Math.floor(gy / 256), k = ty * n + (((tx % n) + n) % n);
      const t = k === lastK ? lastT : (lastK = k, lastT = byNum.get(k) || null);
      if (!t) return 0;
      const c = Math.min(t.w - 1, Math.max(0, gx - tx * 256)), r = Math.min(t.h - 1, Math.max(0, gy - ty * 256));
      const v = t.hts[r * t.w + c];
      return isFinite(v) ? v : 0;
    }
    return function heightAt(lat, lon) {
      const g = gpx(lat, lon, z), fx = g.x - 0.5, fy = g.y - 0.5;
      const x0 = Math.floor(fx), y0 = Math.floor(fy), ax = fx - x0, ay = fy - y0;
      const top = px(x0, y0) * (1 - ax) + px(x0 + 1, y0) * ax, bot = px(x0, y0 + 1) * (1 - ax) + px(x0 + 1, y0 + 1) * ax;
      return top * (1 - ay) + bot * ay;
    };
  }
  function localXZ(lat0, lon0, lat, lon) { return { x: (lon - lon0) * M_PER_DEG * Math.cos(lat0 * D2R), z: -(lat - lat0) * M_PER_DEG }; }

  /* The terrain grid: n x n vertices, row 0 along the north edge, x east, z south, y = height. Two triangles a cell,
     wound counter-clockwise seen from above. Rows are filled by fillRows so the sliced build can share the code. */
  function gridArrays(lat0, lon0, spanM, stepM) {
    const n = Math.round(spanM / stepM) + 1;
    const idx = new Uint32Array((n - 1) * (n - 1) * 6);
    for (let j = 0, k = 0; j < n - 1; j++) for (let i = 0; i < n - 1; i++) {
      const a = j * n + i, b = a + 1, c = a + n, d = c + 1;
      idx[k++] = a; idx[k++] = c; idx[k++] = b; idx[k++] = b; idx[k++] = c; idx[k++] = d;
    }
    return { n, lat0, lon0, spanM, stepM, pos: new Float32Array(n * n * 3), hts: new Float32Array(n * n), idx };
  }
  function fillRows(g, heightAt, j0, j1) {
    const half = g.spanM / 2, mLon = M_PER_DEG * Math.cos(g.lat0 * D2R);
    for (let j = j0; j < j1; j++) {
      const z = -half + j * g.stepM, lat = g.lat0 - z / M_PER_DEG;
      for (let i = 0; i < g.n; i++) {
        const x = -half + i * g.stepM, h = heightAt(lat, g.lon0 + x / mLon), k = j * g.n + i;
        g.pos[k * 3] = x; g.pos[k * 3 + 1] = h; g.pos[k * 3 + 2] = z; g.hts[k] = h;
      }
    }
  }
  function buildGrid(heightAt, lat0, lon0, spanM, stepM) { const g = gridArrays(lat0, lon0, spanM, stepM); fillRows(g, heightAt, 0, g.n); return g; }
  // the same grid, ROWS rows at a time, yielding to the browser between slices so a rebuild never stalls a frame
  async function buildGridSliced(heightAt, lat0, lon0, spanM, stepM, onSlice) {
    const g = gridArrays(lat0, lon0, spanM, stepM), clock = (typeof performance !== 'undefined') ? performance : Date;
    for (let j = 0; j < g.n; j += C.ROWS) {
      const t0 = clock.now();
      fillRows(g, heightAt, j, Math.min(g.n, j + C.ROWS));
      if (onSlice) onSlice(clock.now() - t0);   // how long this slice held the main thread, ms
      await new Promise(r => setTimeout(r, 0));
    }
    return g;
  }

  // terrain-alert band of one height against the aircraft's altitude (metres). Water always water; no bands on the ground.
  function band(hM, acAltM, onGround) {
    if (hM <= 0) return 'water';
    if (onGround) return 'terrain';
    if (hM >= acAltM - C.RED_FT * FT) return 'red';
    if (hM >= acAltM - C.YELLOW_FT * FT) return 'yellow';
    return 'terrain';
  }
  function needsRebuild(centre, lat, lon) { const p = localXZ(centre.lat, centre.lon, lat, lon); return Math.hypot(p.x, p.z) > C.REBUILD_M; }

  // the camera at the aircraft: heading 0 looks north (-z), pitch up is +, right bank tips the camera's up to the right
  function cameraPose(THREE, cam, p) {
    cam.position.set(p.x, p.altM, p.z);
    cam.rotation.order = 'YXZ';
    cam.rotation.set((p.pitch || 0) * D2R, -(p.hdg || 0) * D2R, -(p.bank || 0) * D2R);
  }

  /* Colours, linear 0-1, exactly as written to the canvas (tests read them back). Bands and water are drawn unlit so
     they read as warnings; ordinary terrain is shaded green -> brown -> grey by height and lit by a fixed sun. */
  C.COL = { red: [0.90, 0.10, 0.08], yellow: [0.95, 0.80, 0.10], water: [0.10, 0.25, 0.55], sky: [0.20, 0.45, 0.85],
    horizon: [0.62, 0.74, 0.90], low: [0.30, 0.45, 0.22], mid: [0.50, 0.40, 0.25], high: [0.66, 0.62, 0.56], runway: [0.36, 0.36, 0.39] };
  const v3 = c => 'vec3(' + c.map(x => x.toFixed(4)).join(',') + ')';
  const TERR_VS = 'attribute float aH; varying float vH; varying vec3 vW;' +
    ' void main(){ vH=aH; vec4 w=modelMatrix*vec4(position,1.0); vW=w.xyz; gl_Position=projectionMatrix*viewMatrix*w; }';
  const TERR_FS = 'uniform float uAcAlt; uniform float uBands; uniform float uNight; uniform float uRedM; uniform float uYelM;' +
    ' uniform float uFog0; uniform float uFog1; uniform vec3 uCam; varying float vH; varying vec3 vW;' +
    ' void main(){ vec3 col; float lit=0.0;' +
    '  if(vH<=0.0) col=' + v3(C.COL.water) + ';' +
    '  else if(uBands>0.5&&vH>=uAcAlt-uRedM) col=' + v3(C.COL.red) + ';' +
    '  else if(uBands>0.5&&vH>=uAcAlt-uYelM) col=' + v3(C.COL.yellow) + ';' +
    '  else { col=mix(' + v3(C.COL.low) + ',' + v3(C.COL.mid) + ',smoothstep(0.0,800.0,vH)); col=mix(col,' + v3(C.COL.high) + ',smoothstep(800.0,2500.0,vH)); lit=1.0; }' +
    '  if(lit>0.5){ vec3 n=normalize(cross(dFdx(vW),dFdy(vW))); if(n.y<0.0) n=-n; col*=0.55+0.45*max(0.0,dot(n,normalize(vec3(0.4,0.8,-0.45)))); }' +
    '  bool warn=(lit<0.5&&vH>0.0); col*=mix(1.0,warn?0.85:0.35,uNight);' +
    '  float f=smoothstep(uFog0,uFog1,distance(vW,uCam)); col=mix(col,' + v3(C.COL.horizon) + '*mix(1.0,0.3,uNight),f);' +
    '  gl_FragColor=vec4(col,1.0); }';
  const SKY_VS = 'varying vec3 vD; void main(){ vD=normalize(position); gl_Position=projectionMatrix*modelViewMatrix*vec4(position,1.0); }';
  const SKY_FS = 'uniform float uNight; varying vec3 vD; void main(){' +
    ' vec3 c=mix(' + v3(C.COL.horizon) + ',' + v3(C.COL.sky) + ',smoothstep(0.0,0.14,vD.y)); gl_FragColor=vec4(c*mix(1.0,0.3,uNight),1.0); }';

  /* create(THREE, canvas, {fetchTile(url) -> Promise<ArrayBuffer>, decode?}) -> svt, or null when WebGL cannot start.
     Tiles are fetched at most MAX_FETCH at a time, each once; the mesh is rebuilt in slices and swapped in one step,
     so the picture never stalls. A lost WebGL context (GPU reset, a phone tab put in the background) turns ok() false
     and every call into a no-op until the browser gives the context back. A tile whose request fails is asked again
     after RETRY_MS, doubling to RETRY_MAX_MS, and the mesh is rebuilt when it arrives (final review I-2, I-3). */
  async function decodeDefault(buf) {
    const bmp = await createImageBitmap(new Blob([buf]), { premultiplyAlpha: 'none', colorSpaceConversion: 'none' });
    const c = new OffscreenCanvas(bmp.width, bmp.height), x = c.getContext('2d', { willReadFrequently: true });
    x.drawImage(bmp, 0, 0);
    return { w: bmp.width, h: bmp.height, hts: heightsFromRGBA(x.getImageData(0, 0, bmp.width, bmp.height).data, bmp.width, bmp.height) };
  }
  function create(THREE, canvas, opts) {
    opts = opts || {};
    if (!THREE || !canvas || typeof opts.fetchTile !== 'function') return null;
    let renderer;
    try { renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false }); } catch (e) { return null; }
    if (!renderer || !renderer.getContext()) return null;
    const decode = opts.decode || decodeDefault;
    let lost = false, gone = false;
    const onLost = e => { e.preventDefault(); lost = true; };
    const onRestored = () => { if (gone) return; lost = false; pump(); if (pose) schedule(pose.lat, pose.lon); };   // three re-uploads the mesh itself
    canvas.addEventListener('webglcontextlost', onLost, false);
    canvas.addEventListener('webglcontextrestored', onRestored, false);

    const scene = new THREE.Scene(), cam = new THREE.PerspectiveCamera(30, 4 / 3, 5, 80000);
    const setFov = aspect => { cam.aspect = aspect; cam.fov = 2 * Math.atan(Math.tan(C.HFOV / 2 * D2R) / aspect) / D2R; cam.updateProjectionMatrix(); };
    setFov(4 / 3);
    const U = { uAcAlt: { value: 0 }, uBands: { value: 1 }, uNight: { value: 0 }, uRedM: { value: C.RED_FT * FT }, uYelM: { value: C.YELLOW_FT * FT },
      uFog0: { value: C.FOG0_M }, uFog1: { value: C.FOG1_M }, uCam: { value: new THREE.Vector3() } };
    const terrMat = new THREE.ShaderMaterial({ vertexShader: TERR_VS, fragmentShader: TERR_FS, uniforms: U, side: THREE.DoubleSide, extensions: { derivatives: true } });
    const skyU = { uNight: U.uNight };
    const sky = new THREE.Mesh(new THREE.SphereGeometry(70000, 32, 16),
      new THREE.ShaderMaterial({ vertexShader: SKY_VS, fragmentShader: SKY_FS, uniforms: skyU, side: THREE.BackSide, depthWrite: false }));
    sky.renderOrder = -1; scene.add(sky);
    const rwyMat = new THREE.MeshBasicMaterial({ color: new THREE.Color().fromArray(C.COL.runway), side: THREE.DoubleSide, polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -2 });
    const rwyGroup = new THREE.Group(); scene.add(rwyGroup);

    const tiles = new Map(), asked = new Set(), queue = [], failed = new Map();   // failed: key -> {n, at} (next try)
    let inflight = 0, inflightMax = 0, requests = 0, builds = 0, maxSliceMs = 0;
    let mesh = null, centre = null, sampler = null, target = null, building = false, pose = null, ends = [], rwyCount = 0;

    function pump() {
      while (!lost && !gone && inflight < C.MAX_FETCH && queue.length) {
        const k = queue.shift(), p = k.split('/');
        inflight++; inflightMax = Math.max(inflightMax, inflight); requests++;
        Promise.resolve().then(() => opts.fetchTile(tileURL(+p[0], +p[1], +p[2]))).then(decode)
          .then(t => { if (!(t && t.hts)) throw new Error('no heights'); tiles.set(k, t);
            if (failed.delete(k) && mesh && pose) schedule(pose.lat, pose.lon); },              // a retried tile filled a hole: rebuild
            () => { asked.delete(k); const f = failed.get(k) || { n: 0 }; f.n++;                 // counts as 0 m until it is asked again
              f.at = Date.now() + Math.min(C.RETRY_MAX_MS, C.RETRY_MS * Math.pow(2, f.n - 1)); failed.set(k, f); })
          .then(() => { inflight--; pump(); maybeBuild(); });
      }
    }
    function wanted(t) { return tilesFor(t.lat, t.lon, C.SPAN_M, C.Z).map(q => C.Z + '/' + q.x + '/' + q.y); }
    function schedule(lat, lon) {
      target = { lat, lon };
      const now = Date.now();
      for (const k of wanted(target)) if (!asked.has(k) && !(failed.has(k) && failed.get(k).at > now)) { asked.add(k); queue.push(k); }
      pump(); maybeBuild();
    }
    function retry() {    // failed tiles of the current square whose wait is over go back in the queue (no rebuild unless one arrives)
      if (!failed.size || !centre) return;
      const now = Date.now();
      for (const k of wanted(centre)) { const f = failed.get(k); if (f && f.at <= now && !asked.has(k)) { asked.add(k); queue.push(k); } }
      pump();
    }
    async function maybeBuild() {
      if (!target || building || lost || gone) return;
      const keys = wanted(target);
      if (keys.some(k => asked.has(k) && !tiles.has(k) && (queue.includes(k) || inflight > 0))) return;   // still loading
      if (!keys.some(k => tiles.has(k))) { target = null; return; }    // no tiles at all here: the flat horizon stays
      const t = target; target = null; building = true;
      const at = makeSampler(tiles, C.Z);
      const g = await buildGridSliced(at, t.lat, t.lon, C.SPAN_M, C.STEP_M, ms => { maxSliceMs = Math.max(maxSliceMs, ms); });
      building = false;
      if (lost || gone) return;
      const geo = new THREE.BufferGeometry();
      geo.setAttribute('position', new THREE.BufferAttribute(g.pos, 3));
      geo.setAttribute('aH', new THREE.BufferAttribute(g.hts, 1));
      geo.setIndex(new THREE.BufferAttribute(g.idx, 1));
      const m = new THREE.Mesh(geo, terrMat); m.frustumCulled = false;
      if (mesh) { scene.remove(mesh); mesh.geometry.dispose(); }
      mesh = m; scene.add(mesh); centre = { lat: t.lat, lon: t.lon }; sampler = at; builds++;
      buildRunways();
      if (pose && needsRebuild(centre, pose.lat, pose.lon)) schedule(pose.lat, pose.lon);
      if (target) maybeBuild();
    }
    function buildRunways() {
      for (const c of rwyGroup.children.slice()) { rwyGroup.remove(c); c.geometry.dispose(); }
      rwyCount = 0;
      if (!centre || !sampler) return;
      for (let i = 0; i + 1 < ends.length; i += 2) {
        const A = ends[i], B = ends[i + 1];
        if (!A || !B || typeof A.lat !== 'number' || typeof B.lat !== 'number') continue;
        const a = localXZ(centre.lat, centre.lon, A.lat, A.lon), b = localXZ(centre.lat, centre.lon, B.lat, B.lon);
        const dx = b.x - a.x, dz = b.z - a.z, L = Math.hypot(dx, dz); if (!(L > 1)) continue;
        const nx = -dz / L * C.RWY_W_M / 2, nz = dx / L * C.RWY_W_M / 2;
        const ha = sampler(A.lat, A.lon) + 2, hb = sampler(B.lat, B.lon) + 2;
        const geo = new THREE.BufferGeometry();
        geo.setAttribute('position', new THREE.Float32BufferAttribute([a.x + nx, ha, a.z + nz, a.x - nx, ha, a.z - nz, b.x + nx, hb, b.z + nz, b.x - nx, hb, b.z - nz], 3));
        geo.setIndex([0, 2, 1, 1, 2, 3]);
        rwyGroup.add(new THREE.Mesh(geo, rwyMat)); rwyCount++;
      }
    }
    function render() {
      if (lost || gone || !pose) return;
      let x = 0, z = 0, alt = pose.altM || 0;
      if (centre) { const p = localXZ(centre.lat, centre.lon, pose.lat, pose.lon); x = p.x; z = p.z; }
      if (sampler) alt = Math.max(alt, sampler(pose.lat, pose.lon) + 2);    // never under the ground: on the runway, eye height
      cameraPose(THREE, cam, { x, z, altM: alt, hdg: pose.hdg, pitch: pose.pitch, bank: pose.bank });
      sky.position.copy(cam.position); U.uCam.value.copy(cam.position);
      renderer.render(scene, cam);
    }
    return {
      ok: () => !lost && !gone,
      ready: () => !!mesh && !lost && !gone,
      setPose(p) {
        if (!p || typeof p.lat !== 'number' || typeof p.lon !== 'number') return;
        pose = p;
        if (lost || gone) return;
        const ref = target || centre;
        if (!ref || needsRebuild(ref, p.lat, p.lon)) schedule(p.lat, p.lon); else retry();
      },
      setBands(acAltM, onGround) { U.uAcAlt.value = typeof acAltM === 'number' && isFinite(acAltM) ? acAltM : -1e6; U.uBands.value = onGround ? 0 : 1; },
      setNight(b) { U.uNight.value = b ? 1 : 0; },
      setRunways(e) { ends = Array.isArray(e) ? e.slice() : []; buildRunways(); },
      setSize(w, h, dpr) { if (lost || gone) return; renderer.setPixelRatio(dpr || 1); renderer.setSize(w, h, false); setFov(w / h); },
      heightAt: (lat, lon) => (mesh && sampler) ? sampler(lat, lon) : null,
      render,
      stats: () => ({ requests, inflightMax, builds, runways: rwyCount, maxSliceMs, failed: failed.size }),
      probe(xf, yf) {
        if (lost || gone) return null;
        render();
        const gl = renderer.getContext(), w = gl.drawingBufferWidth, h = gl.drawingBufferHeight, px = new Uint8Array(4);
        gl.readPixels(Math.min(w - 1, Math.floor(xf * w)), Math.min(h - 1, Math.floor((1 - yf) * h)), 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
        return [px[0] / 255, px[1] / 255, px[2] / 255, px[3] / 255];
      },
      dispose() {
        if (gone) return; gone = true;
        canvas.removeEventListener('webglcontextlost', onLost, false);
        canvas.removeEventListener('webglcontextrestored', onRestored, false);
        if (mesh) mesh.geometry.dispose();
        for (const c of rwyGroup.children) c.geometry.dispose();
        sky.geometry.dispose(); terrMat.dispose(); rwyMat.dispose(); renderer.dispose();
      }
    };
  }

  const api = { C, heightsFromRGBA, tileURL, tilesFor, makeSampler, localXZ, buildGrid, buildGridSliced, band, needsRebuild, cameraPose, create };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.PFDSVT = api;
})(typeof window !== 'undefined' ? window : this);

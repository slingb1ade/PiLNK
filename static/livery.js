/* static/livery.js - loaded by approach.html after models.js. Uses that page's _q, D2R and tintMaterial, so it is
   not standalone: those must exist before paintAircraft is first called (they do - it runs on a model load).

   ---------- airline liveries + airframe detail: the painter (3D session, 16 Oct 2026) ----------
   Every model we ship is shape only - no UVs, no textures - so nothing can be painted ON it as it comes.
   The painter gives it both: it draws the aircraft as a picture in a canvas (a side view of each side, a
   plan view from above and from below) and projects that picture back onto the model. The page already
   knows which way each model faces (MODEL_ORIENT), and the fuselage is measured from the model at load,
   so one drawing routine fits the ATR, the A320, the 787 and the 777 alike.
   Two jobs:
     - an airline's scheme, matched by ICAO callsign prefix (ANZ123 -> ANZ); a per-type entry overrides the
       airline's default. Anything not listed keeps the fleet's neutral grey.
     - cabin windows, cockpit windows and door outlines on the airliner shapes in WINDOWED, livery or not.
       A grey aircraft keeps its grey: the picture is white there and only the windows and doors show.
   Colours and airframe detail only. No logos, wordmarks or emblems are drawn here.
   ?livery=ANZ forces a scheme onto whatever is on screen; ?livery=off turns schemes off (windows stay).
   Checked before going in by rendering the shipped models in WebGL (three r128, headless Chromium) with this file
   verbatim: ATR, A320, 787 and 777 in Air NZ, the A320 in both Jetstar schemes, the 787 grey, and every other
   airliner model under a forced livery for errors. */
const LIVERIES={
  /* Air NZ standard scheme (2012 on), matched to photos of the 787-9, A321neo, A320neo and ATR 72-600: white forward,
     the fin black, and the black carried forward over the whole rear fuselage, belly and tail cone included. Its front
     edge sweeps forward as it goes down - furthest forward at the belly (b0, fraction of length back from the nose),
     furthest back at the roof (b1). Engines and wings white. The fern drawn over the black, the koru and the titles
     are trade marks and are not drawn. Per-type lines: the black starts further forward on the shorter A320. */
  ANZ:{ body:'#eef0f2', belly:'#eef0f2', fin:'#111316', wing:'#d5d9dd', aft:{ col:'#111316', b0:0.58, b1:0.83 },   // 787, 777, A321neo
        A320:{ aft:{ col:'#111316', b0:0.50, b1:0.74 } }, A20N:{ aft:{ col:'#111316', b0:0.50, b1:0.74 } },
        AT76:{ aft:{ col:'#111316', b0:0.52, b1:0.78 } }, DH8C:{ aft:{ col:'#111316', b0:0.52, b1:0.78 } } },   // ATR: clear of the gear sponson
  JST:{ body:'#e6e9ec', belly:'#ff5a14', fin:'#5d636b', wing:'#b4b9bf',
        A21N:{ fin:'#ff5a14' } },      // the 2022 A321neo scheme carries the orange up onto the tail
  /* United States (main, 17 Oct): each airline's current standard scheme in body / belly / fin colours only - the
     painter has no stripes or gradients, so a striped or patterned tail takes its main colour. No titles or logos. */
  AAL:{ body:'#b8bfc6', belly:'#b8bfc6', fin:'#1c3f75', wing:'#a9b0b7' },   // American 2013: silver all over; flag tail -> its blue
  DAL:{ body:'#f2f4f6', belly:'#0a2a5c', fin:'#b5122c', wing:'#c9ced3' },   // Delta 2007: white over Delta blue; red tail
  UAL:{ body:'#f2f4f6', belly:'#8a9199', fin:'#0a2a6b', wing:'#c9ced3' },   // United 2019: white over Runway Gray; blue tail
  SWA:{ body:'#304cb2', belly:'#304cb2', fin:'#304cb2', wing:'#b9c0c7' },   // Southwest Heart 2014: Bold Blue; tail stripes -> blue
  ASA:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#01426a', wing:'#c9ced3' },   // Alaska 2016: white; Midnight tail
  JBU:{ body:'#f2f4f6', belly:'#003876', fin:'#1d4f9c', wing:'#c9ced3' },   // JetBlue 2014: white over blue; patterned tail -> blue
  HAL:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#4e2a84', wing:'#c9ced3' },   // Hawaiian: white; purple tail
  FDX:{ body:'#f2f4f6', belly:'#a7acb2', fin:'#4d148c', wing:'#c9ced3' },   // FedEx Express: white over grey; purple tail
  /* UPS 2003, from AJ's side-view drawing (A300-600): white forward, brown rear and tail. The brown's front edge runs the
     other way to Air NZ's - furthest forward at the roof (b1), furthest back at the belly (b0). The gold band along that
     edge and the shield are not drawn. Wings and engines white. */
  UPS:{ body:'#f4f5f6', belly:'#f4f5f6', fin:'#3a2619', wing:'#e3e6e9', aft:{ col:'#3a2619', b0:0.72, b1:0.34 } },
  /* Europe (main, 17 Oct): each airline's current standard scheme, same rules as the US list - body / belly / fin
     only, a striped or patterned tail takes its main colour, no titles, logos or emblems. Special and anniversary
     schemes (Lufthansa 100, the SAS cobalt A330, World Cup jets) are one-offs and keep the standard colours. */
  BAW:{ body:'#f2f4f6', belly:'#14264f', fin:'#0e2a66', wing:'#c9ced3' },   // British Airways Chatham: white over midnight blue; flag tail -> its blue
  DLH:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#05164d', wing:'#c9ced3' },   // Lufthansa 2018: white; deep blue tail
  AFR:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#002157', wing:'#c9ced3' },   // Air France: white; the tail's blue stripe, so it does not read all-white
  KLM:{ body:'#00a1de', belly:'#f2f4f6', fin:'#00a1de', wing:'#c9ced3' },   // KLM: sky blue over white; blue tail
  RYR:{ body:'#f2f4f6', belly:'#073590', fin:'#073590', wing:'#c9ced3' },   // Ryanair: white over dark blue; blue tail
  EZY:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#ff6600', wing:'#c9ced3' },   // easyJet: white; orange tail
  IBE:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#d7192d', wing:'#c9ced3' },   // Iberia 2013: white; red and yellow tail -> red
  SWR:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#e30613', wing:'#c9ced3' },   // Swiss: white; red tail (the cross is not drawn)
  AUA:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#e2001a', wing:'#c9ced3' },   // Austrian: white; red tail
  THY:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#c8102e', wing:'#c9ced3' },   // Turkish: white; red tail
  EWG:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#9b1b5a', wing:'#c9ced3' },   // Eurowings: white; burgundy tail
  LOT:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#11397e', wing:'#c9ced3' },   // LOT: white; navy tail
  ITY:{ body:'#0b2d6b', belly:'#0b2d6b', fin:'#0b2d6b', wing:'#c9ced3' },   // ITA Airways: Blu Italia all over
  ICE:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#003a7d', wing:'#c9ced3' },   // Icelandair: white; blue tail
  WZZ:{ body:'#f2f4f6', belly:'#f2f4f6', fin:'#c6007e', wing:'#c9ced3' }    // Wizz Air: white; magenta tail
};
/* Sister operators flying the parent's colours under their own callsign prefix: easyJet Europe and Switzerland; Ryanair's
   Buzz, Malta Air and Lauda; Lufthansa CityLine; KLM Cityhopper; BA CityFlyer and the BA Shuttle callsign. */
for(const [a,p] of [['EJU','EZY'],['EZS','EZY'],['RYS','RYR'],['MAY','RYR'],['LDM','RYR'],['CLH','DLH'],['KLC','KLM'],['CFE','BAW'],['SHT','BAW']]) LIVERIES[a]=LIVERIES[p];
function liveryFor(callsign,t){
  const f=(_q.get('livery')||'').toUpperCase();
  if(f==='OFF') return null;
  const op=f||((callsign||'').trim().toUpperCase().match(/^[A-Z]{3}(?=\d)/)||[''])[0];
  const L=LIVERIES[op]; if(!L) return null;
  const v=L[(t||'').toUpperCase()]; if(!v) return Object.assign({op,t},L);
  return Object.assign({op,t},L,v);
}
/* ---- the artwork slot ----
   Logos, titles and other artwork are not drawn by this file. They come from image files the node's owner puts in
   static/liveries/<OP>/ (OP: the ICAO prefix, ANZ), listed in art.json there - see static/liveries/README.md. Nothing
   there = colours only. Loaded once per operator, in the background: the aircraft is painted at once without them and
   repainted when they arrive. Items, all optional:
     fin    - on the fin, both sides. b:[from,to] along the length (fraction back from the nose), hc: centre height
              up the fin (0 the roof, 1 the fin tip).
     titles - on the fuselage, both sides. b:[from,to], yc: centre height up the fuselage (0 belly, 1 roof).
     aft    - like titles, for artwork over the rear fuselage.
   Height comes from the image's own shape, so nothing is stretched. Each item is drawn as it is on the left side and
   flipped on the right, so writing reads the right way on both ("right":"mirror" draws it mirrored instead).
   "types":{ "AT76":{ ... } } overrides items for one type. */
const _art=new Map(), _artDone=new Map();
function loadArt(op){
  if(!op) return Promise.resolve(null);
  if(_art.has(op)) return _art.get(op);
  const base='/static/liveries/'+op+'/';
  const p=fetch(base+'art.json',{cache:'no-cache'}).then(r=>r.ok?r.json():null).then(j=>{
    if(!j) return null;
    const files=new Set(); const walk=o=>{ for(const k in o){ const v=o[k]; if(v&&typeof v==='object'){ if(v.file) files.add(v.file); walk(v); } } }; walk(j);
    const imgs={};
    return Promise.all([...files].map(f=>new Promise(res=>{ const im=new Image(); im.onload=()=>{ imgs[f]=im; res(); }; im.onerror=()=>res(); im.src=base+f; })))
      .then(()=>({ spec:j, imgs }));
  }).catch(()=>null).then(a=>{ _artDone.set(op,a); return a; });
  _art.set(op,p); return p;
}
function artItems(art,t){ if(!art) return {}; const s=art.spec, o=Object.assign({},s); delete o.types;
  return Object.assign(o,(s.types||{})[(t||'').toUpperCase()]||{}); }
/* Models that need windows DRAWN: a passenger cabin, but no windows modelled. Each checked in a WebGL render before
   going on this list. Models with their own windows (A320, 777) are not on it - their glass is kept, see keep(). */
const WINDOWED={
  'b787.glb': { pitch:0.53, w:0.28, h:0.46, yc:0.60, cockpit:true,  doors:true  },   // no windows, doors or glass modelled at all
  /* The ATR's windows were only ever in its own texture (see TAKEOVER). yc 0.28: on the frames modelled into its skin. */
  'atr72.glb':{ pitch:0.76, w:0.30, h:0.40, yc:0.28, cockpit:false, doors:false },
  // the A380's windows were only in the texture it lost to TAKEOVER: two decks, so two rows (yc2: the upper deck)
  'a380_v2.glb':{ pitch:0.53, w:0.26, h:0.40, yc:0.38, yc2:0.74, cockpit:false, doors:true }
};
/* Models whose own textures carry ANOTHER operator's markings - names, titles, logos, a registration - greyed but still
   legible on every node: Bahamasair on the ATR, KLM on the 777, ANA on the 747, Airbus house colours on the A380. Found
   by rendering every textured airliner model grey (17 Oct). The painter takes these over even when they are grey, so
   the fleet's neutral look is neutral: plain skin, the model's modelled windows and glass kept, and drawn windows where
   WINDOWED says. The grey tone is matched to what each showed before (see tintMaterial). */
const TAKEOVER=new Set(['atr72.glb','b777_klm.glb','b747_100.glb','a380_v2.glb']);
const PAINT_GREY={ body:'#ffffff', belly:'#ffffff', fin:'#ffffff', wing:'#ffffff', op:'' };
const BELT=0.30;
/* toNonIndexed() is NOT used: in r128 it reads attribute.array directly, which is wrong for the interleaved buffers
   GLTFLoader makes when a file has a byte stride - it scrambled the ATR and the 777. This reads through getX/Y/Z/W. */
const _unshared=new WeakMap();
function unshare(g){
  if(_unshared.has(g)) return _unshared.get(g);       // several meshes can share one geometry
  const idx=g.index, n=idx.count, out=new THREE.BufferGeometry();
  for(const name in g.attributes){ const A=g.attributes[name], s=A.itemSize, arr=new Float32Array(n*s);
    for(let i=0;i<n;i++){ const j=idx.getX(i); arr[i*s]=A.getX(j); if(s>1)arr[i*s+1]=A.getY(j); if(s>2)arr[i*s+2]=A.getZ(j); if(s>3)arr[i*s+3]=A.getW(j); }
    out.setAttribute(name,new THREE.BufferAttribute(arr,s)); }
  for(const gr of g.groups) out.addGroup(gr.start,gr.count,gr.materialIndex);
  out.userData=g.userData; _unshared.set(g,out); g.dispose(); return out;
}
/* ---- measuring the airframe ----
   Everything in the model's OWN frame, turned so f runs nose-ward, l sideways (0 on the centreline) and y up.
   The fuselage is found by cutting the model with planes across it and keeping where the cut edges land near
   the centreline - a cut, not a vertex count, because the 787's fuselage is a long tube with no vertices at
   all between its ends. 99 planes give a profile along the whole length: the median of those 18-30% back
   from the nose sets the section (ahead of the wing on every airliner, behind the cockpit taper), and where
   the floor of the profile stays down marks the constant-section cabin the windows run along. */
function measureAirframe(root,key){
  const meshes=[]; root.traverse(o=>{ if(o.isMesh&&o.geometry&&o.geometry.attributes.position) meshes.push(o); });
  meshes.forEach(o=>{ if(o.geometry.index) o.geometry=unshare(o.geometry); });
  const yaw=((MODEL_ORIENT[key]||{}).yaw||0)*D2R, sy=Math.sin(yaw), cy=Math.cos(yaw);
  root.updateMatrixWorld(true);
  const inv=new THREE.Matrix4().copy(root.matrixWorld).invert(), rel=new THREE.Matrix4(), nm=new THREE.Matrix3(), v=new THREE.Vector3();
  const pts=[], nrm=[];      // per mesh: [f,l,y] per vertex, and the file's own normals turned the same way
  let fMin=1e9,fMax=-1e9,lMin=1e9,lMax=-1e9;
  for(const o of meshes){ rel.multiplyMatrices(inv,o.matrixWorld); nm.getNormalMatrix(rel);
    const P=o.geometry.attributes.position, N=o.geometry.attributes.normal, a=new Float32Array(P.count*3), b=N?new Float32Array(P.count*3):null;
    for(let i=0;i<P.count;i++){ v.fromBufferAttribute(P,i).applyMatrix4(rel);
      const f=v.x*sy-v.z*cy, l=v.x*cy+v.z*sy; a[i*3]=f; a[i*3+1]=l; a[i*3+2]=v.y;
      if(f<fMin)fMin=f; if(f>fMax)fMax=f; if(l<lMin)lMin=l; if(l>lMax)lMax=l;
      if(b){ v.fromBufferAttribute(N,i).applyMatrix3(nm).normalize(); b[i*3]=v.x*sy-v.z*cy; b[i*3+1]=v.x*cy+v.z*sy; b[i*3+2]=v.y; } }
    pts.push(a); nrm.push(b); }
  const len=fMax-fMin, lC=(lMin+lMax)/2, semi=(lMax-lMin)/2;   // not every model is built on its own centreline (the 777 is not)
  if(!(len>0&&semi>0)) return null;
  for(const a of pts) for(let i=1;i<a.length;i+=3) a[i]-=lC;
  const cuts=[]; for(let k=1;k<100;k++) cuts.push({back:k/100, f:fMax-k/100*len, top:-1e9, bot:1e9, hw:0});
  const step=len/100;
  pts.forEach(a=>{ const n=a.length/3;
    for(let t=0;t+2<n;t+=3){ const A=t*3;
      const lo=Math.min(a[A],a[A+3],a[A+6]), hi=Math.max(a[A],a[A+3],a[A+6]);
      const k0=Math.max(1,Math.ceil((fMax-hi)/step)), k1=Math.min(99,Math.floor((fMax-lo)/step));
      for(let k=k0;k<=k1;k++){ const c=cuts[k-1];
        for(const [p,q] of [[A,A+3],[A+3,A+6],[A+6,A]]){
          const dp=a[p]-c.f, dq=a[q]-c.f; if((dp>0)===(dq>0)||dp===dq) continue;
          const u=dp/(dp-dq), l=a[p+1]+(a[q+1]-a[p+1])*u, y=a[p+2]+(a[q+2]-a[p+2])*u;
          if(Math.abs(l)>0.15*semi) continue;     // an engine or the wing must not widen the "fuselage"
          if(y>c.top)c.top=y; if(y<c.bot)c.bot=y; if(Math.abs(l)>c.hw)c.hw=Math.abs(l); } } } });
  const sect=cuts.filter(c=>c.back>=0.18&&c.back<=0.30&&c.top>c.bot), med=k=>{ const s=sect.map(c=>c[k]).sort((x,y)=>x-y); return s[s.length>>1]; };
  if(!sect.length) return null;
  const yTop=med('top'), yBot=med('bot'), hw=med('hw'), H=yTop-yBot;
  // the cabin: the run of cuts, either side of the section, whose floor is still within 10% of the fuselage height
  let c0=sect[0].back, c1=sect[sect.length-1].back;
  for(let k=Math.round(c0*100)-1;k>=1;k--){ const c=cuts[k-1]; if(c.bot>yBot+0.10*H||c.top<yTop-0.05*H) break; c0=c.back; }
  for(let k=Math.round(c1*100)+1;k<=99;k++){ const c=cuts[k-1]; if(c.bot>yBot+0.10*H) break; c1=c.back; }
  let finTop=yTop; pts.forEach(a=>{ for(let i=0;i<a.length;i+=3) if(a[i+2]>finTop) finTop=a[i+2]; });
  /* finEnd: how far back the fin's root reaches - the furthest-back corner of any fin triangle low on the fin.
     Used to tell the fin's steep root (fin) from the tail cone's steep narrow tip behind it (not fin). */
  let finEnd=0.55;
  pts.forEach(a=>{ const n=a.length/3;
    for(let t=0;t+2<n;t+=3){ const i=t*3, f=(a[i]+a[i+3]+a[i+6])/3, l=Math.abs((a[i+1]+a[i+4]+a[i+7])/3), y=(a[i+2]+a[i+5]+a[i+8])/3;
      if(!(y>yTop+0.08*H&&(fMax-f)/len>0.55&&l<0.35*semi)) continue;
      for(let j=0;j<9;j+=3) if(a[i+j+2]<yTop+0.2*H){ const b=(fMax-a[i+j])/len; if(b>finEnd) finEnd=b; } } });
  const m=(MODEL_SPAN[key]||0)/(2*semi)||0;       // metres per model unit, from the real span; 0 = unknown
  return {meshes,pts,nrm,fMax,len,semi,yTop,yBot,hw,H,cab0:c0,cab1:c1,finTop,m,finEnd};
}
/* ---- the picture ----
   Atlas, top to bottom: the body from the left and from the right; the fin from the left and from the right; the
   plan from above and from below; and a strip of flat colour for parts no view suits (engine sides, leading edges).
   The fin has views of its own, not a corner of the body's: which triangles are fin is decided in 3D (finZone),
   so the fin's colour ends exactly where the fin meets the fuselage, and the body's never reaches the fin.
   Body views: x runs nose (left) to tail, y from just under the belly to the fin tip. Fin views: the back half
   only, from below the roof to the fin tip. A gap between panels stops one view bleeding into the next as
   mipmaps blur. 2048 rows in all - a power of two, which mipmapping wants on every GPU. */
function atlasLayout(W){ const k=W/4096, g=8, h={L:544,R:544,FL:272,FR:272,T:160,B:160,S:32}, rows={}; let y=0;
  for(const n of ['L','R','FL','FR','T','B','S']){ rows[n]=[y,Math.round(h[n]*k)]; y+=Math.round(h[n]*k)+g; }
  let H=1; while(H<y) H*=2; return {W,H,rows}; }
const FIN_B0=0.5;    // the fin views cover the back half of the aircraft
function finRange(A){ return [A.yTop-0.4*A.H, A.finTop+0.02*A.H]; }
function drawAtlas(A,liv,L){
  const c=document.createElement('canvas'); c.width=L.W; c.height=L.H; const x=c.getContext('2d');
  const W=L.W, len=A.len, yLo=A.yBot-0.06*A.H, yHi=A.finTop+0.02*A.H;
  const unit=A.m||(4/A.H);            // metres per model unit; unknown models assume a 4 m fuselage
  for(const side of ['L','R']){ const [r0,rh]=L.rows[side];
    const Xb=b=>b*W, Y=y=>r0+(yHi-y)/(yHi-yLo)*rh;
    x.fillStyle=liv.body; x.fillRect(0,r0,W,rh);
    x.fillStyle=liv.belly; x.fillRect(0,Y(A.yBot+BELT*A.H),W,r0+rh-Y(A.yBot+BELT*A.H));
    if(liv.aft){ x.fillStyle=liv.aft.col; x.beginPath(); x.moveTo(W,r0+rh); x.lineTo(Xb(aftB(liv.aft,0)),r0+rh);
      for(let k=0;k<=24;k++){ const yn=k/24; x.lineTo(Xb(aftB(liv.aft,yn)),Y(A.yBot+yn*A.H)); }
      x.lineTo(Xb(aftB(liv.aft,1)),r0); x.lineTo(W,r0); x.closePath(); x.fill(); }
    const S=A.windowed; if(!S) continue;
    const pxM=W/(len*unit), pyM=rh/((yHi-yLo)*unit);   // pixels per metre, along and up
    const D=A.H*unit, wy=Y(A.yBot+S.yc*A.H);
    const pitch=S.pitch*pxM, ww=S.w*pxM, wh=Math.max(2,S.h*pyM);
    const xs=Xb(A.cab0)+0.9*D*pxM, xe=Xb(A.cab1)-0.6*D*pxM;
    x.fillStyle='#1b222c';
    for(const yc of (S.yc2?[S.yc,S.yc2]:[S.yc])){ const wy2=Y(A.yBot+yc*A.H);
      for(let px=xs;px<=xe;px+=pitch){ rrect(x,px-ww/2,wy2-wh/2,ww,wh,Math.min(ww,wh)*0.4); x.fill(); } }
    if(S.doors){ // the first door just ahead of the window line, the last just behind it - outlines, as on the real thing
      x.strokeStyle='rgba(20,24,30,0.45)'; x.lineWidth=Math.max(1,0.05*pxM);
      const dw=0.85*pxM, dh=1.85*pyM, dt=wy-0.55*dh;
      for(const dx of [xs-0.75*D*pxM, xe+0.5*D*pxM]){ rrect(x,dx-dw/2,dt,dw,dh,dw*0.15); x.stroke(); } }
    if(S.cockpit){ // four panes on the nose, 0.45 to 0.92 fuselage-heights back from the tip, raked back along the top
      x.fillStyle='#161c24';
      const n0=Xb(0)+0.45*D*pxM, n1=Xb(0)+0.92*D*pxM, yb=Y(A.yBot+0.63*A.H), yt=Y(A.yBot+0.745*A.H), gap=0.06*pxM;
      const posts=[0,0.24,0.5,0.76,1], rake=0.10*D*pxM;
      for(let i=0;i<4;i++){ const a=n0+(n1-n0)*posts[i]+gap/2, b=n0+(n1-n0)*posts[i+1]-gap/2;
        x.beginPath(); x.moveTo(a,yb); x.lineTo(b,yb); x.lineTo(b-rake*0.35,yt); x.lineTo(a+(i?-rake*0.35:rake*0.6),yt); x.closePath(); x.fill(); } }
  }
  for(const v of ['FL','FR']){ const [r0,rh]=L.rows[v]; x.fillStyle=liv.fin; x.fillRect(0,r0,W,rh); }
  // the artwork slot: images over the paint, in the body views and the fin views
  if(liv.art){ const items=liv.art.items, imgs=liv.art.imgs, pxM=W/(len*unit);
    for(const side of ['L','R']) for(const name of ['titles','aft','fin']){ const it=items[name]; if(!it||!it.b||!imgs[it.file]) continue;
      const im=imgs[it.file], fin=name==='fin', [r0,rh]=L.rows[fin?'F'+side:side];
      const [lo,hi]=fin?finRange(A):[yLo,yHi], pyM=rh/((hi-lo)*unit);
      const Xb=fin?(b=>(b-FIN_B0)/(1-FIN_B0)*W):(b=>b*W), Y=y=>r0+(hi-y)/(hi-lo)*rh;
      const x0=Xb(it.b[0]), x1=Xb(it.b[1]), wM=(x1-x0)/(fin?W/(1-FIN_B0)/(len*unit):pxM), hpx=wM*im.height/im.width*pyM;
      const yc=fin?Y(A.yTop+(it.hc!=null?it.hc:0.5)*(A.finTop-A.yTop)):Y(A.yBot+(it.yc!=null?it.yc:0.5)*A.H);
      x.save(); x.beginPath(); x.rect(0,r0,W,rh); x.clip();
      if(side==='R'&&it.right!=='mirror'){ x.translate(x0+x1,0); x.scale(-1,1); }   // flipped back, so it reads the right way
      x.drawImage(im,x0,yc-hpx/2,x1-x0,hpx); x.restore(); } }
  for(const v of ['T','B']){ const [r0,rh]=L.rows[v];
    x.fillStyle=liv.wing; x.fillRect(0,r0,W,rh);
    const Yl=l=>r0+(l+A.semi)/(2*A.semi)*rh;
    x.fillStyle=v==='T'?liv.body:liv.belly; x.fillRect(0,Yl(-A.hw*1.1),W,Yl(A.hw*1.1)-Yl(-A.hw*1.1));
    if(liv.aft){ // seen from above or below, a point l off the centreline sits at the height the side view gives it there
      x.fillStyle=liv.aft.col; x.beginPath(); x.moveTo(W,Yl(-A.hw*1.1));
      for(let k=0;k<=24;k++){ const lr=-1+2*k/24, h=0.5*Math.sqrt(Math.max(0,1-lr*lr)), yn=v==='T'?0.5+h:0.5-h;
        x.lineTo(aftB(liv.aft,yn)*W,Yl(lr*A.hw*1.1)); }
      x.lineTo(W,Yl(A.hw*1.1)); x.closePath(); x.fill(); } }
  { const [r0,rh]=L.rows.S; x.fillStyle=liv.wing; x.fillRect(0,r0,W,rh); }
  return c;
}
/* The front edge of a scheme's rear colour, as a fraction of length back from the nose, at height yn (0 belly, 1 roof).
   Straight from belly to roof unless the scheme says otherwise (p>1 keeps it forward lower down, then sweeps back). */
function aftB(a,yn){ return a.b0+(a.b1-a.b0)*Math.pow(Math.min(1,Math.max(0,yn)),a.p||1); }
function rrect(x,a,b,w,h,r){ x.beginPath(); x.moveTo(a+r,b); x.arcTo(a+w,b,a+w,b+h,r); x.arcTo(a+w,b+h,a,b+h,r); x.arcTo(a,b+h,a,b,r); x.arcTo(a,b,a+w,b,r); x.closePath(); }
/* ---- projecting it back ----
   Each triangle picks ONE view, by what it is and which way it faces, and all three corners use that view -
   so nothing is stretched across two views. Fin: the fin view of its own side. Body: the body view of its own
   side, or the plan view when it faces mostly up or down (the crown, the belly). Wings, tailplane, engines: the
   plan views, or the flat strip when they face sideways. */
function paintAircraft(root,key,liv,renderer){
  const windowed=WINDOWED[key]||null;
  const old=root.userData.paintTex;
  const meshes=[]; root.traverse(o=>{ if(o.isMesh&&o.geometry&&o.geometry.attributes.position) meshes.push(o); });
  const mats=new Set(); meshes.forEach(o=>(Array.isArray(o.material)?o.material:[o.material]).forEach(mt=>mats.add(mt)));
  /* The model's own detail stays as it is: glass, windows, tyres, lights and anything near-black. Most of our models
     have their cabin windows, doors and cockpit glass modelled - the A320 and the ATR do - and painting over them
     would lose real detail to a drawing of it. Only the skin takes the picture. */
  const byName=mt=>/glass|window|windshield|clear|rubber|tyre|tire|light(red|green|white)|interior|cockpit/i.test(mt.name||'')||mt.transparent;
  const dark=mt=>{ const o=mt.userData._origCol||mt.color; return o?(0.3*o.r+0.59*o.g+0.11*o.b)<0.03:false; };
  // a model with its own textures keeps them when it is grey - the painter only takes it over for a livery, or TAKEOVER
  const ownMaps=[...mats].some(mt=>mt.userData._origMap);
  if(!liv&&!TAKEOVER.has(key)&&(!windowed||ownMaps)){
    if(old){ meshes.forEach(o=>{ const g=o.geometry; if(g.userData.origUV!==undefined){ if(g.userData.origUV) g.setAttribute('uv',g.userData.origUV); else g.deleteAttribute('uv'); delete g.userData.origUV; } });
      mats.forEach(mt=>{ mt.userData.paintMap=null; mt.userData.livery=false; mt.map=null; tintMaterial(mt); }); old.dispose(); root.userData.paintTex=null; }
    return null;
  }
  const A=measureAirframe(root,key); if(!A) return null;
  A.windowed=windowed;
  const maxT=renderer&&renderer.capabilities?renderer.capabilities.maxTextureSize:4096;
  const L=atlasLayout(Math.min(4096,maxT));
  if(liv&&_artDone.get(liv.op)) liv.art={ items:artItems(_artDone.get(liv.op),liv.t), imgs:_artDone.get(liv.op).imgs };
  const tex=new THREE.CanvasTexture(drawAtlas(A,liv||PAINT_GREY,L));
  if(liv&&!_artDone.has(liv.op)) loadArt(liv.op).then(art=>{     // first sight of this operator: repaint when its artwork arrives
    if(!art||root.userData.paintTex!==tex) return;              // none, or the aircraft has changed since
    liv.art={ items:artItems(art,liv.t), imgs:art.imgs }; tex.image=drawAtlas(A,liv,L); tex.needsUpdate=true; });
  tex.flipY=false; tex.encoding=THREE.sRGBEncoding;
  if(renderer&&renderer.capabilities) tex.anisotropy=Math.min(8,renderer.capabilities.getMaxAnisotropy());
  const yLo=A.yBot-0.06*A.H, yHi=A.finTop+0.02*A.H, [fLo,fHi]=finRange(A);
  A.meshes.forEach((o,k)=>{ const a=A.pts[k], nn=A.nrm[k], n=a.length/3, uv=new Float32Array(n*2), g=o.geometry;
    for(let t=0;t+2<n;t+=3){ const i=t*3;
      const f=(a[i]+a[i+3]+a[i+6])/3, ls=(a[i+1]+a[i+4]+a[i+7])/3, l=Math.abs(ls), y=(a[i+2]+a[i+5]+a[i+8])/3, back=(A.fMax-f)/A.len;
      let nl,ny;
      if(nn){ nl=(nn[i+1]+nn[i+4]+nn[i+7])/3; ny=(nn[i+2]+nn[i+5]+nn[i+8])/3; }
      else { const ux=a[i+3]-a[i],uy=a[i+4]-a[i+1],uz=a[i+5]-a[i+2], vx=a[i+6]-a[i],vy=a[i+7]-a[i+1],vz=a[i+8]-a[i+2];
        const cx=uy*vz-uz*vy, cy2=uz*vx-ux*vz, cz=ux*vy-uy*vx, m=Math.hypot(cx,cy2,cz)||1; nl=cy2/m; ny=cz/m; }
      /* finZone. Above the roof, in the back 45%, near the centreline: fin (a T-tail's tailplane goes with it). And the
         fin's ROOT: where the fin rises out of the tail cone the roof has already dropped below the section's roof,
         so a height test alone left a band of body colour at the foot of the fin. A thin, steep surface on the
         centreline, near the roof, no further back than the fin's root, is fin wherever it is. */
      const finRoot=back>0.6&&back<=A.finEnd+0.01&&l<0.15*A.hw&&Math.abs(nl)>0.85&&y>A.yTop-0.3*A.H;
      const fin=finRoot||(y>A.yTop+0.08*A.H&&back>0.55&&l<0.35*A.semi);
      const body=!fin&&l<=A.hw*1.1&&y>=A.yBot-0.05*A.H&&y<=A.yTop+0.08*A.H;
      let view;
      if(fin) view=ls<0?'FL':'FR';
      else if(body) view=Math.abs(ny)>0.8?(y>(A.yTop+A.yBot)/2?'T':'B'):(ls<0?'L':'R');
      else view=Math.abs(ny)>0.5?(ny>0?'T':'B'):'S';
      const [r0,rh]=L.rows[view];
      for(let j=0;j<3;j++){ const p=i+j*3; let u,vv;
        if(view==='L'||view==='R'){ u=(A.fMax-a[p])/A.len; vv=r0+(yHi-a[p+2])/(yHi-yLo)*rh; }
        else if(view==='FL'||view==='FR'){ u=((A.fMax-a[p])/A.len-FIN_B0)/(1-FIN_B0); vv=r0+(fHi-a[p+2])/(fHi-fLo)*rh; }
        else if(view==='S'){ u=0.5; vv=r0+rh/2; }
        else { u=(A.fMax-a[p])/A.len; vv=r0+(a[p+1]+A.semi)/(2*A.semi)*rh; }
        uv[(t+j)*2]=Math.min(1,Math.max(0,u)); uv[(t+j)*2+1]=Math.min(r0+rh,Math.max(r0,vv))/L.H; } }
    if(g.userData.origUV===undefined) g.userData.origUV=g.attributes.uv||null;
    g.setAttribute('uv',new THREE.BufferAttribute(uv,2)); });
  /* A near-black material is kept only where windows are (on the fuselage side, above the floor) or where the tyres and
     gear are (under the fuselage, inboard). Elsewhere it is paint - the registration under the 777's wing is near-black,
     and was being kept, letters and all. Decided by where most of that material's triangles sit. */
  const inBand=new Map(), triN=new Map();
  A.meshes.forEach((o,k)=>{ const a=A.pts[k], n=a.length/3, ms=Array.isArray(o.material)?o.material:[o.material], gr=o.geometry.groups;
    const matAt=t=>{ if(!Array.isArray(o.material)) return o.material; for(const g of gr) if(t*3>=g.start&&t*3<g.start+g.count) return ms[g.materialIndex]; return ms[0]; };
    for(let t=0;t+2<n;t+=3){ const i=t*3, l=Math.abs((a[i+1]+a[i+4]+a[i+7])/3), y=(a[i+2]+a[i+5]+a[i+8])/3;
      const mt=matAt(t/3); triN.set(mt,(triN.get(mt)||0)+1); if(!dark(mt)) continue; const c=inBand.get(mt)||[0,0]; c[1]++;
      if((l<=A.hw*1.15&&y>A.yBot+0.3*A.H&&y<A.yTop+0.05*A.H)||(y<A.yBot+0.05*A.H&&l<0.3*A.semi)) c[0]++;   // windows; or tyres and gear
      inBand.set(mt,c); } });
  const keep=mt=>byName(mt)||(dark(mt)&&(c=>c&&c[0]>=0.5*c[1])(inBand.get(mt)));
  /* A grey TAKEOVER model goes ONE tone, not a grey per material: its materials were the old operator's colours, and
     greying each one separately would leave that livery's pattern on in shades of grey. The tone is the one the model
     showed before, by what most of its skin was (most triangles): textured skin was drawn darker than plain. */
  const skin=[...mats].filter(mt=>!keep(mt)), area=f=>skin.filter(f).reduce((n,mt)=>n+(triN.get(mt)||0),0);
  const tone=(!liv&&TAKEOVER.has(key))?(area(mt=>mt.userData._origMap)>area(()=>true)/2?'tex':'flat'):null;   // by triangles, not by material count
  skin.forEach(mt=>{ mt.vertexColors=false; mt.userData.paintMap=tex; mt.userData.livery=!!liv; mt.userData.paintTone=tone; tintMaterial(mt); });
  root.userData.paintTex=tex; if(old) old.dispose();
  return A;
}

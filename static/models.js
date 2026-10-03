/* PiLNK aircraft model table — the ONE copy (1 Oct 2026).
   Loaded by static/approach.html (3D Approach) and static/sky3d.html (3D Sky). Until now each page kept its own
   copy of MODEL_ORIENT / MODEL_SPAN / modelFor, and the 30 Sep 180° fix had to be found twice. Change a model
   here and both views follow. Lifted verbatim from approach.html as of 1 Oct (bizjet, CRJ, P-8, T-38, Hawk). */

const MODEL_ORIENT={ 'a320_200.glb':{yaw:-90}, 'atr72.glb':{yaw:90}, 'b777_klm.glb':{yaw:180}, 'b737_800.glb':{yaw:180}, 'c172.glb':{yaw:180}, 'b429.glb':{yaw:180}, 'c208.glb':{yaw:90}, 'c130.glb':{yaw:180},
  /* Every yaw on the next two lines was MEASURED from the file, and every one shipped exactly 180° wrong
     until 30 Sep. The measuring method finds the tail fin reliably; it then mapped "nose is at +Z" to the
     wrong yaw. It was validated only on the span axis (9/9), never on direction — and the model inspector
     ignores MODEL_ORIENT, so eyeballing a model there proves nothing about which way it will fly. Caught
     when AJ clicked a departing 787. Re-checked against the eight models whose direction had been
     confirmed on live approaches: the old mapping scored 0/8, every one exactly 180° off; corrected, 8/8.
     ⚠️ When adding a model: confirm its direction on a LIVE aircraft, or with ?force= on a live one. */
  'b52.glb':{yaw:0}, 'e3.glb':{yaw:-90}, 'kc135.glb':{yaw:180}, 'kc46.glb':{yaw:180},
  'b787.glb':{yaw:180}, 'a350.glb':{yaw:180}, 'b1b.glb':{yaw:0}, 'c17.glb':{yaw:-90},
  'a400.glb':{yaw:0},
  'bizjet.glb':{yaw:180}, 'crj.glb':{yaw:180},
  'p8.glb':{yaw:0}, 't38.glb':{yaw:-90}, 'hawk.glb':{yaw:180},
  'e7.glb':{yaw:0} };   // measured, corrected mapping — confirm on a live aircraft before release (a400: confirmed 30 Sep)
/* Real wingspan in metres, per model. Every model is normalised to this at load, so a Cessna is drawn
   Cessna-sized next to a 747. Until now everything was normalised to a single 40 m span, which made an
   ATR 48% too big and a 777 38% too small — invisible in a chase shot where there is nothing to compare
   against, obvious the moment a light aircraft shares the frame with the buildings below it. */
const MODEL_SPAN={ 'c172.glb':11.0, 'b429.glb':13.0, 'c208.glb':15.9, 'atr72.glb':27.1, 'c130.glb':40.4, 'a320_200.glb':35.8, 'a320_v2.glb':35.8,
  'b737_800.glb':35.8, 'b738_v2.glb':35.8, 'a333_v2.glb':60.3, 'b747_100.glb':59.6,
  'b777_klm.glb':64.8, 'a380_v2.glb':79.8,
  'b52.glb':56.4, 'e3.glb':44.4, 'kc135.glb':39.9, 'kc46.glb':47.6,
  'b787.glb':60.1, 'a350.glb':64.8, 'b1b.glb':41.8, 'c17.glb':51.8, 'a400.glb':42.4,
  'bizjet.glb':16.2, 'crj.glb':24.9,
  'p8.glb':37.6, 't38.glb':7.7, 'hawk.glb':9.4,
  'e7.glb':34.4 };   // Phenom 300E and CRJ-900 as modelled; TYPE_SPAN resizes each type
/* ---------- true size: scale by the WINGS, not the longest side (1 Oct 2026) ----------
   Both pages used to divide MODEL_SPAN by max(size.x,size.z) and call it the span. For a long, thin jet that
   is the fuselage: the T-38 (14.1 m long, 7.7 m span) was drawn at 55% of its real size, the Hawk ~80%, the
   airliners 9–21% small. MODEL_ORIENT's yaw already says which way the nose runs in the file: along z at 0/180,
   along x at ±90 — so the wings are the other axis. A model with no ORIENT entry flies nose-along-z (yaw 0). */
function modelWingWidth(key,size){
  const y=Math.abs(((MODEL_ORIENT[key]||{}).yaw||0)%180);
  return (y===90?size.z:size.x)||Math.max(size.x,size.z)||1;
}
/* ---------- per-TYPE span (3D session, 1 Oct 2026) ----------
   MODEL_SPAN sizes a MODEL; one model stands in for many types, so every King Air, Saab 340 and Dash 8
   was drawn at the ATR's 27 m. A type listed here is drawn at its own real wingspan on whatever model it
   is routed to. Measured from the fleet's own last 30 days (aircraft_history, 9,105 airframes, 150 types):
   25% fell through to the catch-all 737 at the bottom of modelFor — 1,593 of them business jets and 496
   regional jets — so the entries below are ordered by what the fleet actually sees, not by what is easy.
   The catch-all deliberately IGNORES this table (see spanFor): a 16 m Phenom shrunk onto a 737 is not an
   improvement. */
const TYPE_SPAN={
  // twin turboprops on the ATR stand-in — previously all 27 m
  B350:17.65, BE20:16.61, BE30:16.61, BE9L:15.32, SF34:21.44, SB20:24.76, DH8A:25.91, DH8B:25.91, DH8C:27.43, DH8D:28.42,
  JS31:15.85, JS32:16.00, JS41:18.42, E120:19.78, D228:16.97, DHC6:19.81, F50:29.0, ATP:30.63, AT43:24.57, AT45:24.57, AT46:24.57,
  SW4:17.37, C441:15.04, C425:13.45,
  // piston singles & twins newly on the C172 stand-in
  DA42:13.55, BE58:11.53, PA31:12.40, PA44:11.77, P32R:11.02, PA46:13.11, C82T:11.0,
  // business jets on BIZJET_MODEL
  E55P:16.2, E50P:12.3, C56X:17.2, CL35:21.0, CL30:19.5, CL60:19.6, C68A:22.0, C700:20.7, C25A:15.1, C25B:16.3, C25C:15.5,
  C25M:14.4, C525:14.3, C510:13.2, C550:15.9, C560:16.3, C650:16.3, C680:19.3, C750:19.4, GLEX:28.7, GL5T:28.7, GL7T:31.7,
  GLF4:23.7, GLF5:28.5, GLF6:30.4, GA5C:26.5, GA6C:28.7, GA7C:31.4, GALX:17.7, G280:19.2, LJ35:12.0, LJ31:13.3, LJ45:14.6,
  LJ60:13.3, LJ75:15.5, H25B:15.7, BE40:13.3, F2TH:19.3, FA50:18.9, FA7X:26.2, FA6X:25.9, F900:19.3, FA20:16.3,
  E545:20.3, E550:20.3, E35L:21.2, PC24:17.0, SF50:11.8, HDJT:12.1,
  // regional jets — rear-engined on the CRJ, E-Jets on the A320 (see EJET below)
  E75L:26.0, E170:26.0, E190:28.7, E195:28.7, E290:33.7, E295:35.1, E145:20.0, E45X:20.0, CRJ2:21.2, CRJ7:23.2, CRJ9:24.9
};
/* Phenom 300E (ryan_cd) and CRJ-900 (CityJet Training), both CC-BY-4.0, added 1 Oct. One bizjet stands in
   for every rear-engined, T-tailed business jet from a Vision Jet to a Global 7500, each drawn at its own span. */
const BIZJET_MODEL='bizjet.glb', REGIONAL_MODEL='crj.glb';
const BIZJET=['E55P','E50P','C56X','CL35','CL30','CL60','C68A','C700','C25A','C25B','C25C','C25M','C525','C510','C550','C560','C650',
  'C680','C750','GLEX','GL5T','GL7T','GLF4','GLF5','GLF6','GA5C','GA6C','GA7C','GALX','G280','LJ35','LJ31','LJ45','LJ60','LJ75',
  'H25B','BE40','F2TH','FA50','FA7X','FA6X','F900','FA20','E545','E550','E35L','PC24','SF50','HDJT'];
/* The regionals are TWO shapes, and the split matters more than the model. A CRJ or an ERJ-145 has its
   engines on the rear fuselage and a T-tail — the CRJ is literally a stretched Challenger bizjet. An E-Jet
   (E170 to E195-E2) has its engines under the wing and a conventional tail: it is a small A320 to look at.
   So E-Jets go on the A320 at their own span, and only the rear-engined types go on the CRJ. */
const REGIONAL=['CRJ2','CRJ7','CRJ9','E145','E45X'];
const EJET=['E75L','E170','E190','E195','E290','E295'];
function spanFor(t,key){
  t=(t||'').toUpperCase();
  if(key==='b738_v2.glb'||key==='a320_v2.glb') return MODEL_SPAN[key]||null;   // the catch-all keeps its own size
  return TYPE_SPAN[t]||MODEL_SPAN[key]||null;
}
function modelFor(t,cat){ t=(t||'').toUpperCase();
  /* Single-engine turboprops get the Caravan; the twins and the regionals keep the ATR. */
  /* The Hercules. Flown by about seventy air forces, so on most nodes in the world it is the military
     transport most likely to appear — and PiLNK already watches for military traffic. */
  if(['C130','C30J','L100','C130J','LMT1','C130H'].includes(t))return'c130.glb';
  /* USAF types, for RAF Fairford and Mildenhall — these were all being drawn as 737s. The tanker
     pair is worth separating because they are the two aircraft a UK node sees most: the KC-135R is
     the narrow 1950s tube, the KC-46A is a 767. The Sentry carries its rotodome, which is the whole
     point of recognising it. The rotodome does not turn: that model is merged by material, not by
     part, so there is no dome node to rotate. */
  if(/^B52/.test(t))return'b52.glb';
  /* EXACT match, not a prefix. The B-1's ICAO type is plain 'B1', and /^B1/ would also swallow B190 —
     the Beech 1900, a common regional turboprop — and draw it as a supersonic bomber. Short type codes
     are the ones to check: a prefix test is only safe when nothing else starts the same way. */
  if(t==='B1'||t==='B1B')return'b1b.glb';
  if(t==='C17')return'c17.glb';          // NOT /^C17/ — that also matches C172, the Cessna 172
  if(t==='A400')return'a400.glb';        // exact: was falling through to the A320 fallback
  /* AUDIT FIX #5 (24 Sep 2026): was /^E3/, which also caught E35L (Embraer Legacy 600/650, a common
     business jet) and E390 (KC-390) and drew them with a rotodome. The Sentry's designators only. */
  if(['E3TF','E3CF','E3'].includes(t))return'e3.glb';
  /* 737 MAX: 477 airframes a month were reaching the catch-all because their codes start B3, not B7. It
     was still a 737, but the catch-all model, not the one whose orientation has been checked on a live
     aircraft. Exact codes, per the prefix rule. */
  if(['B37M','B38M','B39M','B3XM'].includes(t))return'b737_800.glb';
  if(t==='P8')return'p8.glb';            // P-8 Poseidon — RNZAF flies four from Ohakea; exact, no other code is just 'P8'
  if(t==='T38')return't38.glb';          // T-38 Talon, USAF trainer — exact
  if(t==='HAWK')return'hawk.glb';        // BAe Hawk, RAF/Red Arrows — exact
  /* E-7 Wedgetail (737-700 AEW&C), RAAF and now RAF from Lossiemouth. 'E737' is the ICAO Doc 8643 designator
     (doc8643.com/aircraft/E737). No node had reported one when this was added, and some databases list them as
     plain B737 — those still get the 737, the right airframe, just without the radar. Span 34.4 m: no winglets. */
  if(t==='E737')return'e7.glb';
  if(BIZJET_MODEL&&BIZJET.includes(t))return BIZJET_MODEL;
  if(REGIONAL_MODEL&&REGIONAL.includes(t))return REGIONAL_MODEL;
  if(EJET.includes(t))return'a320_200.glb';
  if(/^K46/.test(t))return'kc46.glb';
  if(/^(K35|C135|R135|W135|O135|E8)/.test(t))return'kc135.glb';
  const SETP=['C208','C206','C210','PC12','PC6','TBM7','TBM8','TBM9','P46T','EPIC','K100','AT802','AT502','AT504','C295','GA8','CC19','DHC2','DHC3','U206','C207'];
  if(SETP.includes(t))return'c208.glb';
  const TP=['AT72','AT73','AT75','AT76','AT43','AT45','AT46','DH8A','DH8B','DH8C','DH8D','SF34','SB20','JS31','JS32','JS41','B350','BE20','BE9L','E120','D228','DHC6','F50','ATP','BE30','SW4','C441','C425'];
  /* Light singles and twins. Drawn as a C172 — at the size these appear, a 172, a 152, a Cherokee and
     a Robin are the same silhouette: single prop, fixed gear, high or low wing. Before this they fell
     through to the airliner default and every Cessna was drawn as a Boeing 737. */
  /* AUDIT FIX #5: 'R22' removed — it is the Robinson R22 helicopter, and because GA is checked before
     HELI it was drawn as a Cessna. It is in HELI below. (The Robin R2000 is 'R200', kept.) */
  const GA=['C172','C152','C150','C162','C182','C177','C180','C185','C206','C210','P28A','P28B','P28R','P28T','PA18','PA22','PA24','PA25','PA28','PA32','PA38','R200','DR40','DV20','DA40','DA20','AA5','BE33','BE35','BE36','BE23','M20P','M20T','SR20','SR22','RV6','RV7','RV8','RV9','RV10','RV12','CH70','TOBA','TB20','G115','AT3','C42','EUPA','P28S','ZLIN','YK52','SF25','S22T','DA42','BE58','PA31','PA44','P32R','PA46','C82T'];
  if(GA.includes(t))return'c172.glb';
  /* Helicopters. Six EC30s and two B429s a day over Auckland alone, all of them drawn as airliners
     until now. One twin-engine light helicopter stands in for the lot — at this range a 429, an H130
     and a Squirrel read the same: rotor disc, short fuselage, tail boom. */
  const HELI=['B429','B06','B06T','B407','B412','B427','B430','B505','EC20','EC25','EC30','EC35','EC45','EC55','EC75','H500','H125','H130','H135','H145','H155','H160','H175','AS32','AS50','AS55','AS65','A109','A119','A139','A169','A189','R22','R44','R66','S76','S92','S61','S64','MD52','MD60','MD90','EN28','GAZL','LYNX','PUMA','SK76','BK17','NH90','UH60','UH1','CH47','G2CA','H64'];
  if(HELI.includes(t))return'b429.glb';
  if(TP.includes(t))return'atr72.glb';
  if(['A380','A388'].includes(t))return'a380_v2.glb';
  /* The two current-generation widebodies, both of which were being drawn as a 777 until now. ORDER
     MATTERS: each of these must be tested BEFORE the family rule below it, because /^A3[0-5]/ swallows
     the A350 into the A330 and /^B7(6|7|8)/ swallows the 787 into the 777. Put either one after its
     family rule and it silently never fires. */
  if(/^A35/.test(t))return'a350.glb';          // A359, A35K — the model is a -900, the commoner variant
  if(/^B78/.test(t))return'b787.glb';          // B788, B789, B78X
  if(/^A3[0-5]|^A3[0-9][0-9]$/.test(t)&&!['A318','A319','A320','A321'].includes(t))return'a333_v2.glb';
  if(/^A(318|319|320|321|19N|20N|21N|221|223|220)$/.test(t)||/^BCS/.test(t))return'a320_200.glb';
  if(/^B74/.test(t)||t==='BLCF')return'b747_100.glb';
  if(/^B7(6|7|8)/.test(t))return'b777_klm.glb';
  if(/^B7(3|1|2|5)/.test(t))return'b737_800.glb';
  /* No type, or a type none of the lists know: the transponder's own ADS-B emitter category says what it is
     (2 Oct 2026). ZK-JPT, an RV-7 with no type in the local database, flies as category A1 (light) and was
     drawn as a 737. Only the two unambiguous categories — A2 is noisy (some C172s send it), A3+ is the airliner
     catch-all anyway. A known type above always wins; the 2D map's getCat() does the same. */
  cat=(cat||'').toUpperCase();
  if(cat==='A1')return'c172.glb';
  if(cat==='A7')return'b429.glb';
  return t[0]==='A'?'a320_v2.glb':'b738_v2.glb';
}

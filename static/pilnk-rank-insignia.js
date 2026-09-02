/* pilnk-rank-insignia.js — USAF rank insignia, inline SVG (house rule: no emoji).
 * window.PilnkRanks.insignia(index, size) -> SVG string.
 * 20 ranks, matches pilnk-ranks.php / spec v1.0. Parametric so it stays tiny
 * and scales cleanly from a 24px dashboard chip to a 120px profile badge.
 */
(function (root) {
  var GOLD = '#d4af37', SILVER = '#c9ccd2', DARK = '#3a3f47', EDGE = '#1c1f24';

  // Per-rank draw spec. tier drives colour; shape drives the glyph.
  // enlisted: chevrons over a central star, rockers under, optional diamond.
  var R = [
    { n:'Airman Basic',                  t:'e', chev:0, rock:0 },
    { n:'Airman',                        t:'e', chev:1, rock:0 },
    { n:'Airman First Class',            t:'e', chev:2, rock:0 },
    { n:'Senior Airman',                 t:'e', chev:3, rock:0 },
    { n:'Staff Sergeant',                t:'e', chev:3, rock:1 },
    { n:'Technical Sergeant',            t:'e', chev:3, rock:2 },
    { n:'Master Sergeant',               t:'e', chev:3, rock:3 },
    { n:'Senior Master Sergeant',        t:'e', chev:4, rock:3 },
    { n:'Chief Master Sergeant',         t:'e', chev:5, rock:3 },
    { n:'Command Chief Master Sergeant', t:'e', chev:5, rock:3, diamond:true },
    { n:'Second Lieutenant',             t:'o', bar:1, metal:GOLD },
    { n:'First Lieutenant',              t:'o', bar:1, metal:SILVER },
    { n:'Captain',                       t:'o', bar:2, metal:SILVER },
    { n:'Major',                         t:'o', leaf:1, metal:GOLD },
    { n:'Lieutenant Colonel',            t:'o', leaf:1, metal:SILVER },
    { n:'Colonel',                       t:'o', eagle:true },
    { n:'Brigadier General',             t:'g', stars:1 },
    { n:'Major General',                 t:'g', stars:2 },
    { n:'Lieutenant General',            t:'g', stars:3 },
    { n:'General',                       t:'g', stars:4 }
  ];

  function star(cx, cy, r, fill) {
    var p = '', i, a;
    for (i = 0; i < 10; i++) {
      a = Math.PI / 5 * i - Math.PI / 2;
      var rr = (i % 2 === 0) ? r : r * 0.42;
      p += (i ? 'L' : 'M') + (cx + rr * Math.cos(a)).toFixed(1) + ',' + (cy + rr * Math.sin(a)).toFixed(1);
    }
    return '<path d="' + p + 'Z" fill="' + fill + '" stroke="' + EDGE + '" stroke-width="1"/>';
  }

  // one chevron (^) centred at cx, apex at y, wing half-width w, thickness th
  function chevron(cx, y, w, th, fill) {
    return '<path d="M' + (cx - w) + ',' + (y + w * 0.7) +
      'L' + cx + ',' + y + 'L' + (cx + w) + ',' + (y + w * 0.7) +
      'L' + (cx + w) + ',' + (y + w * 0.7 + th) + 'L' + cx + ',' + (y + th) +
      'L' + (cx - w) + ',' + (y + w * 0.7 + th) + 'Z" fill="' + fill + '" stroke="' + EDGE + '" stroke-width="0.8"/>';
  }
  // one rocker (shallow downward arc) under the star
  function rocker(cx, y, w, th, fill) {
    return '<path d="M' + (cx - w) + ',' + y + 'Q' + cx + ',' + (y + w * 0.55) + ' ' + (cx + w) + ',' + y +
      'L' + (cx + w) + ',' + (y + th) + 'Q' + cx + ',' + (y + w * 0.55 + th) + ' ' + (cx - w) + ',' + (y + th) +
      'Z" fill="' + fill + '" stroke="' + EDGE + '" stroke-width="0.8"/>';
  }
  function bar(x, y, w, h, metal) {
    return '<rect x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="2" fill="' + metal +
      '" stroke="' + EDGE + '" stroke-width="1"/>';
  }
  function oakLeaf(cx, cy, metal) {
    // stylised oak leaf: a lobed blob + stem
    return '<path d="M' + cx + ',' + (cy - 16) +
      ' C' + (cx + 14) + ',' + (cy - 14) + ' ' + (cx + 16) + ',' + (cy + 2) + ' ' + (cx + 6) + ',' + (cy + 8) +
      ' C' + (cx + 12) + ',' + (cy + 12) + ' ' + (cx + 4) + ',' + (cy + 18) + ' ' + cx + ',' + (cy + 14) +
      ' C' + (cx - 4) + ',' + (cy + 18) + ' ' + (cx - 12) + ',' + (cy + 12) + ' ' + (cx - 6) + ',' + (cy + 8) +
      ' C' + (cx - 16) + ',' + (cy + 2) + ' ' + (cx - 14) + ',' + (cy - 14) + ' ' + cx + ',' + (cy - 16) + 'Z"' +
      ' fill="' + metal + '" stroke="' + EDGE + '" stroke-width="1"/>';
  }
  function eagle(cx, cy) {
    // stylised spread-wing eagle silhouette
    return '<path d="M' + cx + ',' + (cy - 6) +
      ' l6,-6 l4,2 l-4,4 l14,-3 l-3,6 l-12,2 l10,6 l-8,1 l-9,4' +
      ' l-9,-4 l-8,-1 l10,-6 l-12,-2 l-3,-6 l14,3 l-4,-4 l4,-2 Z"' +
      ' transform="translate(0,2)" fill="' + SILVER + '" stroke="' + EDGE + '" stroke-width="1"/>' +
      star(cx, cy - 2, 4, SILVER);
  }

  function insignia(index, size) {
    var d = R[index] || R[0];
    size = size || 96;
    var s = '<svg width="' + size + '" height="' + size + '" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">';
    var cx = 50;
    if (d.t === 'e') {
      var col = DARK, metal = SILVER;
      // central star
      s += star(cx, 50, 9, metal);
      // chevrons over the top
      var cy = 34, i;
      for (i = 0; i < d.chev; i++) s += chevron(cx, cy - i * 8, 26 - i * 3, 5, metal);
      // rockers under the bottom
      var ry = 60;
      for (i = 0; i < d.rock; i++) s += rocker(cx, ry + i * 8, 24 - i * 2, 5, metal);
      if (d.diamond) s += '<rect x="46" y="46" width="8" height="8" transform="rotate(45 50 50)" fill="' + GOLD + '" stroke="' + EDGE + '" stroke-width="1"/>';
    } else if (d.t === 'o') {
      if (d.bar) {
        var bw = 12, gap = 6, total = d.bar * bw + (d.bar - 1) * gap, x0 = cx - total / 2, k;
        for (k = 0; k < d.bar; k++) s += bar(x0 + k * (bw + gap), 34, bw, 32, d.metal);
      } else if (d.leaf) {
        s += oakLeaf(cx, 50, d.metal);
      } else if (d.eagle) {
        s += eagle(cx, 50);
      }
    } else { // general — row of stars
      var n = d.stars, sw = 20, tot = n * sw, sx = cx - tot / 2 + sw / 2, j;
      for (j = 0; j < n; j++) s += star(sx + j * sw, 50, 9, GOLD);
    }
    s += '</svg>';
    return s;
  }

  root.PilnkRanks = {
    RANKS: R.map(function (x, i) { return { index: i, name: x.n, tier: x.t }; }),
    name: function (i) { return (R[i] || R[0]).n; },
    insignia: insignia
  };
})(typeof window !== 'undefined' ? window : this);

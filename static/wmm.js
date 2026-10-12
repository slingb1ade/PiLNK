/* PiLNK - magnetic declination, World Magnetic Model WMM-2025 (12 Oct 2026).
 * Lifted unchanged from pilnk.io's static/ar-core.js (tested there against NOAA's geomagnetism library).
 * Used by the 3D Approach: true heading = BDS 6,0 magnetic heading + declination here. The page used to LEARN
 * a "mag var" as the median of (ADS-B track - magnetic heading); that is declination plus the wind's drift,
 * so it drew every aircraft pointing along its track instead of crabbed into the wind (location audit, finding 5).
 * Coefficients: NOAA/BGS, public domain, valid 2025.0-2030.0. WMM-2030 is due in Dec 2029: swap g/h/gd/hd/epoch then.
 * Pure functions, no DOM. Unit test: test_wmm.js. Exposes window.PilnkWMM = {declination, decimalYear, valid}.
 */
(function (root) {
  'use strict';
  const D2R = Math.PI / 180, R2D = 180 / Math.PI;
  const WGS_F = 1 / 298.257223563, WGS_E2 = WGS_F * (2 - WGS_F);
  const WMM = { epoch: 2025, nmax: 12,
    g: [0,-29351.8,-1410.8,-2556.6,2951.1,1649.3,1361,-2404.1,1243.8,453.6,895,799.5,55.7,-281.1,12.1,-233.2,368.9,187.2,-138.7,-142,20.9,64.4,63.8,76.9,-115.7,-40.9,14.9,-60.7,79.5,-77,-8.8,59.3,15.8,2.5,-11.1,14.2,23.2,10.8,-17.5,2,-21.7,16.9,15,-16.8,0.9,4.6,7.8,3,-0.2,-2.5,-13.1,2.4,8.6,-8.7,-12.9,-1.3,-6.4,0.2,2,-1,-0.6,-0.9,1.5,0.9,-2.7,-3.9,2.9,-1.5,-2.5,2.4,-0.6,-0.1,-0.6,-0.1,1.1,-1,-0.2,2.6,-2,-0.2,0.3,1.2,-1.3,0.6,0.6,0.5,-0.1,-0.4,-0.2,-1.3,-0.7],
    h: [0,0,4545.4,0,-3133.6,-815.1,0,-56.6,237.5,-549.5,0,278.6,-133.9,212,-375.6,0,45.4,220.2,-122.9,43,106.1,0,-18.4,16.8,48.8,-59.8,10.9,72.7,0,-48.9,-14.4,-1,23.4,-7.4,-25.1,-2.3,0,7.1,-12.6,11.4,-9.7,12.7,0.7,-5.2,3.9,0,-24.8,12.2,8.3,-3.3,-5.2,7.2,-0.6,0.8,10,0,3.3,0,2.4,5.3,-9.1,0.4,-4.2,-3.8,0.9,-9.1,0,0,2.9,-0.6,0.2,0.5,-0.3,-1.2,-1.7,-2.9,-1.8,-2.3,0,-1.3,0.7,1,-1.4,0,0.6,-0.1,0.8,0.1,-1,0.1,0.2],
    gd: [0,12,9.7,-11.6,-5.2,-8,-1.3,-4.2,0.4,-15.6,-1.6,-2.4,-6,5.6,-7,0.6,1.4,0,0.6,2.2,0.9,-0.2,-0.4,0.9,1.2,-0.9,0.3,0.9,0,-0.1,-0.1,0.5,-0.1,-0.8,-0.8,0.8,-0.1,0.2,0,0.5,-0.1,0.3,0.2,0,0.2,0,-0.1,0.1,0.3,-0.3,0,0.3,-0.1,0.1,-0.1,0.1,0,0.1,0.1,0,-0.3,0,-0.1,-0.1,0,0,0,0,0,0,0,-0.1,0,0,-0.1,-0.1,-0.1,-0.1,0,0,0,0,0,0,0.1,0,0,0,-0.1,0,-0.1],
    hd: [0,0,-21.5,0,-27.7,-12.1,0,4,-0.3,-4.1,0,-1.1,4.1,1.6,-4.4,0,-0.5,2.2,0.4,1.7,1.9,0,0.3,-1.6,-0.4,0.9,0.7,0.9,0,0.6,0.5,-0.8,0,-1,0.6,-0.2,0,-0.2,0.5,-0.4,0.4,-0.5,-0.6,0.3,0.2,0,-0.3,0.3,-0.3,0.3,0.2,-0.1,-0.2,0.4,0.1,0,0,0,-0.2,0.1,-0.1,0.1,0,-0.1,0.2,0,0,0,0.1,0,0.1,0,0,0.1,0,0,0,0,0,0,0,-0.1,0.1,0,0,0,0,0,0,0,-0.1] };
  function decimalYear(date) {
    const d = date || new Date(), y = d.getUTCFullYear();
    const start = Date.UTC(y, 0, 1), end = Date.UTC(y + 1, 0, 1);
    return y + (d.getTime() - start) / (end - start);
  }
  // Declination in degrees (east positive) at geodetic lat/lon, height hKm, decimal year t.
  function declination(lat, lon, t, hKm) {
    const h = hKm || 0, dt = (t == null ? decimalYear() : t) - WMM.epoch;
    const A = 6378.137, E2 = WGS_E2, re = 6371.2;
    const phi = lat * D2R, lam = lon * D2R, sphi = Math.sin(phi), cphi = Math.cos(phi);
    const Rc = A / Math.sqrt(1 - E2 * sphi * sphi);
    const p = (Rc + h) * cphi, z = (Rc * (1 - E2) + h) * sphi;
    const r = Math.hypot(p, z), phic = Math.asin(z / r);
    const ct = Math.sin(phic), st = Math.cos(phic);            // colatitude cos/sin
    const N = WMM.nmax, P = [], dP = [];
    for (let n = 0; n <= N; n++) { P.push(new Array(N + 1).fill(0)); dP.push(new Array(N + 1).fill(0)); }
    P[0][0] = 1;
    for (let n = 1; n <= N; n++) {
      for (let m = 0; m <= n; m++) {
        if (n === m) {
          if (n === 1) { P[1][1] = st; dP[1][1] = ct; }
          else { const k = Math.sqrt((2 * n - 1) / (2 * n)); P[n][n] = k * st * P[n - 1][n - 1]; dP[n][n] = k * (ct * P[n - 1][n - 1] + st * dP[n - 1][n - 1]); }
        } else {
          const k1 = 2 * n - 1, k2 = Math.sqrt((n - 1) * (n - 1) - m * m), den = Math.sqrt(n * n - m * m);
          const p2 = n >= 2 ? P[n - 2][m] : 0, d2 = n >= 2 ? dP[n - 2][m] : 0;
          P[n][m] = (k1 * ct * P[n - 1][m] - k2 * p2) / den;
          dP[n][m] = (k1 * (ct * dP[n - 1][m] - st * P[n - 1][m]) - k2 * d2) / den;
        }
      }
    }
    let X = 0, Y = 0, Z = 0;
    for (let n = 1; n <= N; n++) {
      const ar = Math.pow(re / r, n + 2);
      for (let m = 0; m <= n; m++) {
        const i = n * (n + 1) / 2 + m;
        const g = WMM.g[i] + dt * WMM.gd[i], hh = WMM.h[i] + dt * WMM.hd[i];
        const cm = Math.cos(m * lam), sm = Math.sin(m * lam);
        X += ar * (g * cm + hh * sm) * dP[n][m];                  // -dP/dphi' = +dP/dtheta
        if (st > 1e-10) Y += ar * m * (g * sm - hh * cm) * P[n][m] / st;
        Z -= ar * (n + 1) * (g * cm + hh * sm) * P[n][m];
      }
    }
    const psi = phic - phi;                                       // geocentric -> geodetic
    const Xg = X * Math.cos(psi) - Z * Math.sin(psi);
    return Math.atan2(Y, Xg) * R2D;
  }

  // true while the coefficients above still cover year t (the model is only defined for its five years)
  function valid(t) { const y = (t == null ? decimalYear() : t); return y >= WMM.epoch && y <= WMM.epoch + 5; }

  const API = { declination, decimalYear, valid };
  if (typeof module === 'object' && module.exports) module.exports = API; else root.PilnkWMM = API;
})(typeof self !== 'undefined' ? self : this);

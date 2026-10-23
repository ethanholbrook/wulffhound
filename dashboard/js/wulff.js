/* Wulff shape in the browser: the intersection of the half-spaces n_i . x <= gamma_i, one per symmetry-equivalent
 * facet (the family's Laue orbit), with n_i the unit normal of (h k l) in the data-file lattice.
 * Same conventions as wulffhound's engine (pymatgen WulffShape): lattice rows a, b, c (Cartesian, angstrom),
 * normal of (h k l) = (h k l) . inv(L)^T, plane distance = gamma.
 * Method: start from a large cube and clip it by each plane (Sutherland-Hodgman on every face, the cut closed by
 * a cap polygon). Each final face remembers its plane, so areas per (h k l) and per family follow directly.
 * No dependencies; exported as window.Wulff (and module.exports when available). */
(function (root) {
  'use strict';

  const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  const scale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
  const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
  const norm = (a) => Math.sqrt(dot(a, a));
  const unit = (a) => scale(a, 1 / norm(a));

  function inv3(m) {
    const [a, b, c] = m[0], [d, e, f] = m[1], [g, h, i] = m[2];
    const A = e * i - f * h, B = -(d * i - f * g), C = d * h - e * g;
    const det = a * A + b * B + c * C;
    if (Math.abs(det) < 1e-12) throw new Error('singular lattice matrix');
    return [
      [A / det, -(b * i - c * h) / det, (b * f - c * e) / det],
      [B / det, (a * i - c * g) / det, -(a * f - c * d) / det],
      [C / det, -(a * h - b * g) / det, (a * e - b * d) / det],
    ];
  }

  /** rows of inv(L)^T: the crystallographic reciprocal vectors a*, b*, c* (no 2 pi) */
  function reciprocal(L) {
    const I = inv3(L);
    return [[I[0][0], I[1][0], I[2][0]], [I[0][1], I[1][1], I[2][1]], [I[0][2], I[1][2], I[2][2]]];
  }

  function normalOf(hkl, R) {
    const v = [0, 1, 2].map((j) => hkl[0] * R[0][j] + hkl[1] * R[1][j] + hkl[2] * R[2][j]);
    return unit(v);
  }

  function polyArea(poly, n) {
    let s = [0, 0, 0];
    for (let i = 0; i < poly.length; i++) s = add(s, cross(poly[i], poly[(i + 1) % poly.length]));
    return Math.abs(dot(s, n)) / 2;
  }

  function orderPolygon(pts, n) {
    const c = scale(pts.reduce(add, [0, 0, 0]), 1 / pts.length);
    let u = Math.abs(n[0]) < 0.9 ? cross(n, [1, 0, 0]) : cross(n, [0, 1, 0]);
    u = unit(u);
    const v = cross(n, u);
    return pts.map((p) => ({ p, a: Math.atan2(dot(sub(p, c), v), dot(sub(p, c), u)) }))
      .sort((x, y) => x.a - y.a).map((x) => x.p);   // counter-clockwise seen from outside (along +n)
  }

  function dedupe(pts, tol) {
    const out = [];
    for (const p of pts) if (!out.some((q) => norm(sub(p, q)) < tol)) out.push(p);
    return out;
  }

  /** Clip a convex polyhedron (list of faces {poly, plane}) by n . x <= d. */
  function clip(faces, n, d, planeIndex, tol) {
    const out = [];
    const cap = [];
    let cut = false;
    for (const f of faces) {
      const P = f.poly;
      const s = P.map((p) => dot(n, p) - d);
      if (s.every((x) => x <= tol)) { out.push(f); continue; }
      cut = true;
      const Q = [];
      for (let i = 0; i < P.length; i++) {
        const a = P[i], b = P[(i + 1) % P.length], sa = s[i], sb = s[(i + 1) % P.length];
        const ina = sa <= tol, inb = sb <= tol;
        if (ina) {
          Q.push(a);
          if (Math.abs(sa) <= tol) cap.push(a);      // a lies on the cutting plane
        }
        if (ina !== inb) {
          if ((ina && Math.abs(sa) <= tol) || (inb && Math.abs(sb) <= tol)) continue;   // the vertex is the crossing
          const t = sa / (sa - sb);
          const x = add(a, scale(sub(b, a), t));
          Q.push(x); cap.push(x);
        }
      }
      const Qd = dedupe(Q, tol);
      if (Qd.length >= 3 && polyArea(Qd, f.n) > tol * tol) out.push({ poly: Qd, plane: f.plane, n: f.n });
    }
    if (!cut) return { faces, changed: false };
    const C = dedupe(cap, tol * 10);
    if (C.length >= 3) {
      const poly = orderPolygon(C, n);
      if (polyArea(poly, n) > tol * tol) out.push({ poly, plane: planeIndex, n });
    }
    return { faces: out, changed: true };
  }

  /**
   * facets: [{family, members: [[h,k,l], ...], gamma}], L: lattice rows (3x3).
   * Returns {ok, faces: [{poly, plane, hkl, family}], areaByFamily, areaFraction, totalArea, volume, closed,
   *          onShape(threshold), planes}.
   */
  function build(L, facets, opts) {
    opts = opts || {};
    const R = reciprocal(L);
    const planes = [];
    facets.forEach((f, fi) => {
      for (const m of f.members) planes.push({ n: normalOf(m, R), d: +f.gamma, hkl: m, family: f.family, fi });
    });
    if (!planes.length) return { ok: false, error: 'no facets' };
    const gmax = Math.max(...planes.map((p) => p.d));
    const H = 50 * gmax;
    const tol = 1e-9 * gmax;
    // the starting cube (faces marked plane -1)
    const V = [];
    for (const x of [-H, H]) for (const y of [-H, H]) for (const z of [-H, H]) V.push([x, y, z]);
    const idx = [[0, 1, 3, 2], [4, 6, 7, 5], [0, 4, 5, 1], [2, 3, 7, 6], [0, 2, 6, 4], [1, 5, 7, 3]];
    const ns = [[-1, 0, 0], [1, 0, 0], [0, -1, 0], [0, 1, 0], [0, 0, -1], [0, 0, 1]];
    let faces = idx.map((q, i) => ({ poly: q.map((k) => V[k]), plane: -1, n: ns[i] }));
    // clip by the planes, lowest gamma first (fewer intermediate vertices)
    const order = planes.map((p, i) => i).sort((a, b) => planes[a].d - planes[b].d);
    for (const i of order) faces = clip(faces, planes[i].n, planes[i].d, i, tol).faces;
    const closed = faces.every((f) => f.plane >= 0);
    const areaByFamily = {};
    facets.forEach((f) => { areaByFamily[f.family] = 0; });
    let total = 0, volume = 0;
    const out = [];
    for (const f of faces) {
      const a = polyArea(f.poly, f.n);
      total += a;
      if (f.plane >= 0) {
        const p = planes[f.plane];
        areaByFamily[p.family] += a;
        volume += a * p.d / 3;
        out.push({ poly: f.poly, plane: f.plane, hkl: p.hkl, family: p.family, gamma: p.d, area: a, n: p.n });
      } else {
        out.push({ poly: f.poly, plane: -1, hkl: null, family: null, gamma: null, area: a, n: f.n });
      }
    }
    const areaFraction = {};
    for (const k in areaByFamily) areaFraction[k] = total > 0 ? areaByFamily[k] / total : 0;
    return { ok: closed, closed, error: closed ? null : 'the facets do not enclose a finite crystal', faces: out,
      areaByFamily, areaFraction, totalArea: total, volume, planes };
  }

  /** gamma-weighted surface energy and shape factor as pymatgen defines them (area / volume^(2/3)) */
  function metrics(res) {
    let wse = 0;
    for (const f of res.faces) if (f.plane >= 0) wse += f.area * f.gamma;
    return { weighted_surface_energy: wse / res.totalArea, shape_factor: res.totalArea / Math.pow(res.volume, 2 / 3) };
  }

  const api = { build, metrics, reciprocal, normalOf };
  root.Wulff = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);

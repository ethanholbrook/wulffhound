/* Surface-energy explorer (prototype): reads dataset/*.jsonl (schema/surface_energy.schema.json) and renders
 * the overview, the explorer, a material page (facet table, Wulff shape built in the browser by wulff.js, what-if
 * slider, BFDH overlay, uncertainty bars; shape: equilibrium (Wulff) | growth (attachment energy) | BFDH, with the
 * E_att column when attachment_energy records exist), a comparison, and record / AE record / shape provenance pages.
 * No build step.
 * Data base path: ?data=<url> (default ../dataset/). Everything read from the dataset is escaped before display. */
(function () {
  'use strict';
  const params = new URLSearchParams(location.search);
  const BASE = (params.get('data') || '../dataset/').replace(/\/?$/, '/');
  const D = { records: [], byId: {}, shapes: [], shapeById: {}, ae: [], aeById: {}, index: null, materials: {} };
  const STATE = { mat: {}, colorBy: 'family' };     // per material: {ff, level, construction, bfdh, allTerms, whatif}
  // Categorical facet colours (Tableau 10, then its lighter companions): the same family keeps the same colour on
  // the 3D shape, the legend, the facet table, the uncertainty bars and the strip, whatever the level or what-if.
  const PALETTE = ['#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#76b7b2', '#edc948', '#b07aa1', '#ff9da7', '#9c755f',
    '#bab0ac', '#a0cbe8', '#ffbe7d', '#8cd17d', '#ff9d9a', '#86bcb6', '#f1ce63', '#d4a6c8', '#fabfd2', '#d7b5a6', '#79706e'];
  const FAMCOL = {};    // material id -> {family: colour}
  window.__dash = { ready: false, errors: [], checks: {}, renders: 0 };
  window.addEventListener('error', (e) => window.__dash.errors.push(String(e.message || e)));

  // ------------------------------------------------------------------ helpers
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const fmt = (x, d) => (x == null || Number.isNaN(+x) ? '-' : (+x).toFixed(d == null ? 2 : d));
  const $ = (sel, el) => (el || document).querySelector(sel);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const recLink = (r, text) => '<a href="#/record/' + esc(r.record_id) + '" title="record ' + esc(r.record_id) + '">' + text + '</a>';
  const shapeLink = (s, text, anchor) => '<a href="#/shape/' + esc(s.shape_id) + (anchor ? '?f=' + encodeURIComponent(anchor) : '') + '" title="shape ' + esc(s.shape_id) + '">' + text + '</a>';
  const levelName = { 1: 'L1 unrelaxed', 2: 'L2 relaxed T = 0', 3: 'L3 MD' };
  const hasError = (r) => r.result.gamma_flags.some((f) => f.severity === 'error');
  const famHkl = (f) => f.replace(/[{}]/g, '');
  const aeLink = (r, text) => '<a href="#/ae/' + esc(r.record_id) + '" title="attachment-energy record ' + esc(r.record_id) + '">' + text + '</a>';
  const anyLink = (id, text) => (D.byId[id] ? recLink(D.byId[id], text) : D.aeById[id] ? aeLink(D.aeById[id], text) : esc(text));

  // ------------------------------------------------------------------ shape constructions
  // A wulff_shape record is the equilibrium (Wulff) shape (distance = gamma) or, with construction
  // 'attachment_energy', the growth morphology (distance = |E_att|). The BFDH shape (distance = 1/d_eff) is derived
  // in the browser from the facet set of a Wulff shape, whose recorded bfdh_area_fraction it reproduces.
  const isAE = (s) => !!s && s.construction === 'attachment_energy';
  const dist = (f) => (f.distance != null ? f.distance : f.gamma);
  const CONSTRUCTIONS = [['wulff', 'equilibrium (Wulff)'], ['ae', 'growth (AE)'], ['bfdh', 'BFDH']];
  function qty(s) {
    if (s && s.construction === 'bfdh') return { k: 'bfdh', name: '1/d<sub>eff</sub>', plain: '1/d_eff', units: '1/&Aring;', d: 4 };
    if (isAE(s)) return { k: 'ae', name: '|E<sub>att</sub>|', plain: '|E_att|', units: 'kcal/mol', d: 2 };
    return { k: 'wulff', name: '&gamma;', plain: 'gamma', units: 'mJ/m<sup>2</sup>', d: 2 };
  }
  function bfdhShape(s) {
    return Object.assign({}, s, { construction: 'bfdh', flags: [], sensitivity: { n_samples: 0 },
      facets: s.facets.map((f) => Object.assign({}, f, { distance: 1 / f.d_eff, area_fraction: f.bfdh_area_fraction,
        area_mean: f.bfdh_area_fraction, area_std: 0, area_p05: f.bfdh_area_fraction, area_p95: f.bfdh_area_fraction,
        sigma: 0, termination_label: 'BFDH: d_eff ' + (+f.d_eff).toFixed(3) + ' A' })) });
  }
  /** the shape of a material for one construction: Wulff (the given level, else best, else the first), AE, BFDH */
  function shapeFor(M, ff, kind, level) {
    const ws = shapesOf(M, ff).filter((s) => !isAE(s));
    const w = ws.find((s) => s.level === level) || ws.find((s) => s.level === 'best') || ws[0] || null;
    if (kind === 'ae') return shapesOf(M, ff).find(isAE) || null;
    if (kind === 'bfdh') return w ? bfdhShape(w) : null;
    return w;
  }
  const shapeTitle = (s) => (s.construction === 'bfdh' ? 'BFDH shape (1/d_eff, ' + s.facets.length + ' families)'
    : isAE(s) ? 'Growth (attachment-energy) shape, static slices' : 'Wulff shape, T = ' + fmt(s.temperature_K, 0) + ' K, level ' + esc(s.level));

  /** stable colours for a material's families: the families of its 'best' (else first) shape by recorded area
   * (largest first, so the dominant faces get the most distinct colours), then any other family by name */
  function famColors(M) {
    if (FAMCOL[M.id]) return FAMCOL[M.id];
    const order = [];
    const sh = M.shapes.find((s) => s.level === 'best' && !isAE(s)) || M.shapes.find((s) => !isAE(s)) || M.shapes[0];
    if (sh) sh.facets.slice().sort((a, b) => b.area_fraction - a.area_fraction || a.family.localeCompare(b.family)).forEach((f) => order.push(f.family));
    [...new Set(M.records.map((r) => r.facet.family))].sort().forEach((f) => { if (!order.includes(f)) order.push(f); });
    const m = {};
    order.forEach((f, i) => { m[f] = PALETTE[i % PALETTE.length]; });
    return (FAMCOL[M.id] = m);
  }
  const swatch = (c) => '<span class="swatch" style="background:' + esc(c) + '"></span>';

  function badge(f) {
    return '<span class="badge ' + esc(f.severity) + '" title="' + esc(f.detail + ' (' + f.by + ')') + '">' + esc(f.code.replace(/_/g, ' ')) + '</span>';
  }
  function hexToRgb(h) { h = h.replace('#', ''); return [0, 2, 4].map((i) => parseInt(h.substr(i, 2), 16)); }
  function mix(a, b, t) {
    const A = hexToRgb(a), B = hexToRgb(b);
    return 'rgb(' + A.map((x, i) => Math.round(x + (B[i] - x) * t)).join(',') + ')';
  }
  function plotTheme() {
    return {
      paper_bgcolor: css('--surface'), plot_bgcolor: css('--surface'),
      font: { family: 'system-ui, -apple-system, Segoe UI, sans-serif', color: css('--ink-2'), size: 12 },
      hoverlabel: { bgcolor: css('--surface'), bordercolor: css('--axis'), font: { color: css('--ink') } },
    };
  }
  const axis = (title) => ({ title: { text: title }, gridcolor: css('--grid'), linecolor: css('--axis'), zerolinecolor: css('--axis'), tickcolor: css('--axis'), automargin: true });
  const PCFG = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ['toImage', 'sendDataToCloud'] };

  // ------------------------------------------------------------------ data
  async function getJSONL(name) {
    const t = await (await fetch(BASE + name)).text();
    return t.split('\n').filter((l) => l.trim()).map((l) => JSON.parse(l));
  }
  async function load() {
    D.index = await (await fetch(BASE + 'index.json')).json();
    D.records = await getJSONL(D.index.files.surface_energy);
    D.shapes = await getJSONL(D.index.files.wulff_shape);
    for (const r of D.records) D.byId[r.record_id] = r;
    for (const s of D.shapes) D.shapeById[s.shape_id] = s;
    if (D.index.files.attachment_energy) D.ae = await getJSONL(D.index.files.attachment_energy);
    for (const r of D.ae) D.aeById[r.record_id] = r;
    for (const r of D.records) {
      const m = r.material;
      const M = D.materials[m.material_id] || (D.materials[m.material_id] = {
        id: m.material_id, name: m.name, formula: m.formula, inchikey: m.inchikey, smiles: m.smiles,
        mass: m.molecular_mass, density: m.cell.density, category: m.category, polymorph: m.polymorph,
        structures: {}, ffs: [], records: [], shapes: [], ae: [] });
      M.records.push(r);
      M.structures[m.structure.structure_id] = m;
      if (!M.ffs.includes(r.force_field.name)) M.ffs.push(r.force_field.name);
      if (M.density == null && m.cell.density != null) M.density = m.cell.density;
    }
    for (const s of D.shapes) if (D.materials[s.material_id]) D.materials[s.material_id].shapes.push(s);
    for (const r of D.ae) if (D.materials[r.material.material_id]) D.materials[r.material.material_id].ae.push(r);
    for (const M of Object.values(D.materials)) M.ffs.sort((a, b) => shapesOf(M, b).length - shapesOf(M, a).length || a.localeCompare(b));
    $('#foot-info').innerHTML = 'Dataset schema ' + esc(D.index.schema_version) + ', built ' + esc(D.index.built.slice(0, 10)) + ': ' +
      D.records.length + ' surface-energy records, ' + D.ae.length + ' attachment energies, ' + D.shapes.length + ' shapes, ' + Object.keys(D.materials).length + ' materials.';
  }
  const shapesOf = (M, ff) => M.shapes.filter((s) => !ff || s.force_field === ff);
  const matList = () => Object.values(D.materials).sort((a, b) => a.name.localeCompare(b.name));

  // ------------------------------------------------------------------ router
  function route() {
    const h = location.hash.replace(/^#\/?/, '');
    const [path, q] = h.split('?');
    const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
    const query = new URLSearchParams(q || '');
    document.querySelectorAll('nav a').forEach((a) => a.classList.toggle('active', a.dataset.nav === (parts[0] || 'home') || (parts[0] === 'm' && a.dataset.nav === 'explore')));
    const app = $('#app');
    // free the WebGL contexts of the plots about to be replaced (each 3D plot holds one; browsers cap them)
    if (typeof Plotly !== 'undefined') app.querySelectorAll('.js-plotly-plot').forEach((e) => { try { Plotly.purge(e); } catch (x) { /* already gone */ } });
    try {
      if (!parts.length) home(app);
      else if (parts[0] === 'explore') explore(app, query);
      else if (parts[0] === 'm' && D.materials[parts[1]]) material(app, D.materials[parts[1]], query);
      else if (parts[0] === 'compare') compare(app, parts[1], parts[2], query);
      else if (parts[0] === 'record' && D.byId[parts[1]]) recordPage(app, D.byId[parts[1]]);
      else if (parts[0] === 'ae' && D.aeById[parts[1]]) aePage(app, D.aeById[parts[1]]);
      else if (parts[0] === 'shape' && D.shapeById[parts[1]]) shapePage(app, D.shapeById[parts[1]], query.get('f'));
      else app.innerHTML = '<div class="card"><h1>Not found</h1><p>No page for <code>' + esc(h) + '</code>. <a href="#/">Overview</a></p></div>';
    } catch (e) {
      window.__dash.errors.push('render: ' + e.message);
      app.innerHTML = '<div class="card"><h1>Error</h1><pre class="json">' + esc(e.stack || e) + '</pre></div>';
    }
    window.__dash.renders++;
    if (window.__dash.route !== h) window.scrollTo(0, 0);   // a re-render of the same page keeps its scroll
    window.__dash.route = h;
  }

  // ------------------------------------------------------------------ overview
  function home(app) {
    const c = D.index.counts;
    const src = c.by_source;
    const n = (k) => Object.entries(src).filter(([s]) => s.startsWith(k)).reduce((a, [, v]) => a + v, 0);
    let h = '<div class="card"><h1>Surface energies of molecular crystals</h1>' +
      '<p class="muted">One record per computed surface energy (material, force field, facet, termination, protocol, result, provenance), ' +
      'and derived Wulff shapes. Schema <code>' + esc(D.index.schema) + '</code>, version ' + esc(D.index.schema_version) + '.</p>' +
      '<div class="tiles">' +
      tile(c.surface_energy, 'surface-energy records') + tile(c.wulff_shape, 'Wulff shapes') + tile(c.materials, 'materials') +
      tile(n('wulffhound'), 'from wulffhound ledgers') + tile(n('cif-zoo'), 'from the CIF zoo') + tile(n('surfenergy'), 'from surfenergy pins / notes') +
      tile(c.status_failed, 'failed runs (kept)') + tile(c.with_error_flags, 'flagged as errors (kept)') +
      (c.attachment_energy ? tile(c.attachment_energy, 'attachment energies (' + Object.keys(c.attachment_energy_by_material || {}).length + ' growth shapes)') : '') +
      '</div>' +
      '<div class="banner">Prototype. CIF-zoo values are unrelaxed ReaxFF-lg screening cuts, not validated surface energies. ' +
      'TNT uses a validated non-reactive force field; paracetamol uses ReaxFF-lg, which is not validated for it.</div></div>';
    h += '<div class="card"><h2>Materials with a Wulff shape</h2><div class="matlist">';
    for (const M of matList().filter((M) => M.shapes.length)) h += matCard(M);
    h += '</div></div><div class="card"><h2>All materials</h2><p class="muted small">The CIF zoo computed two facet families per material: ' +
      'its pages show the facet energies, not a shape (' + esc(D.index.no_shape.why) + ').</p><div class="matlist">';
    for (const M of matList()) h += matCard(M);
    h += '</div></div>';
    if (D.index.skipped && D.index.skipped.length) {
      h += '<div class="card"><h2>Not in the dataset</h2><ul class="small">' + D.index.skipped.map((s) => '<li>' + esc(s) + '</li>').join('') + '</ul>' +
        '<p class="small muted">Parquet: ' + esc(D.index.parquet) + '</p></div>';
    }
    app.innerHTML = h;
  }
  const tile = (v, k) => '<div class="tile"><div class="v">' + esc(v) + '</div><div class="k">' + esc(k) + '</div></div>';
  function matCard(M) {
    const ok = M.records.filter((r) => r.result.status === 'ok' && !hasError(r));
    const fams = new Set(ok.map((r) => r.facet.family));
    const levels = [...new Set(ok.map((r) => r.protocol.level))].sort();
    return '<a class="matcard" href="#/m/' + encodeURIComponent(M.id) + '"><div class="t">' + esc(M.name) + '</div>' +
      '<div class="small muted">' + esc(M.formula) + ' &middot; ' + esc(M.polymorph) + '</div>' +
      '<div class="small">' + fams.size + ' facet families &middot; ' + levels.map((l) => 'L' + l).join(', ') + ' &middot; ' + esc(M.ffs.join(' / ')) +
      (M.shapes.length ? ' &middot; <strong>shape</strong>' : '') + (M.ae.length ? ' &middot; <strong>growth (AE)</strong>' : '') + '</div></a>';
  }

  // ------------------------------------------------------------------ 3D Wulff shape
  function facetsForBuild(shape, overrides) {
    return shape.facets.map((f) => ({ family: f.family, members: f.members,
      gamma: overrides && overrides[f.family] != null ? overrides[f.family] : dist(f) }));
  }
  /** draw a shape (and optionally the BFDH shape scaled to its volume) into el; returns the browser build */
  function drawShape(el, shape, opt) {
    opt = opt || {};
    const res = Wulff.build(shape.lattice_matrix, facetsForBuild(shape, opt.overrides));
    if (!res.ok) { el.innerHTML = '<p class="banner">' + esc(res.error) + '</p>'; return res; }
    const faces = res.faces.filter((f) => f.plane >= 0);
    const gs = faces.map((f) => f.gamma);
    const gmin = Math.min(...gs), gmax = Math.max(...gs);
    const lo = css('--seq-lo'), hi = css('--seq-hi');
    const fbyfam = {};
    shape.facets.forEach((f) => { fbyfam[f.family] = f; });
    const traces = [];
    const ex = [], ey = [], ez = [];
    for (const f of faces) {
      const P = f.poly;
      const x = P.map((p) => p[0]), y = P.map((p) => p[1]), z = P.map((p) => p[2]);
      const I = [], J = [], K = [];
      for (let k = 1; k < P.length - 1; k++) { I.push(0); J.push(k); K.push(k + 1); }
      const sf = fbyfam[f.family];
      const q = qty(shape);
      const tip = '(' + f.hkl.join(' ') + ') of ' + f.family + '<br>' + q.plain + ' ' + fmt(f.gamma, q.d) + ' ' + q.units +
        (opt.overrides && opt.overrides[f.family] != null ? ' (what-if)' : q.k === 'wulff' ? ' (' + (sf.level === 2 ? 'L2' : 'L1') + ')' : '') +
        (q.k === 'ae' ? '<br>E_att ' + fmt(sf.E_att, 2) + ' kcal/mol, d_eff ' + fmt(sf.d_eff, 3) + ' &Aring;' : '') +
        '<br>family area fraction ' + fmt(res.areaFraction[f.family], 4) + '<br>' + (q.k === 'ae' ? 'growth slice ' : 'termination ') + esc(sf.termination_label || '-');
      traces.push({ type: 'mesh3d', x, y, z, i: I, j: J, k: K, flatshading: true,
        color: opt.colors && STATE.colorBy === 'family' ? (opt.colors[f.family] || PALETTE[9])
          : mix(lo, hi, gmax > gmin ? (f.gamma - gmin) / (gmax - gmin) : 0.5), opacity: 1,
        lighting: { ambient: 0.78, diffuse: 0.45, specular: 0.05, roughness: 0.9, fresnel: 0.05 },
        lightposition: { x: 1e5, y: 2e5, z: 3e5 }, hovertemplate: tip + '<extra></extra>', name: f.family });
      for (const p of P.concat([P[0]])) { ex.push(p[0]); ey.push(p[1]); ez.push(p[2]); }
      ex.push(null); ey.push(null); ez.push(null);
    }
    traces.push({ type: 'scatter3d', mode: 'lines', x: ex, y: ey, z: ez, hoverinfo: 'skip', line: { color: css('--ink-2'), width: 2 }, showlegend: false });
    let bfdh = null;
    if (opt.bfdh && shape.construction !== 'bfdh') {
      bfdh = Wulff.build(shape.lattice_matrix, shape.facets.map((f) => ({ family: f.family, members: f.members, gamma: 1 / f.d_eff })));
      if (bfdh.ok) {
        const s = Math.cbrt(res.volume / bfdh.volume);
        const bx = [], by = [], bz = [];
        for (const f of bfdh.faces) {
          for (const p of f.poly.concat([f.poly[0]])) { bx.push(p[0] * s); by.push(p[1] * s); bz.push(p[2] * s); }
          bx.push(null); by.push(null); bz.push(null);
        }
        traces.push({ type: 'scatter3d', mode: 'lines', x: bx, y: by, z: bz, hoverinfo: 'skip', name: 'BFDH',
          line: { color: css('--series-2'), width: 3 }, showlegend: false });
      }
    }
    const hide = { visible: false, showgrid: false, zeroline: false, showbackground: false, showticklabels: false, title: { text: '' } };
    const layout = Object.assign(plotTheme(), { margin: { l: 0, r: 0, t: 0, b: 0 }, showlegend: false, uirevision: shape.shape_id,
      scene: { aspectmode: 'data', xaxis: hide, yaxis: hide, zaxis: hide, bgcolor: css('--surface'), camera: { eye: { x: 1.35, y: 1.1, z: 0.9 } } } });
    Plotly.react(el, traces, layout, PCFG);
    res.bfdh = bfdh;
    return res;
  }

  function checkShape(shape) {
    const res = Wulff.build(shape.lattice_matrix, facetsForBuild(shape));
    let max = 0, maxB = 0;
    for (const f of shape.facets) max = Math.max(max, Math.abs((res.areaFraction[f.family] || 0) - f.area_fraction));
    const b = Wulff.build(shape.lattice_matrix, shape.facets.map((f) => ({ family: f.family, members: f.members, gamma: 1 / f.d_eff })));
    for (const f of shape.facets) if (f.bfdh_area_fraction != null) maxB = Math.max(maxB, Math.abs((b.areaFraction[f.family] || 0) - f.bfdh_area_fraction));
    const c = { shape_id: shape.shape_id, construction: shape.construction || 'wulff', max_abs_diff: max, max_abs_diff_bfdh: maxB, n_families: shape.facets.length,
      ok: res.ok && max < 5e-4 && maxB < 5e-4, faces: res.faces.length,
      browser: Object.fromEntries(shape.facets.map((f) => [f.family, res.areaFraction[f.family]])) };
    window.__dash.checks[shape.shape_id + (shape.construction === 'bfdh' ? ':bfdh' : '')] = c;
    return c;
  }

  // ------------------------------------------------------------------ material page
  function material(app, M, query) {
    const st = STATE.mat[M.id] || (STATE.mat[M.id] = { ff: M.ffs[0], level: 'best', construction: 'wulff', bfdh: false, allTerms: false, whatif: {}, wfam: null });
    if (query.get('ff') && M.ffs.includes(query.get('ff'))) st.ff = query.get('ff');
    if (CONSTRUCTIONS.some(([k]) => k === query.get('shape'))) st.construction = query.get('shape');
    const recs = M.records.filter((r) => r.force_field.name === st.ff);
    const shapes = shapesOf(M, st.ff);
    const wshapes = shapes.filter((s) => !isAE(s));
    const aeShape = shapes.find(isAE) || null;
    const aeRecs = M.ae.filter((r) => r.force_field.name === st.ff);
    if (!wshapes.some((s) => s.level === st.level) && wshapes.length) st.level = wshapes[0].level;
    if (st.construction === 'ae' && !aeShape) st.construction = 'wulff';
    if (st.construction !== 'ae' && !wshapes.length && aeShape) st.construction = 'ae';
    const shape = shapeFor(M, st.ff, st.construction, st.level);
    const q = qty(shape);
    const m0 = recs[0].material;
    // the force-field note of the highest level present (a relaxed record's note, not the screening one)
    const ff = recs.slice().sort((x, y) => y.protocol.level - x.protocol.level)[0].force_field;
    const structs = [...new Set(recs.map((r) => r.material.structure.structure_id))];
    let h = '<div class="card"><h1>' + esc(M.name) + ' <span class="muted small">' + esc(M.polymorph) + '</span></h1>' +
      '<div class="cols2"><dl class="kv">' +
      '<dt>Formula</dt><dd>' + esc(M.formula) + ' (' + fmt(M.mass, 2) + ' g/mol)</dd>' +
      '<dt>InChIKey</dt><dd class="mono">' + esc(M.inchikey || '-') + '</dd>' +
      '<dt>SMILES</dt><dd class="mono">' + esc(M.smiles || '-') + '</dd>' +
      '<dt>Space group</dt><dd>' + esc(m0.space_group.symbol_data_setting) + (m0.space_group.number ? ' (No. ' + m0.space_group.number + ')' : '') + ', Laue ' + esc(m0.space_group.laue_class || '-') + '</dd>' +
      '<dt>Cell</dt><dd>a ' + fmt(m0.cell.a, 4) + ', b ' + fmt(m0.cell.b, 4) + ', c ' + fmt(m0.cell.c, 4) + ' &Aring;; &alpha; ' + fmt(m0.cell.alpha, 2) + ', &beta; ' + fmt(m0.cell.beta, 3) + ', &gamma; ' + fmt(m0.cell.gamma, 2) + '&deg;; Z ' + esc(m0.cell.Z) + '; ' + fmt(m0.cell.density, 3) + ' g/cm<sup>3</sup></dd>' +
      '</dl><dl class="kv">' +
      '<dt>Force field</dt><dd>' + esc(ff.name) + ' ' + (ff.validated_for_material ? '<span class="badge good">validated for this material</span>' : '<span class="badge warning">not validated for this material</span>') + '</dd>' +
      '<dt>Structure</dt><dd>' + structs.map((sid) => { const s = M.structures[sid].structure; return esc(s.source) + (s.url ? ' (<a href="' + esc(s.url) + '" rel="noopener">' + esc(s.database) + '</a>)' : '') + '; ' + esc(s.licence); }).join('<br>') + '</dd>' +
      (structs.length === 1 && M.structures[structs[0]].structure.citation ? '<dt>Citation</dt><dd>' + esc(M.structures[structs[0]].structure.citation) + '</dd>' : '') +
      '<dt>Miller setting</dt><dd class="small">' + esc(recs[0].facet.setting.description) + (recs[0].facet.setting.other_settings ? '; ' + esc(recs[0].facet.setting.other_settings) : '') + '</dd>' +
      '</dl></div>';
    if (!ff.validated_for_material) h += '<div class="banner">' + esc(ff.note || 'Force field not validated for this material.') + '</div>';
    h += '<div class="row"><label>Force field <select id="sel-ff">' + M.ffs.map((f) => '<option' + (f === st.ff ? ' selected' : '') + '>' + esc(f) + '</option>').join('') + '</select></label>';
    if (shapes.length) {
      const why = { wulff: wshapes.length ? '' : 'no equilibrium (Wulff) shape for this material', bfdh: wshapes.length ? '' : 'no facet set to build BFDH on',
        ae: aeShape ? '' : 'no attachment-energy records for this material and force field' };
      h += '<span class="seglabel">Shape</span><div class="seg" role="radiogroup" aria-label="Shape construction" id="sel-construction">' +
        CONSTRUCTIONS.map(([k, l]) => '<button type="button" role="radio" data-c="' + k + '" aria-checked="' + (st.construction === k) + '"' +
          (why[k] ? ' disabled title="' + esc(why[k]) + '"' : '') + '>' + esc(l) + '</button>').join('') + '</div>';
      if (wshapes.length) {
        h += '<label>Level <select id="sel-level"' + (st.construction === 'ae' ? ' disabled title="the growth (AE) shape has one level: static slices"' : '') + '>' +
          wshapes.map((s) => '<option value="' + esc(s.level) + '"' + (s.level === st.level ? ' selected' : '') + '>' + (s.level === 'best' ? 'best (L2 where relaxed)' : 'L' + esc(s.level) + ' unrelaxed') + '</option>').join('') + '</select></label>';
      }
      if (st.construction !== 'bfdh') h += '<label><input type="checkbox" id="chk-bfdh"' + (st.bfdh ? ' checked' : '') + '> BFDH overlay</label>';
    }
    h += '<label><input type="checkbox" id="chk-all"' + (st.allTerms ? ' checked' : '') + '> every termination</label>' +
      '<a href="#/compare/' + encodeURIComponent(M.id) + '">compare...</a></div></div>';

    if (shape) {
      h += '<div class="grid2"><div class="card"><div class="row" style="justify-content:space-between"><h2 id="shape-title">' + shapeTitle(shape) + '</h2>' +
        '<label class="small">Colour <select id="sel-color"><option value="family"' + (STATE.colorBy === 'family' ? ' selected' : '') + '>by facet family</option>' +
        '<option value="gamma"' + (STATE.colorBy === 'gamma' ? ' selected' : '') + '>by ' + q.name + '</option></select></label></div>' +
        (STATE.colorBy === 'gamma' ? '<div class="legend-ramp">' + q.name + ' low <span class="ramp"></span> high' + (st.bfdh && q.k !== 'bfdh' ? ' &nbsp;<span class="legend-line"></span> BFDH (same volume)' : '') + '</div>'
          : (st.bfdh && q.k !== 'bfdh' ? '<div class="legend-ramp"><span class="legend-line"></span> BFDH (same volume)</div>' : '')) +
        '<div id="plot3d" class="plot3d" role="img" aria-label="3D Wulff shape of ' + esc(M.name) + '"></div>' +
        '<div id="famlegend" class="famlegend" aria-label="facet families on the shape"></div>' +
        '<p class="small muted">Computed in your browser from the facet normals and ' + (q.k === 'ae' ? 'attachment energies (distance |E_att|)' : q.k === 'bfdh' ? 'slice widths (distance 1/d_eff)' : 'gammas') +
        ' (half-space intersection; data-file axes, a along x). The facet-family colours are the same in the three shapes. Hover a face for its (h k l), ' + q.plain + ', area and ' + (q.k === 'ae' ? 'growth slice' : 'termination') + '. ' +
        (q.k === 'bfdh' ? 'BFDH area fractions recorded in ' : 'Recorded shape: ') + shapeLink(shape, esc(shape.shape_id)) + '.</p><div id="selfcheck"></div></div>' +
        '<div class="card whatif"><h2>What if</h2><p class="small muted">' + (q.k === 'bfdh' ? 'BFDH is geometric (1/d_eff): there is no energy to vary. Pick the equilibrium or growth shape.'
          : 'Change one ' + q.plain + ' and watch the shape respond. What-if values are not data.') + '</p>' +
        '<div class="row"><label>Facet <select id="wf-fam"' + (q.k === 'bfdh' ? ' disabled' : '') + '>' + shape.facets.map((f) => '<option' + (f.family === st.wfam ? ' selected' : '') + '>' + esc(f.family) + '</option>').join('') + '</select></label>' +
        '<button type="button" id="wf-reset"' + (q.k === 'bfdh' ? ' disabled' : '') + '>Reset all</button></div>' +
        '<input type="range" id="wf-slider" min="0.6" max="1.4" step="0.005" value="1" aria-label="' + q.plain + ' factor"' + (q.k === 'bfdh' ? ' disabled' : '') + '>' +
        '<div class="small" id="wf-val"></div><div class="spacer"></div><div id="metrics" class="tiles"></div>' +
        '<div class="spacer"></div><h3>Shape flags</h3><div>' + (shape.flags.length ? shape.flags.map(badge).join('') : '<span class="muted small">none</span>') + '</div></div></div>' +
        '<div class="spacer"></div><div class="card"><h2>Uncertainty: area fraction per family</h2>' +
        '<p class="small muted">' + (q.k === 'bfdh' ? 'Bars: the BFDH area fraction recorded with the Wulff shape (pymatgen); BFDH has no resampling. Dots: the shape in the browser.'
          : 'Bars: the recorded area fraction (pymatgen) with its 5-95 % band from resampling every ' + q.plain + (q.k === 'ae' ? ' by &plusmn;5 % (a sensitivity, not an error bar)' : ' within its sigma') + ' (' + esc(shape.sensitivity.n_samples) + ' samples). Dots: the shape in the browser now (equal to the bars until you move the slider).') + '</p>' +
        '<div id="unc" class="plot"></div></div>';
      if (q.k === 'ae') h += aeExplainer(aeShape, aeRecs);
    } else {
      h += '<div class="card"><h2>Facet energies</h2><div class="banner info">No Wulff shape for this material and force field: ' +
        (new Set(recs.filter((r) => r.result.status === 'ok').map((r) => r.facet.family)).size) + ' facet families computed. ' + esc(D.index.no_shape.why) + '.</div>' +
        '<div id="strip" class="plot" style="height:260px"></div></div>';
    }
    h += '<div class="card"><h2>Facet table</h2><p class="small muted">Every gamma links to its record (inputs, protocol, flags, run). ' +
      (st.allTerms ? 'All terminations.' : 'The lowest termination of each run (and failed runs); tick "every termination" for all.') + (shape ? ' Highlighted rows are the inputs of the shape shown.' : '') + '</p>' +
      '<div class="tablewrap">' + facetTable(recs, shape, st.allTerms, famColors(M), aeRecs) + '</div></div>';
    app.innerHTML = h;
    document.querySelectorAll('#sel-construction button').forEach((b) => {
      b.onclick = () => { if (b.disabled || st.construction === b.dataset.c) return; st.construction = b.dataset.c; st.whatif = {}; route(); };
    });

    $('#sel-ff').onchange = (e) => { st.ff = e.target.value; st.whatif = {}; location.hash = '#/m/' + encodeURIComponent(M.id) + '?ff=' + encodeURIComponent(st.ff); };
    $('#chk-all').onchange = (e) => { st.allTerms = e.target.checked; route(); };
    const cols = famColors(M);
    if (!shape) { drawStrip($('#strip'), recs, cols); return; }
    if ($('#sel-level')) $('#sel-level').onchange = (e) => { st.level = e.target.value; st.whatif = {}; route(); };
    if ($('#chk-bfdh')) $('#chk-bfdh').onchange = (e) => { st.bfdh = e.target.checked; route(); };
    if (!st.wfam || !shape.facets.some((f) => f.family === st.wfam)) st.wfam = shape.facets[0].family;
    const fam = $('#wf-fam'), sl = $('#wf-slider');
    $('#sel-color').onchange = (e) => { STATE.colorBy = e.target.value; route(); };
    const base = (f) => dist(shape.facets.find((x) => x.family === f));
    const syncSlider = () => { sl.value = st.whatif[st.wfam] != null ? st.whatif[st.wfam] / base(st.wfam) : 1; };
    const redraw = () => {
      const res = drawShape($('#plot3d'), shape, { overrides: st.whatif, bfdh: st.bfdh && q.k !== 'bfdh', colors: cols });
      if (res.ok) {
        const on = shape.facets.filter((f) => res.areaFraction[f.family] > 1e-4).sort((a, b) => res.areaFraction[b.family] - res.areaFraction[a.family]);
        $('#famlegend').innerHTML = on.map((f) => '<span class="famitem">' + (STATE.colorBy === 'family' ? swatch(cols[f.family]) : '') +
          '<span class="mono">' + esc(f.family) + '</span> ' + (res.areaFraction[f.family] < 0.0005 ? '&lt;0.1' : fmt(100 * res.areaFraction[f.family], 1)) + ' %</span>').join('');
      }
      window.__dash.lastBuild = { shape_id: shape.shape_id, construction: shape.construction || 'wulff', overrides: Object.assign({}, st.whatif), areaFraction: res.areaFraction, ok: res.ok };
      const g = st.whatif[st.wfam];
      $('#wf-val').innerHTML = esc(st.wfam) + ': ' + q.plain + ' <span class="val">' + fmt(g != null ? g : base(st.wfam), q.d) + '</span> ' + q.units +
        (g != null ? ' (recorded ' + fmt(base(st.wfam), q.d) + ')' : ' (recorded)') + '; area fraction now <span class="val">' + fmt(res.areaFraction[st.wfam], 4) + '</span>' +
        (Object.keys(st.whatif).length ? '<br><span class="badge warning">what-if: ' + Object.entries(st.whatif).map(([k, v]) => esc(k) + ' ' + fmt(v, 1)).join(', ') + '</span>' : '');
      if (res.ok) {
        const mt = Wulff.metrics(res);
        const onS = shape.facets.filter((f) => res.areaFraction[f.family] > 1e-4).length;
        $('#metrics').innerHTML = tile(onS + ' / ' + shape.facets.length, 'families on the shape') + tile(fmt(mt.shape_factor, 3), 'shape factor') +
          tile(fmt(mt.weighted_surface_energy, q.k === 'bfdh' ? 3 : 1), 'area-weighted ' + q.plain + ', ' + q.units.replace(/<\/?sup>/g, '')) +
          tile(fmt(shape.shape_metrics.aspect_ratios.e2_over_e1, 2) + ' / ' + fmt(shape.shape_metrics.aspect_ratios.e3_over_e1, 2), 'aspect e2/e1, e3/e1 (recorded)');
      }
      drawUncertainty($('#unc'), shape, res, cols, q);
    };
    fam.onchange = (e) => { st.wfam = e.target.value; syncSlider(); redraw(); };
    sl.oninput = () => {
      const f = +sl.value;
      if (Math.abs(f - 1) < 1e-9) delete st.whatif[st.wfam]; else st.whatif[st.wfam] = base(st.wfam) * f;
      redraw();
    };
    $('#wf-reset').onclick = () => { st.whatif = {}; syncSlider(); redraw(); };
    syncSlider();
    redraw();
    const c = checkShape(shape);
    $('#selfcheck').innerHTML = '<div class="banner ' + (c.ok ? 'ok' : '') + ' small">Browser vs pymatgen (' + (q.k === 'bfdh' ? 'the recorded BFDH fractions' : q.k === 'ae' ? 'the recorded growth shape' : 'the recorded shape') + '): max |&Delta; area fraction| ' +
      c.max_abs_diff.toExponential(1) + ' over ' + c.n_families + ' families; BFDH ' + c.max_abs_diff_bfdh.toExponential(1) + (c.ok ? ' (agree to 3 decimals)' : ' (DISAGREE)') + '.</div>';
  }

  function facetTable(recs, shape, all, cols, aeRecs) {
    const wulff = shape && !isAE(shape) && shape.construction !== 'bfdh';
    const used = new Set(wulff ? shape.input_record_ids : []);
    const area = {};
    if (shape) for (const f of shape.facets) area[f.family] = f;
    const aeBy = {};
    for (const a of aeRecs || []) aeBy[a.facet.family] = a;
    const hasAE = Object.keys(aeBy).length > 0;
    const seen = new Set();
    const rows = recs.filter((r) => all || r.result.status === 'failed' || r.termination.is_lowest !== false)
      .sort((a, b) => a.facet.family.localeCompare(b.facet.family) || a.protocol.level - b.protocol.level ||
        (a.result.gamma == null) - (b.result.gamma == null) || (a.result.gamma || 0) - (b.result.gamma || 0));
    let h = '<table><thead><tr><th>Family</th><th>(h k l) cut</th><th>Level</th><th class="num">T (K)</th><th class="num">&gamma; &plusmn; &sigma; (mJ/m<sup>2</sup>)</th>' +
      (hasAE ? '<th class="num" title="attachment energy of the growth slice (static), kcal/mol per molecule">E<sub>att</sub> (kcal/mol)</th>' : '') +
      '<th>Termination</th><th>Flags</th>' + (shape ? '<th class="num">Area (' + (wulff ? 'shape' : isAE(shape) ? 'growth shape' : 'BFDH') + ')</th>' : '') + '<th>Run / provenance</th></tr></thead><tbody>';
    for (const r of rows) {
      const R = r.result, P = r.protocol, pv = r.provenance;
      const isUsed = used.has(r.record_id);
      const g = R.status === 'ok' ? recLink(r, fmt(R.gamma, 2) + (R.gamma_error ? ' &plusmn; ' + fmt(R.gamma_error, 1) : '')) : recLink(r, 'failed');
      const sf = area[r.facet.family];
      const first = !seen.has(r.facet.family);
      seen.add(r.facet.family);
      const a = aeBy[r.facet.family];
      const aeCell = !hasAE ? '' : '<td class="num">' + (first && a ? aeLink(a, fmt(a.result.E_att, 2)) + '<div class="small muted">slice ' + fmt(a.slice.d_eff, 2) + ' &Aring;, ' + esc(a.slice.n_slice) + ' mol' +
        (a.result.flags.some((x) => x.code === 'cut_rules_disagree') ? '; cuts differ' : '') + '</div>' : '') + '</td>';
      const areaCell = !shape ? '' : '<td class="num">' + (wulff ? (isUsed && sf ? shapeLink(shape, fmt(sf.area_fraction, 4), sf.family) + (sf.level !== P.level || Math.abs(sf.gamma - R.gamma) > 1e-6 ? '<div class="small muted">built with ' + fmt(sf.gamma, 2) + '</div>' : '') : '')
        : (first && sf ? shapeLink(shape, fmt(sf.area_fraction, 4), sf.family) : '')) + '</td>';
      h += '<tr class="' + (isUsed ? 'used' : '') + (R.status === 'failed' ? ' failed' : '') + '"><td class="mono">' + (cols && cols[r.facet.family] ? swatch(cols[r.facet.family]) : '') + esc(r.facet.family) + '</td>' +
        '<td class="mono">(' + esc(r.facet.hkl.join(' ')) + ')</td><td><span class="badge lvl">' + esc(levelName[P.level]) + '</span></td>' +
        '<td class="num">' + fmt(P.temperature_K, 0) + '</td><td class="num">' + g + (R.gamma_unrelaxed != null ? '<div class="small muted">unrelaxed ' + fmt(R.gamma_unrelaxed, 2) + '</div>' : '') +
        (R.gamma_raw != null ? '<div class="small muted">raw ' + fmt(R.gamma_raw, 2) + '</div>' : '') + '</td>' + aeCell +
        '<td>' + esc(r.termination.label) + (r.termination.faces_equivalent === false ? '<div class="small muted">faces differ</div>' : '') + '</td>' +
        '<td>' + R.gamma_flags.filter((f) => f.code !== 'force_field_not_validated').map(badge).join('') + '</td>' +
        areaCell +
        '<td class="small">' + recLink(r, esc(pv.run_id ? pv.run_id.slice(0, 18) : pv.source)) + '<div class="muted">' + esc(pv.source) + ', ' + esc(String(pv.date).slice(0, 10)) + '</div></td></tr>';
    }
    return h + '</tbody></table>';
  }

  function drawUncertainty(el, shape, res, cols, q) {
    q = q || qty(shape);
    const fs = shape.facets.slice().sort((a, b) => a.area_fraction - b.area_fraction);
    const y = fs.map((f) => f.family);
    const bars = { type: 'bar', orientation: 'h', y, x: fs.map((f) => f.area_fraction), name: 'recorded (pymatgen), 5-95 % band',
      marker: { color: cols && STATE.colorBy === 'family' ? fs.map((f) => cols[f.family] || PALETTE[9]) : css('--series-1') },
      error_x: { type: 'data', symmetric: false, array: fs.map((f) => Math.max(0, f.area_p95 - f.area_fraction)), arrayminus: fs.map((f) => Math.max(0, f.area_fraction - f.area_p05)), color: css('--ink-2'), thickness: 1.5, width: 3 },
      customdata: fs.map((f) => [f.area_mean, f.area_std, f.area_p05, f.area_p95, dist(f), f.sigma, f.level]),
      hovertemplate: '%{y}: %{x:.4f}<br>mean %{customdata[0]:.4f} &plusmn; %{customdata[1]:.4f}<br>5-95 %: %{customdata[2]:.4f} - %{customdata[3]:.4f}<br>' + q.plain +
        ' %{customdata[4]:.' + q.d + 'f} &plusmn; %{customdata[5]:.' + q.d + 'f}' + (q.k === 'wulff' ? ' (L%{customdata[6]})' : '') + '<extra></extra>' };
    const dots = { type: 'scatter', mode: 'markers', y, x: fs.map((f) => res.areaFraction ? res.areaFraction[f.family] : null), name: 'browser, current ' + q.plain,
      marker: { color: css('--ink'), size: 8, line: { color: css('--surface'), width: 2 } }, hovertemplate: '%{y}: %{x:.4f} (browser)<extra></extra>' };
    const layout = Object.assign(plotTheme(), { margin: { l: 80, r: 10, t: 10, b: 40 }, barmode: 'overlay', bargap: 0.35,
      xaxis: Object.assign(axis('area fraction'), { rangemode: 'tozero' }), yaxis: Object.assign(axis(''), { type: 'category' }),
      legend: { orientation: 'h', y: 1.08, x: 0 }, height: Math.max(300, 22 * fs.length + 90) });
    el.style.height = layout.height + 'px';
    Plotly.react(el, [bars, dots], layout, PCFG);
  }

  function drawStrip(el, recs, cols) {
    const ok = recs.filter((r) => r.result.status === 'ok');
    const fams = [...new Set(ok.map((r) => r.facet.family))];
    const tr = {
      type: 'scatter', mode: 'markers', x: ok.map((r) => r.result.gamma), y: ok.map((r) => r.facet.family),
      marker: { size: 11, color: ok.map((r) => { const c = (cols && cols[r.facet.family]) || css('--series-1'); return r.termination.is_lowest ? c : css('--surface'); }),
        line: { color: ok.map((r) => (cols && cols[r.facet.family]) || css('--series-1')), width: 2 } },
      customdata: ok.map((r) => [r.record_id, r.termination.label, 'L' + r.protocol.level]),
      hovertemplate: '%{y} %{customdata[1]} (%{customdata[2]}): %{x:.2f} mJ/m<sup>2</sup><br>record %{customdata[0]}<extra></extra>' };
    const layout = Object.assign(plotTheme(), { margin: { l: 80, r: 10, t: 10, b: 44 }, showlegend: false,
      xaxis: axis('gamma (mJ/m2); filled = lowest termination'), yaxis: Object.assign(axis(''), { type: 'category', categoryarray: fams }) });
    Plotly.react(el, [tr], layout, PCFG);
    el.on('plotly_click', (ev) => { const p = ev.points[0]; if (p) location.hash = '#/record/' + p.customdata[0]; });
  }

  // ------------------------------------------------------------------ explorer
  function explore(app, query) {
    const F = { q: query.get('q') || '', ff: query.get('ff') || '', T: query.get('T') || '', level: query.get('level') || '', x: query.get('x') || 'density' };
    const ffs = [...new Set(D.records.map((r) => r.force_field.name))].sort();
    const Ts = [...new Set(D.records.map((r) => r.protocol.temperature_K))].sort((a, b) => a - b);
    let h = '<div class="card"><h1>Explorer</h1><div class="row">' +
      '<label>Search <input type="search" id="q" placeholder="name, InChIKey, formula, SMILES" value="' + esc(F.q) + '" size="28"></label>' +
      '<label>Force field <select id="f-ff"><option value="">all</option>' + ffs.map((f) => '<option' + (f === F.ff ? ' selected' : '') + '>' + esc(f) + '</option>').join('') + '</select></label>' +
      '<label>T (K) <select id="f-T"><option value="">all</option>' + Ts.map((t) => '<option' + (String(t) === F.T ? ' selected' : '') + '>' + esc(t) + '</option>').join('') + '</select></label>' +
      '<label>Level <select id="f-level"><option value="">all</option>' + [1, 2, 3].map((l) => '<option value="' + l + '"' + (String(l) === F.level ? ' selected' : '') + '>' + esc(levelName[l]) + '</option>').join('') + '</select></label>' +
      '</div></div><div id="ex-body"></div>';
    app.innerHTML = h;
    const update = () => {
      F.q = $('#q').value; F.ff = $('#f-ff').value; F.T = $('#f-T').value; F.level = $('#f-level').value;
      const qs = new URLSearchParams(Object.entries(F).filter(([, v]) => v)).toString();
      history.replaceState(null, '', '#/explore' + (qs ? '?' + qs : ''));
      exploreBody($('#ex-body'), F);
    };
    ['#q', '#f-ff', '#f-T', '#f-level'].forEach((s) => { $(s).addEventListener(s === '#q' ? 'input' : 'change', update); });
    exploreBody($('#ex-body'), F);
  }

  function exploreBody(el, F) {
    const q = F.q.trim().toLowerCase();
    const match = (r) => (!F.ff || r.force_field.name === F.ff) && (!F.T || String(r.protocol.temperature_K) === F.T) && (!F.level || String(r.protocol.level) === F.level);
    const mats = matList().filter((M) => !q || [M.name, M.id, M.inchikey, M.formula, M.smiles].some((s) => s && s.toLowerCase().includes(q)));
    const rows = [];
    for (const M of mats) {
      for (const ff of M.ffs) {
        const rs = M.records.filter((r) => r.force_field.name === ff && match(r));
        if (!rs.length) continue;
        const ok = rs.filter((r) => r.result.status === 'ok' && !hasError(r));
        const min = ok.reduce((a, r) => (!a || r.result.gamma < a.result.gamma ? r : a), null);
        rows.push({ M, ff, rs, ok, min, kind: rs[0].force_field.kind });
      }
    }
    let h = '<div class="card"><div class="row" style="justify-content:space-between"><h2>Lowest gamma vs ' + (F.x === 'mass' ? 'molecular mass' : 'density') + '</h2>' +
      '<label>x <select id="f-x"><option value="density"' + (F.x !== 'mass' ? ' selected' : '') + '>density</option><option value="mass"' + (F.x === 'mass' ? ' selected' : '') + '>molecular mass</option></select></label></div>' +
      '<p class="small muted">One point per material and force field: the lowest gamma among the records that match the filters (all temperatures and levels unless filtered; records flagged as errors left out). Click a point to open the material.</p>' +
      '<div id="scatter" class="plot"></div></div>';
    h += '<div class="card"><h2>Coverage: material x facet x level</h2><div class="row small" style="margin-bottom:8px">' +
      '<span class="chip l1">L1</span> unrelaxed <span class="chip l2">L2</span> relaxed T = 0 <span class="chip l3">L3</span> MD <span class="chip fail">fail</span> failed only</div><div class="cov">';
    for (const row of rows) {
      const byFam = {};
      for (const r of row.rs) {
        const b = byFam[r.facet.family] || (byFam[r.facet.family] = { max: 0, fail: 0, n: 0 });
        b.n++;
        if (r.result.status === 'ok' && !hasError(r)) b.max = Math.max(b.max, r.protocol.level); else b.fail++;
      }
      const chips = Object.entries(byFam).sort().map(([f, b]) => '<a class="chip ' + (b.max ? 'l' + b.max : 'fail') + '" href="#/m/' + encodeURIComponent(row.M.id) + '?ff=' + encodeURIComponent(row.ff) + '" title="' +
        esc(f + ': highest level ' + (b.max || 'none') + ', ' + b.n + ' record(s)' + (b.fail ? ', ' + b.fail + ' failed or flagged' : '')) + '">' + esc(famHkl(f)) + '</a>').join('');
      h += '<div class="name"><a href="#/m/' + encodeURIComponent(row.M.id) + '?ff=' + encodeURIComponent(row.ff) + '">' + esc(row.M.name) + '</a><div class="small muted">' + esc(row.ff) + '</div></div><div class="chips">' + chips + '</div>';
    }
    h += '</div></div><div class="card"><h2>Materials (' + rows.length + ')</h2><div class="tablewrap"><table><thead><tr><th>Material</th><th>Formula</th><th>InChIKey</th><th>Force field</th>' +
      '<th class="num">Families</th><th>Levels</th><th class="num">Lowest &gamma;</th><th class="num">Density</th><th>Shape</th></tr></thead><tbody>';
    for (const row of rows) {
      const fams = new Set(row.ok.map((r) => r.facet.family));
      h += '<tr><td><a href="#/m/' + encodeURIComponent(row.M.id) + '?ff=' + encodeURIComponent(row.ff) + '">' + esc(row.M.name) + '</a></td><td>' + esc(row.M.formula) + '</td><td class="mono small">' + esc(row.M.inchikey || '') + '</td>' +
        '<td class="small">' + esc(row.ff) + '</td><td class="num">' + fams.size + '</td><td>' + [...new Set(row.ok.map((r) => 'L' + r.protocol.level))].sort().join(', ') + '</td>' +
        '<td class="num">' + (row.min ? recLink(row.min, fmt(row.min.result.gamma, 1)) + ' <span class="small muted mono">' + esc(row.min.facet.family) + '</span>' : '-') + '</td>' +
        '<td class="num">' + fmt(row.M.density, 3) + '</td><td>' + (shapesOf(row.M, row.ff).length ? 'yes' : '-') + '</td></tr>';
    }
    el.innerHTML = h + '</tbody></table></div></div>';
    $('#f-x').onchange = (e) => { F.x = e.target.value; const qs = new URLSearchParams(Object.entries(F).filter(([, v]) => v)).toString(); history.replaceState(null, '', '#/explore?' + qs); exploreBody(el, F); };
    const series = [['non-reactive', '--series-1', 'circle'], ['reactive', '--series-2', 'diamond']].map(([kind, col, sym]) => {
      const pts = rows.filter((r) => r.kind === kind && r.min);
      return { type: 'scatter', mode: 'markers', name: kind === 'reactive' ? 'ReaxFF-lg (reactive)' : 'non-reactive',
        x: pts.map((r) => (F.x === 'mass' ? r.M.mass : r.M.density)), y: pts.map((r) => r.min.result.gamma),
        customdata: pts.map((r) => [r.M.id, r.M.name, r.min.facet.family, 'L' + r.min.protocol.level, r.min.record_id, r.ff]),
        marker: { color: css(col), size: 10, symbol: sym, line: { color: css('--surface'), width: 2 } },
        hovertemplate: '%{customdata[1]}<br>%{customdata[5]}<br>lowest gamma %{y:.1f} mJ/m<sup>2</sup>, %{customdata[2]} %{customdata[3]}<br>record %{customdata[4]}<extra></extra>' };
    }).filter((s) => s.x.length);
    const layout = Object.assign(plotTheme(), { margin: { l: 60, r: 10, t: 10, b: 50 }, legend: { orientation: 'h', y: 1.1, x: 0 },
      xaxis: axis(F.x === 'mass' ? 'molecular mass (g/mol)' : 'density (g/cm3)'), yaxis: axis('lowest gamma (mJ/m2)') });
    const sc = $('#scatter');
    Plotly.react(sc, series, layout, PCFG);
    sc.on('plotly_click', (ev) => { const p = ev.points[0]; if (p) location.hash = '#/m/' + encodeURIComponent(p.customdata[0]) + '?ff=' + encodeURIComponent(p.customdata[5]); });
  }

  // ------------------------------------------------------------------ compare
  function compare(app, a, b, query) {
    const ms = matList();
    const withShape = ms.filter((M) => M.shapes.length);
    a = D.materials[a] ? a : (withShape[0] || ms[0]).id;
    b = D.materials[b] ? b : ((withShape.find((M) => M.id !== a) || ms.find((M) => M.id !== a)).id);
    const kinds = { a: (query && query.get('sa')) || 'wulff', b: (query && query.get('sb')) || 'wulff' };
    for (const k of ['a', 'b']) if (!CONSTRUCTIONS.some(([c]) => c === kinds[k])) kinds[k] = 'wulff';
    const go = (x, y, ka, kb) => { location.hash = '#/compare/' + encodeURIComponent(x) + '/' + encodeURIComponent(y) + (ka !== 'wulff' || kb !== 'wulff' ? '?sa=' + ka + '&sb=' + kb : ''); };
    const sel = (id, v) => '<select id="' + id + '">' + ms.map((M) => '<option value="' + esc(M.id) + '"' + (M.id === v ? ' selected' : '') + '>' + esc(M.name) + (M.shapes.length ? ' (shape)' : '') + (M.ae.length ? ' (AE)' : '') + '</option>').join('') + '</select>';
    const ksel = (id, v) => '<select id="' + id + '" aria-label="shape construction">' + CONSTRUCTIONS.map(([c, l]) => '<option value="' + c + '"' + (c === v ? ' selected' : '') + '>' + esc(l) + '</option>').join('') + '</select>';
    app.innerHTML = '<div class="card"><h1>Compare</h1><div class="row"><label>Left ' + sel('ca', a) + '</label><label>shape ' + ksel('ka', kinds.a) + '</label>' +
      '<label>Right ' + sel('cb', b) + '</label><label>shape ' + ksel('kb', kinds.b) + '</label></div>' +
      '<p class="small muted">Pick the same material on both sides to set its equilibrium (Wulff), growth (attachment-energy) and BFDH shapes side by side.</p></div>' +
      '<div class="cols2"><div id="side-a"></div><div id="side-b"></div></div>';
    $('#ca').onchange = (e) => go(e.target.value, b, kinds.a, kinds.b);
    $('#cb').onchange = (e) => go(a, e.target.value, kinds.a, kinds.b);
    $('#ka').onchange = (e) => go(a, b, e.target.value, kinds.b);
    $('#kb').onchange = (e) => go(a, b, kinds.a, e.target.value);
    side($('#side-a'), D.materials[a], 'a', kinds.a);
    side($('#side-b'), D.materials[b], 'b', kinds.b);
  }
  function side(el, M, k, kind) {
    const ff = M.ffs[0];
    const shape = shapeFor(M, ff, kind || 'wulff', 'best');
    const q = qty(shape);
    const recs = M.records.filter((r) => r.force_field.name === ff && r.result.status === 'ok' && !hasError(r));
    let h = '<div class="card"><h2><a href="#/m/' + encodeURIComponent(M.id) + (kind && kind !== 'wulff' ? '?shape=' + kind : '') + '">' + esc(M.name) + '</a></h2><div class="small muted">' + esc(M.formula) + ', ' + esc(ff) +
      (recs[0] && !recs[0].force_field.validated_for_material ? ' <span class="badge warning">not validated</span>' : '') + '</div>';
    if (!shape && kind === 'ae') h += '<div class="banner info small">No growth (attachment-energy) shape for this material' + (D.ae.length ? ' (attachment energies are computed for ' + esc([...new Set(D.ae.map((r) => r.material.name))].join(', ')) + ')' : '') + '.</div>';
    if (shape) {
      // the shape's own inputs: the distance it was built with (gamma, |E_att| or 1/d_eff), its level and area
      h += '<div class="small"><strong id="c-title-' + k + '">' + shapeTitle(shape) + '</strong></div><div id="c3d-' + k + '" class="plot3d" style="height:340px"></div><p class="small muted">' +
        (q.k === 'wulff' ? 'T = ' + fmt(shape.temperature_K, 0) + ' K, level ' + esc(shape.level) + ', ' : q.k === 'ae' ? 'growth morphology (kinetic, vacuum, static slices): not an equilibrium shape; ' : '') +
        (q.k === 'bfdh' ? shape.facets.filter((f) => f.area_fraction > 1e-4).length : shape.shape_metrics.n_on_shape) + ' families on the shape' +
        (q.k === 'bfdh' ? '' : '; shape factor ' + fmt(shape.shape_metrics.shape_factor, 3)) + '. ' + q.name + ' is the value the shape was built with.</p>' +
        '<div class="tablewrap"><table><thead><tr><th>Family</th><th>' + (q.k === 'ae' ? 'E<sub>att</sub> (kcal/mol)' : 'Level') + '</th><th class="num">' + q.name + ' (' + q.units + ')</th><th class="num">Area</th></tr></thead><tbody>' +
        shape.facets.map((f) => { const id = f.input_record_ids[0]; return '<tr><td class="mono">' + swatch(famColors(M)[f.family] || PALETTE[9]) + esc(f.family) + '</td><td>' +
          (q.k === 'ae' ? fmt(f.E_att, 2) : 'L' + f.level + (f.level === 1 && shape.level === 'best' ? ' (scaled)' : '')) + '</td><td class="num">' + anyLink(id, fmt(dist(f), q.d)) + '</td>' +
          '<td class="num">' + shapeLink(shape, fmt(f.area_fraction, 4), f.family) + '</td></tr>'; }).join('') + '</tbody></table></div></div>';
    } else if (kind === 'ae') {
      h += '</div>';
    } else {
      // no shape: the highest level at T = 0 of each family (its lowest termination)
      const best = {};
      for (const r of recs.filter((x) => x.protocol.temperature_K === 0)) {
        const c = best[r.facet.family];
        if (!c || r.protocol.level > c.protocol.level || (r.protocol.level === c.protocol.level && r.result.gamma < c.result.gamma)) best[r.facet.family] = r;
      }
      const fams = Object.keys(best).sort((x, y) => best[x].result.gamma - best[y].result.gamma);
      h += '<div class="banner info small">No Wulff shape (' + fams.length + ' facet families at T = 0).</div>' +
        '<div class="tablewrap"><table><thead><tr><th>Family</th><th>Level</th><th class="num">&gamma; (mJ/m<sup>2</sup>, T = 0)</th></tr></thead><tbody>' +
        fams.map((f) => '<tr><td class="mono">' + esc(f) + '</td><td>L' + best[f].protocol.level + '</td><td class="num">' + recLink(best[f], fmt(best[f].result.gamma, 2)) + '</td></tr>').join('') + '</tbody></table></div></div>';
    }
    el.innerHTML = h;
    if (shape) drawShape($('#c3d-' + k), shape, { colors: famColors(M) });
  }

  // ------------------------------------------------------------------ record and shape pages
  function recordPage(app, r) {
    const R = r.result, P = r.protocol, pv = r.provenance, f = r.facet;
    const shapes = D.shapes.filter((s) => s.input_record_ids.includes(r.record_id));
    app.innerHTML = '<div class="card"><h1>' + esc(r.material.name) + ' ' + esc(f.family) + ' <span class="muted small">record ' + esc(r.record_id) + '</span></h1>' +
      '<p><a href="#/m/' + encodeURIComponent(r.material.material_id) + '?ff=' + encodeURIComponent(r.force_field.name) + '">material page</a></p>' +
      '<div class="cols2"><dl class="kv">' +
      '<dt>Result</dt><dd>' + (R.status === 'ok' ? '<strong>' + fmt(R.gamma, 4) + '</strong> ' + esc(R.units) + (R.gamma_error ? ' &plusmn; ' + fmt(R.gamma_error, 2) : '') : '<strong>failed</strong>: ' + esc(R.error_message)) + '</dd>' +
      '<dt>Error method</dt><dd>' + esc(R.error_method || '-') + '</dd>' +
      (R.gamma_unrelaxed != null ? '<dt>Unrelaxed</dt><dd>' + fmt(R.gamma_unrelaxed, 4) + '</dd>' : '') +
      (R.gamma_raw != null ? '<dt>Sim2L raw value</dt><dd>' + fmt(R.gamma_raw, 4) + '</dd>' : '') +
      '<dt>Basis</dt><dd>' + esc(R.gamma_basis || '-') + '</dd>' +
      '<dt>Flags</dt><dd>' + (R.gamma_flags.length ? R.gamma_flags.map((x) => badge(x) + ' <span class="small">' + esc(x.detail) + '</span>').join('<br>') : 'none') + '</dd>' +
      '<dt>Facet</dt><dd>(' + esc(f.hkl.join(' ')) + ') of ' + esc(f.family) + ', d_hkl ' + fmt(f.d_hkl, 4) + ' &Aring;' + (f.d_eff ? ', d_eff ' + fmt(f.d_eff, 4) : '') + '</dd>' +
      '<dt>Setting</dt><dd class="small">' + esc(f.setting.description) + '</dd>' +
      '<dt>Termination</dt><dd>' + esc(r.termination.label) + (r.termination.n_terminations ? ' of ' + r.termination.n_terminations : '') + (r.termination.faces_equivalent === false ? ', faces differ' : '') + '</dd>' +
      '<dt>Protocol</dt><dd>' + esc(levelName[P.level]) + ', T ' + fmt(P.temperature_K, 0) + ' K, ' + esc(P.n_periods) + ' periods, vacuum ' + esc(P.vacuum_A) + ' &Aring;, search_range ' + esc(P.search_range) +
      (P.slab_from ? ', slab_from ' + esc(P.slab_from) : '') + (P.minimiser ? '; ' + esc(P.minimiser) : '') + (P.production_ps ? '; MD ' + esc(P.equilibration_ps) + ' + ' + esc(P.production_ps) + ' ps' : '') + '</dd>' +
      '<dt>Force field</dt><dd>' + esc(r.force_field.name) + (r.force_field.md5 ? ' <span class="mono small">md5 ' + esc(r.force_field.md5) + '</span>' : '') + '</dd>' +
      '</dl><dl class="kv">' +
      '<dt>Source</dt><dd>' + esc(pv.source) + '</dd><dt>Entry</dt><dd class="mono small">' + esc(pv.source_ref) + '</dd>' +
      '<dt>Sim2L</dt><dd>' + esc(pv.sim2l.name) + (pv.sim2l.revision ? ' (' + esc(pv.sim2l.revision) + ')' : '') + '</dd>' +
      '<dt>Run id</dt><dd class="mono small">' + esc(pv.run_id || 'not recorded') + '</dd>' +
      '<dt>Run folder</dt><dd class="mono small">' + esc(pv.run_ref || '-') + ' <span class="muted">(not shipped)</span></dd>' +
      (pv.child_run_ids.length ? '<dt>Child runs</dt><dd class="mono small">' + pv.child_run_ids.map(esc).join('<br>') + '</dd>' : '') +
      '<dt>Input md5</dt><dd class="mono small">' + Object.entries(pv.input_md5 || {}).map(([k, v]) => esc(k) + ' ' + esc(v)).join('<br>') + '</dd>' +
      '<dt>Date</dt><dd>' + esc(pv.date) + '</dd><dt>Venue</dt><dd>' + esc(pv.venue || '-') + '</dd>' +
      '<dt>CPU / wall</dt><dd>' + fmt(pv.cpu_s, 1) + ' / ' + fmt(pv.wall_s, 1) + ' s</dd>' +
      '<dt>Reproduce</dt><dd class="small">' + esc(pv.reproduce || '-') + '</dd>' +
      (shapes.length ? '<dt>Used in</dt><dd>' + shapes.map((s) => shapeLink(s, 'shape ' + esc(s.level) + ' ' + esc(s.shape_id))).join('<br>') + '</dd>' : '') +
      '</dl></div></div><div class="card"><h2>Full record</h2><pre class="json">' + esc(JSON.stringify(r, null, 1)) + '</pre></div>';
  }
  function shapePage(app, s, focus) {
    const q = qty(s), ae = isAE(s);
    const rows = s.facets.map((f) => '<tr' + (f.family === focus ? ' class="used"' : '') + '><td class="mono">' + esc(f.family) + '</td><td class="num">' + fmt(dist(f), q.d) + ' &plusmn; ' + fmt(f.sigma, q.d) + '</td><td>' + (ae ? fmt(f.E_att, 3) : 'L' + f.level) + '</td>' +
      '<td class="num">' + fmt(f.area_fraction, 4) + '</td><td class="num">' + fmt(f.area_p05, 3) + ' - ' + fmt(f.area_p95, 3) + '</td><td class="num">' + fmt(f.bfdh_area_fraction, 4) + '</td>' +
      '<td>' + f.input_record_ids.map((id) => anyLink(id, id)).join(' ') + '</td><td class="small">' + esc(f.basis) + ' ' + f.flags.map(badge).join('') + '</td></tr>').join('');
    app.innerHTML = '<div class="card"><h1>' + esc(s.material_name) + ': ' + (ae ? 'growth (attachment-energy) shape' : 'Wulff shape') + ' <span class="muted small">' + esc(s.shape_id) + '</span></h1>' +
      '<p><a href="#/m/' + encodeURIComponent(s.material_id) + (ae ? '?shape=ae' : '') + '">material page</a> &middot; ' + esc(s.force_field) + ' &middot; ' + (ae ? 'static slices' : 'T ' + fmt(s.temperature_K, 0) + ' K') + ' &middot; level ' + esc(s.level) + '</p>' +
      '<p class="small">' + esc(s.level_description) + '</p><div class="tiles">' + tile(s.shape_metrics.n_on_shape + ' / ' + s.facets.length, 'families on the shape') +
      tile(fmt(s.shape_metrics.shape_factor, 3), 'shape factor') + tile(fmt(s.shape_metrics.anisotropy, 3), 'anisotropy') + tile(fmt(s.shape_metrics.weighted_surface_energy, 1), 'area-weighted ' + q.plain) +
      tile(fmt(s.sensitivity.max_area_std, 3), 'max area std') + '</div>' +
      '<p class="small">' + s.flags.map((f) => badge(f) + ' ' + esc(f.detail)).join('<br>') + '</p></div>' +
      '<div class="card"><h2>Facets</h2><div class="tablewrap"><table><thead><tr><th>Family</th><th class="num">' + q.name + ' &plusmn; &sigma;</th><th>' + (ae ? 'E<sub>att</sub>' : 'Level') + '</th><th class="num">Area</th><th class="num">5-95 %</th><th class="num">BFDH</th><th>Input record</th><th>Basis</th></tr></thead><tbody>' + rows + '</tbody></table></div></div>' +
      '<div class="card"><h2>Full record</h2><pre class="json">' + esc(JSON.stringify(s, null, 1)) + '</pre></div>';
  }

  /** what the growth (AE) shape is, with this material's numbers from its attachment_energy records */
  function aeExplainer(shape, recs) {
    const n = recs.length;
    const nd = recs.filter((r) => r.result.flags.some((x) => x.code === 'cut_rules_disagree')).length;
    const reax = recs.some((r) => r.result.qeq);
    const qm = recs.filter((r) => r.result.qeq && r.result.qeq.measured).map((r) => r.result.qeq.mean_abs_dq);
    const r0 = recs[0];
    const E = r0 ? r0.result.E_latt_conventions : {};
    const gr = r0 ? r0.result.gas_reference || {} : {};
    const big = shape.facets.filter((f) => f.on_shape).slice(0, 3).map((f) => esc(f.family) + ' ' + fmt(100 * f.area_fraction, 1) + ' %').join(', ');
    return '<div class="spacer"></div><div class="card" id="ae-explain"><h2>What the growth (AE) shape is</h2>' +
      '<p class="small">The <strong>attachment energy</strong> E<sub>att</sub> is the energy released when one growth slice (whole molecules in a layer of thickness d<sub>eff</sub> = d<sub>hkl</sub> / n, as in BFDH) attaches to the crystal face: E<sub>att</sub> = E<sub>latt</sub> &minus; E<sub>slice</sub>. ' +
      'In the Hartman-Perdok model a face grows at a rate proportional to |E<sub>att</sub>|, so the slow faces (small |E<sub>att</sub>|) dominate the grown crystal. The shape is the Wulff construction with |E<sub>att</sub>| in place of &gamma;: a <strong>growth (kinetic) morphology, not the equilibrium shape</strong>. Here: ' + big + '.</p>' +
      '<ul class="small">' +
      '<li><strong>Static, vacuum.</strong> Unrelaxed slices and bulk; no solvent, no supersaturation, no surface relaxation.</li>' +
      '<li><strong>AE &asymp; Wulff(&gamma; / d<sub>eff</sub>) &asymp; BFDH.</strong> For nearest-slice interactions |E<sub>att</sub>| = 2 &gamma; V<sub>m</sub> / d<sub>eff</sub>, so AE multiplies the energetic (Wulff) ranking by BFDH\'s preference for wide slices; it usually looks much more like BFDH than like the Wulff shape.</li>' +
      '<li><strong>Growth slice.</strong> Of the cuts of each family, the one with the lowest |E<sub>att</sub>| (the most strongly bound slice). In ' + nd + ' of ' + n + ' families the lowest-&gamma; cut is another one (flag <em>cut rules disagree</em>; both E<sub>att</sub> are in the record).</li>' +
      (reax ? '<li><strong>ReaxFF charges.</strong> QEq re-equilibrates the charges of each isolated slice, so E<sub>att</sub> includes a charge response that a fixed-charge force field leaves out' + (qm.length ? ' (mean |&Delta;q| ' + fmt(Math.min(...qm), 3) + '-' + fmt(Math.max(...qm), 3) + ' e where measured)' : '') + '.</li>' : '') +
      (r0 ? '<li><strong>Lattice energy</strong> (kcal/mol per molecule): ' + fmt(E.rigid, 2) + ' with the rigid-molecule gas reference (used for E<sub>slice</sub>); ' + fmt(E.static_minus_minimised_gas, 2) + ' against the minimised molecule (includes ' + fmt(gr.intramolecular_strain_mean, 1) + ' of intramolecular strain)' +
        (E.relaxed != null ? '; ' + fmt(E.relaxed, 2) + ' with the bulk relaxed' : '') + '. E<sub>att</sub> does not depend on the gas reference.</li>' : '') +
      '<li>The 5-95 % bands resample |E<sub>att</sub>| by &plusmn;5 %: a sensitivity, not an error bar. A match with grown crystals is the expected direction for a growth model, not proof that the energies are right.</li>' +
      '</ul><p class="small muted">Records: ' + recs.map((r) => aeLink(r, esc(r.facet.family))).join(' ') + '. Shape ' + shapeLink(shape, esc(shape.shape_id)) + '.</p></div>';
  }

  function aePage(app, r) {
    const R = r.result, S = r.slice, f = r.facet, pv = r.provenance;
    const shapes = D.shapes.filter((s) => s.input_record_ids.includes(r.record_id));
    const E = R.E_latt_conventions || {};
    const cuts = (S.cuts || []).map((c) => '<tr' + (c.index === S.cut_index ? ' class="used"' : '') + '><td>' + esc(c.index) + (c.index === S.cut_index ? ' (growth slice)' : '') + (c.index === S.lowest_gamma_cut_index ? ' (lowest &gamma;)' : '') + '</td><td class="num">' + fmt(c.cut_z, 3) + '</td><td class="num">' + fmt(c.gap, 3) + '</td><td class="num">' + fmt(c.E_att, 3) + '</td><td class="num">' + fmt(c.E_slice, 3) + '</td><td class="num">' + fmt(c.gamma_unrelaxed, 2) + '</td><td>' + (c.faces_equivalent === false ? 'faces differ' : '') + '</td></tr>').join('');
    app.innerHTML = '<div class="card"><h1>' + esc(r.material.name) + ' ' + esc(f.family) + ' <span class="muted small">attachment energy ' + esc(r.record_id) + '</span></h1>' +
      '<p><a href="#/m/' + encodeURIComponent(r.material.material_id) + '?ff=' + encodeURIComponent(r.force_field.name) + '&shape=ae">material page (growth shape)</a></p>' +
      '<div class="cols2"><dl class="kv">' +
      '<dt>E<sub>att</sub></dt><dd><strong>' + fmt(R.E_att, 4) + '</strong> ' + esc(R.units) + ' (' + fmt(R.E_att_kJ_per_mol, 2) + ' kJ/mol)</dd>' +
      '<dt>Lowest-&gamma; cut</dt><dd>E<sub>att</sub> ' + fmt(R.E_att_lowest_gamma_cut, 4) + ' (cut ' + esc(S.lowest_gamma_cut_index) + ', &gamma; ' + fmt(R.gamma_unrelaxed_min, 2) + ' mJ/m<sup>2</sup>)</dd>' +
      '<dt>E<sub>slice</sub></dt><dd>' + fmt(R.E_slice, 4) + ' (' + esc(R.energy_convention) + ' gas reference)</dd>' +
      '<dt>E<sub>latt</sub></dt><dd>' + fmt(R.E_latt, 4) + ' (' + esc(R.energy_convention) + '); static &minus; minimised gas ' + fmt(E.static_minus_minimised_gas, 3) + '; relaxed ' + fmt(E.relaxed, 3) + '</dd>' +
      '<dt>Slice</dt><dd>d<sub>eff</sub> ' + fmt(S.d_eff, 4) + ' &Aring; (d<sub>hkl</sub> ' + fmt(f.d_hkl, 4) + ' / n ' + esc(S.extinction_factor) + '), ' + esc(S.n_slice) + ' molecules, area ' + fmt(S.area_A2, 2) + ' &Aring;<sup>2</sup>; rule ' + esc(S.rule) + ', cut ' + esc(S.cut_index) + ' of ' + esc(S.n_cuts) + '</dd>' +
      '<dt>QEq</dt><dd>' + (R.qeq ? (R.qeq.measured ? 'mean |&Delta;q| ' + fmt(R.qeq.mean_abs_dq, 4) + ' e, max ' + fmt(R.qeq.max_abs_dq, 3) + ' e (bulk rms ' + fmt(R.qeq.rms_q_bulk, 3) + ')' : esc(R.qeq.note)) : 'n/a (fixed charges)') + '</dd>' +
      '<dt>Flags</dt><dd>' + (R.flags.length ? R.flags.map((x) => badge(x) + ' <span class="small">' + esc(x.detail) + '</span>').join('<br>') : 'none') + '</dd>' +
      '<dt>Facet</dt><dd>(' + esc(f.hkl.join(' ')) + ') of ' + esc(f.family) + '</dd><dt>Setting</dt><dd class="small">' + esc(f.setting.description) + '</dd>' +
      '</dl><dl class="kv">' +
      '<dt>Source</dt><dd>' + esc(pv.source) + '</dd><dt>Entry</dt><dd class="mono small">' + esc(pv.source_ref) + '</dd>' +
      '<dt>Tool</dt><dd>' + esc(pv.sim2l.name) + (pv.sim2l.revision ? ' (' + esc(pv.sim2l.revision) + ')' : '') + '</dd>' +
      '<dt>Oriented cell</dt><dd class="mono small">' + esc(pv.run_ref || '-') + ' <span class="muted">(not shipped)</span></dd>' +
      '<dt>Input md5</dt><dd class="mono small">' + Object.entries(pv.input_md5 || {}).map(([k, v]) => esc(k) + ' ' + esc(v)).join('<br>') + '</dd>' +
      '<dt>Date</dt><dd>' + esc(pv.date) + '</dd><dt>Reproduce</dt><dd class="small">' + esc(pv.reproduce || '-') + '</dd>' +
      ((r.related_record_ids || []).length ? '<dt>Level-1 &gamma;</dt><dd>' + r.related_record_ids.map((id) => anyLink(id, id)).join('<br>') + '</dd>' : '') +
      (shapes.length ? '<dt>Used in</dt><dd>' + shapes.map((s) => shapeLink(s, 'growth shape ' + esc(s.shape_id))).join('<br>') + '</dd>' : '') +
      '</dl></div></div>' +
      '<div class="card"><h2>Cuts</h2><div class="tablewrap"><table><thead><tr><th>Cut</th><th class="num">z (&Aring;)</th><th class="num">gap (&Aring;)</th><th class="num">E<sub>att</sub></th><th class="num">E<sub>slice</sub></th><th class="num">&gamma; 2-period (mJ/m<sup>2</sup>)</th><th></th></tr></thead><tbody>' + cuts + '</tbody></table></div></div>' +
      '<div class="card"><h2>Full record</h2><pre class="json">' + esc(JSON.stringify(r, null, 1)) + '</pre></div>';
  }

  // ------------------------------------------------------------------ boot
  function setTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    try { localStorage.setItem('wh-theme', t); } catch (e) { /* storage blocked: fine */ }
    route();
  }
  $('#theme-toggle').onclick = () => {
    const cur = document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    setTheme(cur === 'dark' ? 'light' : 'dark');
  };
  window.__dash.setTheme = setTheme;
  load().then(() => {
    window.addEventListener('hashchange', route);
    route();
    window.__dash.ready = true;
  }).catch((e) => {
    window.__dash.errors.push('load: ' + e.message);
    $('#app').innerHTML = '<div class="card"><h1>Could not load the dataset</h1><p>' + esc(e.message) + '</p><p class="small">Serve the repository root over HTTP (tools/serve_dashboard.sh) and open /dashboard/; the page reads ' + esc(BASE) + '.</p></div>';
  });
})();

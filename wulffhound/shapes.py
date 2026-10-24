"""Wulff shapes straight from dataset records, for materials without a wulffhound bundle (CIF-zoo crystals and
campaign materials), with the same resampling and metrics as the bundle shapes of tools/export_dataset.py:

  level 1     unrelaxed, the lowest termination of every family (level1_shape);
  level best  the engine's rule (engine.gamma_table('best')): the relaxed T = 0 value (level 2) where a family has
              one that passes the consistency guard, else its level-1 value x the mean relaxed/unrelaxed ratio of
              the families that have both; sigma max(gamma_error, 2 %) for level 2, (std of the ratios + 2 %, floor
              5 %) x gamma for the scaled level-1 values (best_shape).

    from shapes import level1_shape, best_shape
    rec, why = level1_shape(records_of_one_material, flag=..., rid=..., schema_version='0.1.1')
    rec, why = best_shape(records_of_one_material, flag=..., rid=..., schema_version='0.1.1')

A shape is built only when (1) at least MIN_FAMILIES families have a usable gamma, (2) their Laue orbits
(facet.family_members) enclose the origin (bounded shape), and (3) pymatgen's reciprocal-lattice symmetry gives
exactly those orbits, with as many operations as the Laue class of the record's space group at symprec 1e-5 and
1e-3 (2/m: 4, mmm: 8, ...); else WulffShape would add faces the records do not have, and the browser, which uses
the orbits, would disagree. Otherwise (None, reason).
"""
import datetime
import warnings

import numpy as np

MIN_FAMILIES = 3
SIGMA1_REL = 0.05     # level-1 values as a T = 0 stand-in: 5 % (the engine's level-1 floor)
SIGMA2_REL = 0.02     # level 2 (engine.SIGMA2_REL)
AREA_ON = 1e-4
LAUE_ORDER = {'-1': 2, '2/m': 4, 'mmm': 8, '4/m': 8, '4/mmm': 16, '-3': 6, '-3m': 12, '6/m': 12, '6/mmm': 24,
              'm-3': 24, 'm-3m': 48}


def _bad(r):
    return any(f['severity'] == 'error' for f in r['result']['gamma_flags'])


def _usable(r):
    return (r['result']['status'] == 'ok' and r['result']['gamma'] is not None and r['result']['gamma'] > 0
            and r['protocol']['level'] == 1 and not _bad(r))


def pick_level1(records):
    """family -> the record with the lowest usable level-1 gamma (any source, campaign records included)."""
    best = {}
    for r in records:
        if not _usable(r):
            continue
        f = r['facet']['family']
        if f not in best or r['result']['gamma'] < best[f]['result']['gamma'] - 1e-12:
            best[f] = r
    return best


def pick_level2(records):
    """(family -> the newest ok level-2 T = 0 record of the lowest termination that passes the guard,
    family -> [records that fail it]). Level 2 is one run per family (the lowest unrelaxed termination); the newer
    run wins, as in the engine."""
    good, bad = {}, {}
    for r in records:
        P, R = r['protocol'], r['result']
        if P['level'] != 2 or float(P.get('temperature_K') or 0) != 0 or R['status'] != 'ok' or R['gamma'] is None:
            continue
        if r['termination'].get('is_lowest') is False:
            continue
        f = r['facet']['family']
        if _bad(r) or R['gamma'] <= 0:
            bad.setdefault(f, []).append(r)
            continue
        if f not in good or str(r['provenance'].get('date') or '') > str(good[f]['provenance'].get('date') or ''):
            good[f] = r
    return good, bad


def _checks(L, fams, members, reps, laue):
    """None if pymatgen's reciprocal-lattice operations give exactly the records' orbits (and as many operations as
    the Laue class at symprec 1e-5 and 1e-3), else the reason."""
    from pymatgen.core import Lattice
    lat = Lattice(L)
    out = {}
    for tol in (1e-5, 1e-3):
        ops = lat.get_recp_symmetry_operation(tol)
        out[tol] = len(ops)
        for f in fams:
            pm = {tuple(int(round(x)) for x in op.operate(reps[f])) for op in ops}
            if pm != {tuple(m) for m in members[f]}:
                return None, ('pymatgen\'s lattice symmetry (symprec %g) gives %d members for %s, the Laue orbit %d: '
                              'WulffShape would add faces (Laue class lower than the metric)' % (
                                  tol, len(pm), f, len(members[f])))
    want = LAUE_ORDER.get(laue)
    if want is not None and not (out[1e-5] == out[1e-3] == want):
        return None, ('expected the %s Laue class (%d operations) from pymatgen at symprec 1e-5 and 1e-3, got %d and %d'
                      % (laue, want, out[1e-5], out[1e-3]))
    return {'orbits_equal_pymatgen_recip_ops': True, 'pymatgen_recip_ops_order': out[1e-5],
            'pymatgen_recip_ops_symprec_1em3': out[1e-3], 'wulffshape_symprec': 1e-5, 'bounded': True,
            'laue_class': laue, 'laue_order_expected': want,
            'note': 'the Laue orbits of the records equal the reciprocal-lattice operations of the metric (asserted at '
                    'symprec 1e-5 and 1e-3%s), so pymatgen and the browser build the same shape' % (
                        ', %d operations = %s' % (want, laue) if want else '')}, None


def _closed(L, fams, members):
    from scipy.spatial import ConvexHull
    recip = np.linalg.inv(L).T
    normals = [np.array(m) @ recip for f in fams for m in members[f]]
    try:
        hull = ConvexHull(np.array([n / np.linalg.norm(n) for n in normals]))
        return bool(np.all(hull.equations[:, -1] < -1e-6))
    except Exception:
        return False


def _wulff(L, reps_list, gs):
    from pymatgen.analysis.wulff import WulffShape
    from pymatgen.core import Lattice
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        w = WulffShape(Lattice(L), reps_list, [float(x) for x in gs])
    ad = w.area_fraction_dict
    return w, [float(ad.get(m, 0.0)) for m in reps_list]


def reduction_to_appear(shape, family, amin=AREA_ON):
    """mJ/m^2 by which the gamma of an off-shape family of a shape record must drop for it to appear (bisection,
    as engine._margin), None if it does not appear even at 10 % of its gamma."""
    fs = shape['facets']
    reps = [tuple(f['representative']) for f in fs]
    g = np.array([f['gamma'] for f in fs], float)
    i = next(k for k, f in enumerate(fs) if f['family'] == family)
    L = np.array(shape['lattice_matrix'])
    gg = g.copy()
    gg[i] = 0.1 * g[i]
    if _wulff(L, reps, gg)[1][i] <= amin:
        return None
    lo, hi = 0.0, 0.9
    for _ in range(14):
        mid = 0.5 * (lo + hi)
        gg[i] = g[i] * (1 - mid)
        if _wulff(L, reps, gg)[1][i] > amin:
            hi = mid
        else:
            lo = mid
    return float(g[i] * hi)


def _build(rows, flag, rid, schema_version, level, level_description, sigma_rules, extra_flags, relax_ratio,
           n_samples, seed):
    """rows: [{family, record, gamma, sigma, level, basis, flags}] -> the wulff_shape record (or None, reason)."""
    r0 = rows[0]['record']
    L = np.array(r0['material']['cell']['lattice_matrix'])
    members, reps = {}, {}
    for x in rows:
        r = x['record']
        f = x['family']
        if not r['facet'].get('family_members'):
            return None, 'family %s has no Laue orbit in its record (facet.family_members)' % f
        if not np.allclose(np.array(r['material']['cell']['lattice_matrix']), L, atol=1e-4):
            return None, 'the records of %s use different lattices' % r0['material']['material_id']
        members[f] = [list(m) for m in r['facet']['family_members']]
        reps[f] = tuple(r['facet']['family_representative'] or r['facet']['hkl'])
    fams = [x['family'] for x in rows]
    if not _closed(L, fams, members):
        return None, 'the %d families %s do not enclose a bounded shape' % (len(fams), ', '.join(fams))
    laue = (r0['material'].get('space_group') or {}).get('laue_class')
    checks, why = _checks(L, fams, members, reps, laue)
    if why:
        return None, why
    reps_list = [reps[f] for f in fams]
    g = np.array([x['gamma'] for x in rows], float)
    sig = np.array([x['sigma'] for x in rows], float)
    w, area = _wulff(L, reps_list, g)
    rng = np.random.default_rng(seed)
    S = []
    for _ in range(n_samples):
        gs = np.maximum(g + sig * rng.standard_normal(len(g)), 0.2 * g)
        try:
            S.append(_wulff(L, reps_list, gs)[1])
        except Exception:
            pass
    S = np.array(S)
    # d_eff (BFDH): the row's own (best_shape fills it in from another record of the family when the record used
    # has none, e.g. a surfenergy pin), else the record's, else d_hkl
    d_eff = [x.get('d_eff') or x['record']['facet'].get('d_eff') or x['record']['facet'].get('d_hkl') for x in rows]
    bf = _wulff(L, reps_list, [1.0 / d for d in d_eff])[1]
    facets, ids = [], []
    for j, x in enumerate(rows):
        r = x['record']
        fl = list(x.get('flags') or [])
        if r['termination'].get('faces_equivalent') is False and not any(f['code'] == 'faces_not_equivalent' for f in fl):
            fl += [f for f in r['result']['gamma_flags'] if f['code'] == 'faces_not_equivalent'] or \
                [flag('faces_not_equivalent', 'warning', 'top and bottom faces differ: gamma is their average')]
        facets.append({'family': x['family'], 'representative': list(reps_list[j]), 'members': members[x['family']],
                       'd_eff': round(float(d_eff[j]), 5), 'gamma': round(float(g[j]), 4),
                       'gamma_record': r['result']['gamma'], 'sigma': round(float(sig[j]), 4), 'level': x['level'],
                       'basis': x['basis'], 'termination_label': r['termination']['label'],
                       'area_fraction': round(area[j], 6), 'on_shape': area[j] > AREA_ON,
                       'area_mean': round(float(S[:, j].mean()), 4), 'area_std': round(float(S[:, j].std()), 4),
                       'area_p05': round(float(np.percentile(S[:, j], 5)), 4),
                       'area_p95': round(float(np.percentile(S[:, j], 95)), 4),
                       'p_on_shape': round(float((S[:, j] > AREA_ON).mean()), 3), 'bfdh_area_fraction': round(bf[j], 6),
                       'flags': fl, 'input_record_ids': [r['record_id']]})
        ids.append(r['record_id'])
    facets.sort(key=lambda x: -x['area_fraction'])
    pts = np.array(w.wulff_convex.points)[w.wulff_convex.vertices]
    c = pts - pts.mean(axis=0)
    _, vec = np.linalg.eigh(np.cov(c.T))
    ext = sorted([float(np.ptp(c @ vec[:, k])) for k in range(3)], reverse=True)
    m = r0['material']
    ff = r0['force_field']
    sflags = []
    if not ff['validated_for_material']:
        sflags.append(flag('force_field_not_validated', 'warning', ff.get('note') or 'force field not validated'))
    sflags += extra_flags
    try:
        import pymatgen
        pv = pymatgen.__version__
    except AttributeError:
        import importlib.metadata
        pv = importlib.metadata.version('pymatgen')
    return {
        'schema_version': schema_version, 'record_type': 'wulff_shape', 'shape_id': rid('ws', m['material_id'], level, sorted(ids)),
        'material_id': m['material_id'], 'material_name': m['name'], 'structure_id': m['structure']['structure_id'],
        'force_field': ff['name'], 'force_field_validated': bool(ff['validated_for_material']), 'temperature_K': 0.0,
        'level': level, 'level_description': level_description,
        'facet_set': fams, 'facets': facets,
        'shape_metrics': {'shape_factor': round(float(w.shape_factor), 5), 'anisotropy': round(float(w.anisotropy), 5),
                          'weighted_surface_energy': round(float(w.weighted_surface_energy), 3),
                          'volume': round(float(w.volume), 3), 'surface_area': round(float(w.surface_area), 3),
                          'effective_radius': round(float(w.effective_radius), 4),
                          'aspect_ratios': {'e2_over_e1': round(ext[1] / ext[0], 4), 'e3_over_e1': round(ext[2] / ext[0], 4),
                                            'method': 'extents of the Wulff vertices along the principal axes of their covariance'},
                          'n_on_shape': sum(1 for x in facets if x['on_shape']), 'relax_ratio': relax_ratio},
        'sensitivity': {'method': 'resample every gamma ~ N(gamma, sigma) (clipped at 0.2 gamma), rebuild, area fraction '
                                  'mean / std / 5-95 %', 'n_samples': int(len(S)), 'seed': seed,
                        'max_area_std': round(float(S.std(axis=0).max()), 4), 'sigma_rules': sigma_rules},
        'lattice_matrix': L.tolist(), 'checks': checks,
        'flags': sflags, 'convergence': None, 'input_record_ids': ids,
        'provenance': {'built_by': 'wulffhound shapes.%s from dataset records (pymatgen %s)' % (
                           'level1_shape' if level == '1' else 'best_shape', pv),
                       'date': datetime.date.today().isoformat(), 'area_on_threshold': AREA_ON,
                       'sources': sorted({x['record']['provenance']['source'] for x in rows})}}, None


def level1_shape(records, flag, rid, schema_version, n_samples=40, seed=0):
    best = pick_level1(records)
    if len(best) < MIN_FAMILIES:
        return None, '%d usable facet families at level 1 (a shape needs >= %d)' % (len(best), MIN_FAMILIES)
    fams = sorted(best, key=lambda f: best[f]['result']['gamma'])
    rows = [{'family': f, 'record': best[f], 'gamma': best[f]['result']['gamma'],
             'sigma': SIGMA1_REL * best[f]['result']['gamma'], 'level': 1,
             'basis': 'level-1 (unrelaxed) gamma of the lowest termination, record %s (%s)' % (
                 best[f]['record_id'], best[f]['provenance']['source']),
             'flags': [x for x in best[f]['result']['gamma_flags'] if x['code'] == 'faces_not_equivalent']} for f in fams]
    return _build(rows, flag, rid, schema_version, '1',
                  'unrelaxed level-1 values only (lowest termination of each facet), built from the dataset records '
                  '(wulffhound shapes.py)', 'level 1: %.0f %% of gamma' % (100 * SIGMA1_REL),
                  [flag('shape_not_converged', 'info', 'level-1 (unrelaxed) screening shape of %d families; no '
                        'relaxation, and families beyond these are not computed' % len(fams), by='exporter')],
                  None, n_samples, seed)


def best_shape(records, flag, rid, schema_version, n_samples=40, seed=0):
    """The 'best' shape of a material from its records: None when it has no usable level-2 record (then the level-1
    shape is all there is)."""
    l1 = pick_level1(records)
    l2, bad = pick_level2(records)
    if not l2:
        return None, 'no usable level-2 (relaxed T = 0) record'
    pairs = [l2[f]['result']['gamma'] / l1[f]['result']['gamma'] for f in sorted(l2) if f in l1]
    ratio = float(np.mean(pairs)) if pairs else 1.0
    s1 = max(float(np.std(pairs)) if len(pairs) > 1 else 0.0, SIGMA1_REL - 0.02) + 0.02
    fams = sorted(set(l1) | set(l2), key=lambda f: -float((l1.get(f) or l2[f])['facet'].get('d_eff') or 0))
    if len(fams) < MIN_FAMILIES:
        return None, '%d usable facet families (a shape needs >= %d)' % (len(fams), MIN_FAMILIES)
    rows = []
    for f in fams:
        fl = []
        if f in l2:
            r = l2[f]
            g = r['result']['gamma']
            fl += [x for x in r['result']['gamma_flags'] if x['severity'] == 'warning' and x['code'] not in
                   ('force_field_not_validated',)]
            rows.append({'family': f, 'record': r, 'gamma': g, 'sigma': max(r['result'].get('gamma_error') or 0.0,
                                                                            SIGMA2_REL * g),
                         'd_eff': r['facet'].get('d_eff') or ((l1.get(f) or {}).get('facet') or {}).get('d_eff'),
                         'level': 2, 'basis': 'relaxed T = 0 run, record %s (%s)' % (r['record_id'],
                                                                                  r['provenance']['source']), 'flags': fl})
            continue
        r = l1[f]
        g = r['result']['gamma']
        basis = 'level-1 run x mean relaxed/unrelaxed ratio %.3f of %d relaxed facet(s), record %s' % (
            ratio, len(pairs), r['record_id']) if pairs else 'unrelaxed run, record %s' % r['record_id']
        if pairs:
            fl.append(flag('ratio_scaled_level1', 'info', 'level-1 gamma %.2f x mean relaxed/unrelaxed ratio %.4f'
                           % (g, ratio), by='exporter'))
        if f in bad:
            why = '; '.join('%s (%s)' % (x['record_id'], ', '.join(y['code'] for y in x['result']['gamma_flags']
                                                                  if y['severity'] == 'error')) for x in bad[f])
            fl.append(flag('level1_used_for_inconsistent_level2', 'warning', 'level-2 result(s) %s fail the '
                           'consistency guard; the level-1 value is used' % why, by='exporter'))
            basis += ' [FLAG: level-2 result excluded as inconsistent]'
        fl += [x for x in r['result']['gamma_flags'] if x['code'] == 'faces_not_equivalent']
        rows.append({'family': f, 'record': r, 'gamma': g * ratio, 'sigma': s1 * g * ratio, 'level': 1,
                     'basis': basis, 'flags': fl})
    n2 = sum(1 for x in rows if x['level'] == 2)
    return _build(rows, flag, rid, schema_version, 'best',
                  'relaxed T = 0 where available (level 2), else the level-1 value x the mean relaxed/unrelaxed ratio '
                  '(the rule of the wulffhound engine, build_wulff best), built from the dataset records (shapes.py)',
                  'level 2: max(gamma_error, %.0f %% of gamma); level 1: %.3f of gamma (std of the relaxed/unrelaxed '
                  'ratio + 2 %%, floor 5 %%)' % (100 * SIGMA2_REL, s1),
                  [flag('shape_not_converged', 'info', '%d of %d families relaxed (T = 0), %d ratio-scaled level-1 values; '
                        'families beyond these are not computed' % (n2, len(rows), len(rows) - n2), by='exporter')],
                  round(ratio, 5), n_samples, seed)

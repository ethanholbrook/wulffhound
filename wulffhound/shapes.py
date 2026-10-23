"""Wulff shapes straight from dataset records, for materials without a wulffhound bundle (CIF-zoo crystals and
campaign materials): level 1 (unrelaxed, the lowest termination of every family), pymatgen WulffShape on the
record's lattice, the same resampling and metrics as the bundle shapes of tools/export_dataset.py.

    from shapes import level1_shape
    rec, why = level1_shape(records_of_one_material, flag=..., rid=..., schema_version='0.1.1')

A shape is built only when (1) at least MIN_FAMILIES families have a usable level-1 gamma, (2) their Laue orbits
(facet.family_members) enclose the origin (bounded shape), and (3) pymatgen's reciprocal-lattice symmetry gives
exactly those orbits (else WulffShape would add faces the records do not have, and the browser, which uses the
orbits, would disagree). Otherwise (None, reason).
"""
import datetime
import warnings

import numpy as np

MIN_FAMILIES = 3
SIGMA1_REL = 0.05     # level-1 values as a T = 0 stand-in: 5 % (the engine's level-1 floor)
AREA_ON = 1e-4


def _usable(r):
    return (r['result']['status'] == 'ok' and r['result']['gamma'] is not None and r['result']['gamma'] > 0
            and r['protocol']['level'] == 1 and not any(f['severity'] == 'error' for f in r['result']['gamma_flags']))


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


def level1_shape(records, flag, rid, schema_version, n_samples=40, seed=0):
    from pymatgen.analysis.wulff import WulffShape
    from pymatgen.core import Lattice
    from scipy.spatial import ConvexHull
    best = pick_level1(records)
    if len(best) < MIN_FAMILIES:
        return None, '%d usable facet families at level 1 (a shape needs >= %d)' % (len(best), MIN_FAMILIES)
    fams = sorted(best, key=lambda f: best[f]['result']['gamma'])
    r0 = best[fams[0]]
    L = np.array(r0['material']['cell']['lattice_matrix'])
    for f in fams:
        if not best[f]['facet'].get('family_members'):
            return None, 'family %s has no Laue orbit in its record (facet.family_members)' % f
        if not np.allclose(np.array(best[f]['material']['cell']['lattice_matrix']), L, atol=1e-4):
            return None, 'the records of %s use different lattices' % r0['material']['material_id']
    lat = Lattice(L)
    recip = np.linalg.inv(L).T
    normals = [np.array(m) @ recip for f in fams for m in best[f]['facet']['family_members']]
    try:
        hull = ConvexHull(np.array([n / np.linalg.norm(n) for n in normals]))
        closed = bool(np.all(hull.equations[:, -1] < -1e-6))
    except Exception:
        closed = False
    if not closed:
        return None, 'the %d families %s do not enclose a bounded shape' % (len(fams), ', '.join(fams))
    ops = lat.get_recp_symmetry_operation(1e-5)
    for f in fams:
        rep = best[f]['facet']['family_representative'] or best[f]['facet']['hkl']
        pm = {tuple(int(round(x)) for x in op.operate(rep)) for op in ops}
        ours = {tuple(m) for m in best[f]['facet']['family_members']}
        if pm != ours:
            return None, ('pymatgen\'s lattice symmetry gives %d members for %s, the Laue orbit %d: WulffShape would '
                          'add faces (Laue class lower than the metric)' % (len(pm), f, len(ours)))
    reps = [tuple(best[f]['facet']['family_representative'] or best[f]['facet']['hkl']) for f in fams]
    g = np.array([best[f]['result']['gamma'] for f in fams], float)
    sig = SIGMA1_REL * g

    def build(gs):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            w = WulffShape(lat, reps, [float(x) for x in gs])
        ad = w.area_fraction_dict
        return w, [float(ad.get(m, 0.0)) for m in reps]

    w, area = build(g)
    rng = np.random.default_rng(seed)
    S = []
    for _ in range(n_samples):
        gs = np.maximum(g + sig * rng.standard_normal(len(g)), 0.2 * g)
        try:
            S.append(build(gs)[1])
        except Exception:
            pass
    S = np.array(S)
    d_eff = [best[f]['facet'].get('d_eff') or best[f]['facet'].get('d_hkl') for f in fams]
    bf = build([1.0 / d for d in d_eff])[1]
    facets, ids = [], []
    for j, f in enumerate(fams):
        r = best[f]
        fl = [x for x in r['result']['gamma_flags'] if x['code'] == 'faces_not_equivalent']
        facets.append({'family': f, 'representative': list(reps[j]), 'members': r['facet']['family_members'],
                       'd_eff': round(float(d_eff[j]), 5), 'gamma': round(float(g[j]), 4),
                       'gamma_record': r['result']['gamma'], 'sigma': round(float(sig[j]), 4), 'level': 1,
                       'basis': 'level-1 (unrelaxed) gamma of the lowest termination, record %s (%s)' % (
                           r['record_id'], r['provenance']['source']),
                       'termination_label': r['termination']['label'], 'area_fraction': round(area[j], 6),
                       'on_shape': area[j] > AREA_ON, 'area_mean': round(float(S[:, j].mean()), 4),
                       'area_std': round(float(S[:, j].std()), 4), 'area_p05': round(float(np.percentile(S[:, j], 5)), 4),
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
    sflags.append(flag('shape_not_converged', 'info', 'level-1 (unrelaxed) screening shape of %d families; no '
                       'relaxation, and families beyond these are not computed' % len(fams), by='exporter'))
    try:
        import pymatgen
        pv = pymatgen.__version__
    except AttributeError:
        import importlib.metadata
        pv = importlib.metadata.version('pymatgen')
    return {
        'schema_version': schema_version, 'record_type': 'wulff_shape', 'shape_id': rid('ws', m['material_id'], '1', sorted(ids)),
        'material_id': m['material_id'], 'material_name': m['name'], 'structure_id': m['structure']['structure_id'],
        'force_field': ff['name'], 'force_field_validated': bool(ff['validated_for_material']), 'temperature_K': 0.0,
        'level': '1', 'level_description': 'unrelaxed level-1 values only (lowest termination of each facet), built from '
                                           'the dataset records (wulffhound shapes.py)',
        'facet_set': fams, 'facets': facets,
        'shape_metrics': {'shape_factor': round(float(w.shape_factor), 5), 'anisotropy': round(float(w.anisotropy), 5),
                          'weighted_surface_energy': round(float(w.weighted_surface_energy), 3),
                          'volume': round(float(w.volume), 3), 'surface_area': round(float(w.surface_area), 3),
                          'effective_radius': round(float(w.effective_radius), 4),
                          'aspect_ratios': {'e2_over_e1': round(ext[1] / ext[0], 4), 'e3_over_e1': round(ext[2] / ext[0], 4),
                                            'method': 'extents of the Wulff vertices along the principal axes of their covariance'},
                          'n_on_shape': sum(1 for x in facets if x['on_shape']), 'relax_ratio': None},
        'sensitivity': {'method': 'resample every gamma ~ N(gamma, sigma) (clipped at 0.2 gamma), rebuild, area fraction '
                                  'mean / std / 5-95 %', 'n_samples': int(len(S)), 'seed': seed,
                        'max_area_std': round(float(S.std(axis=0).max()), 4),
                        'sigma_rules': 'level 1: %.0f %% of gamma' % (100 * SIGMA1_REL)},
        'lattice_matrix': L.tolist(),
        'checks': {'orbits_equal_pymatgen_recip_ops': True, 'pymatgen_recip_ops_order': len(ops), 'wulffshape_symprec': 1e-5,
                   'bounded': True, 'laue_class': m['space_group'].get('laue_class'),
                   'note': 'the Laue orbits of the records equal the reciprocal-lattice operations of the metric '
                           '(asserted), so pymatgen and the browser build the same shape'},
        'flags': sflags, 'convergence': None, 'input_record_ids': ids,
        'provenance': {'built_by': 'wulffhound shapes.level1_shape from dataset records (pymatgen %s)' % pv,
                       'date': datetime.date.today().isoformat(), 'area_on_threshold': AREA_ON,
                       'sources': sorted({best[f]['provenance']['source'] for f in fams})}}, None

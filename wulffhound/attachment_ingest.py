"""Attachment-energy (AE) ingest for the dataset exporter (tools/export_dataset.py): the results of the chain's static
Hartman-Perdok prototype (devtools/attachment/results/<m>_{spec,attachment,morphology}.json, READ from the private
chain; only the exported records are written here) -> schema-0.2.0 `attachment_energy` records, and the check of the
AE growth shape (shapes.ae_shape) against the prototype's own morphology JSON.

    from attachment_ingest import find_results, load_json, ae_records, check_against_morphology
    for key, paths in find_results(results_dir).items():
        recs = ae_records(key, load_json(paths['spec']), load_json(paths['attachment']), se_records, X, ref, rev)
        shape, why = shapes.ae_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)
        shape['provenance']['checked_against'] = check_against_morphology(shape, load_json(paths['morphology']), ...)

Conventions and asserts (an AssertionError stops the export):
  * the material, force-field and setting blocks are those of the dataset's surface_energy records of the same unit
    cell (data-file md5) and force field (md5 of the ReaxFF file / of the non-reactive run-0 script);
  * the lattice of the spec equals the records' data-file lattice (1e-4 A), and each family's Laue orbit equals the
    family_members of the records (the shape then asserts them against pymatgen at symprec 1e-5 and 1e-3);
  * the growth slice is the lowest-|E_att| cut, E_att < 0, the structural extinction factor equals the space group's,
    E_latt(rigid) = E_slice(rigid) + E_att, d_eff = d_hkl / n;
  * the lowest 2-period unrelaxed gamma over the cuts equals a level-1 record of the family (to 0.02 mJ/m^2, the zoo's
    rounding): those records are named in related_record_ids;
  * the AE shape's area fractions (and its BFDH ones) equal the morphology JSON's to 3 decimals (|d| < 5e-4).
No coordinates are exported: energies, cut heights, slice sizes, hkl and metadata only.
"""
import glob
import json
import math
import os
import re

KJ = 4.184
TIE = 0.01          # kcal/mol per molecule: the QEq tolerance of the prototype (equivalent members / slices)
GAMMA_TOL = 0.02    # mJ/m^2: per-family minimum 2-period gamma vs the dataset's level-1 record (zoo rounding 0.01)
AREA_TOL = 5e-4     # area fractions equal to 3 decimals


def find_results(d):
    """{material key: {'spec', 'attachment', 'morphology'} paths} for every complete triple in d."""
    out = {}
    for p in sorted(glob.glob(os.path.join(d, '*_attachment.json'))):
        key = os.path.basename(p)[:-len('_attachment.json')]
        trip = {k: os.path.join(d, '%s_%s.json' % (key, k)) for k in ('spec', 'attachment', 'morphology')}
        if all(os.path.isfile(v) for v in trip.values()):
            out[key] = trip
    return out


def _clean(o):
    if isinstance(o, float) and not math.isfinite(o):
        return None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def load_json(p):
    """JSON with NaN / inf -> None (the prototype writes NaN for a lattice energy it did not compute)."""
    with open(p) as f:
        return _clean(json.load(f))


HOME = re.compile(r'(/home|/Users)/[^/\s]+/')


def portable_path(p):
    """A machine-free reference of a path of the chain or of wulffhound's state: 'chain:<rel>', 'state:<rel>'."""
    if not p:
        return None
    p = re.sub(r'^.*?/surface-energy-sim2ls/', 'chain:', p)
    p = re.sub(r'^.*?/wulffhound/state/', 'state:', p)
    p = re.sub(r'^/tmp/[^\s]*', 'tmp (not kept)', p)
    return HOME.sub('~/', p)


def _r(x, n=6):
    return None if x is None else round(float(x), n)


def _members(x):
    return sorted(tuple(int(v) for v in m) for m in x)


def reference_records(spec, att, se_records):
    """The surface_energy records of the same material, unit cell and force field as the AE run."""
    calc = att['calculator']
    ff_md5 = calc.get('supplementary_md5') if calc.get('reaxff') else calc.get('script_md5')
    mid = spec['material_id']
    same = [r for r in se_records if r['material']['material_id'] == mid and r['force_field'].get('md5') == ff_md5]
    ref = [r for r in same if r['provenance']['input_md5'].get('datafile') == att['unitcell_md5']]
    assert ref, ('%s: no surface_energy record with the AE unit cell (datafile md5 %s) and force field (md5 %s)'
                 % (mid, att['unitcell_md5'], ff_md5))
    return ref, same


def ae_records(key, spec, att, se_records, X, results_ref, revision=None):
    """attachment_energy records of one material (one per family, in the results' order). X: the exporter module
    (flag, rid, SCHEMA_VERSION)."""
    import numpy as np
    ref, same = reference_records(spec, att, se_records)
    r0 = ref[0]
    mb, ff, setting = r0['material'], r0['force_field'], r0['facet']['setting']
    L = np.array(mb['cell']['lattice_matrix'])
    assert np.allclose(np.array(spec['lattice_rows_abc']), L, atol=1e-4), \
        '%s: the AE lattice differs from the records\' data-file lattice' % key
    calc = att['calculator']
    reaxff = bool(calc.get('reaxff'))
    sfam = {f['family']: f for f in spec['families']}
    rec_members = {}
    for r in same:
        if r['facet'].get('family_members'):
            rec_members.setdefault(r['facet']['family'], _members(r['facet']['family_members']))
    l1 = {}
    for r in same:
        if r['protocol']['level'] == 1 and r['result']['status'] == 'ok' and r['result']['gamma'] is not None:
            l1.setdefault(r['facet']['family'], []).append(r)
    gas = att.get('gas') or {}
    strain = gas.get('intramolecular_strain_mean')
    E_static, E_rigid, E_relax = att.get('E_latt'), att.get('E_latt_rigid'), att.get('E_latt_relaxed')
    measured_q = {f['family']: f['charges'] for f in att['families'] if f.get('charges')}
    qr = (min(q['mean_abs_dq'] for q in measured_q.values()), max(q['mean_abs_dq'] for q in measured_q.values())) \
        if measured_q else None
    in_md5 = {k: v for k, v in (('datafile', att.get('unitcell_md5')), ('script', calc.get('script_md5')),
                                ('forcefield', calc.get('supplementary_md5'))) if v}
    out = []
    for i, f in enumerate(att['families']):
        fam = f['family']
        if 'error' in f:
            continue                              # the prototype's own failures: none in the committed results
        s = sfam[fam]
        members = _members(s['members'])
        if fam in rec_members:
            assert members == rec_members[fam], '%s %s: Laue orbit %s differs from the records\' %s' % (
                key, fam, members, rec_members[fam])
        assert f['n_ext'] == s['n_ext'] and f.get('n_ext_structural', f['n_ext']) == f['n_ext'], \
            '%s %s: extinction factor %s (structural %s, spec %s)' % (key, fam, f['n_ext'], f.get('n_ext_structural'), s['n_ext'])
        assert abs(f['d_eff'] - s['d_hkl'] / s['n_ext']) < 1e-4 and abs(f['d_eff'] - s['d_eff']) < 1e-4, \
            '%s %s: d_eff %s vs d_hkl / n %s' % (key, fam, f['d_eff'], s['d_hkl'] / s['n_ext'])
        cuts = sorted(f['cuts'], key=lambda c: c['index'])
        g = next(c for c in cuts if c['index'] == f['growth_cut'])
        assert f['E_att'] < 0 and abs(abs(f['E_att']) - min(abs(c['E_att']) for c in cuts)) < 1e-9 \
            and abs(g['E_att'] - f['E_att']) < 1e-9, '%s %s: the growth cut is not the lowest-|E_att| bound cut' % (key, fam)
        lg = next(c for c in cuts if c['index'] == f['lowest_gamma_cut'])
        gmin = f.get('gamma_unrelaxed_min')
        assert abs(lg['gamma_unrelaxed'] - gmin) < 1e-6 and abs(lg['E_att'] - f['E_att_lowest_gamma_cut']) < 1e-9, \
            '%s %s: the lowest-gamma cut is inconsistent' % (key, fam)
        if E_rigid is not None and f.get('E_slice_rigid') is not None:
            assert abs(f['E_slice_rigid'] + f['E_att'] - E_rigid) < 1e-3, '%s %s: E_latt != E_slice + E_att' % (key, fam)
        rel = [r for r in l1.get(fam, []) if abs(r['result']['gamma'] - gmin) <= GAMMA_TOL]
        assert rel, '%s %s: no level-1 record with gamma %.3f (the AE cells must be the dataset\'s)' % (key, fam, gmin)
        flags = []
        if not ff['validated_for_material']:
            flags.append(X.flag('force_field_not_validated', 'warning', ff.get('note') or 'force field not validated',
                                by='attachment ingest'))
        flags.append(X.flag('unrelaxed_screening', 'info', 'static (unrelaxed) slice and bulk: no relaxation of the slice '
                            'or of the crystal, vacuum growth (no solvent)', by='attachment ingest'))
        if f.get('same_cut') is False or (f.get('same_cut') is None and f['growth_cut'] != f['lowest_gamma_cut']):
            d = f['E_att_lowest_gamma_cut'] - f['E_att']
            flags.append(X.flag('cut_rules_disagree', 'info', 'the lowest-|E_att| cut (%d, E_att %.3f) is not the lowest-gamma '
                                'cut (%d, E_att %.3f, gamma %.2f against %.2f mJ/m^2)%s' % (
                                    f['growth_cut'], f['E_att'], f['lowest_gamma_cut'], f['E_att_lowest_gamma_cut'],
                                    gmin, g['gamma_unrelaxed'],
                                    ': a tie within the QEq tolerance (%.3f kcal/mol)' % abs(d) if abs(d) < TIE else
                                    ' (slices interact beyond the neighbour, e.g. a cut through interpenetrating layers)'),
                                by='attachment ingest'))
        qeq = None
        if reaxff:
            q = f.get('charges')
            if q:
                qeq = {'measured': True, 'mean_abs_dq': _r(q.get('mean_abs_dq')), 'max_abs_dq': _r(q.get('max_abs_dq')),
                       'rms_q_bulk': _r(q.get('rms_q_bulk')), 'net_slice_charge': _r(q.get('net_slice_charge'), 8),
                       'n_atoms': q.get('n_atoms'), 'note': 'growth slice vs the same atoms in the bulk'}
                det = 'measured for this family: mean |dq| %.3f e, max %.3f e (bulk rms charge %.2f e)' % (
                    q['mean_abs_dq'], q['max_abs_dq'], q['rms_q_bulk'])
            else:
                qeq = {'measured': False, 'mean_abs_dq': None, 'max_abs_dq': None, 'rms_q_bulk': None,
                       'net_slice_charge': None, 'n_atoms': None,
                       'note': 'not measured for this family' + (
                           '; measured for %s of this material: mean |dq| %.3f-%.3f e' % (', '.join(sorted(measured_q)), qr[0], qr[1])
                           if qr else '')}
                det = qeq['note']
            flags.append(X.flag('qeq_charges_changed', 'info', 'ReaxFF: QEq re-equilibrates the charges of the isolated slice, '
                                'so E_att includes its charge (electronic) response, which a fixed-charge force field '
                                'leaves out; ' + det, by='attachment ingest'))
        if strain is not None and strain > 1.0:
            flags.append(X.flag('intramolecular_strain_in_reference', 'warning' if (E_static or 0) > 0 else 'info',
                                'the static molecules are %.1f kcal/mol above the minimised gas molecule, so the static E_latt '
                                '(%.2f) includes intramolecular strain%s; E_slice and E_latt here use the rigid-molecule '
                                'reference (E_latt %.2f), and E_att is unaffected (same geometry in slice and bulk)' % (
                                    strain, E_static, ' and is meaningless (positive)' if (E_static or 0) > 0 else '', E_rigid),
                                by='attachment ingest'))
        notes = ['material totals (all families): %s energy runs, %s CPU s' % (att.get('n_energy_runs'), att.get('cpu_s')),
                 'oriented cell (%s): %d molecules, %d periods of d_hkl along the normal, cut %s' % (
                     ' '.join(map(str, f.get('cell_hkl') or s.get('cell_hkl') or [])), f['n_molecules_cell'],
                     f.get('periods_in_cell') or 0, 'growth slice = whole molecules with centres of mass in [cut, cut + d_eff)')]
        st = f.get('stack') or {}
        if st.get('W_j'):
            notes.append('slice stack (k = %s): W_j per slice pair %s kcal/mol; sum W_j / N_slice %.4f (E_att %.4f)' % (
                st.get('k'), [round(w, 4) for w in st['W_j'][:3]], st.get('sum_W_per_molecule'), f['E_att']))
        cell = f.get('cell')
        run = re.search(r'RUNS/([0-9a-f]{32})', cell or '') or re.search(r'supercell-([0-9a-f]{16})', cell or '')
        rec = {
            'schema_version': X.SCHEMA_VERSION, 'record_type': 'attachment_energy',
            'record_id': X.rid('ae', key, fam, att['unitcell_md5'], in_md5.get('forcefield') or in_md5.get('script')),
            'material': mb, 'force_field': ff,
            'facet': {'hkl': list(f.get('cell_hkl') or s.get('cell_hkl') or s['representative']), 'family': fam,
                      'family_representative': list(s['representative']), 'family_members': [list(m) for m in s['members']],
                      'multiplicity': s.get('multiplicity'), 'd_hkl': _r(s['d_hkl'], 5), 'd_eff': _r(f['d_eff'], 5),
                      'setting': setting},
            'slice': {'d_eff': _r(f['d_eff']), 'extinction_factor': int(f['n_ext']),
                      'extinction_factor_structural': f.get('n_ext_structural'), 'n_slice': int(f['n_slice']),
                      'n_molecules_cell': f.get('n_molecules_cell'), 'area_A2': _r(f.get('area'), 4),
                      'period_A': _r(f.get('period')), 'volume_per_molecule_A3': _r(f.get('v_per_molecule'), 4),
                      'n_cuts': int(f['n_cuts']), 'cut_index': int(f['growth_cut']), 'cut_z': _r(g.get('cut_z'), 5),
                      'gap': _r(g.get('gap'), 5), 'rule': 'lowest_abs_E_att', 'lowest_gamma_cut_index': f['lowest_gamma_cut'],
                      'gamma_periods': f.get('gamma_periods'),
                      'cuts': [{'index': c['index'], 'cut_z': _r(c['cut_z'], 5), 'gap': _r(c.get('gap'), 5),
                                'E_att': _r(c['E_att']), 'E_slice': _r(c.get('E_slice_rigid')),
                                'gamma_unrelaxed': _r(c.get('gamma_unrelaxed'), 4),
                                'faces_equivalent': c.get('slab_faces_equivalent', c.get('faces_equivalent'))} for c in cuts]},
            'result': {'status': 'ok', 'units': 'kcal/mol per molecule', 'E_att': _r(f['E_att']),
                       'E_att_kJ_per_mol': _r(f['E_att'] * KJ, 4), 'E_att_lowest_gamma_cut': _r(f['E_att_lowest_gamma_cut']),
                       'gamma_unrelaxed_growth_cut': _r(g.get('gamma_unrelaxed'), 4), 'gamma_unrelaxed_min': _r(gmin, 4),
                       'E_slice': _r(f.get('E_slice_rigid')), 'E_latt': _r(E_rigid), 'energy_convention': 'rigid',
                       'E_latt_conventions': {'static_minus_minimised_gas': _r(E_static), 'rigid': _r(E_rigid),
                                              'relaxed': _r(E_relax)},
                       'E_slice_static_minus_minimised_gas': _r(f.get('E_slice')),
                       'gas_reference': {'E_gas_minimised': _r(gas.get('E_gas')),
                                         'E_gas_crystal_conformer_mean': _r(gas.get('E_gas_crystal_conformer_mean')),
                                         'intramolecular_strain_mean': _r(strain), 'box_A': gas.get('box_A'),
                                         'note': gas.get('note')},
                       'e_bulk_per_molecule': _r(f.get('e_bulk')), 'e_bulk_minus_unitcell': _r(f.get('e_bulk_minus_unitcell'), 8),
                       'equivalent_slices_max_dev': _r((f.get('equivalent_slices') or {}).get('max_dev_per_molecule'), 8),
                       'qeq': qeq, 'flags': flags},
            'related_record_ids': sorted(r['record_id'] for r in rel),
            'provenance': {'source': 'attachment-prototype',
                           'source_ref': '%s/%s_attachment.json#families[%d]' % (results_ref, key, i),
                           'sim2l': {'name': 'devtools-attachment', 'revision': revision}, 'run_id': None,
                           'run_ref': portable_path(cell), 'resultsdb_id': None,
                           'child_run_ids': [run.group(1)] if run else [],
                           'input_md5': dict(in_md5, **({'cell': f['cell_md5']} if f.get('cell_md5') else {})),
                           'input_sha256': {}, 'date': att.get('date'),
                           'venue': 'local chain (development machine), serial LAMMPS run-0 energies',
                           'cpu_s': None, 'wall_s': None,
                           'reproduce': 'chain: devtools/attachment/prepare.py, attachment.py, morphology.py %s (see '
                                        'notes/validation/ATTACHMENT_ENERGY.md); run_ref is the oriented cell (not shipped)' % key,
                           'notes': notes}}
        out.append(rec)
    return out


def check_against_morphology(shape, morph, morph_ref, tol=AREA_TOL):
    """The AE shape's area fractions (and BFDH, 5-95 % of the resampling) against the prototype's morphology JSON.
    Raises AssertionError beyond tol (3 decimals) for the areas; returns the deviations."""
    ma = morph['shapes']['ae']['area']
    mb = morph['shapes']['bfdh']['area']
    mf = {f['family']: f for f in morph['families']}
    fams = {f['family'] for f in shape['facets']}
    assert fams == set(ma), 'AE family sets differ: %s vs %s' % (sorted(fams), sorted(ma))
    d = max(abs(f['area_fraction'] - ma[f['family']]) for f in shape['facets'])
    db = max(abs(f['bfdh_area_fraction'] - mb[f['family']]) for f in shape['facets'])
    dp = max(max(abs(f['area_p05'] - mf[f['family']]['ae_area_p05']), abs(f['area_p95'] - mf[f['family']]['ae_area_p95']))
             for f in shape['facets'])
    de = max(abs(f['E_att'] - mf[f['family']]['E_att']) for f in shape['facets'])
    assert d < tol and db < tol, 'AE shape of %s differs from the morphology JSON: max |dA| %.2e, BFDH %.2e' % (
        shape['material_id'], d, db)
    assert de < 1e-3, 'E_att differs from the morphology JSON by %.2e' % de
    return {'ref': morph_ref, 'max_abs_diff_area': round(d, 8), 'max_abs_diff_bfdh': round(db, 8),
            'max_abs_diff_p05_p95': round(dp, 6), 'max_abs_diff_E_att': round(de, 6), 'tolerance': tol,
            'shape_factor_morphology': morph['shapes']['ae'].get('shape_factor'),
            'aspect_morphology': morph['shapes']['ae'].get('aspect')}

"""Unit tests of the attachment-energy (AE) ingest of the dataset exporter (wulffhound/attachment_ingest.py,
shapes.ae_shape): synthetic prototype results on an orthorhombic box crystal (whose AE shape is a box with analytic
area fractions), the asserts that refuse inconsistent inputs, the flags, and the committed dataset/attachment_energy.jsonl.
No chain needed.

    ./wh py -W ignore -m unittest discover -s tests -p test_attachment.py -v
"""
import copy
import json
import os
import sys
import tempfile
import unittest

os.environ.setdefault('WULFFHOUND_STATE', tempfile.mkdtemp(prefix='wh-ae-test-'))
os.environ['WULFFHOUND_CHAIN_ROOT'] = '/nonexistent'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'wulffhound'))
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import attachment_ingest as AI  # noqa: E402
import export_dataset as X  # noqa: E402
import shapes as S  # noqa: E402

SCHEMA = json.load(open(os.path.join(ROOT, 'schema', 'surface_energy.schema.json')))
A, B, C = 10.0, 12.0, 14.0
L = [[A, 0.0, 0.0], [0.0, B, 0.0], [0.0, 0.0, C]]
FAMS = {'{1 0 0}': ([1, 0, 0], A), '{0 1 0}': ([0, 1, 0], B), '{0 0 1}': ([0, 0, 1], C)}
E_ATT = {'{1 0 0}': -12.0, '{0 1 0}': -9.0, '{0 0 1}': -15.0}
GAMMA = {'{1 0 0}': 150.0, '{0 1 0}': 120.0, '{0 0 1}': 170.0}
E_RIGID = -30.0
UC, FF, SC = 'c' * 32, 'a' * 32, 'b' * 32


def validate(rec, kind):
    import jsonschema
    jsonschema.validate(rec, {'$schema': SCHEMA['$schema'], '$defs': SCHEMA['$defs'], '$ref': '#/$defs/%s' % kind})


def members(rep):
    return [list(rep), [-x for x in rep]]


def material():
    return {'material_id': 'test-box', 'name': 'test box crystal', 'inchikey': None, 'smiles': None, 'formula': 'C2H2',
            'molecular_mass': 26.04, 'polymorph': 'test', 'category': None, 'pubchem_cid': None,
            'structure': {'structure_id': 'test-1', 'source': 'synthetic', 'database': None, 'database_id': None,
                          'licence': 'CC0', 'citation': None, 'doi': None, 'url': None, 'sha256': None, 'md5': None,
                          'measurement_T_K': None, 'distributed': False},
            'space_group': {'symbol_data_setting': 'Pmmm', 'number': 47, 'laue_class': 'mmm'},
            'cell': X.cell_block([A, B, C, 90.0, 90.0, 90.0], 4, {'C': 2, 'H': 2})}


def se_records():
    mb = material()
    ff = {'name': 'ReaxFF-lg', 'version': None, 'kind': 'reactive', 'md5': FF, 'file': None,
          'validated_for_material': False, 'note': 'not validated (test)'}
    out = []
    for i, (fam, (rep, d)) in enumerate(FAMS.items()):
        out.append({'record_id': 'se-%012d' % i, 'record_type': 'surface_energy', 'material': mb, 'force_field': ff,
                    'facet': {'hkl': rep, 'family': fam, 'family_representative': rep, 'family_members': members(rep),
                              'd_eff': d, 'setting': X.setting_of(mb)},
                    'termination': {'index': 0, 'label': 't0', 'is_lowest': True},
                    'protocol': {'level': 1, 'temperature_K': 0.0},
                    'result': {'status': 'ok', 'gamma': GAMMA[fam], 'gamma_flags': []},
                    'provenance': {'source': 'campaign', 'input_md5': {'datafile': UC}}})
    return out


def spec():
    return {'material_id': 'test-box', 'lattice_rows_abc': L,
            'families': [{'family': f, 'representative': rep, 'members': members(rep), 'multiplicity': 2, 'd_hkl': d,
                          'n_ext': 1, 'd_eff': d, 'cell_hkl': rep} for f, (rep, d) in FAMS.items()]}


def att():
    fams = []
    for f, (rep, d) in FAMS.items():
        e, g = E_ATT[f], GAMMA[f]
        fams.append({'family': f, 'cell_hkl': rep, 'n_ext': 1, 'n_ext_structural': 1, 'd_eff': d, 'n_slice': 2,
                     'n_molecules_cell': 4, 'area': 100.0, 'period': d, 'v_per_molecule': A * B * C / 4, 'n_cuts': 2,
                     'cuts': [{'index': 0, 'cut_z': 0.0, 'gap': 1.2, 'E_att': e, 'E_slice_rigid': E_RIGID - e,
                               'gamma_unrelaxed': g, 'slab_faces_equivalent': True},
                              {'index': 1, 'cut_z': 0.5, 'gap': 0.3, 'E_att': e - 5.0, 'E_slice_rigid': E_RIGID - e + 5.0,
                               'gamma_unrelaxed': g + 30.0, 'slab_faces_equivalent': False}],
                     'growth_cut': 0, 'lowest_gamma_cut': 0, 'E_att': e, 'E_att_lowest_gamma_cut': e, 'same_cut': True,
                     'E_slice_rigid': E_RIGID - e, 'E_slice': -20.0 - e, 'gamma_unrelaxed_min': g, 'gamma_periods': 2,
                     'e_bulk': -1000.0, 'e_bulk_minus_unitcell': 1e-6,
                     'cell': '/' + 'home/someone/sandbox/surface-energy-sim2ls/supercell/bin/RUNS/' + '0' * 32 + '/data.newcell',
                     'cell_md5': 'd' * 32, 'stack': {'k': [1, 2], 'W_j': [e], 'sum_W_per_molecule': e}})
    fams[1]['charges'] = {'mean_abs_dq': 0.02, 'max_abs_dq': 0.1, 'rms_q_bulk': 0.3, 'net_slice_charge': 1e-6, 'n_atoms': 8}
    return {'calculator': {'reaxff': True, 'supplementary_md5': FF, 'script_md5': SC}, 'unitcell_md5': UC,
            'date': '2026-10-23T22:00:00', 'gas': {'E_gas': -100.0, 'E_gas_crystal_conformer_mean': -90.0,
                                                    'intramolecular_strain_mean': 10.0, 'box_A': 60.0, 'note': 'test'},
            'E_latt': -20.0, 'E_latt_rigid': E_RIGID, 'E_latt_relaxed': float('nan'), 'n_energy_runs': 12, 'cpu_s': 3.0,
            'families': fams}


def box_area(dist):
    """Area fractions of the box |x| <= a, |y| <= b, |z| <= c (faces {1 0 0}: 4bc each, ...)."""
    a, b, c = dist['{1 0 0}'], dist['{0 1 0}'], dist['{0 0 1}']
    s = b * c + a * c + a * b
    return {'{1 0 0}': b * c / s, '{0 1 0}': a * c / s, '{0 0 1}': a * b / s}


def morphology():
    ae = box_area({f: abs(e) for f, e in E_ATT.items()})
    bf = box_area({f: 1.0 / d for f, (_, d) in FAMS.items()})
    return {'shapes': {'ae': {'area': ae, 'shape_factor': None, 'aspect': None}, 'bfdh': {'area': bf}},
            'families': [{'family': f, 'E_att': E_ATT[f], 'ae_area_p05': 0.0, 'ae_area_p95': 1.0} for f in FAMS]}


def records(a=None, s=None, se=None):
    return AI.ae_records('box', s or spec(), a or att(), se or se_records(), X, 'chain:devtools/attachment/results', 'chain abc1234')


class TestAERecords(unittest.TestCase):
    def test_records_valid_and_flags(self):
        p = os.path.join(tempfile.mkdtemp(), 'box_attachment.json')
        with open(p, 'w') as f:
            json.dump(att(), f)                     # writes NaN, as the prototype does
        a = AI.load_json(p)
        self.assertIsNone(a['E_latt_relaxed'])
        recs = records(a)
        self.assertEqual([r['facet']['family'] for r in recs], list(FAMS))
        for r in recs:
            validate(r, 'attachment_energy')
            R = r['result']
            self.assertEqual(R['energy_convention'], 'rigid')
            self.assertAlmostEqual(R['E_slice'] + R['E_att'], R['E_latt'], places=6)
            self.assertEqual(R['E_latt_conventions'], {'static_minus_minimised_gas': -20.0, 'rigid': E_RIGID, 'relaxed': None})
            self.assertEqual(r['slice']['rule'], 'lowest_abs_E_att')
            codes = {x['code']: x for x in R['flags']}
            self.assertIn('qeq_charges_changed', codes)                   # ReaxFF: every record
            self.assertEqual(codes['intramolecular_strain_in_reference']['severity'], 'info')   # static E_latt < 0
            self.assertNotIn('cut_rules_disagree', codes)
            self.assertEqual(r['related_record_ids'], ['se-%012d' % list(FAMS).index(r['facet']['family'])])
            self.assertTrue(r['provenance']['run_ref'].startswith('chain:supercell/bin/RUNS/'))
            self.assertEqual(r['provenance']['child_run_ids'], ['0' * 32])
            self.assertEqual(r['provenance']['sim2l'], {'name': 'devtools-attachment', 'revision': 'chain abc1234'})
            self.assertTrue(r['record_id'].startswith('ae-'))
        self.assertTrue(recs[1]['result']['qeq']['measured'])
        self.assertFalse(recs[0]['result']['qeq']['measured'])
        self.assertIn('measured for {0 1 0}', recs[0]['result']['qeq']['note'])
        self.assertNotIn('/home/', json.dumps(recs))
        self.assertEqual([r['record_id'] for r in recs], [r['record_id'] for r in records()])   # stable ids

    def test_non_reactive_and_strain_warning(self):
        a = att()
        a['calculator'] = {'reaxff': False, 'script_md5': FF}             # the non-reactive route: md5 of the script
        a['E_latt'] = 10.2                                                # static E_latt > 0 (strained X-ray cell)
        for f in a['families']:
            f.pop('charges', None)
        recs = records(a)
        for r in recs:
            self.assertIsNone(r['result']['qeq'])
            codes = {x['code']: x for x in r['result']['flags']}
            self.assertNotIn('qeq_charges_changed', codes)
            self.assertEqual(codes['intramolecular_strain_in_reference']['severity'], 'warning')
            validate(r, 'attachment_energy')

    def test_cut_rules_disagree(self):
        a = att()
        f = a['families'][0]                       # the lowest-gamma cut is the other one
        f['cuts'][1]['gamma_unrelaxed'] = f['gamma_unrelaxed_min'] = 140.0
        f['lowest_gamma_cut'], f['same_cut'], f['E_att_lowest_gamma_cut'] = 1, False, f['cuts'][1]['E_att']
        g = a['families'][2]                       # a tie between equivalent cuts within the QEq tolerance
        g['cuts'][1].update(E_att=g['E_att'] - 0.002, gamma_unrelaxed=g['gamma_unrelaxed_min'] - 0.001)
        g['E_slice_rigid'] = E_RIGID - g['E_att']
        g['gamma_unrelaxed_min'] = g['cuts'][1]['gamma_unrelaxed']
        g['lowest_gamma_cut'], g['same_cut'], g['E_att_lowest_gamma_cut'] = 1, False, g['cuts'][1]['E_att']
        se = se_records()
        se[0]['result']['gamma'] = 140.0
        se[2]['result']['gamma'] = g['gamma_unrelaxed_min']
        recs = records(a, se=se)
        d0 = [x for x in recs[0]['result']['flags'] if x['code'] == 'cut_rules_disagree']
        d2 = [x for x in recs[2]['result']['flags'] if x['code'] == 'cut_rules_disagree']
        self.assertEqual(len(d0), 1)
        self.assertNotIn('tie', d0[0]['detail'])
        self.assertIn('tie within the QEq tolerance', d2[0]['detail'])
        self.assertEqual(recs[0]['result']['E_att_lowest_gamma_cut'], -17.0)
        self.assertEqual(recs[0]['slice']['cut_index'], 0)                # the growth slice stays the lowest |E_att|
        s, why = S.ae_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)
        self.assertIsNone(why)
        self.assertIn('cut_rules_disagree', [x['code'] for x in s['flags']])

    def test_refusals(self):
        se = se_records()
        se[1]['facet']['family_members'] = [[0, 1, 0]]                    # an orbit that differs from the spec's
        with self.assertRaisesRegex(AssertionError, 'Laue orbit'):
            records(se=se)
        s = spec()
        s['lattice_rows_abc'] = [[A + 0.01, 0, 0], [0, B, 0], [0, 0, C]]
        with self.assertRaisesRegex(AssertionError, 'lattice'):
            records(s=s)
        a = att()
        a['unitcell_md5'] = 'e' * 32                                      # another unit cell than the dataset's
        with self.assertRaisesRegex(AssertionError, 'no surface_energy record'):
            records(a)
        a = att()
        a['families'][0]['growth_cut'] = 1                                # not the lowest-|E_att| cut
        with self.assertRaisesRegex(AssertionError, 'lowest-\\|E_att\\|'):
            records(a)
        a = att()
        a['families'][0]['n_ext_structural'] = 2                          # the stacking does not have the symmetry
        with self.assertRaisesRegex(AssertionError, 'extinction'):
            records(a)
        se = se_records()
        se[2]['result']['gamma'] = 171.0                                  # the AE cell is not the dataset's
        with self.assertRaisesRegex(AssertionError, 'no level-1 record'):
            records(se=se)
        a = att()
        a['families'][1]['E_slice_rigid'] += 0.5
        with self.assertRaisesRegex(AssertionError, 'E_latt'):
            records(a)


class TestAEShape(unittest.TestCase):
    def test_box_shape_analytic_and_morphology(self):
        recs = records()
        s, why = S.ae_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)
        self.assertIsNone(why)
        validate(s, 'wulff_shape')
        self.assertEqual((s['construction'], s['level'], s['distance_quantity']), ('attachment_energy', 'ae-static', 'abs_E_att'))
        want = box_area({f: abs(e) for f, e in E_ATT.items()})
        by = {f['family']: f for f in s['facets']}
        for f, a in want.items():
            self.assertAlmostEqual(by[f]['area_fraction'], a, places=5)
            self.assertEqual(by[f]['distance'], abs(E_ATT[f]))
            self.assertEqual(by[f]['E_att'], E_ATT[f])
            self.assertIsNone(by[f]['gamma'])
        self.assertEqual(s['facets'][0]['family'], '{0 1 0}')            # the smallest |E_att| grows slowest
        self.assertEqual((s['checks']['laue_class'], s['checks']['pymatgen_recip_ops_order'],
                          s['checks']['pymatgen_recip_ops_symprec_1em3']), ('mmm', 8, 8))
        self.assertEqual(s['sensitivity']['n_samples'], S.AE_N_SAMPLES)
        self.assertIn('growth_morphology', [x['code'] for x in s['flags']])
        chk = AI.check_against_morphology(s, morphology(), 'chain:x')
        self.assertLess(chk['max_abs_diff_area'], 1e-5)
        bad = morphology()
        bad['shapes']['ae']['area']['{1 0 0}'] += 0.001                   # 3rd decimal off
        with self.assertRaisesRegex(AssertionError, 'differs from the morphology'):
            AI.check_against_morphology(s, bad, 'chain:x')
        # the schema keeps the constructions apart: an AE shape needs E_att / distance, a Wulff one a gamma
        import jsonschema
        s2 = copy.deepcopy(s)
        del s2['facets'][0]['distance']
        with self.assertRaises(jsonschema.ValidationError):
            validate(s2, 'wulff_shape')
        s3 = copy.deepcopy(s)
        s3['construction'] = 'wulff'
        with self.assertRaises(jsonschema.ValidationError):
            validate(s3, 'wulff_shape')

    def test_shape_refusals(self):
        recs = records()
        for r in recs:
            r['material'] = dict(r['material'], space_group={'symbol_data_setting': 'P2/m', 'number': 10, 'laue_class': '2/m'})
        s, why = S.ae_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)
        self.assertIsNone(s)
        self.assertIn('2/m', why)
        s, why = S.ae_shape(records()[:2], X.flag, X.rid, X.SCHEMA_VERSION)
        self.assertIsNone(s)
        self.assertIn('needs >= 3', why)
        recs = records()
        recs[0]['result']['E_att'] = 1.0
        self.assertIn('non-negative', S.ae_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)[1])


class TestCommittedAE(unittest.TestCase):
    def test_dataset_records_and_shapes(self):
        d = os.path.join(ROOT, 'dataset')
        idx = json.load(open(os.path.join(d, 'index.json')))
        self.assertEqual(idx['files'].get('attachment_energy'), 'attachment_energy.jsonl')
        ae = [json.loads(l) for l in open(os.path.join(d, 'attachment_energy.jsonl')) if l.strip()]
        se = {json.loads(l)['record_id'] for l in open(os.path.join(d, 'surface_energy.jsonl')) if l.strip()}
        by = {r['record_id']: r for r in ae}
        self.assertEqual(idx['counts']['attachment_energy'], len(ae))
        self.assertEqual({r['material']['material_id'] for r in ae}, {'tnt-monoclinic', 'paracetamol-i', 'rdx-alpha'})
        for r in ae:
            validate(r, 'attachment_energy')
            self.assertLess(r['result']['E_att'], 0)
            self.assertTrue(set(r['related_record_ids']) <= se and r['related_record_ids'])
        shapes = [json.loads(l) for l in open(os.path.join(d, 'wulff_shape.jsonl')) if l.strip()]
        aes = [s for s in shapes if s.get('construction') == 'attachment_energy']
        self.assertEqual(len(aes), 3)
        self.assertTrue(all(s.get('construction') == 'wulff' for s in shapes if s not in aes))
        import numpy as np
        for s in aes:
            self.assertLess(s['provenance']['checked_against_morphology']['max_abs_diff_area'], AI.AREA_TOL)
            reps = [tuple(f['representative']) for f in s['facets']]
            area = S._wulff(np.array(s['lattice_matrix']), reps, [f['distance'] for f in s['facets']])[1]
            for f, a in zip(s['facets'], area):
                r = by[f['input_record_ids'][0]]
                self.assertEqual(r['facet']['family'], f['family'])
                self.assertAlmostEqual(f['distance'], abs(r['result']['E_att']), places=6)
                self.assertAlmostEqual(f['area_fraction'], a, places=5)


if __name__ == '__main__':
    unittest.main()

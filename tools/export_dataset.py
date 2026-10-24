#!/usr/bin/env python
"""Export the surface-energy dataset (prototype): dataset/surface_energy.jsonl, dataset/wulff_shape.jsonl,
dataset/attachment_energy.jsonl, dataset/index.json, and a Parquet copy of the surface-energy records when pyarrow is
installed.

    ./wh py tools/export_dataset.py [--chain-root PATH] [--zoo PATH] [--ae DIR] [--out dataset]

Sources (schema/surface_energy.schema.json, schema_version 0.2.0):
  1. wulffhound ledgers, data/tnt/ledger.json and data/paracetamol/ledger.json (levels 1 and 2, failures too):
     one record per computed termination group of a level-1 run, one per level-2 run.
  2. The chain's CIF zoo, <chain>/notes/validation/cif_zoo.json: 21 COD crystals x 2 facets, the unrelaxed gamma
     of every termination (ReaxFF-lg screening). The file is READ from the private chain (WULFFHOUND_CHAIN_ROOT,
     SIM2L_DEV_ROOT, --chain-root or --zoo); only the exported records are written here.
  3. surfenergy's pinned regression values and documented runs (TNT and HMX), transcribed below with their
     protocol (no run folder id is recorded for them: flagged no_run_id).
  4. The campaign driver's stores, dataset/campaigns/<campaign>.jsonl (wulffhound/campaign_ingest.py): records made
     by `./wh campaign run`, already schema-valid, with their run ids. A campaign level-2 record of a bundle material
     (TNT, paracetamol) enters that material's shape (a newer run wins over the ledger's, as in the engine), and a
     ledger record it refreshes gets the refreshing record's id in its protocol_superseded flag.
  5. The chain's attachment-energy prototype (static Hartman-Perdok growth slices), <chain>/devtools/attachment/
     results/<m>_{spec,attachment,morphology}.json for TNT, paracetamol and alpha-RDX: READ from the private chain
     (WULFFHOUND_AE_RESULTS, --ae or <chain-root>/devtools/attachment/results; wulffhound/attachment_ingest.py), one
     attachment_energy record per family, and the growth (AE) shape of each material (shapes.ae_shape, construction
     'attachment_energy'; Laue orbits and pymatgen symprec asserted as for the Wulff shapes), whose area fractions are
     asserted equal to the prototype's morphology JSON to 3 decimals. Only the exported records are written here.
Every record is validated against the schema (the export fails on an invalid record); flagged records are kept.
Derived wulff_shape records: TNT and paracetamol at level 'best' and level 1, built with the wulffhound engine
(pymatgen WulffShape on the data-file lattice, symprec 1e-5 <= 1e-3, orbits asserted equal to the Laue class 2/m).
Materials without a bundle get a level-1 shape built from their records (wulffhound/shapes.py) once >= 3 families
are computed and they enclose a bounded shape, and a 'best' shape once they also have relaxed (level-2) records
(the engine's rule: level 2 where available, else level 1 x the mean relaxed/unrelaxed ratio); the orbits are
asserted equal to pymatgen's reciprocal-lattice operations and to the Laue class of the space group (2/m, mmm, ...)
at symprec 1e-5 and 1e-3. With fewer families (the zoo's two per crystal) only the facet energies are recorded.
No atomic coordinates are exported: only gamma values, hkl, lattice parameters and metadata.
"""
import argparse
import datetime
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import warnings

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'wulffhound'))

SCHEMA_VERSION = '0.2.0'
SCHEMA_PATH = os.path.join(ROOT, 'schema', 'surface_energy.schema.json')
MASS = {'C': 12.011, 'H': 1.008, 'N': 14.007, 'O': 15.999}
REAX_FF = {'name': 'ReaxFF-lg', 'version': 'reax-lg-2011 (Liu, Liu, Zybin & Goddard, J. Phys. Chem. A 115, 11016 (2011))',
           'kind': 'reactive', 'file': 'reax-lg-2011.ff (not distributed)'}
REAX_MD5 = 'c129e55db39a4f1e0e5cad3b7a6b0a00'   # md5 of reax-lg-2011.ff in the chain (cif2lammps/examples/hmx, surfenergy/examples/hmx_cif)

# space groups of the zoo (CIF symbols as cif2lammps reports them): number, Laue class
SG = {'P -4 21 m': (113, '4/mmm'), 'P m n n': (58, 'mmm'), 'I -4': (82, '4/m'), 'P 1 21/c 1': (14, '2/m'),
      'P b c a': (61, 'mmm'), 'P 1 21/n 1': (14, '2/m'), 'P n a 21': (33, 'mmm'), 'P c c n': (56, 'mmm'),
      'P 1 21/a 1': (14, '2/m'), 'P c a 21': (29, 'mmm'), 'P -4 21 c': (114, '4/mmm')}
# zoo key -> (material_id, polymorph label)
ZOO_IDS = {'urea': ('urea', 'tetragonal (only known ambient form)'), 'pyrazine': ('pyrazine', 'orthorhombic, 295 K'),
           'pentaerythritol': ('pentaerythritol', 'tetragonal I-4'), 'naphthalene': ('naphthalene', 'monoclinic'),
           'anthracene': ('anthracene', 'monoclinic'), 'benzoic': ('benzoic-acid', 'monoclinic (ordered model)'),
           'succinic': ('succinic-acid-beta', 'beta'), 'oxalic': ('oxalic-acid-alpha', 'alpha'),
           'salicylic': ('salicylic-acid', 'form I'), 'paracetamol': ('paracetamol-i', 'form I'),
           'aspirin': ('aspirin-i', 'form I'), 'theophylline': ('theophylline-ii', 'form II'),
           'carbamazepine': ('carbamazepine-iii', 'form III'), 'nicotinamide': ('nicotinamide', 'alpha'),
           'cytosine': ('cytosine-ii', 'form II'), 'glycine': ('glycine-alpha', 'alpha (zwitterion)'),
           'tnt_mono': ('tnt-monoclinic', 'monoclinic'), 'tnt_ortho': ('tnt-orthorhombic', 'orthorhombic Pca2_1'),
           'hmx': ('hmx-beta', 'beta'), 'rdx': ('rdx-alpha', 'alpha'), 'petn': ('petn-i', 'PETN-I')}
WH_MATERIALS = {
    'tnt': {'material_id': 'tnt-monoclinic', 'name': 'TNT (2,4,6-trinitrotoluene)', 'polymorph': 'monoclinic',
            'inchikey': 'SPSSULHKWOKEEL-UHFFFAOYSA-N', 'category': 'energetic',
            'structure': {'structure_id': 'chain-tnt-unitcell', 'source': 'TNT unit cell of the private chain '
                          '(surfenergy/examples/tnt001_nonreactive), CCDC-derived; not distributed',
                          'database': 'CCDC', 'database_id': None,
                          'licence': 'CCDC terms: not redistributable (only gamma, hkl, lattice parameters and metadata are exported)',
                          'citation': None, 'doi': None, 'url': None, 'sha256': None,
                          'md5': 'ede8756c5b71e4c8a1949038d47427bf', 'measurement_T_K': None, 'distributed': False},
            'space_group': {'symbol_data_setting': 'P2_1/a', 'number': 14, 'laue_class': '2/m'},
            'force_field': {'name': 'TNT non-reactive (buck + lj/cut + coul/wolf, 11 A)', 'version': 'in.tnt-run0.lmp of the chain',
                            'kind': 'non-reactive', 'md5': 'fd7d9da6814b13e8f8c195abb0b05eb1',
                            'file': 'in.tnt-run0.lmp (not distributed)', 'validated_for_material': True,
                            'note': 'a TNT-specific non-reactive force field with full topology, validated for TNT in the chain'}},
    'paracetamol': {'material_id': 'paracetamol-i', 'name': 'paracetamol (acetaminophen)', 'polymorph': 'form I',
                    'inchikey': 'RZVAJINKPMORJF-UHFFFAOYSA-N', 'category': 'pharmaceutical',
                    'structure': None,   # from the zoo (same COD entry)
                    'space_group': {'symbol_data_setting': 'P2_1/n', 'number': 14, 'laue_class': '2/m'},
                    'force_field': dict(REAX_FF, md5=REAX_MD5, validated_for_material=False,
                                        note='ReaxFF-lg is parametrised for nitramine energetic crystals; not validated for '
                                             'hydrogen-bonded pharmaceuticals: screening-level values')},
}


# ---------------------------------------------------------------------------------------------- helpers
def rid(prefix, *key):
    return '%s-%s' % (prefix, hashlib.sha1(json.dumps(key, sort_keys=True, default=str).encode()).hexdigest()[:12])


def flag(code, severity, detail, by='exporter'):
    return {'code': code, 'severity': severity, 'detail': detail, 'by': by}


def fam_str(h):
    return '{%s}' % ' '.join(str(int(x)) for x in h)


def parse_formula(s):
    return {el: int(n or 1) for el, n in re.findall(r'([A-Z][a-z]?)(\d*)', s)}


def hill(f):
    keys = (['C'] if 'C' in f else []) + (['H'] if 'H' in f else []) + sorted(k for k in f if k not in ('C', 'H'))
    return ''.join('%s%s' % (k, f[k] if f[k] != 1 else '') for k in keys)


def molar_mass(f):
    return round(sum(MASS[k] * v for k, v in f.items()), 3)


def lattice_from_params(a, b, c, al, be, ga):
    """Rows a, b, c in the LAMMPS orientation (a along x, b in xy), as cif2lammps writes the data file."""
    al, be, ga = (math.radians(x) for x in (al, be, ga))
    ax = a
    bx, by = b * math.cos(ga), b * math.sin(ga)
    cx = c * math.cos(be)
    cy = (b * c * math.cos(al) - bx * cx) / by
    cz = math.sqrt(max(c * c - cx * cx - cy * cy, 0.0))
    return [[ax, 0.0, 0.0], [bx, by, 0.0], [cx, cy, cz]]


def cell_block(params, Z, formula, matrix=None, note=None):
    a, b, c, al, be, ga = params
    L = np.array(matrix if matrix is not None else lattice_from_params(*params))
    V = float(abs(np.linalg.det(L)))
    dens = molar_mass(formula) * Z / (V * 0.6022140762) if Z else None
    return {'a': round(a, 5), 'b': round(b, 5), 'c': round(c, 5), 'alpha': round(al, 4), 'beta': round(be, 4),
            'gamma': round(ga, 4), 'volume': round(V, 3), 'Z': Z, 'density': round(dens, 4) if dens else None,
            'lattice_matrix': [[round(float(x), 8) for x in r] for r in L], 'note': note}


HOME_RE = re.compile(r'(/home|/Users)/[^/\s]+/')


def sanitize(s, chain_root=None):
    if s is None:
        return None
    if chain_root:
        s = s.replace(chain_root.rstrip('/') + '/', 'chain:')
    s = re.sub(r'(/home|/Users)/[^/\s]+/sandbox/surface-energy-sim2ls/', 'chain:', s)
    s = HOME_RE.sub('~/', s)
    return s


def err_line(err, n=400):
    lines = [l.strip() for l in (err or '').splitlines() if l.strip()]
    good = [l for l in lines if any(w in l.lower() for w in ('error', 'fail', 'no solution', 'refused', 'too large'))]
    return ((good or lines or [''])[-1])[:n]


def run_id_of(ref):
    return os.path.basename(ref.rstrip('/')) if ref else None


def git_rev(path, cwd):
    try:
        return subprocess.run(['git', 'log', '-1', '--format=%h', '--', path], cwd=cwd, capture_output=True,
                              text=True, timeout=20).stdout.strip() or None
    except Exception:
        return None


def md5_file(p):
    h = hashlib.md5()
    with open(p, 'rb') as f:
        h.update(f.read())
    return h.hexdigest()


# ---------------------------------------------------------------------------------------------- material blocks
class Materials:
    def __init__(self, zoo):
        self.zoo = {r['key']: r for r in zoo}
        self.by_inchikey = {}
        for r in zoo:
            c = r.get('cif2lammps') or {}
            if c.get('ok'):
                self.by_inchikey.setdefault(r['inchikey'], r)

    def zoo_material(self, r):
        c = r['cif2lammps']
        mid, poly = ZOO_IDS[r['key']]
        f = parse_formula(c['formula'])
        num, laue = SG.get(c['space_group'], (None, None))
        mT = re.search(r';\s*(?:neutron,\s*)?(\d+)\s*K', r.get('citation') or '')
        doi = re.search(r'doi:(\S+)', r.get('citation') or '')
        return {'material_id': mid, 'name': re.sub(r'\s*\((?:imidazole|hexamine|uracil)[^)]*\)', '', r['name']),
                'inchikey': r['inchikey'], 'smiles': c.get('smiles'), 'formula': hill(f), 'molecular_mass': molar_mass(f),
                'polymorph': poly, 'category': r.get('category'), 'pubchem_cid': (r.get('pubchem') or {}).get('cid'),
                'structure': {'structure_id': 'cod-%d' % r['cod'], 'source': 'COD %d' % r['cod'], 'database': 'COD',
                              'database_id': str(r['cod']),
                              'licence': 'CC0 (Crystallography Open Database); acknowledge the original authors',
                              'citation': r.get('citation'), 'doi': doi.group(1).rstrip(';,') if doi else None,
                              'url': 'https://www.crystallography.net/cod/%d.html' % r['cod'], 'sha256': r.get('sha256'),
                              'md5': None, 'measurement_T_K': float(mT.group(1)) if mT else None,
                              'distributed': r['key'] == 'paracetamol'},
                'space_group': {'symbol_data_setting': c['space_group'].replace(' ', ''), 'number': num, 'laue_class': laue},
                'cell': cell_block(c['cell'], c['Z'], f, note='CIF cell (cif2lammps keeps the CIF a, b, c; data-file orientation)')}

    def wh_material(self, mat, sym):
        m = WH_MATERIALS[mat]
        z = self.by_inchikey.get(m['inchikey'])
        f = sym['formula_molecule']
        lp = sym['lattice_parameters']
        out = {'material_id': m['material_id'], 'name': m['name'], 'inchikey': m['inchikey'],
               'smiles': (z or {}).get('cif2lammps', {}).get('smiles'), 'formula': hill(f), 'molecular_mass': molar_mass(f),
               'polymorph': m['polymorph'], 'category': m['category'],
               'pubchem_cid': ((z or {}).get('pubchem') or {}).get('cid'),
               'structure': m['structure'] or self.zoo_material(z)['structure'],
               'space_group': m['space_group'],
               'cell': cell_block([lp[k] for k in ('a', 'b', 'c', 'alpha', 'beta', 'gamma')], sym['n_molecules'], f,
                                  matrix=sym['lattice_rows_abc'], note='the data-file cell of the wulffhound bundle')}
        if mat == 'paracetamol':
            out['structure'] = dict(out['structure'], distributed=True)
        return out


# ---------------------------------------------------------------------------------------------- source 1: ledgers
def setting_of(material_block, extra=None):
    m = material_block
    d = ('Miller indices relative to the data-file a, b, c of %s (%s, a %.4f b %.4f c %.4f A, alpha %.2f beta %.3f '
         'gamma %.2f deg)' % (m['structure']['structure_id'], m['space_group']['symbol_data_setting'], m['cell']['a'],
                               m['cell']['b'], m['cell']['c'], m['cell']['alpha'], m['cell']['beta'], m['cell']['gamma']))
    return {'description': d, 'structure_id': m['structure']['structure_id'], 'other_settings': extra}


def wh_records(mat, mats, chain_root):
    from engine import consistency
    bundle = os.path.join(ROOT, 'data', mat)
    led = json.load(open(os.path.join(bundle, 'ledger.json')))
    sym = json.load(open(os.path.join(bundle, 'cell_symmetry.json')))
    mj = json.load(open(os.path.join(bundle, 'material.json')))
    fams = {f['family']: f for f in sym['families']}
    member = {tuple(m): f['family'] for f in sym['families'] for m in f['members']}
    mb = mats.wh_material(mat, sym)
    ff = WH_MATERIALS[mat]['force_field']
    other = None
    if mj.get('setting_map'):
        other = 'literature setting %s: %s' % (mj['setting_map']['literature_setting'], mj['setting_map']['explicit'])
    setting = setting_of(mb, other)
    in_md5 = {'datafile': mj.get('datafile_md5_recorded'), 'script': mj.get('script_md5_recorded')}
    if mat == 'paracetamol':
        in_md5['cif'] = md5_file(os.path.join(bundle, 'paracetamol_I_COD2104364.cif'))
        in_md5['forcefield'] = REAX_MD5
    out, entry_records = [], {}
    for i, e in enumerate(led['entries']):
        lv = e['level']
        h = tuple(e['hkl'])
        fam = e['family']
        frec = fams.get(fam) or {}
        rd = e.get('run_dir')
        sim2l = 'surfenergy' if lv == 2 or (rd and 'surfenergy/bin/RUNS' in rd) else 'supercell+slabcutter'
        prov_base = {
            'source': 'wulffhound-ledger', 'sim2l': {'name': sim2l, 'revision': None},
            'run_id': run_id_of(rd), 'run_ref': rd, 'resultsdb_id': None,
            'child_run_ids': [run_id_of(c['run_dir']) for c in e.get('child_runs') or [] if c.get('run_dir')
                              and run_id_of(c['run_dir']) != run_id_of(rd)],
            'input_md5': {k: v for k, v in dict(in_md5, datafile=e.get('datafile_md5') or in_md5['datafile'],
                                                script=e.get('script_md5') or in_md5['script']).items() if v},
            'input_sha256': {},
            'date': e.get('recorded') or led.get('built'),
            'venue': 'local chain (development machine), serial LAMMPS',
            'cpu_s': e.get('cpu_s'), 'wall_s': e.get('wall_s'),
            'reproduce': "wulffhound engine (needs the chain): Engine('%s').run_facet('%s', level=%d, hkl=%s, "
                         "search_range=%s, n_periods=%s)" % (mat, fam, lv, list(h), e.get('search_range'), e.get('n_periods')),
            'notes': [sanitize(n, chain_root)[:400] for n in (e.get('notes') or [])
                      if 'commands of the input script not used' not in n][:6] + ['ledger source: %s' % e.get('source')]}
        facet = {'hkl': list(h), 'family': fam, 'family_representative': frec.get('representative'),
                 'family_members': frec.get('members'), 'multiplicity': frec.get('multiplicity'),
                 'd_hkl': round(e.get('d_hkl') or frec.get('d_hkl') or 0, 5) or None,
                 'd_eff': round(frec['d_eff'], 5) if frec.get('d_eff') else None, 'setting': setting}
        proto = {'level': lv, 'level_name': 'unrelaxed' if lv == 1 else 'relaxed_T0', 'temperature_K': float(e.get('temperature') or 0.0),
                 'n_periods': e.get('n_periods'), 'vacuum_A': 30.0, 'search_range': e.get('search_range'),
                 'slab_from': 'static_cut' if lv == 2 else None,
                 'minimiser': None, 'timestep_fs': None, 'equilibration_ps': None, 'production_ps': None, 'n_blocks': None,
                 'mnp': e.get('mnp_used'), 'n_molecules_cell': e.get('n_molecules_cell'),
                 'n_molecules_slab': e.get('n_molecules_slab'), 'n_molecules_bulk': e.get('n_molecules_bulk'),
                 'surface_area_A2': round(e['surface_area'], 4) if e.get('surface_area') else
                 (round(e['surface_area_static'], 4) if e.get('surface_area_static') else None),
                 'thickness_A': round(e['thickness'], 4) if e.get('thickness') else None, 'energy_check': None}
        if lv == 2:
            proto['minimiser'] = ('CG with box/relax (bulk), CG in the bulk lateral box (slab)' if not mj.get('use_reaxff')
                                  else 'surfenergy CG (minimiser-limited on ReaxFF), then FIRE at fixed box (spike)')
        base_flags = []
        if not ff['validated_for_material']:
            base_flags.append(flag('force_field_not_validated', 'warning', ff['note']))
        recs = []
        if not e.get('ok', True):
            recs.append(({'index': None, 'label': 'n/a (run failed)', 'group': None, 'equivalent_indices': [], 'gap': None,
                          'faces_equivalent': None, 'n_terminations': e.get('n_terminations'), 'is_lowest': None},
                         {'status': 'failed', 'gamma': None, 'gamma_error': None, 'error_method': None, 'units': 'mJ/m^2',
                          'stationary': None, 'gamma_unrelaxed': None, 'gamma_raw': None, 'gamma_basis': None,
                          'gamma_flags': [flag('run_failed', 'error', sanitize(err_line(e.get('error')), chain_root))],
                          'molecules_intact': None, 'minimisation': None,
                          'error_message': sanitize(err_line(e.get('error')), chain_root)}, 'run'))
        elif lv == 1:
            terms = e.get('terminations') or []
            used = e.get('termination_used')
            used_group = next((t.get('group') for t in terms if t['index'] == used), None)
            comp = [t for t in terms if t.get('sfe_source') == 'computed' and t.get('sfe_unrelaxed') is not None]
            if not comp:
                comp = [{'index': used, 'group': None, 'gap': None, 'faces_equivalent': e.get('faces_equivalent'),
                         'sfe_unrelaxed': e['gamma']}]
            gmin = min(t['sfe_unrelaxed'] for t in comp)
            for t in comp:
                eq = [x['index'] for x in terms if x.get('group') is not None and x.get('group') == t.get('group')]
                fl = list(base_flags) + [flag('unrelaxed_screening', 'info', 'unrelaxed cut of the bulk (no relaxation): '
                                              'an upper bound of the T = 0 relaxed gamma')]
                if t.get('faces_equivalent') is False:
                    fl.append(flag('faces_not_equivalent', 'warning', 'the top and bottom faces of this slab differ: gamma '
                                   'is the average of the two faces'))
                lowest = (t['index'] == used) or (used_group is not None and t.get('group') == used_group) or \
                         (used is None and abs(t['sfe_unrelaxed'] - gmin) < 1e-9)
                recs.append(({'index': t['index'], 'label': 't%s%s' % (t['index'], ' (gap %.2f A)' % t['gap'] if t.get('gap') else ''),
                              'group': t.get('group'), 'equivalent_indices': eq or ([t['index']] if t['index'] is not None else []),
                              'gap': t.get('gap'), 'faces_equivalent': t.get('faces_equivalent'),
                              'n_terminations': e.get('n_terminations'), 'is_lowest': bool(lowest)},
                             {'status': 'ok', 'gamma': round(float(t['sfe_unrelaxed']), 4), 'gamma_error': 0.0,
                              'error_method': 'none: deterministic static energies', 'units': 'mJ/m^2', 'stationary': None,
                              'gamma_unrelaxed': None, 'gamma_raw': None, 'gamma_basis': 'slabcutter unrelaxed SFE, run 0 of bulk and slab',
                              'gamma_flags': fl, 'molecules_intact': None, 'minimisation': None, 'error_message': None},
                             't%s' % t['index']))
        else:
            good, why = consistency(e)
            fl = list(base_flags)
            if not good:
                code = 'gamma_not_positive' if float(e['gamma']) <= 0 else \
                    'molecules_not_intact' if e.get('molecules_intact') is False and 'relaxed gamma' not in why else 'relaxed_above_unrelaxed'
                fl.append(flag(code, 'error', why + ' (left out of Wulff shapes; the level-1 value is used)', by='wulffhound-guard'))
            if e.get('gamma_surfenergy_raw') is not None:
                rg, rwhy = consistency(dict(e, gamma=e['gamma_surfenergy_raw']))
                fl.append(flag('raw_value_superseded', 'info', 'surfenergy reported %.2f mJ/m^2 (minimiser-limited%s); '
                               'this record is the FIRE-continued relaxation of the same run' % (
                                   e['gamma_surfenergy_raw'], '; that value fails the guard: ' + rwhy if not rg else '')))
            if not mj.get('use_reaxff'):
                fl.append(flag('protocol_superseded', 'info', 'slab_from static_cut (the static cut mapped onto the relaxed '
                               'bulk box): surfenergy re-cuts the slab from the relaxed bulk since 2026-10-23 (R3); for TNT '
                               '(0 0 1) that moved gamma by -0.07 % (104.0965 -> 104.0232)'))
            if e.get('faces_equivalent') is False:
                fl.append(flag('faces_not_equivalent', 'warning', 'the top and bottom faces of this slab differ: gamma is '
                               'the average of the two faces'))
            recs.append(({'index': e.get('termination_used'), 'label': 't%s (lowest unrelaxed)' % e.get('termination_used'),
                          'group': None, 'equivalent_indices': [], 'gap': None, 'faces_equivalent': e.get('faces_equivalent'),
                          'n_terminations': e.get('n_terminations'), 'is_lowest': True},
                         {'status': 'ok', 'gamma': round(float(e['gamma']), 4), 'gamma_error': e.get('gamma_error'),
                          'error_method': 'none: T = 0 minimisation (deterministic)', 'units': 'mJ/m^2', 'stationary': None,
                          'gamma_unrelaxed': round(float(e['gamma_unrelaxed']), 4) if e.get('gamma_unrelaxed') is not None else None,
                          'gamma_raw': round(float(e['gamma_surfenergy_raw']), 4) if e.get('gamma_surfenergy_raw') is not None else None,
                          'gamma_basis': e.get('gamma_basis') or 'surfenergy relaxed T = 0 gamma',
                          'gamma_flags': fl, 'molecules_intact': e.get('molecules_intact'), 'minimisation': None,
                          'error_message': None}, 'run'))
        ids = []
        for term, res, tag in recs:
            ref = 'data/%s/ledger.json#entries[%d]%s' % (mat, i, '' if tag == 'run' else '/terminations[%s]' % tag[1:])
            r = {'schema_version': SCHEMA_VERSION, 'record_type': 'surface_energy', 'record_id': rid('se', 'wh', ref),
                 'material': mb, 'force_field': ff, 'facet': facet, 'termination': term, 'protocol': proto, 'result': res,
                 'provenance': dict(prov_base, source_ref=ref)}
            out.append(r)
            ids.append(r)
        entry_records[i] = ids
    return out, entry_records, led


# ---------------------------------------------------------------------------------------------- source 2: zoo
def zoo_records(zoo, mats, chain_root, zoo_rev, zoo_ref):
    out, skipped = [], []
    for r in zoo:
        c = r.get('cif2lammps') or {}
        if not c.get('ok'):
            skipped.append('%s (COD %s): cif2lammps rejected it as expected: %s' % (r['key'], r['cod'], c.get('error', '')[:120]))
            continue
        mb = mats.zoo_material(r)
        bf = {tuple(b['rep']): b for b in r.get('bfdh') or []}
        ff = dict(REAX_FF, md5=REAX_MD5, validated_for_material=False,
                  note='CIF-zoo screening with ReaxFF-lg (parametrised for nitramines): unrelaxed cuts, not validated surface energies')
        for fi, f in enumerate(r.get('facets') or []):
            rep = tuple(f['family'])
            b = bf.get(rep, {})
            sc = f.get('supercell') or {}
            st = f.get('slabcutter') or {}
            hkl = sc.get('facet_hkl') or sc.get('hkl') or list(rep)
            facet = {'hkl': list(hkl), 'family': fam_str(rep), 'family_representative': list(rep),
                     'family_members': b.get('members'), 'multiplicity': b.get('multiplicity'),
                     'd_hkl': round(sc.get('d_hkl') or b.get('d_hkl') or 0, 5) or None, 'd_eff': b.get('d_eff'),
                     'setting': setting_of(mb)}
            proto = {'level': 1, 'level_name': 'unrelaxed', 'temperature_K': 0.0, 'n_periods': 2, 'vacuum_A': 30.0,
                     'search_range': sc.get('search_range'), 'slab_from': None, 'minimiser': None, 'timestep_fs': None,
                     'equilibration_ps': None, 'production_ps': None, 'n_blocks': None, 'mnp': sc.get('mnp_used'),
                     'n_molecules_cell': sc.get('n_molecules'), 'n_molecules_slab': st.get('n_molecules_slab'),
                     'n_molecules_bulk': None, 'surface_area_A2': st.get('surface_area'), 'thickness_A': None,
                     'energy_check': sc.get('energy_check')}
            run = run_id_of(sc.get('outdir'))
            prov = {'source': 'cif-zoo', 'sim2l': {'name': 'supercell+slabcutter', 'revision': zoo_rev},
                    'run_id': run, 'run_ref': 'chain:supercell/bin/RUNS/%s' % run if run else None, 'resultsdb_id': None,
                    'child_run_ids': [], 'input_md5': {'forcefield': REAX_MD5},
                    'input_sha256': {'cif': r.get('sha256')}, 'date': r.get('date'),
                    'venue': 'local chain (development machine), LAMMPS 22Jul2025',
                    'cpu_s': st.get('cpu_s'), 'wall_s': st.get('wall_s'),
                    'reproduce': 'chain: devtools/run_cif_zoo.py (cif2lammps normalize_hydrogens + relax_unitcell atoms, '
                                 'supercell hkl mode, slabcutter every termination, 2 periods, 30 A vacuum)',
                    'notes': ['supercell run %s (simtool); slabcutter engine runs in the zoo work folder (no run id); '
                              'cpu_s / wall_s are slabcutter, all terminations of the facet' % run,
                              'cif2lammps: X-H normalised to Allen & Bruno 2010, atoms relaxed in the CIF cell with '
                              'ReaxFF (capped minimisation, not converged)',
                              'supercell outcome: %s; attempts: %s' % (f.get('outcome'), ', '.join(
                                  '(%s) r%s %s' % (' '.join(map(str, a['hkl'])), a['search_range'], 'ok' if a['ok'] else 'failed')
                                  for a in f.get('attempts') or []))]}
            base = [flag('force_field_not_validated', 'warning', ff['note']),
                    flag('unrelaxed_screening', 'info', 'unrelaxed cut of the bulk (no relaxation): an upper bound of the '
                         'T = 0 relaxed gamma')]
            if sc.get('energy_check') is False:
                base.append(flag('energy_check_skipped', 'info', "supercell's ReaxFF energy check ran out of memory; the "
                                 'run was repeated with RunEnergyCheck = False'))
            gam = st.get('gammas') or []
            if not (st.get('ok') and gam):
                msg = sanitize(err_line(st.get('error') or f.get('outcome') or 'no slab'), chain_root)
                ref = '%s#%s/facets[%d]' % (zoo_ref, r['key'], fi)
                out.append({'schema_version': SCHEMA_VERSION, 'record_type': 'surface_energy', 'record_id': rid('se', 'zoo', ref),
                            'material': mb, 'force_field': ff, 'facet': facet,
                            'termination': {'index': None, 'label': 'n/a (run failed)', 'group': None, 'equivalent_indices': [],
                                            'gap': None, 'faces_equivalent': None, 'n_terminations': None, 'is_lowest': None},
                            'protocol': proto,
                            'result': {'status': 'failed', 'gamma': None, 'gamma_error': None, 'error_method': None,
                                       'units': 'mJ/m^2', 'stationary': None, 'gamma_unrelaxed': None, 'gamma_raw': None,
                                       'gamma_basis': None,
                                       'gamma_flags': base[:1] + [flag('run_failed', 'error', msg)],
                                       'molecules_intact': None, 'minimisation': None, 'error_message': msg},
                            'provenance': dict(prov, source_ref=ref)})
                continue
            gmin = min(gam)
            fe = st.get('faces_equivalent') or [None] * len(gam)
            for t, g in enumerate(gam):
                fl = list(base)
                if fe[t] is False:
                    fl.append(flag('faces_not_equivalent', 'warning', 'the top and bottom faces of this slab differ: gamma is '
                                   'the average of the two faces'))
                ref = '%s#%s/facets[%d]/slabcutter.gammas[%d]' % (zoo_ref, r['key'], fi, t)
                out.append({'schema_version': SCHEMA_VERSION, 'record_type': 'surface_energy', 'record_id': rid('se', 'zoo', ref),
                            'material': mb, 'force_field': ff, 'facet': facet,
                            'termination': {'index': t, 'label': 't%d' % t, 'group': None, 'equivalent_indices': [t],
                                            'gap': None, 'faces_equivalent': fe[t], 'n_terminations': st.get('n_terminations'),
                                            'is_lowest': g == gmin},
                            'protocol': proto,
                            'result': {'status': 'ok', 'gamma': float(g), 'gamma_error': 0.0,
                                       'error_method': 'none: deterministic static energies (rounded to 0.01 in the zoo file)',
                                       'units': 'mJ/m^2', 'stationary': None, 'gamma_unrelaxed': None, 'gamma_raw': None,
                                       'gamma_basis': 'slabcutter unrelaxed SFE, ReaxFF run 0 of bulk and slab',
                                       'gamma_flags': fl, 'molecules_intact': None, 'minimisation': None, 'error_message': None},
                            'provenance': dict(prov, source_ref=ref)})
    return out, skipped


# ---------------------------------------------------------------------------------------------- source 3: surfenergy
def surfenergy_records(mats, wh_tnt_material, chain_root):
    """surfenergy's pinned T = 0 regression values (devtools/test_surfenergy.py PINNED_T0) and the default-length
    TNT 300 K run (notes/validation/SURFENERGY_TNT300_DEFAULT.md), transcribed with their protocol. No run folder
    id is recorded for them (flag no_run_id)."""
    out = []
    tnt_ff = WH_MATERIALS['tnt']['force_field']
    tnt_sym = json.load(open(os.path.join(ROOT, 'data', 'tnt', 'cell_symmetry.json')))
    f001 = next(f for f in tnt_sym['families'] if f['family'] == '{0 0 1}')
    tnt_facet = {'hkl': [0, 0, 1], 'family': '{0 0 1}', 'family_representative': [0, 0, 1], 'family_members': f001['members'],
                 'multiplicity': 2, 'd_hkl': 19.5763, 'd_eff': round(f001['d_eff'], 5), 'setting': setting_of(wh_tnt_material)}
    tnt_term = {'index': 2, 'label': 't2 (lowest unrelaxed; t3 same group)', 'group': 2, 'equivalent_indices': [2, 3],
                'gap': None, 'faces_equivalent': False, 'n_terminations': 4, 'is_lowest': True}
    faces = flag('faces_not_equivalent', 'warning', 'the top and bottom faces of this slab differ: gamma is the average of the two faces')
    norun = flag('no_run_id', 'info', 'value recorded as a regression pin / in the validation notes; the run folder id is not recorded')
    common = {'resultsdb_id': None, 'child_run_ids': [], 'venue': 'local chain (development machine), lmp_serial 22Jul2025',
              'input_sha256': {}}
    # TNT T = 0
    out.append({
        'record_id': rid('se', 'surfenergy', 'tnt_T0', '2026-10-23'), 'material': wh_tnt_material, 'force_field': tnt_ff,
        'facet': tnt_facet, 'termination': tnt_term,
        'protocol': {'level': 2, 'level_name': 'relaxed_T0', 'temperature_K': 0.0, 'n_periods': 2, 'vacuum_A': 30.0,
                     'search_range': 6, 'slab_from': 'relaxed_bulk', 'minimiser': 'CG with box/relax (bulk), slab re-cut from '
                     'the relaxed bulk then CG', 'timestep_fs': None, 'equilibration_ps': None, 'production_ps': None,
                     'n_blocks': None, 'mnp': [-1, 0, 0, 0, -1, 0, 1, 0, 2], 'n_molecules_cell': 16, 'n_molecules_slab': 16,
                     'n_molecules_bulk': 16, 'surface_area_A2': 90.237, 'thickness_A': 39.153, 'energy_check': True},
        'result': {'status': 'ok', 'gamma': 104.0232, 'gamma_error': 0.0, 'error_method': 'none: T = 0 minimisation (deterministic)',
                   'units': 'mJ/m^2', 'stationary': None, 'gamma_unrelaxed': 118.24, 'gamma_raw': None,
                   'gamma_basis': 'surfenergy relaxed T = 0 gamma (pinned 104.0232 +- 0.01; two notebook runs bit-identical; '
                                  're-cut unrelaxed 115.10)',
                   'gamma_flags': [faces, norun], 'molecules_intact': True,
                   'minimisation': {'converged': True, 'note': 'every minimisation converged (CG for TNT); gamma_flags empty'},
                   'error_message': None},
        'provenance': dict(common, source='surfenergy-regression',
                           source_ref='chain:devtools/test_surfenergy.py PINNED_T0[tnt_T0]; notes/SURFENERGY_STATUS.md (R3)',
                           sim2l={'name': 'surfenergy', 'revision': 'chain d5c5a7b'}, run_id=None, run_ref=None,
                           input_md5={'datafile': 'ede8756c5b71e4c8a1949038d47427bf', 'script': 'fd7d9da6814b13e8f8c195abb0b05eb1'},
                           date='2026-10-23', cpu_s=None, wall_s=115.0,
                           reproduce='chain: devtools/test_surfenergy.py --cases tnt_T0 (surfenergy/examples/tnt001_nonreactive, '
                                     'temperature 0)',
                           notes=['n_periods 1 / 2 / 3: 104.0478 / 104.0232 / 104.0099 (thickness converged)',
                                  'slab_from static_cut (the protocol before R3) gives 104.0965'])})
    # TNT 300 K, default MD lengths
    out.append({
        'record_id': rid('se', 'surfenergy', 'tnt_300_default', '2026-10-22'), 'material': wh_tnt_material, 'force_field': tnt_ff,
        'facet': tnt_facet, 'termination': tnt_term,
        'protocol': {'level': 3, 'level_name': 'md', 'temperature_K': 300.0, 'n_periods': 2, 'vacuum_A': 30.0,
                     'search_range': 6, 'slab_from': 'static_cut', 'minimiser': None, 'timestep_fs': 0.5,
                     'equilibration_ps': 10.0, 'production_ps': 20.0, 'n_blocks': 5, 'mnp': [-1, 0, 0, 0, -1, 0, 1, 0, 2],
                     'n_molecules_cell': 16, 'n_molecules_slab': 16, 'n_molecules_bulk': 16, 'surface_area_A2': None,
                     'thickness_A': None, 'energy_check': True},
        'result': {'status': 'ok', 'gamma': 96.8, 'gamma_error': 5.7, 'error_method': 'larger of the block and '
                   'autocorrelation standard errors (here the block SEM), 1 sigma', 'units': 'mJ/m^2', 'stationary': True,
                   'gamma_unrelaxed': 118.24, 'gamma_raw': None, 'gamma_basis': 'surfenergy NPT bulk / NVT slab, <PE>',
                   'gamma_flags': [faces, norun, flag('protocol_superseded', 'info', 'slab_from static_cut (the slab mapped '
                                   'onto the NPT bulk box); since 2026-10-23 the default re-cuts the slab from the '
                                   'equilibrated bulk (R3): a default-length re-cut run is still to do')],
                   'molecules_intact': True, 'minimisation': None, 'error_message': None},
        'provenance': dict(common, source='surfenergy-notes', source_ref='chain:notes/validation/SURFENERGY_TNT300_DEFAULT.md',
                           sim2l={'name': 'surfenergy', 'revision': None}, run_id=None, run_ref=None,
                           input_md5={'datafile': 'ede8756c5b71e4c8a1949038d47427bf', 'script': 'fd7d9da6814b13e8f8c195abb0b05eb1'},
                           date='2026-10-22', cpu_s=1091.0, wall_s=1099.0,
                           reproduce='chain: devtools/run_sim2l.py surfenergy surfenergy/examples/tnt001_nonreactive --set '
                                     'equilibration_ps=10.0 --set production_ps=20.0 --set n_blocks=5 (and slab_from=static_cut)',
                           notes=['bulk PE 14.481 +- 0.065 kcal/mol per molecule, 300.2 K, 30 atm; slab 299.6 K, 250 samples',
                                  'equipartition term 0 (N_slab = N_bulk), reported not subtracted'])})
    # HMX T = 0 on the COD 4110146 cell (the chain example since 6b9edf5): b-unique P 1 21/n 1, facet (0 1 0)
    # (the facet called (0 0 1) in the earlier c-unique CCDC cell, whose value 224.43 is superseded)
    hz = mats.by_inchikey.get('UZGLIIJVICEWHF-UHFFFAOYSA-N')
    hmat = mats.zoo_material(hz)
    hff = dict(REAX_FF, md5=REAX_MD5, validated_for_material=False,
               note='ReaxFF-lg is parametrised for nitramines such as HMX; its surface energies are not validated against '
                    'a reference; T = 0 ReaxFF relaxations stop at force-field cusps (FIRE)')
    out.append({
        'record_id': rid('se', 'surfenergy', 'hmx_T0', 'cod4110146', '2026-10-23'), 'material': hmat, 'force_field': hff,
        'facet': {'hkl': [0, 1, 0], 'family': '{0 1 0}', 'family_representative': [0, 1, 0], 'family_members': [[0, 1, 0], [0, -1, 0]],
                  'multiplicity': 2, 'd_hkl': 10.761, 'd_eff': None,
                  'setting': {'description': 'Miller indices relative to the COD 4110146 cell (P 1 21/n 1, b unique)',
                              'structure_id': hmat['structure']['structure_id'],
                              'other_settings': '(0 1 0) here is (0 0 1) in the c-unique CCDC 1225494 setting (P 1 1 21/n)'}},
        'termination': {'index': 0, 'label': 't0 (lowest unrelaxed; t1 same group)', 'group': 0, 'equivalent_indices': [0, 1],
                        'gap': 5.3805, 'faces_equivalent': True, 'n_terminations': 2, 'is_lowest': True},
        'protocol': {'level': 2, 'level_name': 'relaxed_T0', 'temperature_K': 0.0, 'n_periods': 2, 'vacuum_A': 30.0,
                     'search_range': 6, 'slab_from': 'relaxed_bulk', 'minimiser': 'FIRE in fixed cells (unit cell 2000 steps '
                     'as the bulk reference, slab 2000), stopped by the energy-change criterion', 'timestep_fs': None,
                     'equilibration_ps': None, 'production_ps': None, 'n_blocks': None, 'mnp': [-1, 0, -4, -1, 0, 0, 0, 1, 0],
                     'n_molecules_cell': 8, 'n_molecules_slab': 16, 'n_molecules_bulk': 2, 'surface_area_A2': 186.367,
                     'thickness_A': None, 'energy_check': True},
        'result': {'status': 'ok', 'gamma': 226.0728, 'gamma_error': 0.0, 'error_method': 'none: T = 0 minimisation (deterministic)',
                   'units': 'mJ/m^2', 'stationary': None, 'gamma_unrelaxed': 236.9898, 'gamma_raw': None,
                   'gamma_basis': 'surfenergy relaxed T = 0 gamma (pinned 226.07 +- 1; notebook run and test agree)',
                   'gamma_flags': [flag('force_field_not_validated', 'warning', hff['note'])],
                   'molecules_intact': True,
                   'minimisation': {'converged': True, 'unit_cell_steps': 2000, 'slab_steps': 2000, 'stop': 'energy-change criterion'},
                   'error_message': None},
        'provenance': dict(common, source='surfenergy-run',
                           source_ref='chain:surfenergy/bin/RUNS/a1915be190a7484bbda8dd7362f1b734; devtools/test_surfenergy.py PINNED_T0[hmx_T0]',
                           sim2l={'name': 'surfenergy', 'revision': 'chain 6b9edf5'}, run_id='a1915be190a7484bbda8dd7362f1b734',
                           run_ref='chain:surfenergy/bin/RUNS/a1915be190a7484bbda8dd7362f1b734',
                           input_md5={'cif': '21f25ba03e3a4428cc7242de7da49f09', 'forcefield': REAX_MD5},
                           date='2026-10-23', cpu_s=347.7, wall_s=351.7,
                           reproduce='chain: devtools/run_sim2l.py surfenergy surfenergy/examples/hmx_cif (COD 4110146, (0 1 0), '
                                     'normalize_hydrogens, relax_unitcell atoms)',
                           notes=['the CCDC 1225494 cell gave 224.43 (unrelaxed 228.71) at its (0 0 1); the COD 20 K cell is 3 % denser',
                                  'X-H normalised to Allen & Bruno 2010 (cif2lammps; largest shift 0.003 A on this 20 K structure)'])})
    for r in out:
        r.update({'schema_version': SCHEMA_VERSION, 'record_type': 'surface_energy'})
    skipped = [
        'tnt_300 / hmx_300 short test-length MD (0.5 + 1 ps and 0.1 + 0.2 ps): e.g. 99.83 +- 23.32 and 178.89 +- 19.25 '
        'mJ/m^2, flagged not stationary by surfenergy, re-run at every protocol change, no run id: test settings, not results',
        'older protocol values of the same cases (hmx_T0 224.43 on the CCDC cell, 226.45 / 225.22 / 228.24, tnt_T0 104.0965 static cut, the '
        'pre-fix 300 K values of SURFENERGY_E2E.md): superseded by the pinned values above',
        'TNT 300 K example defaults (1 + 2 ps): 101.0 +- 13.9 / 121.7 +- 14.9, not stationary, no run id',
        'HMX 300 K at production length: never run (needs a cluster venue)']
    return out, skipped


# ---------------------------------------------------------------------------------------------- Wulff shapes
def campaign_entries(mat, campaign_recs):
    """Engine ledger entries for the campaign records of a bundle material (lowest termination, T = 0, ok, levels 1-2),
    so that its shape uses them exactly as the engine would use a newer run. Each names its record (dataset_record_id)."""
    mid = WH_MATERIALS[mat]['material_id']
    out = []
    for r in campaign_recs:
        P, R = r['protocol'], r['result']
        if r['material']['material_id'] != mid or R['status'] != 'ok' or P['level'] > 2 or P['temperature_K'] != 0 \
                or not r['termination'].get('is_lowest'):
            continue
        out.append({'family': r['facet']['family'], 'hkl': r['facet']['hkl'], 'level': P['level'], 'ok': True,
                    'gamma': R['gamma'], 'gamma_error': R.get('gamma_error') or 0.0, 'gamma_unrelaxed': R.get('gamma_unrelaxed'),
                    'termination_used': r['termination']['index'], 'faces_equivalent': r['termination'].get('faces_equivalent'),
                    'molecules_intact': R.get('molecules_intact'), 'n_periods': P['n_periods'],
                    'search_range': P['search_range'], 'temperature': 0.0, 'slab_from': P.get('slab_from'),
                    'datafile_md5': r['provenance']['input_md5'].get('datafile'),
                    'script_md5': r['provenance']['input_md5'].get('script'), 'recorded': r['provenance']['date'],
                    'cpu_s': r['provenance'].get('cpu_s'), 'n_molecules_cell': P.get('n_molecules_cell'),
                    'source': 'campaign: ' + r['provenance']['source_ref'], 'run_dir': r['provenance'].get('run_ref'),
                    'dataset_record_id': r['record_id'], 'units': 'mJ/m^2'})
    return out


def build_shapes(mat, entry_records, led, n_samples=40, seed=0, campaign_recs=(), rec_by_id=None):
    from engine import Engine, AREA_ON, consistency
    from pymatgen.core import Lattice
    import pymatgen
    tmp = tempfile.mkdtemp(prefix='wh-export-')
    eng = Engine(mat, state_dir=tmp, import_cached=True, session='export')
    extra = campaign_entries(mat, campaign_recs)
    if extra:
        eng.ledger.entries.extend(extra)
        eng.ledger.save()
    # acquire every successful family at every level (the full cache)
    for lv in (1, 2):
        for fam in eng.ledger.families(level=lv):
            e = eng.ledger.best(fam, lv)
            if e is not None:
                eng._acquire(fam, lv, e, 0.0, True)
    chk = eng.checks()
    lat = Lattice(np.array(eng.sym['lattice_rows_abc']))
    n_ops_1e3 = len(lat.get_recp_symmetry_operation(1e-3))
    assert chk['lattice_matrix_same'] and chk['recip_same'], chk
    assert chk['orbits_equal_laue_class'], 'pymatgen orbits differ from the Laue class: %s' % chk
    assert eng.sym['laue_order'] == 4 and chk['pymatgen_recip_ops_order'] == 4 and n_ops_1e3 == 4, \
        'expected Laue 2/m (4 operations) at symprec 1e-5 and 1e-3: %s, %d' % (chk, n_ops_1e3)
    chk = dict(chk, laue_class='2/m', pymatgen_recip_ops_symprec_1em3=n_ops_1e3, wulffshape_symprec=1e-5,
               note='WulffShape expands each family by the reciprocal-lattice operations of the metric (symprec 1e-5); '
                    'they equal the 2/m Laue orbits here (asserted at 1e-5 and 1e-3)')
    led_entries = led['entries']
    key = lambda e: json.dumps(e, sort_keys=True, default=str)
    index_of = {key(e): i for i, e in enumerate(led_entries)}

    def rec_for(entry):
        if entry.get('dataset_record_id'):
            return rec_by_id[entry['dataset_record_id']], None
        i = index_of.get(key(entry))
        if i is None:
            raise RuntimeError('ledger entry not found in the bundle: %s' % entry.get('family'))
        recs = entry_records[i]
        low = [r for r in recs if r['termination'].get('is_lowest')]
        return (low or recs)[0], i

    shapes = []
    mj = json.load(open(os.path.join(ROOT, 'data', mat, 'material.json')))
    for level in ('best', 1):
        res = eng.build_wulff(level, n_samples=n_samples, seed=seed)
        assert res.get('ok'), res.get('error')
        tab, meta = eng.gamma_table(level)
        w = eng._last_w
        fams = res['families_in_build']
        g = np.array([res['gamma_in_build'][f] for f in fams])
        sig = np.array([tab[f]['sigma'] for f in fams])
        # the engine's resampling, repeated to keep the percentiles (same seed, same order -> same samples)
        rng = np.random.default_rng(seed)
        S = []
        for _ in range(n_samples):
            gs = np.maximum(g + sig * rng.standard_normal(len(g)), 0.2 * g)
            try:
                S.append(list(eng._wulff(fams, gs)[1].values()))
            except Exception:
                pass
        S = np.array(S)
        bf_area = eng._wulff(fams, [1.0 / eng.fam[f]['d_eff'] for f in fams])[1]
        rows_by = {r['family']: r for r in res['facets']}
        facets, in_ids = [], []
        for j, f in enumerate(fams):
            t, r = tab[f], rows_by[f]
            assert abs(r['area_mean'] - round(float(S[:, j].mean()), 4)) < 1e-9 and \
                abs(r['area_std'] - round(float(S[:, j].std()), 4)) < 1e-9, 'resampling differs from the engine'
            rec, i = rec_for(t['entry'])
            fl = []
            if t.get('inconsistent_level2') and level == 'best':
                bad = [x['record_id'] for x in [y for k in entry_records for y in entry_records[k]] + list(campaign_recs)
                       if x['material']['material_id'] == WH_MATERIALS[mat]['material_id'] and
                       x['facet']['family'] == f and x['protocol']['level'] == 2 and
                       any(fl_['severity'] == 'error' for fl_ in x['result']['gamma_flags'])]
                fl.append(flag('level1_used_for_inconsistent_level2', 'warning', 'level-2 result(s) %s fail the consistency '
                               'guard (%s); the level-1 value is used' % (', '.join(bad), t['inconsistent_level2']), by='engine'))
            if 'gamma_run' in t:
                fl.append(flag('ratio_scaled_level1', 'info', 'level-1 gamma %.2f x mean relaxed/unrelaxed ratio %.4f'
                               % (t['gamma_run'], meta['relax_ratio']), by='engine'))
            if rec['termination'].get('faces_equivalent') is False:
                fl.append(flag('faces_not_equivalent', 'warning', 'top and bottom faces differ: gamma is their average'))
            fr = eng.fam[f]
            facets.append({'family': f, 'representative': fr['representative'], 'members': fr['members'],
                           'd_eff': round(fr['d_eff'], 5), 'gamma': round(float(t['gamma']), 4),
                           'gamma_record': rec['result']['gamma'], 'sigma': round(float(t['sigma']), 4),
                           'level': t['level'], 'basis': t['basis'], 'termination_label': rec['termination']['label'],
                           'area_fraction': round(float(w.area_fraction_dict.get(tuple(fr['representative']), 0.0)), 6),
                           'on_shape': bool(r['on_shape']), 'area_mean': r['area_mean'], 'area_std': r['area_std'],
                           'area_p05': round(float(np.percentile(S[:, j], 5)), 4),
                           'area_p95': round(float(np.percentile(S[:, j], 95)), 4),
                           'p_on_shape': r['p_on_shape'], 'bfdh_area_fraction': round(float(bf_area[f]), 6),
                           'flags': fl, 'input_record_ids': [rec['record_id']]})
            in_ids.append(rec['record_id'])
        facets.sort(key=lambda x: -x['area_fraction'])
        pts = np.array(w.wulff_convex.points)[w.wulff_convex.vertices]
        c = pts - pts.mean(axis=0)
        _, vec = np.linalg.eigh(np.cov(c.T))
        ext = sorted([float(np.ptp(c @ vec[:, k])) for k in range(3)], reverse=True)
        sflags = []
        if not mj.get('forcefield_validated'):
            sflags.append(flag('force_field_not_validated', 'warning', mj.get('forcefield_note') or ''))
        conv = res.get('convergence') or {}
        if not conv.get('converged', False):
            sflags.append(flag('shape_not_converged', 'info', 'stop rule not met: ' + '; '.join(conv.get('open', [])[:4]), by='engine'))
        lvl = 'best' if level == 'best' else '1'
        shapes.append({
            'schema_version': SCHEMA_VERSION, 'record_type': 'wulff_shape', 'construction': 'wulff',
            'shape_id': rid('ws', mat, lvl, sorted(in_ids)),
            'material_id': WH_MATERIALS[mat]['material_id'], 'material_name': WH_MATERIALS[mat]['name'],
            'structure_id': entry_records[0][0]['material']['structure']['structure_id'],
            'force_field': WH_MATERIALS[mat]['force_field']['name'],
            'force_field_validated': bool(mj.get('forcefield_validated')), 'temperature_K': 0.0, 'level': lvl,
            'level_description': ('relaxed T = 0 where available (level 2), else the level-1 value x the mean '
                                  'relaxed/unrelaxed ratio (wulffhound engine, build_wulff best)' if level == 'best'
                                  else 'unrelaxed level-1 values only (lowest termination of each facet)'),
            'facet_set': fams, 'facets': facets,
            'shape_metrics': {'shape_factor': round(float(w.shape_factor), 5), 'anisotropy': round(float(w.anisotropy), 5),
                              'weighted_surface_energy': round(float(w.weighted_surface_energy), 3),
                              'volume': round(float(w.volume), 3), 'surface_area': round(float(w.surface_area), 3),
                              'effective_radius': round(float(w.effective_radius), 4),
                              'aspect_ratios': {'e2_over_e1': round(ext[1] / ext[0], 4), 'e3_over_e1': round(ext[2] / ext[0], 4),
                                                'method': 'extents of the Wulff vertices along the principal axes of their covariance'},
                              'n_on_shape': sum(1 for x in facets if x['on_shape']),
                              'relax_ratio': round(meta['relax_ratio'], 5) if level == 'best' else None},
            'sensitivity': {'method': 'resample every gamma ~ N(gamma, sigma) (clipped at 0.2 gamma), rebuild, area '
                                      'fraction mean / std / 5-95 %', 'n_samples': int(len(S)), 'seed': seed,
                            'max_area_std': round(float(S.std(axis=0).max()), 4),
                            'sigma_rules': 'level 2: max(gamma_error, 2 %% of gamma); level 1: %.3f of gamma (std of the '
                                           'relaxed/unrelaxed ratio + 2 %%, floor 5 %%)' % meta['sigma1_rel']},
            'lattice_matrix': eng.sym['lattice_rows_abc'], 'checks': chk, 'flags': sflags,
            'convergence': conv, 'input_record_ids': in_ids,
            'provenance': {'built_by': 'tools/export_dataset.py with wulffhound engine.build_wulff(%r) (pymatgen %s)'
                                       % (level, pymatgen.__version__ if hasattr(pymatgen, '__version__') else
                                          __import__('importlib.metadata').metadata.version('pymatgen')),
                           'date': datetime.date.today().isoformat(), 'area_on_threshold': AREA_ON,
                           'ledger': 'data/%s/ledger.json' % mat,
                           'campaign_records_offered': [e['dataset_record_id'] for e in extra]}})
    return shapes


def load_campaigns(d):
    """Records of the campaign driver's stores (dataset/campaigns/*.jsonl), by campaign name."""
    out = {}
    for p in sorted(glob.glob(os.path.join(d, '*.jsonl'))):
        out[os.path.splitext(os.path.basename(p))[0]] = [json.loads(l) for l in open(p) if l.strip()]
    return out


# ---------------------------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--chain-root', default=os.environ.get('WULFFHOUND_CHAIN_ROOT') or os.environ.get('SIM2L_DEV_ROOT')
                    or os.path.join(os.path.dirname(ROOT), 'surface-energy-sim2ls'))
    ap.add_argument('--zoo', default=None, help='cif_zoo.json (default <chain>/notes/validation/cif_zoo.json)')
    ap.add_argument('--ae', default=os.environ.get('WULFFHOUND_AE_RESULTS'),
                    help='attachment-energy results folder (<m>_spec/_attachment/_morphology.json; default '
                         '$WULFFHOUND_AE_RESULTS, else <chain>/devtools/attachment/results)')
    ap.add_argument('--out', default=os.path.join(ROOT, 'dataset'))
    args = ap.parse_args()
    chain = os.path.abspath(os.path.expanduser(args.chain_root))
    zoo_path = args.zoo or os.path.join(chain, 'notes', 'validation', 'cif_zoo.json')
    if not os.path.isfile(zoo_path):
        sys.exit('cif_zoo.json not found at %s: set WULFFHOUND_CHAIN_ROOT or pass --zoo' % zoo_path)
    ae_dir = os.path.abspath(os.path.expanduser(args.ae or os.path.join(chain, 'devtools', 'attachment', 'results')))
    import attachment_ingest as AI
    ae_found = AI.find_results(ae_dir) if os.path.isdir(ae_dir) else {}
    if not ae_found:
        sys.exit('no attachment-energy results (<m>_spec.json, _attachment.json, _morphology.json) in %s: set '
                 'WULFFHOUND_AE_RESULTS or pass --ae' % ae_dir)
    zoo = json.load(open(zoo_path))
    zoo_rev = git_rev('notes/validation/cif_zoo.json', chain)
    zoo_rev = 'chain %s' % zoo_rev if zoo_rev else None
    mats = Materials(zoo)

    import jsonschema
    schema = json.load(open(SCHEMA_PATH))
    validator = jsonschema.Draft202012Validator(schema)
    sub = {t: jsonschema.Draft202012Validator({'$schema': schema['$schema'], '$defs': schema['$defs'],
                                               '$ref': '#/$defs/%s' % t})
           for t in ('surface_energy', 'wulff_shape', 'attachment_energy')}

    se, shapes, entry_recs, counts, skipped = [], [], {}, {}, []
    for mat in ('tnt', 'paracetamol'):
        recs, er, led = wh_records(mat, mats, chain)
        se += recs
        counts['wulffhound-ledger:%s' % mat] = len(recs)
        entry_recs[mat] = (er, led)
    zr, zskip = zoo_records(zoo, mats, chain, zoo_rev, 'chain:notes/validation/cif_zoo.json')
    se += zr
    counts['cif-zoo'] = len(zr)
    skipped += ['cif-zoo: ' + s for s in zskip]
    camp = load_campaigns(os.path.join(args.out, 'campaigns'))
    camp_recs = []
    for name, recs in camp.items():
        camp_recs += recs
        counts['campaign:%s' % name] = len(recs)
    tnt_mat = next(r['material'] for r in se if r['material']['material_id'] == 'tnt-monoclinic'
                   and r['provenance']['source'] == 'wulffhound-ledger')
    sr, sskip = surfenergy_records(mats, tnt_mat, chain)
    se += sr
    counts['surfenergy'] = len(sr)
    skipped += ['surfenergy: ' + s for s in sskip]
    # campaign records: the newest protocol refreshes a ledger record (same material, family, level, T)
    for r in se:
        P = r['protocol']
        for f_ in r['result']['gamma_flags']:
            if f_['code'] != 'protocol_superseded':
                continue
            new = [c for c in camp_recs if c['result']['status'] == 'ok' and c['material']['material_id'] == r['material']['material_id']
                   and c['facet']['family'] == r['facet']['family'] and c['protocol']['level'] == P['level']
                   and c['protocol']['temperature_K'] == P['temperature_K'] and c['protocol'].get('slab_from') == 'relaxed_bulk'
                   and c['termination'].get('is_lowest')]
            if new:
                f_['detail'] += '; refreshed by campaign record %s (gamma %.4f, slab_from relaxed_bulk, %s)' % (
                    new[0]['record_id'], new[0]['result']['gamma'], new[0]['provenance']['source_ref'].split('#')[0])
    se += camp_recs
    rec_by_id = {r['record_id']: r for r in se}
    # shape inputs beyond the ledgers: the campaign runs and surfenergy's own pinned/run T = 0 relaxed values (e.g.
    # TNT {0 0 1} 104.0232, R3), which would otherwise leave that family at its ratio-scaled level-1 value
    surf_l2 = [r for r in se if r['provenance']['source'] in ('surfenergy-regression', 'surfenergy-run')
               and r['result']['status'] == 'ok' and r['protocol']['level'] == 2 and r['protocol']['temperature_K'] == 0]
    for mat in ('tnt', 'paracetamol'):
        er, led = entry_recs[mat]
        shapes += build_shapes(mat, er, led, campaign_recs=camp_recs + surf_l2, rec_by_id=rec_by_id)
    # materials without a bundle: a level-1 shape from their records once they have enough families
    from shapes import best_shape, level1_shape
    have_shape = {s['material_id'] for s in shapes}
    by_mat = {}
    for r in se:
        by_mat.setdefault((r['material']['material_id'], r['force_field']['name']), []).append(r)
    no_shape = {}
    for (mid, ffn), recs in sorted(by_mat.items()):
        if mid in have_shape:
            continue
        s_, why = level1_shape(recs, flag=flag, rid=rid, schema_version=SCHEMA_VERSION)
        if s_:
            shapes.append(s_)
            have_shape.add(mid)
            # and a 'best' shape once the material has relaxed (level-2) records: the engine's rule, from the records
            b_, bwhy = best_shape(recs, flag=flag, rid=rid, schema_version=SCHEMA_VERSION)
            if b_:
                shapes.append(b_)
        else:
            no_shape[mid] = why
    zoo_no_shape = sorted(m for m in no_shape if m not in have_shape)

    # attachment energies (growth morphology): records + the AE shape, checked against the prototype's morphology
    from shapes import ae_shape
    rel = os.path.relpath(ae_dir, chain)
    ae_ref = 'chain:' + rel if not rel.startswith('..') else 'external:' + os.path.basename(ae_dir)
    ae, ae_counts, ae_checks = [], {}, {}
    for key, paths in ae_found.items():
        rev = git_rev(os.path.relpath(paths['attachment'], chain), chain) if not rel.startswith('..') else None
        recs = AI.ae_records(key, AI.load_json(paths['spec']), AI.load_json(paths['attachment']), se, sys.modules[__name__],
                             ae_ref, 'chain %s' % rev if rev else None)
        s_, why = ae_shape(recs, flag=flag, rid=rid, schema_version=SCHEMA_VERSION)
        if not s_:
            sys.exit('attachment energies of %s: no AE shape: %s' % (key, why))
        chk = AI.check_against_morphology(s_, AI.load_json(paths['morphology']), '%s/%s_morphology.json' % (ae_ref, key))
        s_['provenance']['checked_against_morphology'] = chk
        s_['provenance']['attachment_results'] = '%s/%s_attachment.json' % (ae_ref, key)
        ae += recs
        shapes.append(s_)
        ae_counts[recs[0]['material']['material_id']] = len(recs)
        ae_checks[recs[0]['material']['material_id']] = chk['max_abs_diff_area']

    # validate
    ids = set()
    bad = 0
    for r in se + shapes + ae:
        errs = list(validator.iter_errors(r))
        if errs:   # the oneOf error says nothing: report the record type's own errors
            errs = list(sub[r.get('record_type', 'surface_energy')].iter_errors(r)) or errs
        k = r.get('record_id') or r.get('shape_id')
        if k in ids:
            errs.append('duplicate id %s' % k)
        ids.add(k)
        if errs:
            bad += 1
            print('INVALID %s: %s' % (k, '; '.join(('%s: %s' % ('/'.join(map(str, e.path)), e.message[:200])) if hasattr(e, 'path') else str(e) for e in errs[:3])))
    for s in shapes:
        missing = [i for i in s['input_record_ids'] if i not in ids]
        if missing:
            bad += 1
            print('INVALID %s: unknown input records %s' % (s['shape_id'], missing))
    if bad:
        sys.exit('%d invalid records: nothing written' % bad)
    blob = json.dumps(se + shapes + ae)
    if re.search(r'(/home|/Users)/[A-Za-z]', blob):
        sys.exit('an absolute home path leaked into the records: nothing written')

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'surface_energy.jsonl'), 'w') as f:
        for r in se:
            f.write(json.dumps(r, sort_keys=False) + '\n')
    with open(os.path.join(args.out, 'wulff_shape.jsonl'), 'w') as f:
        for r in shapes:
            f.write(json.dumps(r) + '\n')
    with open(os.path.join(args.out, 'attachment_energy.jsonl'), 'w') as f:
        for r in ae:
            f.write(json.dumps(r) + '\n')
    try:
        import pyarrow  # noqa: F401
        import pyarrow.json  # noqa: F401
        import pyarrow.parquet as pq
        import pyarrow as pa
        flat = [{'record_id': r['record_id'], 'material_id': r['material']['material_id'], 'json': json.dumps(r)} for r in se]
        pq.write_table(pa.Table.from_pylist(flat), os.path.join(args.out, 'surface_energy.parquet'))
        parquet = 'written: surface_energy.parquet'
    except ImportError:
        parquet = 'skipped: pyarrow is not installed in this environment (JSON Lines only)'

    n_flag = sum(1 for r in se if any(f['severity'] in ('error', 'warning') for f in r['result']['gamma_flags']))
    n_err = sum(1 for r in se if any(f['severity'] == 'error' for f in r['result']['gamma_flags']))
    materials = {}
    for r in se:
        m = r['material']
        d = materials.setdefault(m['material_id'], {'material_id': m['material_id'], 'name': m['name'], 'inchikey': m['inchikey'],
                                                    'formula': m['formula'], 'structures': [], 'force_fields': [], 'n_records': 0})
        d['n_records'] += 1
        for k, v in (('structures', m['structure']['structure_id']), ('force_fields', r['force_field']['name'])):
            if v not in d[k]:
                d[k].append(v)
    index = {'schema_version': SCHEMA_VERSION, 'schema': 'schema/surface_energy.schema.json',
             'built': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
             'built_by': 'tools/export_dataset.py',
             'files': {'surface_energy': 'surface_energy.jsonl', 'wulff_shape': 'wulff_shape.jsonl',
                       'attachment_energy': 'attachment_energy.jsonl'},
             'parquet': parquet,
             'counts': {'surface_energy': len(se), 'wulff_shape': len(shapes), 'attachment_energy': len(ae),
                        'attachment_energy_by_material': ae_counts, 'by_source': counts,
                        'status_failed': sum(1 for r in se if r['result']['status'] == 'failed'),
                        'with_warning_or_error_flags': n_flag, 'with_error_flags': n_err,
                        'materials': len(materials)},
             'materials': sorted(materials.values(), key=lambda d: d['material_id']),
             'no_shape': {'materials': zoo_no_shape,
                          'why': 'fewer than 3 computed facet families (the CIF zoo computed two per material): that is '
                                 'not a Wulff shape (every other facet is missing), so only the facet energies are recorded',
                          'by_material': {m: no_shape[m] for m in zoo_no_shape}},
             'campaigns': {name: {'records': len(recs), 'store': 'campaigns/%s.jsonl' % name,
                                  'materials': sorted({r['material']['material_id'] for r in recs})}
                           for name, recs in camp.items()},
             'skipped': skipped,
             'attachment_energy': {'store': 'attachment_energy.jsonl', 'materials': sorted(ae_counts),
                                   'source': ae_ref, 'growth_shapes_checked_against_morphology_max_abs_diff': ae_checks,
                                   'what': 'static (unrelaxed) Hartman-Perdok attachment energies of the growth slices '
                                           '(d_eff = d_hkl / n), kcal/mol per molecule; the growth (AE) shape uses |E_att| '
                                           'as the distance: a kinetic, vacuum morphology, not an equilibrium shape'},
             'limits': ['prototype (schema 0.2.0)', 'attachment energies: static slices, vacuum, prototype tool (not a Sim2L yet)', 'zoo values are unrelaxed ReaxFF-lg screening cuts, not validated surface energies',
                        'TNT: validated non-reactive force field', 'paracetamol: ReaxFF-lg, not validated (screening-level)']}
    json.dump(index, open(os.path.join(args.out, 'index.json'), 'w'), indent=1)
    print('surface_energy records: %d (%s); failed runs %d; with warning/error flags %d (errors %d)' % (
        len(se), ', '.join('%s %d' % kv for kv in counts.items()), index['counts']['status_failed'], n_flag, n_err))
    print('wulff_shape records: %d (%s)' % (len(shapes), ', '.join('%s L%s %d families' % (s['material_id'], s['level'],
                                                                                          len(s['facets'])) for s in shapes)))
    print('attachment_energy records: %d (%s); AE shapes vs the morphology JSON: max |dA| %s' % (
        len(ae), ', '.join('%s %d' % kv for kv in ae_counts.items()), ', '.join('%s %.1e' % kv for kv in ae_checks.items())))
    print('all records valid against schema %s' % SCHEMA_VERSION)
    print('parquet: %s' % parquet)
    print('no shape (< 3 families or not bounded): %d materials' % len(zoo_no_shape))
    print('skipped: %d' % len(skipped))
    for s in skipped:
        print('  - ' + s[:160])


if __name__ == '__main__':
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        main()

#!/usr/bin/env python
"""wulffhound chain runner: ONE facet run at level 1 or 2, in the chain env (cif2lammps-dev, Python 3.8).

The engine (wulff-dev) launches this as a subprocess (with a timeout) and reads the JSON it writes.

  level 1  unrelaxed screening of every termination: a supercell child run (hkl mode) and slabcutter child
           runs with compute_unrelaxed_sfe (termination 0, then one per look-alike group), through
           surfenergy_core.ChildRunner, exactly the screening step of surfenergy (no MD, no minimisation).
  level 2  relaxed T = 0 gamma: one surfenergy run (temperature 0, termination_mode lowest) through
           devtools/run_sim2l.py of the chain.
  level 3  MD at T > 0: the same surfenergy run at req['temperature'] with the MD lengths of req['overrides']
           (campaign driver only).

Optional request keys (campaign driver; the engine sends none of them, so its runs are unchanged):
  overrides    surfenergy inputs passed through as given: slab_from, vacuum, angle_dev, s_tol, equilibration_ps,
               production_ps, n_blocks, timestep, fire_max_steps_bulk, fire_max_steps_slab (absent = surfenergy's
               own default, e.g. slab_from relaxed_bulk; the FIRE step caps exist in surfenergy since 2026-10-24)
  temperature  level 3 only
A material without an example_dir (a CIF-zoo or cif2lammps unit cell) gets an inputs.yaml written into the work
dir (material_source unitcell, the data file, script and supplementary file by absolute path).

    SIM2L_DEV_ROOT=<chain> SIM2L_ENV_PREFIX=<chain env> <chain>/devtools/sim2l-env \
        python wulffhound/chain_runner.py request.json result.json

request.json: {"level": 1|2, "hkl": [h,k,l], "search_range": 6, "n_periods": 2, "material": {...}, "work_dir": ...,
"chain_root": <chain>} (chain_root defaults to $SIM2L_DEV_ROOT). The chain (surface-energy-sim2ls) is not part of
wulffhound; this file only runs inside it.
"""
import json
import os
import re
import resource
import subprocess
from proc import run_group  # kills the whole job tree on timeout
import sys
import time
import traceback

REPO = os.environ.get('SIM2L_DEV_ROOT', '')

KEEP2 = ('gamma', 'gamma_error', 'gamma_unrelaxed', 'termination_used', 'n_terminations', 'terminations_screened',
         'results_all', 'facet_hkl', 'd_hkl', 'temperature', 'surface_area', 'surface_area_static', 'lateral_strain',
         'thickness', 'n_molecules_slab', 'n_molecules_bulk', 'n_molecules_cell', 'bulk_pe_per_molecule', 'slab_pe',
         'faces_equivalent', 'molecules_intact', 'mnp_used', 'solution_pick_used', 'notes',
         # kept for the campaign driver (ignored by the engine's ledger)
         'gamma_flags', 'minimization', 'slab_from', 'slab_start', 'stationary', 'gamma_error_block',
         'gamma_equipartition_term', 'timestep_used', 'forcefield_kind', 'molecular_formula', 'inchikey',
         'bulk_temperature_mean', 'bulk_pressure_mean', 'child_runs')
OVERRIDES = ('slab_from', 'vacuum', 'angle_dev', 's_tol', 'equilibration_ps', 'production_ps', 'n_blocks', 'timestep',
             'fire_max_steps_bulk', 'fire_max_steps_slab')


def cpu_children():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    s = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime + s.ru_utime + s.ru_stime


def level1(req, mat):
    import surfenergy_core as sc
    work = req['work_dir']
    child = sc.ChildRunner(work_dir=os.path.join(work, 'children'), dev_root=REPO, log=lambda *a: None)
    h, k, l = req['hkl']
    files = {'datafilename': mat['datafile'], 'inputfilename': mat['script']}
    if mat.get('supplementary'):
        files['supplementaryfilename'] = mat['supplementary']
    values = {'orientation_mode': 'hkl', 'h': h, 'k': k, 'l': l, 'search_range': req['search_range'],
              'use_reaxff': bool(mat.get('use_reaxff')), 'RunEnergyCheck': not mat.get('use_reaxff')}
    ov = req.get('overrides') or {}
    for k, name in (('angle_dev', 'angle_dev'), ('s_tol', 's_tol')):
        if ov.get(k) is not None:
            values[name] = ov[k]
    vacuum = float(ov['vacuum']) if ov.get('vacuum') is not None else 30.0
    notes = []
    s = child.run('supercell', values, files)
    cell = s.file('DataFileOutput')
    if not cell:
        raise sc.SurfError('supercell returned no oriented cell')
    out = {'facet_hkl': [int(x) for x in (s.read('facet_hkl') or req['hkl'])], 'd_hkl': float(s.read('d_hkl')),
           'n_molecules_cell': int(s.read('n_molecules')), 'mnp_used': [int(x) for x in (s.read('mnp_used') or [])],
           'solution_pick_used': str(s.read('solution_pick_used') or '')}
    diff = s.read('PotEng_per_molecule_Difference')
    if sc._finite(diff) and abs(float(diff)) > sc.ENERGY_CHECK_LIMIT:
        notes.append('WARNING: supercell energy check %.3g kcal/mol per molecule' % float(diff))
    child_runs = [{'tool': 'supercell', 'run_dir': s.run_dir, 'inputs': values}]
    runs = {}

    def cut(t):
        v = {'termination_index': int(t), 'n_periods': req['n_periods'], 'vacuum': vacuum, 'compute_unrelaxed_sfe': True}
        f = {'datafilename': cell, 'inputfilename': mat['script']}
        if mat.get('supplementary'):
            f['supplementaryfilename'] = mat['supplementary']
        r = child.run('slabcutter', v, f)
        runs[t] = r
        child_runs.append({'tool': 'slabcutter', 'run_dir': r.run_dir, 'inputs': v})
        for line in sc._note_lines(r.read('notes') or ''):
            line = 'slabcutter t%d: %s' % (t, line)
            if line not in notes:
                notes.append(line)
        return r

    r0 = cut(0)
    table = list(r0.read('terminations') or [])
    rows = {}
    for t in table:
        i = int(t['index'])
        g = t.get('group')
        rows[i] = {'index': i, 'group': int(g) if g is not None else i, 'gap': t.get('gap'), 'cut_z': t.get('cut_z'),
                   'faces_equivalent': t.get('faces_equivalent'), 'sfe_unrelaxed': None, 'sfe_source': ''}
    for g in sorted({r['group'] for r in rows.values()}):
        r = runs.get(g) or cut(g)
        rows[g]['sfe_unrelaxed'] = float(r.read('sfe_unrelaxed'))
        rows[g]['sfe_source'] = 'computed'
        fe = r.read('faces_equivalent')
        if fe is not None:
            rows[g]['faces_equivalent'] = bool(fe)
    for row in rows.values():
        if row['sfe_source'] != 'computed':
            row['sfe_unrelaxed'] = rows[row['group']]['sfe_unrelaxed']
            row['sfe_source'] = 'group %d' % row['group']
    vals = [(r['sfe_unrelaxed'], i) for i, r in rows.items() if r['sfe_unrelaxed'] is not None]
    best = min(vals)
    out.update({'gamma_unrelaxed': best[0], 'termination_used': best[1], 'n_terminations': len(rows),
                'terminations_screened': [rows[i] for i in sorted(rows)],
                'n_molecules_slab': int(runs[best[1]].read('n_molecules_slab')) if best[1] in runs else None,
                'surface_area_static': float(r0.read('surface_area')), 'thickness': float(r0.read('thickness')),
                'notes': '\n'.join(notes)})
    return out, child_runs, work


def unitcell_example(req, mat):
    """An inputs.yaml for a material that has no surfenergy example folder (unit-cell route, absolute paths)."""
    d = os.path.join(req['work_dir'], 'example')
    os.makedirs(d, exist_ok=True)
    f = lambda p: json.dumps('file://' + os.path.abspath(p)) if p else 'null'
    with open(os.path.join(d, 'inputs.yaml'), 'w') as fh:
        fh.write('# written by wulffhound chain_runner for a campaign job\n'
                 'material_source: unitcell\ndatafilename: %s\ninputfilename: %s\nsupplementaryfilename: %s\n'
                 'ciffile: null\nuse_reaxff: %s\n' % (f(mat['datafile']), f(mat['script']), f(mat.get('supplementary')),
                                                       'true' if mat.get('use_reaxff') else 'false'))
    return d


def level2(req, mat):
    T = float(req.get('temperature') or 0.0) if req['level'] == 3 else 0.0
    sets = {'temperature': T, 'h': req['hkl'][0], 'k': req['hkl'][1], 'l': req['hkl'][2],
            'search_range': req['search_range'], 'n_periods': req['n_periods'], 'termination_mode': 'lowest',
            'termination_index': 0}
    for k in OVERRIDES:
        if (req.get('overrides') or {}).get(k) is not None:
            sets[k] = req['overrides'][k]
    example = mat.get('example_dir') or unitcell_example(req, mat)
    cmd = [sys.executable, os.path.join(REPO, 'devtools', 'run_sim2l.py'), os.path.join(REPO, 'surfenergy'),
           example]
    for k, v in sets.items():
        cmd += ['--set', '%s=%s' % (k, v)]
    p = run_group(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=REPO,
                  timeout=req.get('timeout_s', 900))
    log = p.stdout.decode(errors='replace')
    m = re.search(r'run directory: (\S+)', log)
    run_dir = os.path.abspath(os.path.join(REPO, 'surfenergy', 'bin', m.group(1))) if m else None
    if p.returncode != 0 or not m:
        tail = '\n'.join(l for l in log.strip().splitlines()[-25:] if l.strip())[-3000:]
        raise RuntimeError('surfenergy failed (rc %s, run dir %s):\n%s' % (p.returncode, run_dir, tail))
    blob = log[m.end():]
    outputs = json.loads(blob[blob.index('{'):])
    return {k: outputs.get(k) for k in KEEP2 if k in outputs}, [{'tool': 'surfenergy', 'run_dir': run_dir,
                                                                    'inputs': sets}], run_dir


def main():
    global REPO
    req = json.load(open(sys.argv[1]))
    REPO = os.path.abspath(req.get('chain_root') or REPO)
    sys.path.insert(0, os.path.join(REPO, 'surfenergy', 'simtool'))
    mat = req['material']
    os.makedirs(req['work_dir'], exist_ok=True)
    t0, c0 = time.time(), cpu_children()
    res = {'level': req['level'], 'hkl': req['hkl'], 'search_range': req['search_range'],
           'n_periods': req['n_periods']}
    try:
        fn = level1 if req['level'] == 1 else level2
        outputs, child_runs, run_dir = fn(req, mat)
        res.update({'ok': True, 'outputs': outputs, 'child_runs': child_runs, 'run_dir': run_dir})
    except Exception as exc:
        res.update({'ok': False, 'error': ('%s: %s' % (type(exc).__name__, exc))[-3000:],
                    'traceback_tail': traceback.format_exc()[-1500:]})
    res['wall_s'] = round(time.time() - t0, 1)
    res['cpu_s'] = round(cpu_children() - c0, 1)
    with open(sys.argv[2], 'w') as f:
        json.dump(res, f, indent=1, default=str)
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""wulffhound campaign ingest: finished jobs -> schema-valid surface_energy records -> the dataset.

Every done or failed job in the journal becomes records in dataset/campaigns/<campaign>.jsonl (rewritten from the
journal, so ingest is idempotent): level 1 one record per computed termination group (as the exporter does for the
ledgers), levels 2-3 one record for the termination used. Each record carries its run id (surfenergy's run hash, or
the wulffhound job folder for a level-1 screening), the child run ids, revision 'chain <git short sha>' ('+dirty'
when the chain's tools had uncommitted changes), the md5 of the data file / script / force-field file, CPU and wall
time, the reproduce line, surfenergy's gamma_flags and the wulffhound consistency guard. A failed job is a record
with status failed and the tool's error text. Then tools/export_dataset.py rebuilds the dataset (it reads the
campaign stores) and the Wulff shapes of the affected materials are rebuilt with it.
"""
import datetime
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from config import ROOT, portable  # noqa: E402
import symmetry  # noqa: E402
from campaign import (CAMPAIGN_STORE, FIRE_CAP_KEYS, LEVEL_NAME, campaign_dir, chain_revision, error_line, load_shapes,  # noqa: E402
                      make_context, md5, read_journal, resolve_material, _export_module)

SEVERITY = {'gamma_not_positive': 'error', 'relaxed_above_unrelaxed': 'error', 'minimisation_not_converged': 'warning',
            'molecules_not_intact': 'error'}
SCHEMA_CODES = None


def _schema():
    return json.load(open(os.path.join(ROOT, 'schema', 'surface_energy.schema.json')))


def _codes():
    global SCHEMA_CODES
    if SCHEMA_CODES is None:
        SCHEMA_CODES = set(_schema()['$defs']['flag']['properties']['code']['enum'])
    return SCHEMA_CODES


def sim2l_flags(strings, X):
    """surfenergy's gamma_flags ('code: why') -> schema flags (by 'surfenergy')."""
    out = []
    for s in strings or []:
        code, _, why = str(s).partition(':')
        code = code.strip()
        c = code if code in _codes() else 'sim2l_flag'
        out.append(X.flag(c, SEVERITY.get(code, 'warning'), (why.strip() if c == code else s)[:600], by='surfenergy'))
    return out


def _run_ids(d):
    out = []
    if isinstance(d, dict):
        if d.get('run_id'):
            out.append(str(d['run_id']))
        for v in d.values():
            if isinstance(v, (dict, list)):
                out += _run_ids(v)
    elif isinstance(d, list):
        for v in d:
            out += _run_ids(v)
    return out


def material_block(mat, X, mats):
    if mat.bundle:
        return mats.wh_material(mat.bundle, mat.symmetry())
    if mat.zoo:
        return mats.zoo_material(mat.zoo)
    sym = mat.symmetry()
    lp = sym['lattice_parameters']
    f = sym['formula_molecule']
    c2 = mat.meta.get('cif2lammps') or {}
    return {'material_id': mat.material_id, 'name': mat.name, 'inchikey': mat.meta.get('inchikey') or None,
            'smiles': c2.get('smiles'), 'formula': X.hill(f), 'molecular_mass': X.molar_mass(f),
            'polymorph': mat.meta.get('polymorph') or 'not given', 'category': None, 'pubchem_cid': None,
            'structure': {'structure_id': mat.structure_id(), 'source': 'campaign input (%s)' % mat.kind,
                          'database': None, 'database_id': None,
                          'licence': mat.meta.get('licence') or 'not given (structure not distributed)', 'citation': None,
                          'doi': None, 'url': None, 'sha256': None, 'md5': mat.meta.get('cif_md5'),
                          'measurement_T_K': None, 'distributed': False},
            'space_group': {'symbol_data_setting': sym['spacegroup']['international_data_setting'],
                            'number': sym['spacegroup']['number'],
                            'laue_class': symmetry.laue_class(sym['spacegroup'].get('pointgroup'))},
            'cell': X.cell_block([lp[k] for k in ('a', 'b', 'c', 'alpha', 'beta', 'gamma')], sym['n_molecules'], f,
                                 matrix=sym['lattice_rows_abc'], note='the data-file cell of the campaign material')}


def job_records(job, ev, res, mat, spec, X, mats, revision, dirty):
    """Records of one finished job (ok or failed)."""
    from engine import consistency
    name = spec['campaign']
    mb = material_block(mat, X, mats)
    ff = dict(mat.route['ff_block'])
    fams = mat.families()
    fr = fams.get(job['family']) or {}
    o = res.get('outputs') or {}
    lv = job['level']
    pr = spec['protocol']
    facet = {'hkl': [int(x) for x in (o.get('facet_hkl') or job['hkl'])], 'family': job['family'],
             'family_representative': fr.get('representative'), 'family_members': fr.get('members'),
             'multiplicity': fr.get('multiplicity'),
             'd_hkl': round(float(o.get('d_hkl') or fr.get('d_hkl') or 0), 5) or None,
             'd_eff': round(fr['d_eff'], 5) if fr.get('d_eff') else None, 'setting': X.setting_of(mb)}
    slab_from = o.get('slab_from') or ((pr.get('slab_from') or 'relaxed_bulk') if lv >= 2 else None)
    proto = {'level': lv, 'level_name': LEVEL_NAME[lv], 'temperature_K': float(job['temperature_K']),
             'n_periods': int(pr['n_periods']), 'vacuum_A': float(pr.get('vacuum') or 30.0),
             'search_range': int(job['search_range']), 'slab_from': slab_from if lv >= 2 else None,
             'minimiser': None, 'timestep_fs': None, 'equilibration_ps': None, 'production_ps': None, 'n_blocks': None,
             'mnp': o.get('mnp_used'), 'n_molecules_cell': o.get('n_molecules_cell'),
             'n_molecules_slab': o.get('n_molecules_slab'), 'n_molecules_bulk': o.get('n_molecules_bulk'),
             'surface_area_A2': round(float(o.get('surface_area') or o.get('surface_area_static') or 0), 4) or None,
             'thickness_A': round(float(o['thickness']), 4) if o.get('thickness') else None,
             'energy_check': (not mat.route['use_reaxff']) if lv == 1 else None}
    if lv == 2:
        caps = ['%s %d' % (k, int(pr[k])) for k in FIRE_CAP_KEYS if pr.get(k) is not None]
        proto['minimiser'] = ('FIRE in fixed cells (unit cell as the bulk reference, then the slab), surfenergy' +
                              (' (%s)' % ', '.join(caps) if caps else '')
                              if mat.route['use_reaxff'] else
                              'CG with box/relax (bulk), slab %s then CG (surfenergy)' % (
                                  're-cut from the relaxed bulk' if slab_from == 'relaxed_bulk' else 'static cut mapped'))
    if lv == 3:
        md = spec['md']
        proto.update({'timestep_fs': o.get('timestep_used'), 'equilibration_ps': float(md['equilibration_ps']),
                      'production_ps': float(md['production_ps']), 'n_blocks': int(md['n_blocks'])})
    in_md5 = {}
    for k, p in (('datafile', mat.route['datafile']), ('script', mat.route['script']),
                 ('forcefield', mat.route.get('supplementary') if mat.route['use_reaxff'] else None)):
        if p and os.path.isfile(p):
            in_md5[k] = md5(p)
    if mat.meta.get('cif_md5'):
        in_md5['cif'] = mat.meta['cif_md5']
    run_dir = res.get('run_dir')
    if not run_dir and not res.get('ok'):
        m = re.search(r'run dir (\S+?)\)', res.get('error') or '')     # chain_runner names the failed run's folder
        run_dir = m.group(1) if m and m.group(1) != 'None' else None
    run_id = os.path.basename(str(run_dir).rstrip('/')) if run_dir else job['job_id']
    # the oriented-cell settings of this job: the protocol's, with the planner's cell choice (cell_fallbacks) over it
    cell = dict({k: pr.get(k) for k in ('slab_from', 'vacuum', 'angle_dev', 's_tol')},
                **{k: v for k, v in (job.get('cell_overrides') or {}).items() if v is not None})
    if cell.get('angle_dev') is None and ev.get('angle_dev') is not None:
        cell['angle_dev'] = ev['angle_dev']
    if lv == 1:
        child = [os.path.basename(str(c.get('run_dir')).rstrip('/')) for c in res.get('child_runs') or [] if c.get('run_dir')]
        sim2l = 'supercell+slabcutter'
        chain_call = ('chain_runner level 1: supercell hkl (%s) search_range %s%s, then slabcutter compute_unrelaxed_sfe '
                      'per termination group, %s periods, vacuum %s' % (
                          ' '.join(map(str, job['hkl'])), job['search_range'],
                          ' angle_dev %s' % cell['angle_dev'] if cell.get('angle_dev') is not None else '',
                          pr['n_periods'], proto['vacuum_A']))
    else:
        child = _run_ids(o.get('child_runs'))
        sim2l = 'surfenergy'
        ex = mat.inputs.get('example_dir')
        sets = ['temperature=%g' % (0 if lv == 2 else job['temperature_K'])] + \
            ['%s=%s' % (k, v) for k, v in zip('hkl', job['hkl'])] + \
            ['search_range=%s' % job['search_range'], 'n_periods=%s' % pr['n_periods'], 'termination_mode=lowest'] + \
            ['%s=%s' % (k, cell[k]) for k in ('slab_from', 'vacuum', 'angle_dev', 's_tol') if cell.get(k) is not None] + \
            ['%s=%d' % (k, int(pr[k])) for k in FIRE_CAP_KEYS if lv == 2 and pr.get(k) is not None]
        chain_call = 'chain: devtools/run_sim2l.py surfenergy %s --set %s' % (
            portable(ex) if ex else '<unit-cell inputs.yaml written by chain_runner>', ' --set '.join(sets))
    spec_ref = spec.get('_source') or 'campaigns/%s.yaml' % name
    notes = ['campaign %s (spec %s), policy %s, backend %s; job %s' % (name, spec_ref, spec['policy'],
                                                                       res.get('backend', 'local'), job['job_id'])]
    if cell.get('angle_dev') is not None:
        notes.append('oriented cell: hkl (%s), search_range %s, GCCM angle_dev %s deg (tool default 1; the planner\'s cell '
                     'choice, protocol.cell_fallbacks)' % (' '.join(map(str, job['hkl'])), job['search_range'], cell['angle_dev']))
    if dirty:
        notes.append('the chain had uncommitted changes in %s at run time (revision marked +dirty)' % ', '.join(dirty[:4]))
    for n in (o.get('notes') or '').splitlines():
        n = n.strip()
        if n and 'commands of the input script not used' not in n:
            notes.append(X.sanitize(n)[:400])
    prov = {'source': 'campaign', 'sim2l': {'name': sim2l, 'revision': revision}, 'run_id': run_id,
            'run_ref': portable(run_dir) if run_dir else None, 'resultsdb_id': None, 'child_run_ids': child,
            'input_md5': in_md5, 'input_sha256': {}, 'date': ev.get('time'),
            'venue': 'local chain (development machine), serial LAMMPS' if res.get('backend', 'local') == 'local' else res.get('backend'),
            'cpu_s': res.get('cpu_s'), 'wall_s': res.get('wall_s'),
            'reproduce': './wh campaign run %s --only %s --retry-failed (needs the chain); %s' % (
                spec_ref, job['job_id'], chain_call),
            'notes': notes[:10]}
    base_flags = []
    if not ff['validated_for_material']:
        base_flags.append(X.flag('force_field_not_validated', 'warning', ff.get('note') or 'force field not validated for '
                                 'this material'))
    store = 'dataset/campaigns/%s.jsonl' % name
    recs = []

    def make(term, result, tag):
        ref = '%s#%s%s' % (store, job['job_id'], tag)
        return {'schema_version': X.SCHEMA_VERSION, 'record_type': 'surface_energy',
                'record_id': X.rid('se', 'campaign', name, job['job_id'], tag), 'material': mb, 'force_field': ff,
                'facet': facet, 'termination': term, 'protocol': proto, 'result': result,
                'provenance': dict(prov, source_ref=ref)}

    if not res.get('ok'):
        msg = X.sanitize(error_line(res.get('error'))) or 'run failed'
        full = X.sanitize(msg + '\n--- end of the tool output ---\n' + (res.get('error') or '')[-1000:])
        recs.append(make({'index': None, 'label': 'n/a (run failed)', 'group': None, 'equivalent_indices': [],
                          'gap': None, 'faces_equivalent': None, 'n_terminations': None, 'is_lowest': None},
                         {'status': 'failed', 'gamma': None, 'gamma_error': None, 'error_method': None, 'units': 'mJ/m^2',
                          'stationary': None, 'gamma_unrelaxed': None, 'gamma_raw': None, 'gamma_basis': None,
                          'gamma_flags': base_flags[:1] + [X.flag('run_failed', 'error', msg, by='campaign driver')],
                          'molecules_intact': None, 'minimisation': None, 'error_message': full}, ''))
        return recs
    if lv == 1:
        terms = o.get('terminations_screened') or []
        used = o.get('termination_used')
        used_group = next((t.get('group') for t in terms if t['index'] == used), None)
        for t in [t for t in terms if t.get('sfe_source') == 'computed' and t.get('sfe_unrelaxed') is not None]:
            eq = [x['index'] for x in terms if x.get('group') == t.get('group')]
            fl = list(base_flags) + [X.flag('unrelaxed_screening', 'info', 'unrelaxed cut of the bulk (no relaxation): an '
                                            'upper bound of the T = 0 relaxed gamma')]
            if t.get('faces_equivalent') is False:
                fl.append(X.flag('faces_not_equivalent', 'warning', 'the top and bottom faces of this slab differ: gamma '
                                 'is the average of the two faces'))
            recs.append(make({'index': t['index'], 'label': 't%s%s' % (t['index'], ' (gap %.2f A)' % t['gap'] if t.get('gap') else ''),
                              'group': t.get('group'), 'equivalent_indices': eq or [t['index']], 'gap': t.get('gap'),
                              'faces_equivalent': t.get('faces_equivalent'), 'n_terminations': o.get('n_terminations'),
                              'is_lowest': t['index'] == used or (used_group is not None and t.get('group') == used_group)},
                             {'status': 'ok', 'gamma': round(float(t['sfe_unrelaxed']), 4), 'gamma_error': 0.0,
                              'error_method': 'none: deterministic static energies', 'units': 'mJ/m^2', 'stationary': None,
                              'gamma_unrelaxed': None, 'gamma_raw': None,
                              'gamma_basis': 'slabcutter unrelaxed SFE, run 0 of bulk and slab%s' % (
                                  ', ReaxFF' if mat.route['use_reaxff'] else ''),
                              'gamma_flags': fl, 'molecules_intact': None, 'minimisation': None, 'error_message': None},
                             '/t%s' % t['index']))
        return recs
    # levels 2-3: the termination used
    fl = list(base_flags) + sim2l_flags(o.get('gamma_flags'), X)
    entry = {'level': 2, 'ok': True, 'gamma': o.get('gamma'), 'gamma_unrelaxed': o.get('gamma_unrelaxed'),
             'molecules_intact': o.get('molecules_intact')}
    if lv == 2:
        good, why = consistency(entry)
        if not good:
            have = {x['code'] for x in fl}
            for code, txt in (('relaxed_above_unrelaxed', 'relaxed gamma'), ('gamma_not_positive', 'not positive'),
                              ('molecules_not_intact', 'not intact')):
                if txt in why and code not in have:
                    fl.append(X.flag(code, 'error', why, by='wulffhound-guard'))
    if lv == 3 and o.get('stationary') is False:
        fl.append(X.flag('not_stationary', 'warning', 'surfenergy: the production run is not stationary (trend or < 10 '
                         'independent samples): gamma and its error bar are not reliable', by='surfenergy'))
    if o.get('molecules_intact') is False and not any(x['code'] == 'molecules_not_intact' for x in fl):
        fl.append(X.flag('molecules_not_intact', 'error', 'surfenergy: molecules not intact after the run', by='surfenergy'))
    if o.get('faces_equivalent') is False:
        fl.append(X.flag('faces_not_equivalent', 'warning', 'the top and bottom faces of this slab differ: gamma is the '
                         'average of the two faces'))
    mins = {}
    for k, v in (o.get('minimization') or {}).items():
        if isinstance(v, dict):
            mins[k] = {x: v.get(x) for x in ('method', 'converged', 'stop_reason', 'steps', 'step_cap', 'pe_change_last',
                                             'fnorm_final', 'gamma_change_last') if v.get(x) is not None}
    st = o.get('slab_start') or {}
    terms = o.get('terminations_screened') or []
    used = o.get('termination_used')
    t = next((x for x in terms if x.get('index') == used), {})
    basis = 'surfenergy %s gamma, slab_from %s%s' % ('relaxed T = 0' if lv == 2 else 'MD at %g K' % job['temperature_K'],
                                                    slab_from, ' (%s)' % st['method'] if st.get('method') else '')
    if st.get('sfe_unrelaxed_recut') is not None:
        basis += '; unrelaxed SFE of the re-cut %.2f' % float(st['sfe_unrelaxed_recut'])
    if lv == 3:
        err_m = 'larger of the block and autocorrelation standard errors, 1 sigma (surfenergy)'
    else:
        err_m = 'none: T = 0 minimisation (deterministic)'
    recs.append(make({'index': used, 'label': 't%s (lowest unrelaxed)' % used, 'group': t.get('group'),
                      'equivalent_indices': [x['index'] for x in terms if t and x.get('group') == t.get('group')],
                      'gap': t.get('gap'), 'faces_equivalent': o.get('faces_equivalent'),
                      'n_terminations': o.get('n_terminations'), 'is_lowest': True},
                     {'status': 'ok', 'gamma': round(float(o['gamma']), 4), 'gamma_error': o.get('gamma_error'),
                      'error_method': err_m, 'units': 'mJ/m^2', 'stationary': o.get('stationary') if lv == 3 else None,
                      'gamma_unrelaxed': round(float(o['gamma_unrelaxed']), 4) if o.get('gamma_unrelaxed') is not None else None,
                      'gamma_raw': None, 'gamma_basis': basis, 'gamma_flags': fl,
                      'molecules_intact': o.get('molecules_intact'), 'minimisation': mins or None,
                      'error_message': None}, ''))
    return recs


def build_records(spec, journal=None, plan_jobs=None, ctx=None):
    """All records of a campaign from its journal (done and failed jobs whose result.json is there)."""
    from campaign import job_key, job_id
    X = _export_module()
    import json as _j
    journal = read_journal(spec['campaign']) if journal is None else journal
    ctx = ctx or make_context(spec)
    mats_x = X.Materials(ctx['zoo'])
    mats = [resolve_material(e, spec, ctx) for e in spec['materials']]
    revision, dirty = chain_revision()
    jobs = {}
    for j in plan_jobs or []:
        jobs[j['job_id']] = j
    out, problems = [], []
    for jid, ev in sorted(journal.items(), key=lambda kv: kv[1]['time']):
        if ev.get('event') not in ('done', 'failed'):
            continue
        rp = os.path.join(campaign_dir(spec['campaign']), 'jobs', jid, 'result.json')
        if not os.path.isfile(rp):
            problems.append('%s: no result.json' % jid)
            continue
        res = _j.load(open(rp))
        mat = next((m for m in mats if m.material_id == ev.get('material_id')), None)
        if mat is None:
            problems.append('%s: material %s is not in the spec any more' % (jid, ev.get('material_id')))
            continue
        j = jobs.get(jid) or {'job_id': jid, 'material_id': ev['material_id'], 'family': ev['family'],
                              'level': ev['level'], 'temperature_K': ev.get('temperature_K') or 0.0,
                              'hkl': ev.get('hkl') or res.get('hkl'), 'search_range': res.get('search_range'),
                              'cell_overrides': {'angle_dev': ev.get('angle_dev')} if ev.get('angle_dev') is not None else {}}
        j.setdefault('search_range', res.get('search_range'))
        out += job_records(j, ev, res, mat, spec, X, mats_x, revision, dirty)
    return out, problems


def validate(records):
    import jsonschema
    s = _schema()
    v = jsonschema.Draft202012Validator({'$schema': s['$schema'], '$defs': s['$defs'], '$ref': '#/$defs/surface_energy'})
    bad = []
    for r in records:
        errs = list(v.iter_errors(r))
        if errs:
            bad.append('%s: %s' % (r.get('record_id'), '; '.join('%s: %s' % ('/'.join(map(str, e.path)), e.message[:160])
                                                               for e in errs[:3])))
    return bad


def write_store(name, records, store_dir=None):
    d = store_dir or CAMPAIGN_STORE
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name + '.jsonl')
    blob = ''.join(json.dumps(r) + '\n' for r in records)
    if re.search(r'(/home|/Users)/[A-Za-z]', blob):
        raise RuntimeError('an absolute home path leaked into the campaign records: nothing written')
    with open(p + '.tmp', 'w') as f:
        f.write(blob)
    os.replace(p + '.tmp', p)
    return p


def ingest(spec, export=True, store_dir=None):
    """Journal -> records -> dataset/campaigns/<name>.jsonl (validated) -> tools/export_dataset.py -> shape diff."""
    recs, problems = build_records(spec)
    bad = validate(recs)
    if bad:
        raise RuntimeError('invalid campaign records, nothing written:\n' + '\n'.join(bad[:10]))
    p = write_store(spec['campaign'], recs, store_dir)
    before = {(s['material_id'], s['level']): s for s in load_shapes()}
    out = {'campaign': spec['campaign'], 'store': portable(p), 'n_records': len(recs), 'problems': problems,
           'records': [{'record_id': r['record_id'], 'material_id': r['material']['material_id'],
                        'family': r['facet']['family'], 'level': r['protocol']['level'],
                        'termination': r['termination']['label'], 'status': r['result']['status'],
                        'gamma': r['result']['gamma'], 'flags': [f['code'] for f in r['result']['gamma_flags']
                                                                 if f['severity'] != 'info']} for r in recs]}
    if export:
        e = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'tools', 'export_dataset.py')], cwd=ROOT,
                           capture_output=True, text=True, timeout=1200)
        out['export'] = {'rc': e.returncode, 'output': (e.stdout + e.stderr)[-3000:]}
        after = {(s['material_id'], s['level']): s for s in load_shapes()}
        touched = {r['material']['material_id'] for r in recs}
        ch = []
        for k, s in sorted(after.items()):
            b = before.get(k)
            if b is None or b['shape_id'] != s['shape_id']:
                top = sorted(s['facets'], key=lambda f: -f['area_fraction'])[:5]
                ch.append({'material_id': k[0], 'level': k[1], 'shape_id': s['shape_id'], 'new': b is None,
                           'previous': b['shape_id'] if b else None, 'n_families': len(s['facets']),
                           'n_on_shape': s['shape_metrics']['n_on_shape'], 'affected_by_campaign': k[0] in touched,
                           'largest': ['%s %.3f (%s %.1f)' % (f['family'], f['area_fraction'], 'L%d' % f['level'],
                                                               f['gamma']) for f in top]})
        out['shapes_changed'] = ch
    out['text'] = format_ingest(out)
    return out


def format_ingest(r):
    L = ['ingest %s: %d records -> %s%s' % (r['campaign'], r['n_records'], r['store'],
                                           ('; problems: %s' % r['problems']) if r['problems'] else '')]
    for x in r['records']:
        L.append('  %s %-18s %-10s L%d %-26s %-6s %s %s' % (x['record_id'], x['material_id'][:18], x['family'], x['level'],
                                                           x['termination'][:26], x['status'],
                                                           '%.2f' % x['gamma'] if x['gamma'] is not None else '-',
                                                           ' '.join(x['flags'])))
    if 'export' in r:
        L.append('export (tools/export_dataset.py): rc %d' % r['export']['rc'])
        L += ['  ' + l for l in r['export']['output'].strip().splitlines()[:8]]
        for s in r.get('shapes_changed') or []:
            L.append('  shape %s %s level %s: %s, %d families (%d on the shape); largest %s' % (
                'NEW' if s['new'] else 'changed', s['material_id'], s['level'], s['shape_id'], s['n_families'],
                s['n_on_shape'], ', '.join(s['largest'])))
    return '\n'.join(L)

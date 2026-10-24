"""wulffhound campaign executor: runs a plan's jobs through a backend, with a resumable journal.

Backends (spec executor.backend):
  local    the simulation chain through wulffhound/chain_runner.py in the chain's Sim2L env (devtools/sim2l-env),
           exactly as the engine's live runs: level 1 = supercell + slabcutter screening, levels 2-3 = one surfenergy
           run (temperature 0, or T with the MD lengths). Works now.
  nanohub  simtool.Run against installed revisions of the Sim2Ls in a nanoHUB session (venue / cluster options).
  aura     the AURA MCP's run_sim2l (an agent-side call; the driver writes the payload).
           Both are documented stubs: the Sim2Ls are not installed on nanoHUB yet, so run() raises
           BackendNotAvailable saying what is needed, and describe(job) gives the exact call that would be made.

Journal: state/campaigns/<name>/journal.jsonl, one JSON event per line (started, done, failed, skipped, recovered).
A job with a done/failed event is never run again by `run` (failures are not retried blindly: --retry-failed or a
protocol change makes it a new decision); a job left 'started' by a crash is recovered from its result.json when
there is one, else run again (it never finished). Job folders: state/campaigns/<name>/jobs/<job_id>/.
Workers: default 1, at most 2 (MAX_WORKERS). The budget is checked before each start against the CPU actually
spent plus the estimates of the jobs in flight. Estimates are recalibrated as the campaign runs: once jobs of a
level have finished, every later estimate of that level is multiplied by the median measured/estimated CPU ratio
(only upwards, never below the plan's estimate), and the per-run cap and the budget are checked on that.
"""
import concurrent.futures as cf
import datetime
import json
import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
from config import STATE, chain_env, chain_present, chain_root, portable  # noqa: E402
from campaign import MAX_WORKERS, Material, SpecError, campaign_dir, error_line, md5, read_journal  # noqa: E402


class BackendNotAvailable(RuntimeError):
    pass


class PolicyRefused(RuntimeError):
    pass


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def _err_tail(err, n=1500):
    return (err or '')[-n:]


# ============================================================================================ requests
def job_request(job, mat, spec, work_dir, timeout_s):
    """The chain_runner request of a job (level 1 / 2 / 3). Only the overrides the spec gives are sent: an absent
    one is the tool's own default (slab_from relaxed_bulk, vacuum 30, ...)."""
    pr, md = spec['protocol'], spec['md']
    ov = {k: pr.get(k) for k in ('slab_from', 'vacuum', 'angle_dev', 's_tol') if pr.get(k) is not None}
    ov.update({k: v for k, v in (job.get('cell_overrides') or {}).items() if v is not None})   # the planner's cell choice
    if job['level'] == 3:
        ov.update({k: md[k] for k in ('equilibration_ps', 'production_ps', 'n_blocks', 'timestep') if md.get(k)})
    r = mat.route
    own = mat.kind in ('bundle', 'example') and r['name'] == mat.default_route and r['datafile'] == mat.inputs['datafile']
    return {'level': job['level'], 'hkl': list(job['hkl']), 'search_range': int(job['search_range']),
            'n_periods': int(pr['n_periods']), 'temperature': float(job['temperature_K']), 'overrides': ov,
            'work_dir': work_dir, 'timeout_s': int(timeout_s), 'chain_root': chain_root(),
            'material': {'datafile': r['datafile'], 'script': r['script'], 'supplementary': r.get('supplementary'),
                         'example_dir': mat.inputs.get('example_dir') if own else None, 'use_reaxff': r['use_reaxff']}}


# ============================================================================================ backends
class Backend:
    name = 'base'

    def __init__(self, cfg=None):
        self.cfg = cfg or {}

    def available(self):
        return False, 'abstract backend'

    def run(self, job, mat, spec, work_dir, timeout_s):
        raise NotImplementedError


class LocalBackend(Backend):
    """The chain on this machine (the engine's live-run mechanism, one chain_runner subprocess per job)."""
    name = 'local'

    def available(self):
        if not chain_present():
            return False, config.NO_CHAIN_MSG
        return True, 'chain at %s' % portable(chain_root())

    def run(self, job, mat, spec, work_dir, timeout_s):
        os.makedirs(work_dir, exist_ok=True)
        req = job_request(job, mat, spec, work_dir, timeout_s)
        rq, rs = os.path.join(work_dir, 'request.json'), os.path.join(work_dir, 'result.json')
        if os.path.isfile(rs):
            os.remove(rs)
        json.dump(req, open(rq, 'w'), indent=1)
        croot = chain_root()
        env = dict(os.environ, SIM2L_DEV_ROOT=croot, SIM2L_ENV_PREFIX=chain_env())
        env.pop('PYTHONPATH', None)
        cmd = [os.path.join(croot, 'devtools', 'sim2l-env'), 'python', os.path.join(HERE, 'chain_runner.py'), rq, rs]
        t0 = time.time()
        try:
            p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=croot,
                               timeout=timeout_s + 60, start_new_session=True)
            log = p.stdout.decode(errors='replace')
        except subprocess.TimeoutExpired as exc:
            log = (exc.stdout or b'').decode(errors='replace') + '\nTIMEOUT after %d s (the job exceeded its time ' \
                'limit: estimate x timeout_factor)' % (timeout_s + 60)
        open(os.path.join(work_dir, 'runner.log'), 'w').write(log)
        if os.path.isfile(rs):
            res = json.load(open(rs))
        else:
            res = {'ok': False, 'error': 'chain runner wrote no result: ' + log[-1500:],
                   'wall_s': round(time.time() - t0, 1), 'cpu_s': None}
        res.update({'level': job['level'], 'hkl': list(job['hkl']), 'search_range': job['search_range'],
                    'n_periods': spec['protocol']['n_periods'], 'backend': self.name})
        if job['level'] == 1:
            res['run_dir'] = work_dir
        json.dump(res, open(rs, 'w'), indent=1, default=str)
        return res


def _hub_inputs(job, mat, spec):
    """The Sim2L inputs of a job on nanoHUB (files by name: they must be uploaded to the session first)."""
    r = mat.route
    files = {'datafilename': os.path.basename(r['datafile'] or ''), 'inputfilename': os.path.basename(r['script'] or '')}
    if r.get('supplementary'):
        files['supplementaryfilename'] = os.path.basename(r['supplementary'])
    pr = spec['protocol']
    h, k, l = job['hkl']
    if job['level'] == 1:
        sc_cell = {x: y for x, y in (job.get('cell_overrides') or {}).items() if y is not None}
        return [{'tool': 'supercell', 'inputs': dict(files, orientation_mode='hkl', h=h, k=k, l=l,
                                                      search_range=job['search_range'], use_reaxff=r['use_reaxff'],
                                                      **sc_cell)},
                {'tool': 'slabcutter', 'inputs': dict(files, datafilename='<supercell DataFileOutput>', termination_index=0,
                                                       n_periods=pr['n_periods'], vacuum=pr.get('vacuum') or 30.0,
                                                       compute_unrelaxed_sfe=True),
                 'then': 'one slabcutter run per further termination group (termination_index = group representative)'}]
    v = dict(files, material_source='unitcell', use_reaxff=r['use_reaxff'], h=h, k=k, l=l,
             search_range=job['search_range'], n_periods=pr['n_periods'], termination_mode='lowest',
             temperature=0.0 if job['level'] == 2 else job['temperature_K'])
    for x in ('slab_from', 'vacuum', 'angle_dev', 's_tol'):
        if pr.get(x) is not None:
            v[x] = pr[x]
    v.update({k: x for k, x in (job.get('cell_overrides') or {}).items() if x is not None})
    if job['level'] == 3:
        v.update({x: spec['md'][x] for x in ('equilibration_ps', 'production_ps', 'n_blocks', 'timestep')})
    return [{'tool': 'surfenergy', 'inputs': v}]


class NanohubBackend(Backend):
    """simtool.Run in a nanoHUB session against INSTALLED revisions (executor.nanohub.revision: {tool: N}), venue
    and cluster options (executor.nanohub.venue, walltime_minutes, ncpus). Not available until the Sim2Ls are
    installed on nanoHUB."""
    name = 'nanohub'
    NEEDS = ('the four Sim2Ls (cif2lammps, supercell, slabcutter, surfenergy) installed and published on nanoHUB '
             '(they are not installed yet)',
             'a nanoHUB Jupyter session with the simtool package (searchForSimTool / Run)',
             'executor.nanohub.revision: the installed revision number of each tool',
             'optional: executor.nanohub.venue (a cluster queue) and walltime_minutes / ncpus for submit')

    def describe(self, job, mat, spec):
        rev = self.cfg.get('revision') or {}
        calls = []
        for c in _hub_inputs(job, mat, spec):
            calls.append(dict(c, revision=rev.get(c['tool']),
                              call="simtool.Run(searchForSimTool('%s', revision=%s), inputs, venue=%r, cache=True)"
                                   % (c['tool'], rev.get(c['tool'], 'N'), self.cfg.get('venue'))))
        return {'backend': self.name, 'job_id': job['job_id'], 'calls': calls,
                'upload': [portable(p) for p in (mat.route['datafile'], mat.route['script'], mat.route.get('supplementary')) if p],
                'venue': self.cfg.get('venue'), 'walltime_minutes': self.cfg.get('walltime_minutes'),
                'ncpus': self.cfg.get('ncpus', 1)}

    def available(self):
        missing = []
        try:
            import simtool  # noqa: F401
        except ImportError:
            missing.append(self.NEEDS[1])
        missing.insert(0, self.NEEDS[0])
        if not self.cfg.get('revision'):
            missing.append(self.NEEDS[2])
        return False, 'needs ' + '; '.join(missing)

    def run(self, job, mat, spec, work_dir, timeout_s):
        ok, why = self.available()
        raise BackendNotAvailable('nanohub backend not available here: %s. The job would run: %s' % (
            why, json.dumps(self.describe(job, mat, spec)['calls'])[:800]))


class AuraBackend(Backend):
    """The AURA MCP (run_sim2l, then get_job_status / get_job_result): an agent-side call. The driver writes the
    payload; the agent (or an MCP client) makes the call. AURA's own advice applies: query ResultsDB first
    (find_novel_datapoints / get_data_from_nanohub), which the planner's dataset look-up mirrors."""
    name = 'aura'
    NEEDS = ('the Sim2Ls installed and published on nanoHUB (they are not installed yet), so that AURA get_tool_metadata reports '
             'tool_kind sim2l / execution_tool run_sim2l for them',
             'an MCP client with the AURA server (an agent session); a Python process cannot call it directly',
             'a way to pass the data file / script / force-field file to the hub run (AURA artifacts or a hub path)')

    def describe(self, job, mat, spec):
        out = []
        for c in _hub_inputs(job, mat, spec):
            out.append({'mcp_tool': 'run_sim2l', 'arguments': {
                'nH_tool': c['tool'], 'chosen_inputs': c['inputs'],
                'chosen_outputs': (['gamma', 'gamma_error', 'gamma_unrelaxed', 'gamma_flags', 'termination_used',
                                    'terminations_screened', 'minimization', 'slab_from', 'molecules_intact',
                                    'faces_equivalent', 'child_runs', 'notes'] if c['tool'] == 'surfenergy'
                                   else ['DataFileOutput', 'n_molecules', 'mnp_used'] if c['tool'] == 'supercell'
                                   else ['sfe_unrelaxed', 'terminations', 'faces_equivalent', 'notes']),
                'trace_id': self.cfg.get('trace_id', '')}, 'then': c.get('then')})
        return {'backend': self.name, 'job_id': job['job_id'], 'calls': out,
                'poll': 'get_job_status(job_id) until finished, then get_job_result(job_id) once'}

    def available(self):
        return False, 'needs ' + '; '.join(self.NEEDS)

    def run(self, job, mat, spec, work_dir, timeout_s):
        ok, why = self.available()
        raise BackendNotAvailable('aura backend not available here: %s. The MCP call would be: %s' % (
            why, json.dumps(self.describe(job, mat, spec)['calls'])[:800]))


BACKENDS = {'local': LocalBackend, 'nanohub': NanohubBackend, 'aura': AuraBackend}


def make_backend(spec):
    ex = spec['executor']
    name = ex.get('backend', 'local')
    if name not in BACKENDS:
        raise SpecError('unknown backend %r (known: %s)' % (name, ', '.join(BACKENDS)))
    return BACKENDS[name](ex.get(name) or {})


# ============================================================================================ CIF preparation
def _data_body(path):
    """A LAMMPS data file without its first (title) line: what makes two unit cells the same."""
    with open(path) as f:
        lines = f.read().splitlines()
    return '\n'.join(lines[1:]).strip()


def prepare_cif(entry, spec, ctx):
    """{cif: PATH} -> cif2lammps (chain, local) with surfenergy's CIF defaults (normalize_hydrogens, relax_unitcell
    atoms, the ReaxFF file), cached by the CIF's md5. Returns the name of a known material when the unit cell is
    one (same data-file md5 as a bundle or zoo material), else a new Material (kind 'cif')."""
    from campaign import _path, _reroot
    cif = _path(entry['cif'], ctx['base'])
    if not cif or not os.path.isfile(cif):
        raise SpecError('CIF not found: %s' % entry['cif'])
    ff = _path(((entry.get('forcefield') or {}).get('ffield') or spec['forcefield'].get('ffield')), ctx['base']) or \
        os.path.join(chain_root(), 'cif2lammps', 'examples', 'hmx', 'reax-lg-2011.ff')
    key = md5(cif)
    d = os.path.join(STATE, 'campaigns', '_materials', key + '-' + md5(ff)[:8])
    out_p = os.path.join(d, 'cif2lammps_outputs.json')
    if not os.path.isfile(out_p):
        if not chain_present():
            raise SpecError('%s: a CIF needs cif2lammps (the chain): %s' % (entry['cif'], config.NO_CHAIN_MSG))
        ex = os.path.join(d, 'example')
        os.makedirs(ex, exist_ok=True)
        shutil.copy(cif, os.path.join(ex, os.path.basename(cif)))
        shutil.copy(ff, os.path.join(ex, os.path.basename(ff)))
        with open(os.path.join(ex, 'inputs.yaml'), 'w') as f:
            f.write('ciffile: file://%s\nffieldfilename: file://%s\nbond_tolerance: 0.45\nnormalize_hydrogens: true\n'
                    'relax_unitcell: atoms\n' % (os.path.basename(cif), os.path.basename(ff)))
        croot = chain_root()
        env = dict(os.environ, SIM2L_DEV_ROOT=croot, SIM2L_ENV_PREFIX=chain_env())
        env.pop('PYTHONPATH', None)
        p = subprocess.run([os.path.join(croot, 'devtools', 'sim2l-env'), 'python',
                            os.path.join(croot, 'devtools', 'run_sim2l.py'), os.path.join(croot, 'cif2lammps'), ex],
                           cwd=croot, env=env, capture_output=True, text=True, timeout=600)
        log = p.stdout
        if p.returncode != 0 or 'run directory:' not in log:
            raise SpecError('cif2lammps failed on %s: %s' % (entry['cif'], (log + p.stderr)[-800:]))
        rd = log.split('run directory:', 1)[1].split()[0]
        outs = json.loads(log[log.index('{', log.index('run directory:')):])
        # simtool returns file outputs as their contents: write them under the names the script uses
        script = outs.get('InputScriptOutput') or ''
        rdn = [l.split()[1] for l in script.splitlines() if l.split()[:1] == ['read_data'] and len(l.split()) > 1]
        names = {'DataFileOutput': rdn[0] if rdn else 'unitcell.data', 'InputScriptOutput': 'in.run0.lmp'}
        for k, fname in names.items():
            v = outs.get(k) or ''
            dst = os.path.join(d, fname)
            if '\n' not in v and os.path.isfile(os.path.join(croot, 'cif2lammps', 'bin', rd, v)):
                shutil.copy(os.path.join(croot, 'cif2lammps', 'bin', rd, v), dst)
            else:
                open(dst, 'w').write(v)
            outs[k] = dst
        outs['_run_id'] = os.path.basename(rd)
        outs['_ffield'] = ff
        json.dump(outs, open(out_p, 'w'), indent=1, default=str)
    outs = json.load(open(out_p))
    dmd5 = md5(outs['DataFileOutput'])
    from engine import MATERIALS
    for m, mj in MATERIALS.items():
        if mj.get('datafile_md5_recorded') == dmd5:
            return m
    body = _data_body(outs['DataFileOutput'])
    for r in ctx['zoo']:
        p = _reroot((r.get('cif2lammps') or {}).get('data'))
        if p and os.path.isfile(p) and (md5(p) == dmd5 or _data_body(p) == body):
            # the same unit cell (the title line names the CIF file, e.g. 'from rdx1.cif' / 'from 5000148.cif')
            ctx.setdefault('cif_mapped', {})[r['key']] = (
                'CIF %s -> cif2lammps run %s: the same data file as the CIF-zoo entry %s (COD %s) apart from the title '
                'line, so the material is that entry (its data file and records)' % (
                    os.path.basename(cif), outs.get('_run_id'), r['key'], r.get('cod')))
            return r['key']
    mat = Material(entry.get('id') or os.path.splitext(os.path.basename(cif))[0], 'cif')
    mat.material_id = entry.get('id') or (outs.get('compound_name') or mat.key).lower().replace(' ', '-')[:40]
    mat.name = entry.get('name') or outs.get('compound_name') or mat.key
    mat.inputs.update(datafile=outs['DataFileOutput'], script=outs['InputScriptOutput'], supplementary=ff,
                      use_reaxff=True)
    mat.default_route = 'reaxff'
    mat.meta.update({'cif2lammps': {k: outs.get(k) for k in ('inchikey', 'smiles', 'molecular_formula', 'space_group',
                                                           'space_group_number', 'cell_parameters', 'density',
                                                           'n_molecules', '_run_id')},
                     'cif_md5': key, 'inchikey': entry.get('inchikey') or outs.get('inchikey'),
                     'structure_id': entry.get('structure_id') or 'cif-%s' % key[:8]})
    mat.ff_note = 'ReaxFF route from a CIF (cif2lammps); not validated for this material unless stated'
    mat.notes.append('cif2lammps run %s (cached under state/campaigns/_materials/)' % outs['_run_id'])
    return mat


# ============================================================================================ executor
class Journal:
    def __init__(self, name):
        self.dir = campaign_dir(name)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, 'journal.jsonl')
        self.lock = threading.Lock()

    def write(self, **ev):
        ev.setdefault('time', now())
        with self.lock, open(self.path, 'a') as f:
            f.write(json.dumps(ev, default=str) + '\n')
        return ev


def _base(j):
    """The job's estimate before any recalibration (the model's own number): the base of every ratio."""
    return j.get('est_plan_minutes', j.get('est_minutes'))


def _finish_event(job, res, work_dir):
    o = res.get('outputs') or {}
    ev = {'job_id': job['job_id'], 'event': 'done' if res.get('ok') else 'failed', 'material_id': job['material_id'],
          'family': job['family'], 'level': job['level'], 'temperature_K': job['temperature_K'],
          'hkl': job['hkl'], 'search_range': job.get('search_range'),
          'angle_dev': (job.get('cell_overrides') or {}).get('angle_dev'), 'cpu_s': res.get('cpu_s'), 'wall_s': res.get('wall_s'),
          'est_minutes': job.get('est_minutes'), 'est_base_minutes': _base(job), 'result': portable(os.path.join(work_dir, 'result.json')),
          'run_dir': portable(res.get('run_dir'))}
    if res.get('ok'):
        ev['gamma'] = o.get('gamma') if job['level'] > 1 else o.get('gamma_unrelaxed')
        ev['gamma_flags'] = o.get('gamma_flags') or []
    else:
        ev['error_line'] = error_line(res.get('error'))
        ev['error'] = _err_tail(res.get('error'), 1200)
        # every oriented cell of this job that has failed so far (the journal keeps the last event per job)
        prev = [c for c in job.get('failed_cells') or [] if str(c.get('ref', '')).startswith('journal')]
        ev['cells_failed'] = [{k: c.get(k) for k in ('hkl', 'search_range', 'angle_dev')} for c in prev] + \
            [{'hkl': ev['hkl'], 'search_range': ev['search_range'], 'angle_dev': ev['angle_dev']}]
    return ev


def execute(p, spec, backend=None, only=None, retry_failed=False, log=print, timeout_factor=None):
    """Run the plan's jobs (p = campaign.plan(spec)). Returns a summary. Raises PolicyRefused for policy 'agent'
    and BackendNotAvailable when the backend cannot run here."""
    if spec['policy'] == 'agent':
        raise PolicyRefused('policy agent: the queue is left for the MCP agent (campaign_plan / campaign_status '
                            'show it; the agent runs facets with screen_facet / relax_facet). Use policy baseline or '
                            'exhaustive to run it here.')
    backend = backend or make_backend(spec)
    ok, why = backend.available()
    if not ok:
        raise BackendNotAvailable('%s backend not available here: %s' % (backend.name, why))
    name = p['campaign']
    jr = Journal(name)
    mats = {m.key: m for m in p['_materials']}
    workers = max(1, min(int(spec['executor'].get('workers') or 1), MAX_WORKERS))
    tf = float(timeout_factor or spec['executor'].get('timeout_factor') or 3.0)
    last = read_journal(name)
    jobs = [j for j in p['jobs'] if not only or j['job_id'] in only]
    budget = float(spec['budget']['cpu_minutes'])
    spent = sum(float(e.get('cpu_s') or 0) for e in last.values() if e.get('event') in ('done', 'failed')) / 60.0
    todo, summary = [], {'campaign': name, 'backend': backend.name, 'workers': workers, 'done': [], 'failed': [],
                         'skipped': [], 'recovered': []}
    for j in jobs:
        ev = last.get(j['job_id'])
        wd = os.path.join(jr.dir, 'jobs', j['job_id'])
        # a failed job runs again only with --retry-failed, or when the planner chose another cell for it
        # (dedup.retry_failed other_cell: j['retry_of_failed'])
        if ev and ev['event'] == 'done' or (ev and ev['event'] == 'failed' and not retry_failed
                                            and not j.get('retry_of_failed')):
            summary['skipped'].append({'job_id': j['job_id'], 'why': 'journal: %s at %s' % (ev['event'], ev['time'])})
            continue
        if not j.get('runnable', True) and backend.name == 'local':
            summary['skipped'].append(jr.write(job_id=j['job_id'], event='skipped', why='inputs not here (replay mode): '
                                               + config.NO_CHAIN_MSG))
            continue
        if ev and ev['event'] == 'started' and os.path.isfile(os.path.join(wd, 'result.json')):
            res = json.load(open(os.path.join(wd, 'result.json')))
            fin = jr.write(**dict(_finish_event(j, res, wd), recovered=True))
            if res.get('ok') and res.get('cpu_s') and j.get('est_minutes'):
                last[j['job_id']] = fin
            summary['recovered'].append(fin)
            summary['done' if res.get('ok') else 'failed'].append(fin)
            spent += float(res.get('cpu_s') or 0) / 60.0
            continue
        todo.append(j)
    inflight = {}
    ratios = {}
    for e in last.values():
        b = e.get('est_base_minutes') or e.get('est_minutes')
        if e.get('event') == 'done' and e.get('cpu_s') and b:
            ratios.setdefault(e.get('level'), []).append(float(e['cpu_s']) / 60.0 / float(b))
    cap = float(spec['budget']['per_run_cap_minutes'])

    def eff(j):
        r = sorted(ratios.get(j['level']) or [])
        f = max(1.0, r[len(r) // 2]) if r else 1.0
        return (_base(j) or 0) * f, f

    def one(j):
        wd = os.path.join(jr.dir, 'jobs', j['job_id'])
        t_s = max(300, int((j['est_minutes'] or 1) * 60 * tf + 120))
        try:
            return backend.run(j, mats[j['material']], spec, wd, t_s), wd
        except BackendNotAvailable:
            raise
        except Exception as exc:     # a driver-side error: recorded like a tool failure, never hidden
            import traceback
            return {'ok': False, 'error': 'driver: %s: %s\n%s' % (type(exc).__name__, exc, traceback.format_exc()[-800:]),
                    'cpu_s': None}, wd

    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        while todo or inflight:
            while todo and len(inflight) < workers:
                j = todo.pop(0)
                e_j, f = eff(j)
                committed = spent + sum(eff(x)[0] for x in inflight.values())
                why = None
                if e_j > cap + 1e-9:
                    why = ('over the per-run cap at run time: estimate %.1f x %.2f (this campaign\'s measured/estimated '
                           'CPU, level %d) = %.1f > %.1f CPU min' % (_base(j), f, j['level'], e_j, cap))
                elif committed + e_j > budget + 1e-9:
                    why = ('over budget at run time: %.1f spent or in flight + %.1f (estimate x %.2f) > %.1f CPU min'
                           % (committed, e_j, f, budget))
                if why:
                    summary['skipped'].append(jr.write(job_id=j['job_id'], event='skipped', why=why,
                                                       material_id=j['material_id'], family=j['family'], level=j['level']))
                    log('skip   %s %s %s L%d: %s' % (j['job_id'], j['material_id'], j['family'], j['level'], why))
                    continue
                jr.write(job_id=j['job_id'], event='started', material_id=j['material_id'], family=j['family'],
                         level=j['level'], hkl=j['hkl'], search_range=j['search_range'],
                         angle_dev=(j.get('cell_overrides') or {}).get('angle_dev'), est_minutes=j['est_minutes'],
                         backend=backend.name)
                log('start  %s %s %s L%d (%s) est %.1f min' % (j['job_id'], j['material_id'], j['family'], j['level'],
                                                                ' '.join(map(str, j['hkl'])), j['est_minutes'] or 0))
                inflight[pool.submit(one, j)] = j
            if not inflight:
                break
            done, _ = cf.wait(list(inflight), return_when=cf.FIRST_COMPLETED)
            for f in done:
                j = inflight.pop(f)
                res, wd = f.result()
                ev = jr.write(**_finish_event(j, res, wd))
                spent += float(res.get('cpu_s') or 0) / 60.0
                if res.get('ok') and res.get('cpu_s') and _base(j):
                    ratios.setdefault(j['level'], []).append(float(res['cpu_s']) / 60.0 / float(_base(j)))
                summary['done' if res.get('ok') else 'failed'].append(ev)
                log('%s %s %s %s L%d: %s, %.1f CPU min' % ('done  ' if res.get('ok') else 'FAILED', j['job_id'],
                                                           j['material_id'], j['family'], j['level'],
                                                           ('gamma %.2f' % ev['gamma']) if ev.get('gamma') is not None
                                                           else ev.get('error_line') or '',
                                                           float(res.get('cpu_s') or 0) / 60.0))
    summary['cpu_min_spent_campaign'] = round(spent, 2)
    return summary


def status(spec, p=None):
    """Journal state of a campaign: per job its last event, plus the totals."""
    name = spec['campaign']
    last = read_journal(name)
    rows = sorted(last.values(), key=lambda e: e['time'])
    cpu = sum(float(e.get('cpu_s') or 0) for e in rows if e.get('event') in ('done', 'failed')) / 60.0
    out = {'campaign': name, 'journal': portable(os.path.join(campaign_dir(name), 'journal.jsonl')),
           'n_events_last': len(rows), 'by_event': {}, 'cpu_min': round(cpu, 2), 'jobs': rows}
    for e in rows:
        out['by_event'][e['event']] = out['by_event'].get(e['event'], 0) + 1
    if p is not None:
        out['planned_not_started'] = [j['job_id'] for j in p['jobs'] if j['job_id'] not in last]
    return out

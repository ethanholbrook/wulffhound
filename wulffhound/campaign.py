"""wulffhound campaign driver: a campaign spec -> a plan of jobs -> runs (campaign_exec.py) -> dataset records
(campaign_ingest.py). The physics stays in the chain's Sim2Ls: the driver only decides what to run and calls the
core force-field routes (ReaxFF, non-reactive typed) exactly as they are.

    from campaign import load_spec, plan
    p = plan(load_spec('campaigns/tnt_t0_relax.yaml'))
    print(format_plan(p))

A spec (YAML or JSON, schema/campaign.schema.json) names materials, a force-field route, facets, levels,
temperatures, protocol overrides, a CPU budget with a per-run cap and a policy. The planner

  1. resolves each material (wulffhound bundle, CIF-zoo entry of the chain, chain example folder or a CIF via
     cif2lammps) and its force-field route (FORCEFIELD_ROUTES: 'reaxff', 'nonreactive'; a later route registers
     itself with register_route and is then a valid 'route' value);
  2. selects facet families (explicit, bfdh_top N, max_index K, on_shape of the material's current shape) and
     expands material x family x level x T into jobs;
  3. looks before it runs: a job whose exact key (material, structure, force field, family, level, T, n_periods,
     slab_from, vacuum, MD lengths) is already in the dataset or in this campaign's journal is skipped and the
     record ids are named; same material/family/level with another protocol is reported as a near-duplicate
     (the job still runs: that is a refresh); a key that failed before is never retried blindly;
  4. estimates every job's CPU with the engine's calibrated model (bundle materials, measured cells) and with
     surfenergy's own estimator (its GUI model, run in the chain env), skips jobs over the per-run cap, orders
     the rest by the policy and cuts at the budget;
  5. returns the plan (a dry run: nothing is run here).
"""
import copy
import datetime
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
from config import ROOT, STATE, chain_env, chain_present, chain_root, portable  # noqa: E402

SPEC_SCHEMA = os.path.join(ROOT, 'schema', 'campaign.schema.json')
DATASET = os.path.join(ROOT, 'dataset')
CAMPAIGN_STORE = os.path.join(DATASET, 'campaigns')
LEVEL_NAME = {1: 'unrelaxed', 2: 'relaxed_T0', 3: 'md'}
SLAB_FROM_DEFAULT = 'relaxed_bulk'      # surfenergy's default since R3 (2026-10-23)
VACUUM_DEFAULT = 30.0
SEARCH_RANGE_NONBUNDLE = 4              # the CIF zoo's search range (small cells first)
MAX_WORKERS = 2                         # at most two heavy jobs at once
REAX_MD5 = 'c129e55db39a4f1e0e5cad3b7a6b0a00'
# surfenergy's FIRE step caps (ReaxFF at T = 0; inputs since chain 2026-10-24): protocol overrides of level 2, and part
# of the dedup key only when they differ from surfenergy's defaults (so the keys and job ids of default runs are
# unchanged, and a run with a raised cap is not taken for the capped one)
FIRE_CAP_KEYS = ('fire_max_steps_bulk', 'fire_max_steps_slab')
FIRE_CAP_DEFAULTS = (10000, 12000)

DEFAULTS = {
    'description': '',
    'forcefield': {'route': 'auto'},
    'facets': {'select': 'bfdh_top', 'n': 4},
    'levels': [1],
    'temperatures': [0.0],
    'md': {'equilibration_ps': 10.0, 'production_ps': 20.0, 'n_blocks': 5, 'timestep': 0.0},
    'protocol': {'n_periods': 2, 'search_range': None, 'slab_from': None, 'vacuum': None, 'angle_dev': None,
                 's_tol': None, 'fire_max_steps_bulk': None, 'fire_max_steps_slab': None, 'cell_fallbacks': []},
    'level_facets': {},
    'budget': {'cpu_minutes': 30.0, 'per_run_cap_minutes': 6.0},
    'policy': 'baseline',
    'executor': {'backend': 'local', 'workers': 1},
    'dedup': {'retry_failed': False},
}


class SpecError(ValueError):
    pass


# ============================================================================================ spec
def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_facets(f):
    """facets: a list (families / hkl), 'bfdh_top_N', 'max_index K', 'on_shape', or a dict with 'select'."""
    if f is None:
        return dict(DEFAULTS['facets'])
    if isinstance(f, list):
        return {'select': 'explicit', 'families': f}
    if isinstance(f, str):
        s = f.strip().replace('-', '_')
        if s.startswith('bfdh_top'):
            n = s[len('bfdh_top'):].strip('_ ')
            return {'select': 'bfdh_top', 'n': int(n) if n else 4}
        if s.startswith('max_index'):
            return {'select': 'max_index', 'k': int(s[len('max_index'):].strip('_ ') or 2)}
        if s == 'on_shape':
            return {'select': 'on_shape'}
        if s == 'wulff_priority':
            return {'select': 'wulff_priority'}
        raise SpecError('facets %r: use a list, "bfdh_top_N", "max_index K", "on_shape", "wulff_priority" or a dict' % f)
    if isinstance(f, dict):
        d = dict(f)
        d.setdefault('select', 'explicit' if 'families' in d else 'bfdh_top')
        return d
    raise SpecError('facets: unsupported value %r' % (f,))


def load_spec(path_or_dict, base_dir=None):
    """A campaign spec (YAML/JSON path, YAML/JSON text, or a dict), validated and with the defaults filled in."""
    src = None
    if isinstance(path_or_dict, dict):
        raw = copy.deepcopy(path_or_dict)
    else:
        txt = path_or_dict
        if os.path.isfile(str(path_or_dict)):
            src = os.path.abspath(path_or_dict)
            base_dir = base_dir or os.path.dirname(src)
            txt = open(src).read()
        try:
            raw = json.loads(txt)
        except ValueError:
            try:
                import yaml
            except ImportError:
                raise SpecError('YAML specs need pyyaml (environment.yml); or write the spec as JSON')
            raw = yaml.safe_load(txt)
        if not isinstance(raw, dict):
            raise SpecError('the spec is not a mapping (did you give a file that does not exist? %r)'
                            % str(path_or_dict)[:200])
    if isinstance(raw.get('level_facets'), dict):          # YAML gives {2: ...}: the schema keys are strings
        raw['level_facets'] = {str(k): v for k, v in raw['level_facets'].items()}
    try:
        import jsonschema
        jsonschema.validate(raw, json.load(open(SPEC_SCHEMA)))
    except ImportError:
        pass
    except Exception as exc:
        raise SpecError('spec does not match schema/campaign.schema.json: %s' % getattr(exc, 'message', exc))
    spec = _merge(DEFAULTS, {k: v for k, v in raw.items() if k != 'facets'})
    spec['facets'] = parse_facets(raw.get('facets'))
    spec['level_facets'] = {int(k): parse_facets(v) for k, v in (raw.get('level_facets') or {}).items()}
    for lv in spec['level_facets']:
        if lv not in (1, 2, 3):
            raise SpecError('level_facets: level %r is not 1, 2 or 3' % lv)
    rf = spec['dedup'].get('retry_failed')
    if rf not in (True, False, None, 'other_cell'):
        raise SpecError("dedup.retry_failed must be true, false or 'other_cell', not %r" % (rf,))
    spec['levels'] = sorted({int(x) for x in spec['levels']})
    spec['temperatures'] = [float(t) for t in spec['temperatures']]
    if 3 in spec['levels'] and not any(t > 0 for t in spec['temperatures']):
        raise SpecError('level 3 (MD) needs a temperature > 0 in temperatures')
    if spec['policy'] not in ('exhaustive', 'baseline', 'agent'):
        raise SpecError("policy must be 'exhaustive', 'baseline' or 'agent', not %r" % spec['policy'])
    w = int(spec['executor'].get('workers') or 1)
    spec['executor']['workers_requested'] = w
    spec['executor']['workers'] = max(1, min(w, MAX_WORKERS))
    spec['_source'] = portable(src) if src else None
    spec['_base_dir'] = base_dir or ROOT
    spec.setdefault('campaign', os.path.splitext(os.path.basename(src))[0] if src else 'campaign')
    return spec


# ============================================================================================ force-field routes
FORCEFIELD_ROUTES = {}


def register_route(name):
    """Register a force-field route: fn(material, ff_spec, ctx) -> dict(use_reaxff, supplementary, datafile, script,
    ff_block). The core routes 'reaxff' and 'nonreactive' are the defaults; a new route (e.g. OpenFF) only adds a
    name here and never changes them."""
    def deco(fn):
        FORCEFIELD_ROUTES[name] = fn
        return fn
    return deco


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _path(p, base):
    """'chain:...', 'data:...' -> resolved; a relative path -> under the spec's folder, then the repository."""
    if not p:
        return None
    if p.startswith('chain:') or p.startswith('data:'):
        return config.resolve(p)
    p = os.path.expanduser(p)
    if os.path.isabs(p):
        return p
    for b in (base, ROOT, chain_root()):
        q = os.path.join(b, p)
        if os.path.exists(q):
            return os.path.abspath(q)
    return os.path.abspath(os.path.join(base, p))


@register_route('reaxff')
def route_reaxff(mat, ff, ctx):
    ffield = _path(ff.get('ffield'), ctx['base']) or mat.inputs.get('supplementary')
    if not ffield:
        raise SpecError('%s: route reaxff needs forcefield.ffield (the ReaxFF force-field file)' % mat.key)
    if not os.path.isfile(ffield):
        raise SpecError('%s: ReaxFF force-field file not found: %s' % (mat.key, portable(ffield)))
    m = md5(ffield)
    lg = m == REAX_MD5
    block = {'name': ff.get('name') or ('ReaxFF-lg' if lg else 'ReaxFF'),
             'version': ff.get('version') or ('reax-lg-2011 (Liu, Liu, Zybin & Goddard, J. Phys. Chem. A 115, 11016 '
                                              '(2011))' if lg else os.path.basename(ffield)),
             'kind': 'reactive', 'md5': m, 'file': os.path.basename(ffield) + ' (not distributed)',
             'validated_for_material': bool(ff.get('validated', mat.ff_validated)),
             'note': ff.get('note') or mat.ff_note}
    return {'use_reaxff': True, 'supplementary': ffield, 'datafile': mat.inputs['datafile'],
            'script': mat.inputs['script'], 'ff_block': block}


@register_route('nonreactive')
def route_nonreactive(mat, ff, ctx):
    data = _path(ff.get('datafile'), ctx['base']) or mat.inputs.get('datafile')
    script = _path(ff.get('script'), ctx['base']) or mat.inputs.get('script')
    for what, p in (('datafile (typed, full topology)', data), ('script (the force field, run 0)', script)):
        if not p or not os.path.isfile(p):
            raise SpecError('%s: route nonreactive needs forcefield.%s: %s' % (mat.key, what, p and portable(p)))
    sup = _path(ff.get('supplementary'), ctx['base']) or mat.inputs.get('supplementary')
    block = {'name': ff.get('name') or mat.ff_name or 'non-reactive (%s)' % os.path.basename(script),
             'version': ff.get('version') or mat.ff_version or os.path.basename(script),
             'kind': 'non-reactive', 'md5': md5(script), 'file': os.path.basename(script) + ' (not distributed)',
             'validated_for_material': bool(ff.get('validated', mat.ff_validated)),
             'note': ff.get('note') or mat.ff_note}
    return {'use_reaxff': False, 'supplementary': sup, 'datafile': data, 'script': script, 'ff_block': block}


# ============================================================================================ materials
def _export_module():
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    import export_dataset
    return export_dataset


def load_zoo():
    p = os.path.join(chain_root(), 'notes', 'validation', 'cif_zoo.json')
    return json.load(open(p)) if os.path.isfile(p) else []


def _reroot(p):
    """An absolute chain path recorded on another machine/checkout -> under this chain root."""
    if not p:
        return p
    for marker in ('/.sim2l/', '/surface-energy-sim2ls/'):
        if marker in p:
            rest = p.split(marker, 1)[1]
            return os.path.join(chain_root(), '.sim2l', rest) if marker == '/.sim2l/' else os.path.join(chain_root(), rest)
    return p


class Material:
    """One resolved material: inputs for the chain, its force-field route and the dataset identity."""

    def __init__(self, key, kind):
        self.key, self.kind = key, kind
        self.material_id = None
        self.name = key
        self.inputs = {'datafile': None, 'script': None, 'supplementary': None, 'example_dir': None, 'use_reaxff': False}
        self.default_route = 'nonreactive'
        self.ff_validated, self.ff_note, self.ff_name, self.ff_version = False, None, None, None
        self.bundle = None          # wulffhound bundle name (tnt, paracetamol)
        self.zoo = None             # the cif_zoo.json entry
        self.meta = {}              # spec-given metadata (inchikey, name, polymorph, licence ...)
        self.route = None           # FORCEFIELD_ROUTES result
        self.notes = []
        self._sym = None
        self._engine = None

    # ---------------------------------------------------------------- identity
    def describe(self):
        return {'key': self.key, 'kind': self.kind, 'material_id': self.material_id, 'name': self.name,
                'route': self.route_name, 'force_field': (self.route or {}).get('ff_block', {}).get('name'),
                'datafile': portable(self.route['datafile']) if self.route else None,
                'structure_id': self.structure_id(), 'notes': self.notes}

    def structure_id(self):
        if self.bundle == 'tnt':
            return 'chain-tnt-unitcell'
        if self.bundle == 'paracetamol' or self.zoo:
            cod = (self.zoo or {}).get('cod') or 2104364
            return 'cod-%d' % cod
        return self.meta.get('structure_id') or 'local-%s' % (self.datafile_md5() or 'unknown')[:8]

    def datafile_md5(self):
        p = (self.route or {}).get('datafile') or self.inputs.get('datafile')
        return md5(p) if p and os.path.isfile(p) else self.meta.get('datafile_md5')

    # ---------------------------------------------------------------- symmetry, engine
    def engine(self):
        if self.bundle and self._engine is None:
            from engine import Engine
            self._engine = Engine(self.bundle, state_dir=os.path.join(STATE, 'campaigns', '_engine'),
                                  session='campaign')
        return self._engine

    def symmetry(self):
        """Families (Laue orbits, BFDH rank, d_eff), lattice and space group in the data-file setting."""
        if self._sym is None:
            if self.bundle:
                self._sym = self.engine().sym
            else:
                p = self.route['datafile']
                if not p or not os.path.isfile(p):
                    raise SpecError('%s: data file not here (%s): %s' % (self.key, p and portable(p),
                                                                         config.NO_CHAIN_MSG))
                d = os.path.join(STATE, 'campaigns', '_symmetry')
                os.makedirs(d, exist_ok=True)
                c = os.path.join(d, md5(p) + '.json')
                if os.path.isfile(c):
                    self._sym = json.load(open(c))
                else:
                    import symmetry
                    self._sym = symmetry.analyse_cell(p, 3)
                    self._sym['datafile'] = portable(p)
                    json.dump(self._sym, open(c, 'w'), indent=1)
        return self._sym

    def families(self):
        return {f['family']: f for f in self.symmetry()['families']}

    def family_of(self, hkl):
        import numpy as np
        h = [int(x) for x in hkl]
        g = math.gcd(math.gcd(abs(h[0]), abs(h[1])), abs(h[2]))
        if g == 0:
            raise SpecError('(0 0 0) is not a facet')
        h = tuple(x // g for x in h)
        for f in self.symmetry()['families']:
            if list(h) in [list(m) for m in f['members']]:
                return f['family']
        orbit = sorted({tuple(int(x) for x in np.array(h) @ np.array(M)) for M in self.symmetry()['laue_ops_on_miller']},
                       reverse=True)
        rep = sorted(orbit, key=lambda x: (-(sum(1 for v in x if v >= 0)), [-v for v in x]))[0]
        return '{%s}' % ' '.join(str(v) for v in rep)

    @property
    def route_name(self):
        return (self.route or {}).get('name')


def _bundle_aliases():
    from engine import MATERIALS
    ex = _export_module()
    out = {}
    for m in MATERIALS:
        wm = ex.WH_MATERIALS.get(m, {})
        for a in (m, wm.get('material_id'), wm.get('inchikey')):
            if a:
                out[a.lower()] = m
    return out


def resolve_material(entry, spec, ctx):
    """A spec material entry (a name / material_id / InChIKey string, or {name|inchikey|cif|example: ...}) ->
    Material with its force-field route."""
    from engine import MATERIALS
    e = {'name': entry} if isinstance(entry, str) else dict(entry)
    ex = _export_module()
    zoo = ctx['zoo']
    bundle_alias = ctx['bundle_alias']
    zoo_alias = {}
    for r in zoo:
        if not (r.get('cif2lammps') or {}).get('ok'):
            continue
        mid = ex.ZOO_IDS.get(r['key'], (r['key'],))[0]
        for a in (r['key'], mid, r['name'], r.get('inchikey')):
            if a:
                zoo_alias.setdefault(a.lower(), r)
    mat = None
    want = (e.get('name') or e.get('inchikey') or e.get('material_id') or '').lower()
    if e.get('example'):
        d = _path(e['example'], ctx['base'])
        y = os.path.join(d, 'inputs.yaml')
        if not os.path.isfile(y):
            raise SpecError('example %s: no inputs.yaml (%s)' % (e['example'], config.NO_CHAIN_MSG))
        import yaml
        v = yaml.safe_load(open(y))
        f = lambda k: os.path.join(d, v[k][len('file://'):]) if isinstance(v.get(k), str) and v[k].startswith('file://') else None
        if v.get('material_source', 'unitcell') == 'cif':
            e = dict(e, cif=f('ciffile'), forcefield=dict(e.get('forcefield') or {}, route='reaxff',
                                                           ffield=f('supplementaryfilename')))
        else:
            data_md5 = md5(f('datafilename'))
            for m, mj in MATERIALS.items():
                if mj.get('datafile_md5_recorded') == data_md5:
                    want = m
            if want not in bundle_alias:
                mat = Material(e.get('id') or os.path.basename(d), 'example')
                mat.material_id = e.get('id') or os.path.basename(d).replace('_', '-').lower()
                mat.inputs.update(datafile=f('datafilename'), script=f('inputfilename'),
                                  supplementary=f('supplementaryfilename'), example_dir=d,
                                  use_reaxff=bool(v.get('use_reaxff')))
                mat.default_route = 'reaxff' if v.get('use_reaxff') else 'nonreactive'
    if mat is None and e.get('cif'):
        mat = ctx['prepare_cif'](e, spec, ctx)          # cif2lammps (campaign_exec.prepare_cif); may map to a known material
        if isinstance(mat, str):
            want = mat
            mat = None
    if mat is None:
        if want in bundle_alias:
            b = bundle_alias[want]
            mj = MATERIALS[b]
            mat = Material(e.get('name') or b, 'bundle')
            mat.bundle = b
            mat.material_id = ex.WH_MATERIALS[b]['material_id']
            mat.name = ex.WH_MATERIALS[b]['name']
            mat.inputs.update({k: config.resolve(mj.get(k)) for k in ('datafile', 'script', 'supplementary', 'example_dir')})
            mat.inputs['use_reaxff'] = bool(mj.get('use_reaxff'))
            mat.default_route = 'reaxff' if mj.get('use_reaxff') else 'nonreactive'
            fb = ex.WH_MATERIALS[b]['force_field']
            mat.ff_validated, mat.ff_note = bool(mj.get('forcefield_validated')), fb.get('note')
            mat.ff_name, mat.ff_version = fb.get('name'), fb.get('version')
            mat.meta['datafile_md5'] = mj.get('datafile_md5_recorded')
        elif want in zoo_alias:
            r = zoo_alias[want]
            c = r['cif2lammps']
            mat = Material(e.get('name') or r['key'], 'zoo')
            mat.zoo = r
            mat.material_id = ex.ZOO_IDS[r['key']][0]
            mat.name = r['name']
            mat.inputs.update(datafile=_reroot(c.get('data')), script=_reroot(c.get('script')), use_reaxff=True,
                              supplementary=os.path.join(chain_root(), 'cif2lammps', 'examples', 'hmx', 'reax-lg-2011.ff'))
            mat.default_route = 'reaxff'
            mat.ff_note = ('CIF-zoo screening with ReaxFF-lg (parametrised for nitramines): unrelaxed cuts, not '
                           'validated surface energies')
        else:
            raise SpecError('material %r not found: not a wulffhound bundle material (%s) nor a CIF-zoo entry of the '
                            'chain%s; give {cif: PATH} or {example: DIR}' % (
                                entry, ', '.join(sorted(MATERIALS)), '' if zoo else ' (zoo not readable: ' +
                                config.NO_CHAIN_MSG + ')'))
    if mat.zoo and ctx.get('cif_mapped', {}).get(mat.zoo['key']):
        mat.notes.append(ctx['cif_mapped'][mat.zoo['key']])
    mat.meta.update({k: v for k, v in e.items() if k not in ('forcefield',)})
    ffs = _merge(spec['forcefield'], e.get('forcefield') or {})
    route = ffs.get('route') or 'auto'
    if route == 'auto':
        route = mat.default_route
    if route not in FORCEFIELD_ROUTES:
        raise SpecError('%s: unknown force-field route %r (known: %s; a new route registers itself with '
                        'campaign.register_route)' % (mat.key, route, ', '.join(sorted(FORCEFIELD_ROUTES))))
    if route != mat.default_route:
        mat.notes.append('force-field route %s instead of the material\'s own %s' % (route, mat.default_route))
    have_inputs = mat.inputs.get('datafile') and os.path.isfile(mat.inputs['datafile'])
    if not have_inputs and mat.bundle:
        # replay mode: no chain; the bundle still plans (dedup, estimates), but nothing can run
        mat.route = {'name': route, 'use_reaxff': mat.inputs['use_reaxff'], 'datafile': None, 'script': None,
                     'supplementary': None, 'ff_block': dict(ex.WH_MATERIALS[mat.bundle]['force_field']),
                     'runnable': False}
        mat.notes.append('inputs not here (replay mode): planned, not runnable: ' + config.NO_CHAIN_MSG)
        return mat
    mat.route = dict(FORCEFIELD_ROUTES[route](mat, ffs, ctx), name=route, runnable=True)
    if mat.bundle and route == mat.default_route:
        # the bundle's own force-field block (same names as the dataset's ledger records)
        mat.route['ff_block'] = dict(ex.WH_MATERIALS[mat.bundle]['force_field'])
    if mat.zoo and route == 'reaxff' and mat.route['ff_block']['md5'] == REAX_MD5:
        # the dataset's ReaxFF-lg block (same name as the zoo records); a spec may give its own note / validated
        mat.route['ff_block'] = dict(ex.REAX_FF, md5=REAX_MD5, validated_for_material=bool(ffs.get('validated', False)),
                                     note=ffs.get('note') or mat.ff_note)
    return mat


# ============================================================================================ dataset (look before you run)
def load_records(dataset_dir=None):
    """Every surface_energy record: dataset/surface_energy.jsonl plus the campaign stores (records ingested but not
    yet exported), by record id."""
    d = dataset_dir or DATASET
    out = {}
    files = [os.path.join(d, 'surface_energy.jsonl')] + sorted(glob.glob(os.path.join(d, 'campaigns', '*.jsonl')))
    for p in files:
        if os.path.isfile(p):
            for line in open(p):
                line = line.strip()
                if line:
                    r = json.loads(line)
                    if r.get('record_type') == 'surface_energy':
                        out[r['record_id']] = r
    return list(out.values())


def load_shapes(dataset_dir=None):
    p = os.path.join(dataset_dir or DATASET, 'wulff_shape.jsonl')
    return [json.loads(l) for l in open(p) if l.strip()] if os.path.isfile(p) else []


def record_key(r):
    """The dedup key of a dataset record (same fields as job_key)."""
    P = r['protocol']
    lv = P['level']
    md = (P.get('equilibration_ps'), P.get('production_ps'), P.get('n_blocks')) if lv == 3 else None
    return {'material_id': r['material']['material_id'], 'structure_id': r['material']['structure']['structure_id'],
            'force_field': r['force_field']['name'], 'family': r['facet']['family'], 'level': lv,
            'temperature_K': float(P.get('temperature_K') or 0.0), 'n_periods': P.get('n_periods'),
            'slab_from': (P.get('slab_from') or 'static_cut') if lv >= 2 else None,
            'vacuum_A': float(P['vacuum_A']) if P.get('vacuum_A') is not None else VACUUM_DEFAULT, 'md': md,
            **_fire_key(lv, record_fire_caps(r))}


KEY_FIELDS = ('material_id', 'structure_id', 'force_field', 'family', 'level', 'temperature_K', 'n_periods',
              'slab_from', 'vacuum_A', 'md', 'fire_max_steps')


def record_fire_caps(r):
    """(unit cell, slab) FIRE step caps of a record: the step_cap its minimisation entries carry (the slab's from
    any slab_* entry), the default for a missing one (records from before the caps were inputs ran at the defaults)."""
    m = r['result'].get('minimisation') or {}
    uc = (m.get('unitcell') or {}).get('step_cap')
    sl = [v.get('step_cap') for k, v in m.items() if k.startswith('slab') and isinstance(v, dict) and v.get('step_cap')]
    # a failed run has no minimisation: the --set of its reproduce line (campaign_ingest) gives the caps it asked for
    rep = str((r.get('provenance') or {}).get('reproduce') or '')
    given = [re.search(r'%s=(\d+)' % k, rep) for k in FIRE_CAP_KEYS]
    return (int(uc) if uc else int(given[0].group(1)) if given[0] else FIRE_CAP_DEFAULTS[0],
            int(max(sl)) if sl else int(given[1].group(1)) if given[1] else FIRE_CAP_DEFAULTS[1])


def _fire_key(level, caps):
    """{'fire_max_steps': [bulk, slab]} for a level-2 key whose caps differ from surfenergy's defaults, else {} (the
    field is left out, so default keys and job ids stay as they were)."""
    caps = tuple(int(c) for c in caps)
    return {'fire_max_steps': list(caps)} if level == 2 and caps != FIRE_CAP_DEFAULTS else {}


def job_key(mat, family, level, T, spec):
    pr = spec['protocol']
    md = None
    if level == 3:
        m = spec['md']
        md = (float(m['equilibration_ps']), float(m['production_ps']), int(m['n_blocks']))
    return {'material_id': mat.material_id, 'structure_id': mat.structure_id(),
            'force_field': mat.route['ff_block']['name'], 'family': family, 'level': level,
            'temperature_K': float(T), 'n_periods': int(pr['n_periods']),
            'slab_from': (pr.get('slab_from') or SLAB_FROM_DEFAULT) if level >= 2 else None,
            'vacuum_A': float(pr['vacuum']) if pr.get('vacuum') is not None else VACUUM_DEFAULT, 'md': md,
            **_fire_key(level, [pr.get(k) or d for k, d in zip(FIRE_CAP_KEYS, FIRE_CAP_DEFAULTS)])}


def job_id(key):
    return 'j-' + hashlib.sha1(json.dumps(key, sort_keys=True).encode()).hexdigest()[:10]


class DatasetIndex:
    def __init__(self, records):
        self.by = {}
        for r in records:
            k = record_key(r)
            self.by.setdefault((k['material_id'], k['family'], k['level']), []).append((k, r))

    def lookup(self, key, ff_md5=None, datafile_md5=None):
        """(exact ok records, exact failed records, near-duplicates [(record, differing fields)])."""
        exact_ok, exact_bad, near = [], [], []
        for k, r in self.by.get((key['material_id'], key['family'], key['level']), []):
            diff = [f for f in KEY_FIELDS if k.get(f) != key.get(f)]
            rm = r['force_field'].get('md5')
            if ff_md5 and rm and rm != ff_md5:
                diff.append('force-field md5')
            dm = (r['provenance'].get('input_md5') or {}).get('datafile')
            if datafile_md5 and dm and dm != datafile_md5:
                diff.append('data-file md5')
            if diff:
                near.append((r, diff))
            elif r['result']['status'] == 'ok':
                exact_ok.append(r)
            else:
                exact_bad.append(r)
        return exact_ok, exact_bad, near


# ============================================================================================ facets
class ShapeNeeded(SpecError):
    """A selection that needs a Wulff shape the dataset does not have yet (the jobs of that level wait)."""


def select_families_tiered(mat, fs, shapes):
    """(families, how, tiers): tiers {family: (tier, why)} for a wulff_priority selection, else {}."""
    if fs.get('select') != 'wulff_priority':
        fams, how = select_families(mat, fs, shapes)
        return fams, how, {}
    lv = str(fs.get('shape_level') or '1')
    amin = float(fs.get('min_area') or 1e-4)
    k = float(fs.get('k_sigma', 2.0))
    sh = [s for s in shapes if s['material_id'] == mat.material_id and s['level'] == lv]
    if not sh:
        raise ShapeNeeded('wulff_priority needs the level-%s Wulff shape of %s in the dataset (none yet: it is built '
                          'from the records once the earlier levels are ingested)' % (lv, mat.material_id))
    import shapes as shapes_mod
    s0 = sh[0]
    rank = {f['family']: f['bfdh_rank'] for f in mat.symmetry()['families']}
    tiers = {}
    for f in s0['facets']:
        if f['area_fraction'] > amin:
            tiers[f['family']] = (0, 'on the level-%s shape (area %.3f)' % (lv, f['area_fraction']))
    for f in s0['facets']:
        if f['family'] in tiers:
            continue
        why = []
        if (f.get('p_on_shape') or 0) > 0:
            why.append('on the shape in %.0f %% of the sensitivity samples' % (100 * f['p_on_shape']))
        red = shapes_mod.reduction_to_appear(s0, f['family'], amin)
        if red is not None and red <= k * (f.get('sigma') or 0):
            why.append('appears when its gamma is lowered by %.1f mJ/m^2 <= %g sigma (%.1f)' % (
                red, k, k * (f.get('sigma') or 0)))
        if why:
            tiers[f['family']] = (1, 'within the uncertainty of the level-%s shape: %s' % (lv, '; '.join(why)))
    for f in s0['facets']:
        if f['family'] not in tiers:
            tiers[f['family']] = (2, 'off the level-%s shape; BFDH rank %s' % (lv, rank.get(f['family'])))
    fams = sorted(tiers, key=lambda f: (tiers[f][0], -next(x['area_fraction'] for x in s0['facets'] if x['family'] == f)
                                        if tiers[f][0] == 0 else rank.get(f, 999)))
    n = [sum(1 for t in tiers.values() if t[0] == i) for i in range(3)]
    return fams, ('wulff_priority on the level-%s shape %s: %d on it, %d within %g sigma, then %d more in BFDH order%s'
                  % (lv, s0['shape_id'], n[0], n[1], k, n[2], ', until %d have a value' % int(fs['target_ok'])
                     if fs.get('target_ok') else '')), tiers


def select_families(mat, fs, shapes):
    """The facet families of a material, in the selection's order (BFDH order for bfdh_top / max_index, area
    order for on_shape, the given order for explicit)."""
    fams = sorted(mat.symmetry()['families'], key=lambda f: f['bfdh_rank'])
    sel = fs['select']
    if sel == 'explicit':
        out = []
        for x in fs.get('families') or []:
            f = x.strip() if isinstance(x, str) and x.strip().startswith('{') else None
            if f is None:
                h = [int(v) for v in (x.replace(',', ' ').split() if isinstance(x, str) else x)]
                f = mat.family_of(h)
            else:
                f = mat.family_of([int(v) for v in f.strip('{}').split()])
            if f not in out:
                out.append(f)
        return out, 'explicit list'
    if sel == 'bfdh_top':
        n = int(fs.get('n') or 4)
        return [f['family'] for f in fams[:n]], 'the %d first families in BFDH order (d_eff)' % n
    if sel == 'max_index':
        k = int(fs.get('k') or 2)
        return [f['family'] for f in fams if f['max_abs_index'] <= k], 'every family with max |index| <= %d, BFDH order' % k
    if sel == 'on_shape':
        lv = str(fs.get('shape_level') or 'best')
        amin = float(fs.get('min_area') or 1e-4)
        sh = [s for s in shapes if s['material_id'] == mat.material_id and s['level'] == lv]
        if not sh:
            raise SpecError('%s: on_shape needs a level-%s Wulff shape of %s in the dataset (have none)'
                            % (mat.key, lv, mat.material_id))
        rows = sorted([f for f in sh[0]['facets'] if f['area_fraction'] > amin], key=lambda f: -f['area_fraction'])
        return [f['family'] for f in rows], 'on the level-%s shape %s (area > %g), largest first' % (
            lv, sh[0]['shape_id'], amin)
    raise SpecError('facets.select %r: explicit | bfdh_top | max_index | on_shape' % sel)


# ============================================================================================ cost
def engine_estimate(eng, family, level, n_periods):
    """The engine's calibrated CPU model for a bundle material, without its cache shortcut (a refresh with another
    protocol is a new run): (minutes, hkl, search_range, n_molecules_cell, basis)."""
    h, sr, cell = eng.pick_cell(family)
    f = eng.fam.get(family)
    lv = min(level, 2)
    if cell:
        n_cell, mnp = cell['n_molecules'], cell['mnp']
        basis = 'engine model, oriented cell of %d molecules (%s, search_range %s)' % (n_cell, cell['source'], sr)
    else:
        hs = sum(abs(x) for x in (h or [1, 1, 1]))
        n_cell, mnp = eng.sym['n_molecules'] * 2 * hs * hs, None
        basis = 'engine model, ROUGH: no oriented cell known (%d molecules assumed)' % n_cell
    prev = eng.ledger.best(family, 1)
    cal, ncal = eng._calibration(lv)
    cpu = eng._raw_cost(lv, n_cell, mnp, f['d_hkl'] if f else None, n_periods,
                        prev.get('n_terminations') if prev else None) * cal
    measured = [e for e in eng.ledger.entries if e['family'] == family and e['level'] == lv and e.get('ok', True)
                and e.get('cpu_s') and list(e['hkl']) == list(h or []) and e.get('search_range') == sr]
    if measured:
        m = max(float(e['cpu_s']) for e in measured)
        basis += '; measured %.0f s for the same cell before' % m
        cpu = max(cpu, m)
    return cpu / 60.0, h, sr, n_cell, basis + '; calibrated on %d level-%d run(s) (x%.2f)' % (ncal, lv, cal)


def surfenergy_estimates(rows):
    """surfenergy's own estimator (chain env) for a batch: rows [{id, cell, n_atoms, ...}] -> {id: estimate}."""
    if not rows or not chain_present():
        return {'_error': 'chain not present: ' + config.NO_CHAIN_MSG} if rows else {}
    d = os.path.join(STATE, 'campaigns', '_estimates')
    os.makedirs(d, exist_ok=True)
    tag = hashlib.sha1(json.dumps(rows, sort_keys=True).encode()).hexdigest()[:12]
    q, a = os.path.join(d, tag + '.in.json'), os.path.join(d, tag + '.out.json')
    if not os.path.isfile(a):
        json.dump(rows, open(q, 'w'))
        croot = chain_root()
        env = dict(os.environ, SIM2L_DEV_ROOT=croot, SIM2L_ENV_PREFIX=chain_env())
        env.pop('PYTHONPATH', None)
        p = subprocess.run([os.path.join(croot, 'devtools', 'sim2l-env'), 'python', os.path.join(HERE, 'chain_estimate.py'),
                            q, a], env=env, cwd=croot, capture_output=True, text=True, timeout=300)
        if not os.path.isfile(a):
            return {'_error': 'chain_estimate failed: ' + (p.stdout + p.stderr)[-500:]}
    out = json.load(open(a))
    if '_error' in out:
        os.remove(a)
    return out


def cell_settings(pr):
    """The oriented-cell settings a non-bundle job may use, in order: the protocol's (search_range, angle_dev), then
    each of protocol.cell_fallbacks merged over it. The first whose cell the estimator accepts (a cell exists, the
    ReaxFF memory rule passes, the estimate is within the per-run cap) is used."""
    base = {'search_range': int(pr.get('search_range') or SEARCH_RANGE_NONBUNDLE), 'angle_dev': pr.get('angle_dev')}
    out = [base]
    for fb in pr.get('cell_fallbacks') or []:
        st = dict(base, **{k: v for k, v in fb.items() if k in ('search_range', 'angle_dev')})
        st['search_range'] = int(st['search_range'])
        if st not in out:
            out.append(st)
    return out


def _setting_text(st):
    return 'search_range %d, angle_dev %s' % (st['search_range'], st['angle_dev'] if st.get('angle_dev') is not None
                                              else 'default (1)')


def choose_cell(j, est, cap):
    """Pick the cell setting and family member of a non-bundle job from the estimates est {job:si:mi: estimate}:
    settings in order, the member with the smallest estimated cell; a setting is passed over when it has no cell,
    the ReaxFF memory rule (slabcutter's) refuses its cell, or its estimate is over the per-run cap. Sets hkl,
    search_range, cell_overrides, cell_choice / cell_tried / cell_refused on j; returns the estimate used (or None)."""
    tried, first_over = [], None
    if not est:                                 # the estimator is not available: nothing to choose from
        j.update(hkl=j['members_considered'][0], search_range=j['_settings'][0]['search_range'], cell_overrides={})
        return None
    failed = j.get('failed_cells') or []
    n_failed_out = 0
    for si, st in enumerate(j['_settings']):
        cand = [(int(k.split(':')[2]), v) for k, v in est.items() if int(k.split(':')[1]) == si and 'seconds' in v]
        # a member whose cell at this setting failed before is passed over (try another family member)
        probe = lambda mi: {'hkl': j['members_considered'][mi], 'search_range': st['search_range'],
                            'cell_overrides': {'angle_dev': st.get('angle_dev')}}
        out = [mi for mi, v in cand if any(same_cell(c, probe(mi)) for c in failed)]
        if out:
            n_failed_out += len(out)
            tried.append('%s: (%s) failed before, passed over' % (_setting_text(st), '), ('.join(
                ' '.join(map(str, j['members_considered'][mi])) for mi in out)))
            cand = [(mi, v) for mi, v in cand if mi not in out]
            if not cand:
                continue
        refused = [(mi, v) for mi, v in cand if v.get('reaxff_refused')]
        okc = [(mi, v) for mi, v in cand if not v.get('reaxff_refused')]
        if not cand:
            errs = sorted({v.get('error', '')[:120] for k, v in est.items() if int(k.split(':')[1]) == si})
            tried.append('%s: no cell (%s)' % (_setting_text(st), '; '.join(errs) or 'no estimate'))
            continue
        if not okc:
            mi, v = min(refused, key=lambda x: (x[1]['molecules_cell'], x[0]))
            tried.append('%s: refused, ReaxFF memory: %s' % (_setting_text(st), v['reaxff_refused']))
            continue
        mi, v = min(okc, key=lambda x: (x[1]['molecules_cell'], x[0]))
        if v['seconds'] / 60.0 > cap:
            tried.append('%s: %d molecules, est. %.1f min > cap %.1f' % (_setting_text(st), v['molecules_cell'],
                                                                         v['seconds'] / 60.0, cap))
            first_over = first_over or (si, mi, v)
            continue
        _set_cell(j, si, mi, v, tried)
        return v
    j['cell_tried'] = tried
    if first_over:                              # over the cap at every setting: report the first (skipped later)
        si, mi, v = first_over
        _set_cell(j, si, mi, v, tried)
        return v
    j['hkl'] = j['members_considered'][0]
    j['search_range'] = j['_settings'][0]['search_range']
    j['cell_overrides'] = {}
    j['cell_refused'] = 'no usable oriented cell: ' + ' | '.join(tried)
    j['all_cells_failed'] = bool(n_failed_out)
    return None


def _set_cell(j, si, mi, v, tried):
    st = j['_settings'][si]
    j['hkl'] = j['members_considered'][mi]
    j['search_range'] = st['search_range']
    j['cell_overrides'] = {'angle_dev': st['angle_dev']} if st.get('angle_dev') is not None else {}
    j['cell_choice'] = 'cell: (%s) at %s%s' % (' '.join(map(str, j['hkl'])), _setting_text(st),
                                               ' (after: %s)' % ' | '.join(tried) if tried else '')
    j['cell_tried'] = tried


def failed_cell_of_event(ev):
    """The oriented cell of a failed journal event (None fields: not recorded, they match any value)."""
    return {'hkl': ev.get('hkl'), 'search_range': ev.get('search_range'), 'angle_dev': ev.get('angle_dev', None),
            'angle_dev_known': 'angle_dev' in ev, 'ref': 'journal %s' % ev.get('time')}


def failed_cells_of_event(ev):
    """All the cells a job has failed with: the executor lists them in the failed event (cells_failed)."""
    if ev.get('cells_failed'):
        return [dict(c, angle_dev_known=True, ref='journal %s' % ev.get('time')) for c in ev['cells_failed']]
    return [failed_cell_of_event(ev)]


def failed_cell_of_record(r):
    """The oriented cell of a failed dataset record; records carry no angle_dev, and every one so far used the tool
    default."""
    return {'hkl': (r.get('facet') or {}).get('hkl'), 'search_range': (r.get('protocol') or {}).get('search_range'),
            'angle_dev': None, 'angle_dev_known': True, 'ref': r.get('record_id')}


def same_cell(c, j):
    ad = (j.get('cell_overrides') or {}).get('angle_dev')
    if c.get('hkl') is not None and list(c['hkl']) != list(j['hkl']):
        return False
    if c.get('search_range') is not None and int(c['search_range']) != int(j['search_range']):
        return False
    if c.get('angle_dev_known') and (c.get('angle_dev') or 1.0) != (ad or 1.0):
        return False
    return True


def _est_row(mat, sym, jid, hkl, sr, level, T, spec, angle_dev=None):
    lp = sym['lattice_parameters']
    m = spec['md']
    extra = {}
    if mat.route.get('use_reaxff') and mat.route.get('script'):
        extra['script'] = mat.route['script']       # slabcutter's ReaxFF memory rule reads its pair_style line
    return dict(extra, **{'id': jid, 'cell': [lp[k] for k in ('a', 'b', 'c', 'alpha', 'beta', 'gamma')],
            'n_atoms': sym.get('n_atoms') or sym['n_molecules'] * sym['atoms_per_molecule'],
            'n_molecules': sym['n_molecules'], 'hkl': list(hkl), 'search_range': sr, 'level': level, 'temperature': T,
            'n_periods': spec['protocol']['n_periods'], 'slab_from': spec['protocol'].get('slab_from'),
            'kind': 'reaxff' if mat.route['use_reaxff'] else 'nonreactive',
            'equilibration_ps': m['equilibration_ps'], 'production_ps': m['production_ps'], 'n_blocks': m['n_blocks'],
            'timestep': m.get('timestep'),
            'angle_dev': angle_dev if angle_dev is not None else spec['protocol'].get('angle_dev'),
            'fire_max_steps_bulk': spec['protocol'].get('fire_max_steps_bulk'),
            'fire_max_steps_slab': spec['protocol'].get('fire_max_steps_slab')})


# ============================================================================================ journal
def campaign_dir(name):
    return os.path.join(STATE, 'campaigns', name)


def read_journal(name):
    """job id -> its last journal event (started / done / failed / skipped / recovered)."""
    p = os.path.join(campaign_dir(name), 'journal.jsonl')
    last = {}
    if os.path.isfile(p):
        for line in open(p):
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            last[ev['job_id']] = ev
    return last


# ============================================================================================ plan
def error_line(err, n=400):
    """The tool's own message in an error tail: the last 'SomethingError: ...' line (papermill re-raises the
    notebook's error there), else the last line naming an error or a failure, else the last line."""
    lines = [l.strip() for l in (err or '').splitlines() if l.strip()]
    exc = [l for l in lines if re.match(r'^[A-Za-z_.]*(Error|Exception|Timeout)[A-Za-z]*: \S', l)
           and not l.startswith('papermill.exceptions.PapermillExecutionError')]
    good = exc or [l for l in lines if any(w in l.lower() for w in ('error', 'fail', 'timeout', 'refused'))] or lines
    return (good[-1] if good else '')[:n]


def chain_revision():
    """'chain <short sha>' of the chain checkout, '+dirty' when its tools have uncommitted changes."""
    r = chain_root()
    try:
        sha = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=r, capture_output=True, text=True,
                             timeout=20).stdout.strip()
        if not sha:
            return None, []
        st = subprocess.run(['git', 'status', '--porcelain', '--', 'supercell', 'slabcutter', 'surfenergy', 'cif2lammps'],
                            cwd=r, capture_output=True, text=True, timeout=20).stdout.rstrip().splitlines()
        dirty = [l[3:] for l in st if l.strip() and not l[3:].startswith(('supercell/bin/RUNS', 'surfenergy/bin/RUNS'))]
        return 'chain %s%s' % (sha, '+dirty' if dirty else ''), dirty
    except Exception:
        return None, []


def make_context(spec, prepare_cif=None):
    if prepare_cif is None:
        from campaign_exec import prepare_cif
    return {'base': spec.get('_base_dir') or ROOT, 'zoo': load_zoo(), 'bundle_alias': _bundle_aliases(),
            'prepare_cif': prepare_cif}


def plan(spec, records=None, shapes=None, journal=None, estimator=None, ctx=None):
    """Expand, dedup, estimate, order and cut a campaign. Nothing is run (cif2lammps for a {cif: ...} material is
    the one exception: a few seconds, cached). records / shapes / journal / estimator: injectable for tests."""
    ctx = ctx or make_context(spec)
    records = load_records() if records is None else records
    shapes = load_shapes() if shapes is None else shapes
    journal = read_journal(spec['campaign']) if journal is None else journal
    estimator = estimator or surfenergy_estimates
    idx = DatasetIndex(records)
    pr = spec['protocol']
    cap = float(spec['budget']['per_run_cap_minutes'])
    budget = float(spec['budget']['cpu_minutes'])
    limit_new = spec['facets'].get('limit_new')
    mats, jobs, skipped, notes = [], [], [], []
    for entry in spec['materials']:
        mats.append(resolve_material(entry, spec, ctx))
    retry = spec['dedup'].get('retry_failed')
    deferred = []
    for mi, mat in enumerate(mats):
        fam_rec = mat.families()
        hows = []
        for level in spec['levels']:
            fs = spec['level_facets'].get(level) or spec['facets']
            try:
                fams, how, tiers = select_families_tiered(mat, fs, shapes)
            except ShapeNeeded as exc:
                deferred.append({'material': mat.key, 'material_id': mat.material_id, 'level': level, 'why': str(exc)})
                hows.append('L%d: waits for a shape (%s)' % (level, exc))
                continue
            hows.append('L%d: %s' % (level, how))
            for fi, fam in enumerate(fams):
                for T in ([0.0] if level < 3 else [t for t in spec['temperatures'] if t > 0]):
                    key = job_key(mat, fam, level, T, spec)
                    j = {'job_id': job_id(key), 'key': key, 'material': mat.key, 'material_id': mat.material_id,
                         'family': fam, 'level': level, 'level_name': LEVEL_NAME[level], 'temperature_K': T,
                         'bfdh_rank': (fam_rec.get(fam) or {}).get('bfdh_rank'), 'order': (mi, fi, level, T),
                         'near_duplicates': [], 'runnable': mat.route.get('runnable', True)}
                    if fam in tiers:
                        j['tier'], j['tier_why'] = tiers[fam]
                    if fs.get('target_ok'):
                        j['target_ok'] = int(fs['target_ok'])
                    ok, bad, near = idx.lookup(key, mat.route['ff_block'].get('md5'), mat.datafile_md5())
                    j['near_duplicates'] = [{'record_id': r['record_id'], 'gamma': r['result']['gamma'],
                                             'status': r['result']['status'], 'differs_in': d,
                                             'flags': [f['code'] for f in r['result']['gamma_flags']]} for r, d in near]
                    ev = journal.get(j['job_id'])
                    why = None
                    if ok:
                        why = ('in the dataset: %s' % ', '.join('%s (gamma %s, %s)' % (
                            r['record_id'], r['result']['gamma'], r['provenance']['source']) for r in ok[:4]))
                    elif ev and ev.get('event') == 'done':
                        why = 'done in this campaign (journal %s)' % ev.get('time')
                    elif retry == 'other_cell' and ((ev and ev.get('event') == 'failed') or bad):
                        # not retried blindly: only with another oriented cell than the one that failed (checked
                        # once the cell is chosen, below)
                        j['failed_cells'] = failed_cells_of_event(ev) if ev and ev.get('event') == 'failed' else []
                        j['failed_cells'] += [failed_cell_of_record(r) for r in bad]
                    elif ev and ev.get('event') == 'failed' and not retry:
                        why = 'failed in this campaign, not retried blindly: %s' % error_line(ev.get('error'), 300)
                    elif bad and not retry:
                        why = 'failed before (%s): %s; not retried blindly (change the protocol, or dedup.retry_failed)' % (
                            bad[0]['record_id'], error_line(bad[0]['result'].get('error_message'), 200))
                    if why:
                        mine = bool(ev and ev.get('event') in ('done', 'failed')) or any(
                            r['provenance'].get('source') == 'campaign' and str(r['provenance'].get('source_ref', ''))
                            .startswith('dataset/campaigns/%s.jsonl' % spec['campaign']) for r in ok + bad)
                        skipped.append(dict(j, reason='cached' if ok or (ev and ev.get('event') == 'done') else 'failed_before',
                                            why=why, this_campaign=mine))
                    else:
                        jobs.append(j)
        mat.selection = hows[0].split(': ', 1)[1] if len(hows) == 1 or (
            not spec['level_facets'] and not deferred) else '; '.join(hows)
    # ---------------------------------------------------------------- cell + cost
    rows, by_id = [], {}
    for j in jobs:
        mat = next(m for m in mats if m.key == j['material'])
        sym = mat.symmetry()
        if mat.bundle:
            em, h, sr, ncell, basis = engine_estimate(mat.engine(), j['family'], j['level'], pr['n_periods'])
            if pr.get('search_range'):
                sr = int(pr['search_range'])
            j.update({'hkl': h, 'search_range': sr, 'est_engine_minutes': round(em, 2), 'est_basis': basis,
                      'n_molecules_cell': ncell, 'cell_overrides': {}})
            rows.append(_est_row(mat, sym, j['job_id'] + ':0:0', h, sr, j['level'], j['temperature_K'], spec))
        else:
            members = (mat.families().get(j['family']) or {}).get('members') or \
                [[int(v) for v in j['family'].strip('{}').split()]]
            j['members_considered'] = members[:8]
            j['_settings'] = cell_settings(pr)
            for si, st in enumerate(j['_settings']):
                for i, h in enumerate(members[:8]):
                    rows.append(_est_row(mat, sym, '%s:%d:%d' % (j['job_id'], si, i), h, st['search_range'], j['level'],
                                         j['temperature_K'], spec, angle_dev=st.get('angle_dev')))
        by_id[j['job_id']] = j
    est = estimator(rows) if rows else {}
    if '_error' in est:
        notes.append('surfenergy estimator: ' + est['_error'])
    for j in jobs:
        mine = {k: v for k, v in est.items() if k.startswith(j['job_id'] + ':')}
        errs = [v['error'] for v in mine.values() if 'error' in v]
        if 'hkl' not in j:                       # non-bundle: the first cell setting that fits, smallest member
            v = choose_cell(j, mine, cap)
        else:
            v = next((x for x in mine.values() if 'seconds' in x), None)
        if v:
            j.update({'est_surfenergy_minutes': round(v['seconds'] / 60.0, 2),
                      'est_surfenergy_range_minutes': [round(v['low'] / 60.0, 2), round(v['high'] / 60.0, 2)],
                      'molecules_cell_est': v['molecules_cell'], 'molecules_slab_est': v['molecules_slab']})
            if v.get('reaxff_gb') is not None:
                j['reaxff_reservation_gb_est'] = v['reaxff_gb']
            j.setdefault('n_molecules_cell', v['molecules_cell'])
        if j.get('est_engine_minutes') is not None and j['level'] < 3:
            j['est_minutes'] = j['est_engine_minutes']
        elif v:
            j['est_minutes'] = j['est_surfenergy_minutes']
            j['est_basis'] = ('surfenergy estimator (its GUI model): oriented cell ~%d molecules, slab ~%d'
                              % (v['molecules_cell'], v['molecules_slab'])) + (
                '; %s' % j['cell_choice'] if j.get('cell_choice') else '')
        else:
            j['est_minutes'] = None
            j['est_basis'] = 'no estimate: %s' % (j.get('cell_refused') or '; '.join(errs)[:300] or
                                                 est.get('_error', 'estimator unavailable'))
        j.pop('members_considered', None)
        j.pop('_settings', None)
    # ---------------------------------------------------------------- recalibration from this campaign's runs
    ratios = {}
    for e in journal.values():
        b = e.get('est_base_minutes') or e.get('est_minutes')
        if e.get('event') == 'done' and e.get('cpu_s') and b:
            ratios.setdefault(e.get('level'), []).append(float(e['cpu_s']) / 60.0 / float(b))
    for j in jobs:
        r = sorted(ratios.get(j['level']) or [])
        f = r[len(r) // 2] if r else 1.0
        if j.get('est_minutes') is not None and f > 1.0:
            j['est_plan_minutes'] = j['est_minutes']
            j['est_minutes'] = round(j['est_minutes'] * f, 2)
            j['est_basis'] = (j.get('est_basis') or '') + '; x %.2f: median measured/estimated CPU of %d finished ' \
                'level-%d job(s) of this campaign' % (f, len(r), j['level'])
    # ---------------------------------------------------------------- per-run cap, limit_new, order, budget
    keep = []
    for j in jobs:
        same = [c for c in j.get('failed_cells') or [] if j.get('est_minutes') is not None and same_cell(c, j)]
        if same:
            skipped.append(dict(j, reason='failed_before', why='the same oriented cell (hkl %s, search_range %s, '
                                'angle_dev %s) failed before (%s): not retried; another cell setting would be' % (
                                    ' '.join(map(str, j['hkl'])), j['search_range'],
                                    (j.get('cell_overrides') or {}).get('angle_dev', 'default'), same[0].get('ref'))))
            continue
        if j.get('failed_cells'):
            j['retry_of_failed'] = [c.get('ref') for c in j['failed_cells']]
        if j['est_minutes'] is None and j.get('all_cells_failed'):
            skipped.append(dict(j, reason='failed_before', why='no cell left that has not failed before: ' + j['est_basis']))
        elif j['est_minutes'] is None:
            skipped.append(dict(j, reason='no_estimate', why=j['est_basis']))
        elif j['est_minutes'] > cap:
            skipped.append(dict(j, reason='over_cap', why='estimated %.1f CPU min > per-run cap %.1f' % (
                j['est_minutes'], cap)))
        else:
            keep.append(j)
    if limit_new:
        # jobs this campaign already ran count towards its limit (re-planning a finished campaign adds nothing)
        n_by = {}
        for x in skipped:
            if x.get('this_campaign'):
                k = (x['material'], x['level'], x['temperature_K'])
                n_by[k] = n_by.get(k, 0) + 1
        kept = []
        for j in sorted(keep, key=lambda j: j['order']):
            k = (j['material'], j['level'], j['temperature_K'])
            n_by[k] = n_by.get(k, 0) + 1
            if n_by[k] > int(limit_new):
                skipped.append(dict(j, reason='limit_new', why='beyond facets.limit_new = %d for this material/level'
                                    % int(limit_new)))
            else:
                kept.append(j)
        keep = kept
    # a wulff_priority level ranks on a shape built from the lower levels: while some of those still run (e.g. a
    # retry of a failed level-1 cell), its jobs wait for the next stage
    low = {}
    for j in keep:
        low.setdefault(j['material'], set()).add(j['level'])
    waiting = [j for j in keep if j.get('tier') is not None and any(lv < j['level'] for lv in low.get(j['material'], ()))]
    for mat_key, lv in sorted({(j['material'], j['level']) for j in waiting}):
        d0 = next(j for j in waiting if j['material'] == mat_key and j['level'] == lv)
        deferred.append({'material': mat_key, 'material_id': d0['material_id'], 'level': lv,
                         'why': 'its selection ranks on a shape that the level-%s jobs of this plan will change (%d '
                                'job(s) wait)' % ('/'.join(str(x) for x in sorted(low[mat_key]) if x < lv),
                                                  sum(1 for j in waiting if j['material'] == mat_key and j['level'] == lv))})
    keep = [j for j in keep if j not in waiting]
    keep = order_jobs(keep, spec['policy'], shapes)
    keep = apply_target_ok(keep, skipped)
    spent = sum(float(e.get('cpu_s') or 0) for e in journal.values() if e.get('event') in ('done', 'failed')) / 60.0
    total, run = spent, []
    if spent:
        notes.append('this campaign has spent %.1f CPU min already (journal): the budget left is %.1f'
                     % (spent, max(0.0, budget - spent)))
    for j in keep:
        if total + j['est_minutes'] > budget + 1e-9:
            skipped.append(dict(j, reason='over_budget', why='%.1f planned + %.1f > budget %.1f CPU min' % (
                total, j['est_minutes'], budget)))
            continue
        total += j['est_minutes']
        j['cum_est_minutes'] = round(total, 2)
        run.append(j)
    rev, dirty = chain_revision() if chain_present() else (None, [])
    return {'campaign': spec['campaign'], 'description': spec.get('description', ''), 'spec': spec.get('_source'),
            'created': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
            'policy': spec['policy'], 'backend': spec['executor']['backend'], 'workers': spec['executor']['workers'],
            'budget_cpu_min': budget, 'per_run_cap_min': cap,
            'chain': {'present': chain_present(), 'revision': rev, 'uncommitted': dirty[:10]},
            'materials': [dict(m.describe(), selection=getattr(m, 'selection', None)) for m in mats],
            'jobs': run, 'skipped': skipped, 'deferred': deferred, 'notes': notes,
            'totals': {'n_jobs': len(run), 'est_cpu_min': round(total - spent, 2), 'spent_cpu_min': round(spent, 2),
                       'n_skipped': len(skipped), 'n_deferred': len(deferred),
                       'skipped_by_reason': _count(s['reason'] for s in skipped),
                       'n_near_duplicates': sum(1 for j in run if j['near_duplicates'])},
            '_materials': mats}


def _count(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def order_jobs(jobs, policy, shapes):
    """exhaustive: the spec's order (materials, the facet selection, levels). baseline (greedy, as baseline.py):
    level 1 first in BFDH order, then the higher levels by relevance / cost, relevance = the family's area on the
    material's current shape (best, else level 1), else its BFDH rank. agent: the baseline order (the agent may
    reorder; run refuses)."""
    if policy == 'exhaustive':
        return sorted(jobs, key=lambda j: j['order'])
    area = {}
    for s in shapes:
        for f in s['facets']:
            k = (s['material_id'], f['family'])
            if s['level'] == 'best' or k not in area:
                area[k] = f['area_fraction']

    def rel(j):
        a = area.get((j['material_id'], j['family']))
        return a if a is not None else 1.0 / (1.0 + (j.get('bfdh_rank') or 50))

    for j in jobs:
        j['relevance'] = round(rel(j), 4)
        j['score'] = round((j['relevance'] + 1e-3) / max(j['est_minutes'], 0.05), 4)
    l1 = sorted([j for j in jobs if j['level'] == 1], key=lambda j: (j['bfdh_rank'] or 999, j['order']))
    # a wulff_priority selection sets tiers: 0 on the shape (by relevance / cost, as before), 1 within the
    # uncertainty, 2 the rest (both in BFDH order); jobs without a tier keep the plain greedy order
    hi = sorted([j for j in jobs if j['level'] > 1], key=lambda j: (
        j['level'], j.get('tier', 0), -j['score'] if j.get('tier', 0) == 0 else (j['bfdh_rank'] or 999), j['order']))
    return l1 + hi


def apply_target_ok(jobs, skipped):
    """facets.target_ok N: per material / level / T, run jobs (in the policy's order) only until N families of the
    selection have an ok value: the ones in the dataset or done in this campaign count, failed or skipped ones do
    not (so a refused cell makes room for the next family)."""
    have = {}
    for x in skipped:
        if x.get('target_ok') and x['reason'] == 'cached':
            k = (x['material'], x['level'], x['temperature_K'])
            have[k] = have.get(k, 0) + 1
    out = []
    for j in jobs:
        if not j.get('target_ok'):
            out.append(j)
            continue
        k = (j['material'], j['level'], j['temperature_K'])
        if have.get(k, 0) >= j['target_ok']:
            skipped.append(dict(j, reason='target_reached', why='facets.target_ok = %d families with a value reached '
                                '(dataset + this campaign + jobs planned before this one)' % j['target_ok']))
            continue
        have[k] = have.get(k, 0) + 1
        out.append(j)
    return out


# ============================================================================================ output
def public_plan(p):
    return {k: v for k, v in p.items() if not k.startswith('_')}


def format_plan(p):
    L = ['campaign %s: policy %s, backend %s, %d worker(s); budget %.1f CPU min, per-run cap %.1f min'
         % (p['campaign'], p['policy'], p['backend'], p['workers'], p['budget_cpu_min'], p['per_run_cap_min']),
         'chain: %s' % (p['chain']['revision'] or ('not present (replay only)' if not p['chain']['present'] else '?'))
         + (' (uncommitted: %s)' % ', '.join(p['chain']['uncommitted'][:4]) if p['chain']['uncommitted'] else '')]
    for m in p['materials']:
        L.append('  material %s -> %s (%s, route %s, %s); facets: %s' % (m['key'], m['material_id'], m['kind'], m['route'],
                                                                        m['force_field'], m.get('selection')))
        for n in m['notes']:
            L.append('    note: ' + n)
    L.append('')
    L.append('RUN %d job(s), est. %.1f CPU min:' % (p['totals']['n_jobs'], p['totals']['est_cpu_min']))
    L.append('  %-12s %-16s %-10s %-3s %-6s %-11s %-4s %7s %7s  %s' % ('job', 'material', 'family', 'L', 'T', 'hkl', 'sr',
                                                                   'est', 'cum', 'basis / near-duplicates'))
    for j in p['jobs']:
        nd = '; '.join('%s %s (%s differs)' % (d['record_id'], d['gamma'], ', '.join(d['differs_in']))
                       for d in j['near_duplicates'][:2])
        if j.get('tier') is not None:
            nd = (nd + '; ' if nd else '') + 'tier %d: %s' % (j['tier'], j.get('tier_why', ''))
        if j.get('retry_of_failed'):
            nd = (nd + '; ' if nd else '') + 'retry with another cell of failed %s' % ', '.join(map(str, j['retry_of_failed']))
        L.append('  %-12s %-16s %-10s %-3d %-6g %-11s %-4s %7.2f %7.2f  %s%s' % (
            j['job_id'], j['material_id'][:16], j['family'], j['level'], j['temperature_K'],
            ' '.join(str(x) for x in j.get('hkl') or []), j.get('search_range'), j['est_minutes'], j['cum_est_minutes'],
            (j.get('est_basis') or '')[:90] + (' [angle_dev %s]' % j['cell_overrides']['angle_dev']
                                               if (j.get('cell_overrides') or {}).get('angle_dev') is not None else ''),
            ('\n' + ' ' * 16 + ('refresh of: ' if j['near_duplicates'] else '') + nd) if nd else ''))
    L.append('')
    L.append('SKIPPED %d:' % len(p['skipped']))
    for s in p['skipped']:
        L.append('  %-12s %-16s %-10s L%d  %-13s %s' % (s['job_id'], s['material_id'][:16], s['family'], s['level'],
                                                       s['reason'], s['why'][:150]))
    for d in p.get('deferred') or []:
        L.append('DEFERRED %s L%d: %s; `run` re-plans after the ingest' % (d['material_id'], d['level'], d['why']))
    for n in p['notes']:
        L.append('note: ' + n)
    return '\n'.join(L)

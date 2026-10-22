"""wulffhound engine (wulff-dev env, Python 3.11 + pymatgen): candidates, cost, runs, Wulff shape, uncertainty.

Deterministic physics and bookkeeping only. The agent (or baseline.py) decides what to call; every gamma here
comes from a ledger entry with provenance (ledger.py), never from the caller.

    from engine import Engine
    eng = Engine('tnt')
    eng.candidates()                         # symmetry-distinct families, BFDH rank, ledger status, cost
    eng.estimate_cost('{0 1 1}', level=1)
    eng.run_facet('{0 0 1}', level=1)        # cached if identical, else a chain_runner subprocess
    w = eng.build_wulff('best')              # area fractions, off-shape margins, sensitivity, next facet
    eng.write_report()

Chain runs go to the chain's Sim2L env through <chain>/devtools/sim2l-env (see chain_runner.py, config.py);
without the chain (replay mode) only the ledger answers.
"""
import json
import math
import os
import subprocess
import sys
import time
import uuid
import warnings

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
from config import DATA, NO_CHAIN_MSG, chain_env, chain_present, chain_root, resolve  # noqa: E402
from ledger import Ledger, md5, now, rel  # noqa: E402
import symmetry  # noqa: E402


def load_materials():
    """data/<material>/material.json: name, route, force field (validated or not), Miller setting, the input files
    as 'chain:<path>' / 'data:<path>' references, and their recorded md5s (the replay key when the chain is absent)."""
    out = {}
    if os.path.isdir(DATA):
        for m in sorted(os.listdir(DATA)):
            p = os.path.join(DATA, m, 'material.json')
            if os.path.isfile(p):
                out[m] = json.load(open(p))
    return out


MATERIALS = load_materials()

DEFAULTS = {'search_range': 6, 'n_periods': 2, 'max_index': 3, 'n_candidates': 20}
SIGMA2_REL = 0.02     # level 2: T = 0 minimisation, thickness (README: 104.06-104.40 over 1-4 periods), protocol
SIGMA1_REL = 0.05     # level 1 used as a stand-in for level 2 (relative spread of the relaxation, default)
AREA_ON = 1e-4        # area fraction above which a family counts as on the shape
CONSISTENCY_TOL = 1.0  # mJ/m^2: a relaxed gamma may not exceed the unrelaxed one by more than this
CONSISTENCY_NOTE = ('deterministic guard, every material: a level-2 result whose relaxed gamma exceeds its unrelaxed '
                    'gamma by more than %.0f mJ/m^2 (a minimisation can only lower the energy), whose gamma is not '
                    'positive, or whose molecules are not intact, is marked inconsistent and left out of build_wulff by default (the level-1 value is '
                    'used instead, flagged) and out of the relaxed/unrelaxed ratio' % CONSISTENCY_TOL)


def consistency(e):
    """(ok, reason) of a ledger entry under the consistency guard (level 2 only; others are ok)."""
    if not e or e.get('level') != 2 or not e.get('ok', True) or e.get('gamma') is None:
        return True, None
    why = []
    gu = e.get('gamma_unrelaxed')
    if gu is not None and float(e['gamma']) > float(gu) + CONSISTENCY_TOL:
        why.append('relaxed gamma %.1f > unrelaxed gamma %.1f mJ/m^2 (a T = 0 minimisation cannot raise the surface '
                   'energy: the bulk and slab references are inconsistent)' % (float(e['gamma']), float(gu)))
    if float(e['gamma']) <= 0.0:
        why.append('relaxed gamma %.1f mJ/m^2 is not positive (non-physical: the bulk reference of this run is '
                   'off)' % float(e['gamma']))
    if e.get('molecules_intact') is False:
        why.append('molecules not intact after the relaxation')
    return (not why), ('; '.join(why) or None)


def _load_jsonl(p):
    out = []
    if os.path.isfile(p):
        for line in open(p):
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _err_line(err, n=200):
    """The most informative line of a tool error (the last one naming an error / failure)."""
    lines = [l.strip() for l in (err or '').splitlines() if l.strip()]
    good = [l for l in lines if any(w in l.lower() for w in ('error', 'fail', 'no solution', 'timeout', 'refused'))]
    return ((good or lines or [''])[-1])[:n]


class Engine:
    def __init__(self, material='tnt', state_dir=None, import_cached=True, session='agent', budget_min=None,
                 cached_only=False, live_runs=None, live_level=None, live_max_min=None, fresh_after=None):
        """session: whose decisions, literature, budget and report ('agent' -> state/<material>/, any other name
        -> state/<material>/<session>/). The ledger (the cache of runs) is shared by every session; a session
        only builds its shape from what it acquired itself (screen/relax calls, cached or live), and every
        acquisition is charged its recorded CPU time, so a replay from the cache costs the same budget as the
        live run did. budget_min: refuse runs beyond it (None: no budget). cached_only: never run live.
        live_runs / live_level / live_max_min: the live-run allowance of the session (at most live_runs live runs,
        each of level <= live_level and estimated <= live_max_min CPU min; None: no limit), enforced by the tool.
        fresh_after (ISO time): ledger entries made live by another session at or after this time are not 'cache'
        for this session: replaying one counts as one of its live runs (for a fair agent-vs-baseline comparison)."""
        if material not in MATERIALS:
            raise ValueError('unknown material %r (have: %s)' % (material, ', '.join(MATERIALS)))
        self.material = material
        self.mat = dict(MATERIALS[material])
        self.bundle = os.path.join(DATA, material)
        self.mat_notes = []
        for k in ('datafile', 'script', 'supplementary', 'example_dir'):
            self.mat[k + '_ref'] = self.mat.get(k)
            self.mat[k] = resolve(self.mat.get(k))
        # md5 of the inputs: measured when the file is here (live), else the recorded one (replay)
        for k in ('datafile', 'script'):
            rec = self.mat.get(k + '_md5_recorded')
            if self.mat[k] and os.path.isfile(self.mat[k]):
                self.mat[k + '_md5'] = md5(self.mat[k])
                if rec and rec != self.mat[k + '_md5']:
                    self.mat_notes.append('%s md5 %s differs from the bundle (%s): cached results do not apply to '
                                          'this file' % (k, self.mat[k + '_md5'], rec))
            else:
                self.mat[k + '_md5'] = rec
        self.chain_ok = chain_present() and all(
            self.mat[k] and os.path.exists(self.mat[k]) for k in ('datafile', 'script', 'supplementary', 'example_dir')
            if self.mat.get(k + '_ref'))
        self.mode = 'live' if self.chain_ok else 'replay'
        self.ledger = Ledger(material, state_dir)
        self.dir = self.ledger.dir
        self.session = session
        self.out = self.dir if session == 'agent' else os.path.join(self.dir, session)
        os.makedirs(self.out, exist_ok=True)
        self.budget_min = budget_min
        self.cached_only = cached_only
        self.live_runs, self.live_level, self.live_max_min = live_runs, live_level, live_max_min
        self.fresh_after = fresh_after
        self._acq_path = os.path.join(self.out, 'acquired.json')
        self.acquired = json.load(open(self._acq_path)) if os.path.isfile(self._acq_path) else []
        self.sym = self._symmetry()
        self.fam = {f['family']: f for f in self.sym['families']}
        self._member = {}
        for f in self.sym['families']:
            for m in f['members']:
                self._member[tuple(m)] = f['family']
        self.import_notes = []
        if import_cached:
            self.import_cached()
        self._bfdh = None

    # ------------------------------------------------------------------ material, symmetry, families
    def _symmetry(self):
        for p in (os.path.join(self.dir, 'cell_symmetry.json'), os.path.join(self.bundle, 'cell_symmetry.json')):
            if os.path.isfile(p):
                d = json.load(open(p))
                if d.get('datafile_md5') == self.mat['datafile_md5'] and d.get('max_index') == DEFAULTS['max_index']:
                    return d
        p = os.path.join(self.dir, 'cell_symmetry.json')
        if not (self.mat['datafile'] and os.path.isfile(self.mat['datafile'])):
            raise RuntimeError('no cell symmetry for %s: the bundle has none for data file md5 %s and the data file '
                               'is not here (%s)' % (self.material, self.mat['datafile_md5'], NO_CHAIN_MSG))
        d = symmetry.analyse_cell(self.mat['datafile'], DEFAULTS['max_index'])
        d['datafile'] = rel(d['datafile'])
        d['datafile_md5'] = self.mat['datafile_md5']
        with open(p, 'w') as f:
            json.dump(d, f, indent=1)
        return d

    def family_of(self, hkl):
        h = tuple(int(x) for x in hkl)
        g = math.gcd(math.gcd(abs(h[0]), abs(h[1])), abs(h[2]))
        if g == 0:
            raise ValueError('(0 0 0) is not a facet')
        h = tuple(x // g for x in h)
        if h in self._member:
            return self._member[h]
        # beyond max_index: orbit under the Laue class
        orbit = sorted({tuple(int(x) for x in np.array(h) @ np.array(M)) for M in self.sym['laue_ops_on_miller']},
                       reverse=True)
        rep = sorted(orbit, key=lambda x: (-(sum(1 for v in x if v >= 0)), [-v for v in x]))[0]
        return '{%s}' % ' '.join(str(v) for v in rep)

    def resolve(self, family=None, hkl=None):
        """family name or hkl -> (family, family record or None)."""
        if hkl is not None:
            family = self.family_of(hkl)
        if family is None:
            raise ValueError('give a family like "{0 1 1}" or an hkl like [0, 1, 1]')
        family = family.strip()
        if not family.startswith('{'):
            family = self.family_of([int(x) for x in family.replace(',', ' ').split()])
        return family, self.fam.get(family)

    def material_info(self):
        s = self.sym
        return {'material': self.material, 'name': self.mat['name'], 'route': self.mat['route'],
                'datafile': self.mat['datafile_ref'], 'datafile_md5': self.mat['datafile_md5'],
                'script': self.mat['script_ref'], 'script_md5': self.mat['script_md5'],
                'supplementary': self.mat.get('supplementary_ref'),
                'force_field': self.mat['force_field'],
                'forcefield_validated': bool(self.mat.get('forcefield_validated')),
                'forcefield_note': self.mat.get('forcefield_note'),
                'mode': self.mode, 'mode_note': ('live runs possible (chain at %s)' % chain_root()) if self.chain_ok
                else 'replay only: ' + NO_CHAIN_MSG,
                'consistency_guard': CONSISTENCY_NOTE,
                'formula_molecule': s['formula_molecule'], 'Z': s['n_molecules'],
                'atoms_per_molecule': s['atoms_per_molecule'],
                'lattice_parameters': {k: round(v, 4) for k, v in s['lattice_parameters'].items()},
                'spacegroup_data_setting': self.mat.get('spacegroup_data_setting'),
                'spacegroup_spglib': s['spacegroup'], 'laue_order': s['laue_order'],
                'centrosymmetric': s['has_inversion'],
                'systematic_absences_data_setting': s['systematic_absences_data_setting'],
                'miller_setting': self.mat['setting_note'],
                'setting_map': self.mat.get('setting_map'),
                'source_structure': self.mat.get('source_structure'),
                'levels': {'0': 'd_hkl / BFDH (free; 1/d_eff is not an energy)',
                           '1': 'unrelaxed gamma of every termination (screen_facet, ~0.5-3 min)',
                           '2': 'relaxed T = 0 gamma (relax_facet, ~2-10 min)'},
                'ledger': rel(self.ledger.path), 'n_ledger_entries': len(self.ledger.entries),
                'budget': self.budget(),
                'cpu_min_in_ledger': round(self.ledger.cpu_spent() / 60.0, 1),
                'import_notes': self.import_notes[:10]}

    def import_cached(self):
        """Merge the committed replay bundle (data/<material>/ledger.json) into the local ledger (the cache)."""
        p = os.path.join(self.bundle, 'ledger.json')
        n = 0
        if os.path.isfile(p):
            have = {(Ledger.key(e), tuple(e['hkl']), e.get('source'), e.get('run_dir')) for e in self.ledger.entries}
            new = [e for e in json.load(open(p)).get('entries', [])
                   if (Ledger.key(e), tuple(e['hkl']), e.get('source'), e.get('run_dir')) not in have]
            if new:
                self.ledger.entries.extend(new)
                self.ledger.save()
                n = len(new)
        self.import_notes = (['seeded %d entries from the replay bundle data/%s/ledger.json' % (n, self.material)]
                             if n else []) + self.mat_notes
        return n, []

    # ------------------------------------------------------------------ oriented-cell sizes (scout + ledger)
    def _cells(self, family):
        """Known oriented cells of a family: (hkl, search_range, n_molecules, mnp, ok, source, error)."""
        f = self.fam.get(family)
        members = {tuple(m) for m in f['members']} if f else set()
        out = []
        cells_p = os.path.join(self.bundle, 'cells.json')
        for r in (json.load(open(cells_p)) if os.path.isfile(cells_p) else []):
            if tuple(r.get('hkl', ())) in members:
                ok = r.get('rc') == 0 and bool(r.get('n_molecules'))
                err = None
                if not ok and r.get('error_tail'):
                    lines = [l for l in r['error_tail'].strip().splitlines() if l.strip()]
                    err = lines[-1][:300] if lines else None
                out.append({'hkl': r['hkl'], 'search_range': r.get('search_range'), 'n_molecules': r.get('n_molecules'),
                            'mnp': r.get('mnp_used'), 'ok': ok, 'source': 'spike supercell scout', 'error': err,
                            'cpu_s': r.get('cpu_s')})
        for e in self.ledger.entries:
            if e['family'] == family and e['level'] > 0:
                out.append({'hkl': e['hkl'], 'search_range': e.get('search_range'),
                            'n_molecules': e.get('n_molecules_cell'), 'mnp': e.get('mnp_used'),
                            'ok': bool(e.get('ok', True) and e.get('n_molecules_cell')), 'source': 'ledger',
                            'error': (e.get('error') or '')[-300:] or None})
        return out

    def pick_cell(self, family, hkl=None, search_range=None):
        """The member hkl and search_range to run: the smallest known working cell (smallest-first, as
        supercell's own pick), else the representative at the default search range."""
        cells = [c for c in self._cells(family) if c['ok'] and c['n_molecules']]
        if hkl is None and search_range is None:
            # a cell that already ran through the chain wins (its slabs are known to work)
            done = [c for c in cells if c['source'] == 'ledger']
            cells = done or cells
        if hkl is not None:
            cells = [c for c in cells if list(c['hkl']) == list(hkl)]
        if search_range is not None:
            cells = [c for c in cells if c['search_range'] == search_range]
        if cells:
            c = min(cells, key=lambda c: (c['n_molecules'], c['search_range']))
            return list(c['hkl']), c['search_range'], c
        f = self.fam.get(family)
        h = list(hkl) if hkl is not None else (f['representative'] if f else None)
        return h, search_range or DEFAULTS['search_range'], None

    # ------------------------------------------------------------------ cost
    def _calibration(self, level):
        """measured / predicted CPU of this level's live or imported runs in the ledger (median), 1 if none."""
        ratios = []
        for e in self.ledger.entries:
            if e['level'] == level and e.get('ok', True) and e.get('cpu_s') and e.get('n_molecules_cell'):
                pred = self._raw_cost(level, e['n_molecules_cell'], e.get('mnp_used'), e.get('d_hkl'),
                                      e.get('n_periods') or 2, e.get('n_terminations'))
                ratios.append(float(e['cpu_s']) / pred)
        return (float(np.median(ratios)), len(ratios)) if ratios else (1.0, 0)

    def _periods(self, mnp, d):
        if not mnp or not d:
            return None
        L = np.array(self.sym['lattice_rows_abc'])
        x1, x2, x3 = (np.dot(mnp[3 * i:3 * i + 3], L) for i in range(3))
        n = np.cross(x1, x2)
        n /= np.linalg.norm(n)
        return max(1, int(round(abs(np.dot(x3, n)) / d)))

    def _raw_cost(self, level, n_cell, mnp, d, n_periods, n_term=None):
        apm = self.sym['atoms_per_molecule']
        if level == 1:
            groups = max(1, (n_term or 6) // 2 + 1)
            return 20.0 + groups * 6.0 * (1.0 + n_cell * apm / 3000.0)
        periods = self._periods(mnp, d) or 2
        n_slab = n_periods * max(1, n_cell // periods)
        return 60.0 + 0.076 * apm * (n_cell + n_slab)

    def estimate_cost(self, family=None, level=1, hkl=None, search_range=None, n_periods=None):
        family, f = self.resolve(family, hkl)
        n_periods = n_periods or DEFAULTS['n_periods']
        if level == 0:
            return {'family': family, 'level': 0, 'est_minutes': 0.0, 'basis': 'geometry only'}
        h, sr, cell = self.pick_cell(family, hkl, search_range)
        cached = None
        if hkl is None and search_range is None:
            cached = self.ledger.lookup(level, family, None, None, n_periods, 0.0, self.mat['datafile_md5'],
                                        self.mat['script_md5'], include_failed=False)
        cached = cached or self.ledger.lookup(level, family, h, sr, n_periods, 0.0, self.mat['datafile_md5'],
                                              self.mat['script_md5'])
        out = {'family': family, 'level': level, 'hkl': h, 'search_range': sr, 'n_periods': n_periods}
        notes = []
        if cached and cached.get('ok', True):
            charge = cached.get('cpu_s')
            if charge is None:
                charge = 60.0 * self._estimate_uncached(family, level, cached)
            out.update({'cached': True, 'est_minutes': 0.0, 'charge_minutes': round(charge / 60.0, 2),
                        'basis': 'identical request in the ledger (no recompute); replaying it is charged its '
                                 'recorded CPU time to the session budget'})
            return out
        if cell:
            n_cell, mnp, basis = cell['n_molecules'], cell['mnp'], 'oriented cell %d molecules (%s, search_range %s)' % (
                cell['n_molecules'], cell['source'], cell['search_range'])
        else:
            hs = sum(abs(x) for x in (h or [1, 1, 1]))
            n_cell, mnp = self.sym['n_molecules'] * 2 * hs * hs, None
            basis = 'ROUGH: no oriented cell known for this family; assumed %d molecules (Z x 2 x (|h|+|k|+|l|)^2)' % n_cell
            notes.append('cell size unknown: the estimate can be off by 5x; a screen_facet run (level 1) measures it')
        bad = [c for c in self._cells(family) if not c['ok'] and (search_range is None or c['search_range'] == sr)
               and list(c['hkl']) == list(h or [])]
        if bad:
            notes.append('supercell failed before for %s at search_range %s: %s' % (h, bad[0]['search_range'],
                                                                                 bad[0]['error']))
        d = f['d_hkl'] if f else None
        n_term = None
        prev = self.ledger.best(family, 1)
        if prev:
            n_term = prev.get('n_terminations')
        cal, ncal = self._calibration(level)
        cpu = self._raw_cost(level, n_cell, mnp, d, n_periods, n_term) * cal
        out.update({'cached': False, 'est_minutes': round(cpu / 60.0, 1), 'n_molecules_cell': n_cell,
                    'basis': basis + '; model calibrated on %d measured level-%d run(s) (x%.2f)' % (ncal, level, cal),
                    'notes': notes})
        if cpu * 2 > 3600:
            out['notes'].append('large cell: consider a smaller search_range or another member hkl (see '
                                'list_candidate_facets cells)')
        return out

    # ------------------------------------------------------------------ runs
    def run_facet(self, family=None, level=1, hkl=None, search_range=None, n_periods=None, max_minutes=None,
                  timeout_s=None, force=False, dry=False):
        """Level-1 or level-2 run of one family. Identical requests come from the ledger (cached True).
        Refuses (no run) when the estimate exceeds max_minutes. Records failures with the tool's error."""
        family, f = self.resolve(family, hkl)
        n_periods = n_periods or DEFAULTS['n_periods']
        h, sr, cell = self.pick_cell(family, hkl, search_range)
        if h is None:
            return {'ok': False, 'family': family, 'error': 'no representative hkl for %s' % family}
        hit = None
        if hkl is None and search_range is None:
            # 'screen/relax this family' with no cell specified: any successful stored run of the family counts
            hit = self.ledger.lookup(level, family, None, None, n_periods, 0.0, self.mat['datafile_md5'],
                                     self.mat['script_md5'], include_failed=False)
        hit = hit or self.ledger.lookup(level, family, h, sr, n_periods, 0.0, self.mat['datafile_md5'],
                                        self.mat['script_md5'])
        if hit and not force:
            charge = hit.get('cpu_s')
            if charge is None:
                charge = 60.0 * self._estimate_uncached(family, level, hit)
            already = any(a['family'] == family and a['level'] == level for a in self.acquired)
            fresh = bool(self.fresh_after and str(hit.get('source', '')).startswith('live')
                         and (hit.get('recorded') or '') >= self.fresh_after)
            if fresh and not already:
                gate = self._live_gate(level, hit.get('estimate_minutes') if hit.get('estimate_minutes') is not None
                                       else charge / 60.0)
                if gate:
                    return {'ok': False, 'refused': True, 'family': family, 'level': level, 'budget': self.budget(),
                            'error': 'would be a live run for this session (the stored run was made by another '
                                     'session after %s): %s' % (self.fresh_after, gate)}
            if (not already and self.budget_min is not None
                    and self.spent_min() + charge / 60.0 > self.budget_min + 1e-9):
                return {'ok': False, 'refused': True, 'family': family, 'level': level, 'cached': True,
                        'budget': self.budget(), 'error': 'over budget: %.1f spent + %.1f (recorded CPU of the cached '
                        'run) > %.1f CPU min' % (self.spent_min(), charge / 60.0, self.budget_min)}
            self._acquire(family, level, hit, charge, True, as_live=fresh)
            out = self.summary(hit)
            if fresh:
                out['counted_as_live'] = True
            out['charged_cpu_min'] = round(charge / 60.0, 2)
            out['budget'] = self.budget()
            if not hit.get('ok', True):
                out['recovery_hints'] = self._hints(hit.get('error') or '', family, hit['hkl'], hit.get('search_range'))
            return out
        est = self.estimate_cost(family, level, h, sr, n_periods)
        if self.budget_min is not None and self.spent_min() + est['est_minutes'] > self.budget_min:
            return {'ok': False, 'refused': True, 'family': family, 'level': level, 'estimate': est,
                    'budget': self.budget(), 'error': 'over budget: %.1f spent + %.1f estimated > %.1f CPU min'
                    % (self.spent_min(), est['est_minutes'], self.budget_min)}
        if self.cached_only:
            dry = True
        if max_minutes is not None and est['est_minutes'] > max_minutes:
            return {'ok': False, 'refused': True, 'family': family, 'level': level, 'hkl': h, 'search_range': sr,
                    'estimate': est, 'error': 'estimated %.1f CPU min exceeds max_minutes %.1f: not run'
                    % (est['est_minutes'], max_minutes)}
        gate = self._live_gate(level, est['est_minutes'])
        if gate:
            return {'ok': False, 'refused': True, 'family': family, 'level': level, 'hkl': h, 'search_range': sr,
                    'estimate': est, 'budget': self.budget(), 'error': 'not run: ' + gate}
        if dry:
            return {'ok': False, 'dry': True, 'family': family, 'level': level, 'hkl': h, 'estimate': est,
                    'error': 'not in the ledger (offline/cached-only mode): would cost %.1f CPU min live'
                    % est['est_minutes']}
        if not self.chain_ok:
            return {'ok': False, 'refused': True, 'replay_only': True, 'family': family, 'level': level, 'hkl': h,
                    'estimate': est, 'budget': self.budget(),
                    'error': 'not in the ledger (replay mode, would cost %.1f CPU min live): %s'
                    % (est['est_minutes'], NO_CHAIN_MSG)}
        run_id = '%s-L%d-%s-%s' % (time.strftime('%Y%m%d-%H%M%S'), level, ''.join(str(x) for x in h).replace('-', 'm'),
                                    uuid.uuid4().hex[:6])
        work = os.path.join(self.dir, 'runs', run_id)
        os.makedirs(work, exist_ok=True)
        timeout_s = timeout_s or int(max(300, est['est_minutes'] * 60 * 3 + 120))
        req = {'level': level, 'hkl': h, 'search_range': sr, 'n_periods': n_periods, 'work_dir': work,
               'timeout_s': timeout_s, 'chain_root': chain_root(),
               'material': {k: self.mat.get(k) for k in ('datafile', 'script', 'supplementary', 'example_dir',
                                                         'use_reaxff')}}
        rq, rs = os.path.join(work, 'request.json'), os.path.join(work, 'result.json')
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
        except subprocess.TimeoutExpired:
            log = 'TIMEOUT after %d s' % (timeout_s + 60)
        open(os.path.join(work, 'runner.log'), 'w').write(log)
        if os.path.isfile(rs):
            res = json.load(open(rs))
        else:
            res = {'level': level, 'hkl': h, 'search_range': sr, 'n_periods': n_periods, 'ok': False,
                   'error': 'chain runner wrote no result: ' + log[-1500:], 'wall_s': round(time.time() - t0, 1)}
        res.setdefault('run_dir', work)
        if level == 1:
            res['run_dir'] = work
        res['run_dir'] = rel(res.get('run_dir'))
        entries = __import__('ledger').entries_from_result(res, family, self.mat, 'live')
        for e in entries:
            e['estimate_minutes'] = est['est_minutes']
            self.ledger.add(e)
        mine = [e for e in entries if e['level'] == level][0]
        self._acquire(family, level, mine, mine.get('cpu_s'), False)
        out = self.summary(dict(mine, cached=False))
        out['estimate_minutes'] = est['est_minutes']
        out['budget'] = self.budget()
        if not mine.get('ok', True):
            out['recovery_hints'] = self._hints(mine.get('error') or '', family, h, sr)
        return out

    def live_used(self):
        return sum(1 for a in self.acquired if a.get('live', not a.get('cached')) and not a.get('note'))

    def _live_gate(self, level, est_minutes):
        """Why a live run is not allowed under the session's live-run allowance (None: allowed)."""
        if self.live_runs is not None and self.live_used() >= self.live_runs:
            return 'live-run allowance used up (%d of %d live runs)' % (self.live_used(), self.live_runs)
        if self.live_level is not None and level > self.live_level:
            return 'live runs are limited to level <= %d in this session' % self.live_level
        if self.live_max_min is not None and (est_minutes or 0) > self.live_max_min:
            return 'estimate %.1f CPU min exceeds the live-run cap of %.1f min in this session' % (
                est_minutes or 0, self.live_max_min)
        return None

    def limits(self):
        return {'budget_cpu_min': self.budget_min, 'live_runs': self.live_runs, 'live_level': self.live_level,
                'live_max_min': self.live_max_min, 'cached_only': self.cached_only, 'fresh_after': self.fresh_after}

    def session_info(self):
        p = os.path.join(self.out, 'session.json')
        d = json.load(open(p)) if os.path.isfile(p) else {}
        if not d.get('started') and self.acquired:
            d['started'] = min(a['time'] for a in self.acquired)
        return d

    @classmethod
    def from_session(cls, material='tnt', session='agent'):
        """An engine with the budget and live-run allowance recorded in the session's session.json (for
        rebuilding a session's report outside the MCP server)."""
        e = cls(material, session=session)
        lim = e.session_info().get('limits') or {}
        e.budget_min, e.live_runs = lim.get('budget_cpu_min'), lim.get('live_runs')
        e.live_level, e.live_max_min = lim.get('live_level'), lim.get('live_max_min')
        e.cached_only = bool(lim.get('cached_only'))
        return e

    def note_session(self):
        """Record the session's start (if new) and its limits in session.json."""
        d = self.session_info()
        d.setdefault('started', now())
        d.update({'session': self.session, 'limits': self.limits()})
        json.dump(d, open(os.path.join(self.out, 'session.json'), 'w'), indent=1)
        return d

    def _estimate_uncached(self, family, level, entry):
        """CPU minutes a level-1 result derived from a level-2 run would have cost alone (for the charge)."""
        n = entry.get('n_molecules_cell') or self.sym['n_molecules'] * 2
        return self._raw_cost(level, n, entry.get('mnp_used'), entry.get('d_hkl'), entry.get('n_periods') or 2,
                              entry.get('n_terminations')) * self._calibration(level)[0] / 60.0

    def _hints(self, err, family, hkl, sr):
        hints = []
        e = err.lower()
        if 'gccm' in e or 'no solution' in e or 'solution' in e or 'orientation' in e:
            ok = [c for c in self._cells(family) if c['ok']]
            hints.append('supercell/GCCM found no usable cell for %s at search_range %s: retry with a larger '
                         'search_range (8, 10) or another member of the family%s'
                         % (hkl, sr, (' (known to work: %s)' % ', '.join('%s r%s %s mol' % (c['hkl'], c['search_range'],
                                                                             c['n_molecules']) for c in ok[:4])) if ok else ''))
        if 'timeout' in e:
            hints.append('the run timed out: the cell is probably too big; use a smaller search_range or skip it')
        if 'broken' in e or 'intact' in e:
            hints.append('molecules broke: check the cell (bond_tolerance) or skip this facet')
        if not hints:
            hints.append('read the error; a retry with the same inputs will fail the same way (runs are deterministic)')
        return hints

    def summary(self, e):
        """Compact view of a ledger entry for tools."""
        keys = ('family', 'hkl', 'level', 'ok', 'gamma', 'gamma_error', 'units', 'gamma_unrelaxed', 'termination_used',
                'n_terminations', 'faces_equivalent', 'molecules_intact', 'n_molecules_cell', 'n_molecules_slab',
                'search_range', 'n_periods', 'cpu_s', 'wall_s', 'run_dir', 'source', 'cached', 'same_family_member',
                'datafile_md5', 'script_md5', 'error', 'recorded')
        out = {k: e.get(k) for k in keys if e.get(k) is not None}
        if isinstance(out.get('gamma'), float):
            out['gamma'] = round(out['gamma'], 2)
        if e.get('terminations'):
            out['terminations'] = ['t%d %.1f%s%s' % (t['index'], t['sfe_unrelaxed'], '' if t.get('sfe_source') == 'computed'
                                                     else '*', '' if t.get('faces_equivalent') in (True, None) else ' (faces differ)')
                                   for t in e['terminations'] if t.get('sfe_unrelaxed') is not None][:12]
        notes = [n for n in e.get('notes') or [] if 'commands of the input script not used' not in n]
        if notes:
            out['notes'] = [n[:240] for n in notes[:6]]
        good, why = consistency(e)
        if not good:
            out['consistent'] = False
            out['inconsistent_reason'] = why
            out['used_in_wulff'] = 'no (level-1 value used instead, flagged); see material_info.consistency_guard'
        return out

    # ------------------------------------------------------------------ candidates
    def candidates(self, n=None, max_index=None):
        n = n or DEFAULTS['n_candidates']
        bfdh = self.bfdh()
        area_b = {r['family']: r['area_fraction'] for r in bfdh['facets']}
        out = []
        for f in self.sym['families']:
            if max_index and f['max_abs_index'] > max_index:
                continue
            fam = f['family']
            l1, l2 = self.have(fam, 1), self.have(fam, 2)
            c1, c2 = self.ledger.best(fam, 1), self.ledger.best(fam, 2)
            fails = [e for e in self.ledger.entries if e['family'] == fam and not e.get('ok', True)]
            h, sr, cell = self.pick_cell(fam)
            c = {'family': fam, 'hkl_to_run': h, 'search_range': sr, 'multiplicity': f['multiplicity'],
                 'd_hkl': round(f['d_hkl'], 3), 'extinction_factor': f['extinction_factor'], 'd_eff': round(f['d_eff'], 3),
                 'bfdh_rank': f['bfdh_rank'], 'bfdh_area': round(area_b.get(fam, 0.0), 4),
                 'n_molecules_cell': cell['n_molecules'] if cell else None,
                 'level_done': 2 if l2 else 1 if l1 else 0,
                 'gamma_L1': round(l1['gamma'], 2) if l1 else None, 'gamma_L2': round(l2['gamma'], 2) if l2 else None,
                 'in_cache': [lv for lv, e in ((1, c1), (2, c2)) if e]}
            good, why = consistency(l2 or c2)
            if not good:
                c['level2_inconsistent'] = why
            if fails:
                c['failures'] = ['L%d %s r%s: %s' % (x['level'], x['hkl'], x.get('search_range'), _err_line(x.get('error')))
                                 for x in fails[:2]]
            out.append(c)
            if len(out) >= n:
                break
        for c in out:
            nxt = min(c['level_done'] + 1, 2)
            if c['level_done'] < 2:
                c['est_minutes_next'] = self.estimate_cost(c['family'], nxt)['est_minutes']
        return out

    # ------------------------------------------------------------------ Wulff
    def _wulff(self, fams, gammas):
        from pymatgen.analysis.wulff import WulffShape
        from pymatgen.core import Lattice
        lat = Lattice(np.array(self.sym['lattice_rows_abc']))
        millers = [tuple(self.fam[f]['representative']) if f in self.fam else self._rep(f) for f in fams]
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            w = WulffShape(lat, millers, [float(g) for g in gammas])
        ad = w.area_fraction_dict
        return w, {f: float(ad.get(m, 0.0)) for f, m in zip(fams, millers)}

    @staticmethod
    def _rep(f):
        return tuple(int(x) for x in f.strip('{}').split())

    def closed(self, fams):
        """True when the symmetry-expanded normals enclose the origin (the Wulff shape is bounded)."""
        from scipy.spatial import ConvexHull
        recip = np.linalg.inv(np.array(self.sym['lattice_rows_abc'])).T
        normals = []
        for f in fams:
            for m in (self.fam[f]['members'] if f in self.fam else [self._rep(f)]):
                g = np.array(m) @ recip
                normals.append(g / np.linalg.norm(g))
        try:
            hull = ConvexHull(np.array(normals))
        except Exception:
            return False
        return bool(np.all(hull.equations[:, -1] < -1e-6))

    def checks(self):
        """The spike's convention checks: pymatgen keeps our a, b, c and its reciprocal-lattice symmetry gives
        the same orbits as our Laue class."""
        from pymatgen.core import Lattice
        L = np.array(self.sym['lattice_rows_abc'])
        lat = Lattice(L)
        ops = lat.get_recp_symmetry_operation(1e-5)
        laue = [np.array(m) for m in self.sym['laue_ops_on_miller']]
        ok = True
        for f in self.sym['families'][:30]:
            h = f['representative']
            ours = {tuple(int(x) for x in np.array(h) @ M) for M in laue}
            pm = {tuple(int(round(x)) for x in op.operate(h)) for op in ops}
            ok &= ours == pm
        return {'lattice_matrix_same': bool(np.allclose(lat.matrix, L)),
                'recip_same': bool(np.allclose(lat.reciprocal_lattice_crystallographic.matrix, np.linalg.inv(L).T)),
                'orbits_equal_laue_class': bool(ok), 'pymatgen_recip_ops_order': len(ops)}

    def bfdh(self):
        if self._bfdh is None:
            fams = [f['family'] for f in self.sym['families']]
            g = [1.0 / f['d_eff'] for f in self.sym['families']]
            w, area = self._wulff(fams, g)
            self._bfdh = {'level': 0, 'what': 'BFDH (Donnay-Harker): gamma proxy 1/d_eff, every family up to |index| %d'
                          % self.sym['max_index'],
                          'facets': sorted([{'family': f, 'proxy': round(x, 5), 'area_fraction': area[f]}
                                            for f, x in zip(fams, g)], key=lambda r: -r['area_fraction']),
                          'shape_factor': float(w.shape_factor), 'wulff': w}
        return self._bfdh

    def gamma_table(self, level, include_inconsistent=False):
        """family -> (gamma, sigma, level used, basis) for a Wulff build at level 1, 2 or 'best'.
        Level-2 results that fail the consistency guard are left out (unless include_inconsistent): the family
        falls back to its level-1 value, flagged in the basis."""
        pairs, bad = [], {}
        for fam in self.have_families():
            e2 = self.have(fam, 2)
            good, why = consistency(e2)
            if not good:
                bad[fam] = why
        for fam in self.have_families():
            e1, e2 = self.have(fam, 1), self.have(fam, 2)
            if e1 and e2 and e1['gamma'] and e2['gamma'] and fam not in bad:
                pairs.append(e2['gamma'] / e1['gamma'])
        ratio = float(np.mean(pairs)) if pairs else 1.0
        s1 = max(float(np.std(pairs)) if len(pairs) > 1 else 0.0, SIGMA1_REL - 0.02) + 0.02
        tab = {}
        for fam in self.have_families():
            e1, e2 = self.have(fam, 1), self.have(fam, 2)
            if fam in bad and not include_inconsistent:
                e2 = None
                if level == 2:
                    continue
            if level == 2 or (level == 'best' and e2):
                if e2:
                    g = e2['gamma']
                    tab[fam] = {'gamma': g, 'sigma': max(e2.get('gamma_error') or 0.0, SIGMA2_REL * g), 'level': 2,
                                'basis': 'relaxed T = 0 run', 'entry': e2}
            elif level == 1 or level == 'best':
                if e1:
                    g = e1['gamma']
                    if level == 'best' and pairs:
                        tab[fam] = {'gamma': g * ratio, 'sigma': s1 * g * ratio, 'level': 1,
                                    'basis': 'level-1 run x mean relaxed/unrelaxed ratio %.3f of %d relaxed facet(s)'
                                    % (ratio, len(pairs)), 'entry': e1, 'gamma_run': g}
                    else:
                        tab[fam] = {'gamma': g, 'sigma': s1 * g, 'level': 1, 'basis': 'unrelaxed run', 'entry': e1}
                    if fam in bad and not include_inconsistent:
                        tab[fam]['basis'] += ' [FLAG: level-2 result excluded as inconsistent: %s]' % bad[fam]
                        tab[fam]['inconsistent_level2'] = bad[fam]
        return tab, {'relax_ratio': ratio, 'n_pairs': len(pairs), 'sigma1_rel': s1, 'sigma2_rel': SIGMA2_REL,
                     'inconsistent_level2': bad, 'include_inconsistent': bool(include_inconsistent)}

    def build_wulff(self, level='best', n_samples=40, seed=0, n_candidates=None, n_next=8, include_inconsistent=False):
        if level in (0, '0'):
            b = self.bfdh()
            return {'level': 0, 'what': b['what'], 'shape_factor': b['shape_factor'],
                    'on_shape': [{'family': r['family'], 'area_fraction': round(r['area_fraction'], 4)}
                                 for r in b['facets'] if r['area_fraction'] > AREA_ON], 'checks': self.checks()}
        level = level if level == 'best' else int(level)
        tab, meta = self.gamma_table(level, include_inconsistent)
        fams = sorted(tab, key=lambda f: self.fam[f]['bfdh_rank'] if f in self.fam else 999)
        res = {'level': level, 'built': now(), 'n_families': len(fams), 'meta': meta, 'warnings': [],
               'forcefield_validated': bool(self.mat.get('forcefield_validated'))}
        if meta['inconsistent_level2']:
            res['inconsistent'] = [{'family': f, 'reason': why, 'used': 'level-1 value instead (flagged)'
                                    if not include_inconsistent else 'level-2 value (include_inconsistent=True)'}
                                   for f, why in sorted(meta['inconsistent_level2'].items())]
        rej = [x for x in self.rejected_level2() if x['family'] not in meta['inconsistent_level2']]
        if rej:
            res['rejected_level2_runs'] = rej
        if not self.mat.get('forcefield_validated'):
            res['warnings'].append('force field not validated for %s: %s' % (self.material,
                                                                             self.mat.get('forcefield_note') or ''))
        if len(fams) < 2 or not self.closed(fams):
            res.update({'ok': False, 'error': 'shape not closed: the computed families %s do not enclose a finite '
                        'crystal (their symmetry-expanded normals must surround the origin). Compute a family with '
                        'a normal out of the plane of the others, e.g. one with k != 0 such as {0 1 1}, {1 1 0} or '
                        '{1 1 -1}.' % fams, 'families': fams})
            res['next'] = self.next_facets(None, tab, n_candidates, n_next)
            self._last = res
            return res
        g = np.array([tab[f]['gamma'] for f in fams])
        sig = np.array([tab[f]['sigma'] for f in fams])
        w, area = self._wulff(fams, g)
        # sensitivity: resample every gamma within its sigma (normal), recompute the area fractions
        rng = np.random.default_rng(seed)
        samples = []
        for _ in range(n_samples):
            gs = g + sig * rng.standard_normal(len(g))
            gs = np.maximum(gs, 0.2 * g)
            try:
                samples.append([v for v in self._wulff(fams, gs)[1].values()])
            except Exception:
                pass
        S = np.array(samples) if samples else np.zeros((1, len(fams)))
        rows = []
        for i, f in enumerate(fams):
            t = tab[f]
            r = {'family': f, 'gamma': round(float(t['gamma']), 2), 'sigma': round(float(t['sigma']), 2),
                 'level': t['level'], 'basis': t['basis'], 'area_fraction': round(area[f], 4),
                 'on_shape': area[f] > AREA_ON, 'area_mean': round(float(S[:, i].mean()), 4),
                 'area_std': round(float(S[:, i].std()), 4), 'p_on_shape': round(float((S[:, i] > AREA_ON).mean()), 2),
                 'run_dir': t['entry'].get('run_dir'), 'cached_source': t['entry'].get('source'),
                 'faces_equivalent': t['entry'].get('faces_equivalent'), 'bfdh_rank': self.fam[f]['bfdh_rank']
                 if f in self.fam else None}
            if 'gamma_run' in t:
                r['gamma_run'] = round(t['gamma_run'], 2)
            if t.get('inconsistent_level2'):
                r['flag'] = 'level-2 result inconsistent (excluded): ' + t['inconsistent_level2']
            if r['faces_equivalent'] is False:
                res['warnings'].append('%s: top and bottom faces of the slab differ; its gamma is the average of two '
                                       'faces (Wulff needs one per face; centrosymmetric crystal, so the pair is '
                                       'one +-(hkl) family)' % f)
            if not r['on_shape']:
                r.update(self._margin(fams, g, i, t['sigma']))
            rows.append(r)
        rows.sort(key=lambda r: -r['area_fraction'])
        res.update({'ok': True, 'facets': rows, 'on_shape': [r['family'] for r in rows if r['on_shape']],
                    'families_in_build': fams, 'gamma_in_build': {f: float(x) for f, x in zip(fams, g)},
                    'shape_factor': round(float(w.shape_factor), 4), 'anisotropy': round(float(w.anisotropy), 4),
                    'weighted_surface_energy': round(float(w.weighted_surface_energy), 2),
                    'n_samples': len(samples), 'max_area_std': round(float(S.std(axis=0).max()), 4),
                    'checks': self.checks()})
        res['next'] = self.next_facets(res, tab, n_candidates, n_next)
        res['convergence'] = self.convergence(res, tab)
        self._last = res
        self._last_w = w
        return res

    def _margin(self, fams, g, i, sigma):
        """For an off-shape family: the smallest relative gamma reduction that puts it on the shape (bisection)."""
        lo, hi = 0.0, 0.9
        gg = g.copy()
        gg[i] = g[i] * (1 - hi)
        if self._wulff(fams, gg)[1][fams[i]] <= AREA_ON:
            return {'reduction_to_appear': '>90%', 'within_sigma': False}
        for _ in range(14):
            mid = 0.5 * (lo + hi)
            gg[i] = g[i] * (1 - mid)
            if self._wulff(fams, gg)[1][fams[i]] > AREA_ON:
                hi = mid
            else:
                lo = mid
        red = hi
        return {'reduction_to_appear': '%.1f%%' % (100 * red), 'reduction_to_appear_mJ': round(float(g[i] * red), 2),
                'within_sigma': bool(g[i] * red <= sigma), 'within_2sigma': bool(g[i] * red <= 2 * sigma)}

    def next_facets(self, res, tab, n_candidates=None, top=6):
        """Score = relevance x information gain / cost (minutes) for the next run of each candidate family.
        relevance: on the current shape -> 0.5 + p_on_shape; off the shape -> exp(-(margin/sigma)^2 / 2);
        not computed -> BFDH prior (0.5 + BFDH area if on the BFDH shape, else 0.3 exp(-(rank - 1)/8)).
        information gain: level 0 -> 1 (unknown gamma); level 1 -> 0.4 + min(0.6, 10 x area_std); level 2 -> 0."""
        n = n_candidates or DEFAULTS['n_candidates']
        rows = {r['family']: r for r in (res or {}).get('facets', [])}
        area_b = {r['family']: r['area_fraction'] for r in self.bfdh()['facets']}
        out = []
        for f in self.sym['families'][:n]:
            fam = f['family']
            lvl = 2 if self.have(fam, 2) else 1 if self.have(fam, 1) else 0
            if lvl == 2:
                continue
            r = rows.get(fam)
            if r is not None:
                if r['on_shape']:
                    rel_ = 0.5 + r['p_on_shape']
                else:
                    m = r.get('reduction_to_appear_mJ')
                    rel_ = math.exp(-0.5 * (m / r['sigma']) ** 2) if m is not None and r['sigma'] > 0 else 0.0
                    rel_ = max(rel_, r['p_on_shape'])
                gain = 0.4 + min(0.6, 10 * r['area_std'])
                why = ('on the shape (area %.3f, p_on %.2f)' % (r['area_fraction'], r['p_on_shape']) if r['on_shape']
                       else 'off the shape, needs -%s to appear (sigma %.1f)' % (r.get('reduction_to_appear'), r['sigma']))
            elif lvl == 1:
                rel_, gain, why = 0.3, 0.5, 'screened, not in this build'
            else:
                a = area_b.get(fam, 0.0)
                rel_ = 0.5 + a if a > AREA_ON else 0.3 * math.exp(-(f['bfdh_rank'] - 1) / 8.0)
                gain = 1.0
                why = 'not computed; BFDH rank %d%s' % (f['bfdh_rank'], ', on the BFDH shape (area %.3f)' % a
                                                         if a > AREA_ON else '')
            est = self.estimate_cost(fam, lvl + 1)
            # the budget cost: a cached result is charged its recorded CPU, so the policy is the same with or
            # without the cache
            charge = est.get('charge_minutes', est['est_minutes'])
            mins = max(charge, 0.2)
            out.append({'family': fam, 'next_level': lvl + 1, 'score': round(rel_ * gain / mins, 4),
                        'relevance': round(rel_, 3), 'gain': round(gain, 2), 'est_minutes': round(charge, 2),
                        'cached': bool(est.get('cached')),
                        'why': why, 'cost_basis': 'ROUGH' if est.get('basis', '').startswith('ROUGH') else 'measured cell'})
        out.sort(key=lambda x: -x['score'])
        return out[:top]

    def convergence(self, res, tab):
        """Stop rule: every on-shape family at level 2, area fractions stable (max std < 0.02), no off-shape
        family within 1 sigma of appearing below level 2, every family on the BFDH shape computed."""
        reasons = []
        bad = res.get('meta', {}).get('inconsistent_level2') or {}
        for r in res['facets']:
            if r['on_shape'] and r['level'] < 2:
                reasons.append('%s on the shape but only level %d%s' % (
                    r['family'], r['level'], ' (its level-2 result is inconsistent: not settled by this protocol)'
                    if r['family'] in bad else ''))
            if not r['on_shape'] and r.get('within_sigma') and r['level'] < 2:
                reasons.append('%s off the shape but within 1 sigma of appearing (level %d)' % (r['family'], r['level']))
        if res['max_area_std'] >= 0.02:
            worst = max(res['facets'], key=lambda r: r['area_std'])
            reasons.append('area fractions not stable: %s area std %.3f' % (worst['family'], worst['area_std']))
        for r in self.bfdh()['facets']:
            if r['area_fraction'] > AREA_ON and r['family'] not in tab:
                reasons.append('%s is on the BFDH shape but not computed' % r['family'])
        return {'converged': not reasons, 'open': reasons}

    # ------------------------------------------------------------------ decisions, literature, report
    def _jsonl(self, name):
        return os.path.join(self.out, name)

    # ------------------------------------------------------------------ session: acquisitions and budget
    def reset_session(self):
        for n in ('acquired.json', 'decisions.jsonl', 'literature.jsonl', 'session.json'):
            p = os.path.join(self.out, n)
            if os.path.isfile(p):
                os.remove(p)
        self.acquired = []
        json.dump({'session': self.session, 'started': now(), 'limits': self.limits()},
                  open(os.path.join(self.out, 'session.json'), 'w'), indent=1)

    def _acquire(self, family, level, entry, charge_s, cached, as_live=False):
        if any(a['family'] == family and a['level'] == level for a in self.acquired):
            return 0.0
        self.acquired.append({'family': family, 'level': level, 'charge_cpu_s': round(float(charge_s or 0), 1),
                              'cached': bool(cached), 'live': (not cached) or bool(as_live),
                              'ok': bool(entry.get('ok', True)), 'hkl': entry.get('hkl'),
                              'run_dir': entry.get('run_dir'), 'time': now()})
        if level == 2 and entry.get('ok', True) and self.ledger.best(family, 1):
            # the level-2 run screened every termination first: level 1 comes with it
            if not any(a['family'] == family and a['level'] == 1 for a in self.acquired):
                e1 = self.ledger.best(family, 1)
                self.acquired.append({'family': family, 'level': 1, 'charge_cpu_s': 0.0, 'cached': bool(cached),
                                      'live': False,
                                      'ok': True, 'hkl': e1.get('hkl'), 'run_dir': e1.get('run_dir'), 'time': now(),
                                      'note': 'screening step of the level-2 run'})
        json.dump(self.acquired, open(self._acq_path, 'w'), indent=1)
        return float(charge_s or 0)

    def spent_min(self):
        return sum(a['charge_cpu_s'] for a in self.acquired) / 60.0

    def budget(self):
        b = {'session': self.session, 'spent_cpu_min': round(self.spent_min(), 2),
             'n_acquired': len(self.acquired)}
        if self.budget_min is not None:
            b.update({'budget_cpu_min': self.budget_min, 'remaining_cpu_min': round(self.budget_min - self.spent_min(), 2)})
        lv = sum(a['charge_cpu_s'] for a in self.acquired if a.get('live', not a.get('cached'))) / 60.0
        b.update({'live_cpu_min': round(lv, 2), 'cached_replay_cpu_min': round(self.spent_min() - lv, 2),
                  'live_runs_used': self.live_used()})
        if self.live_runs is not None:
            b['live_runs_allowed'] = self.live_runs
        if self.live_level is not None:
            b['live_level_max'] = self.live_level
        if self.live_max_min is not None:
            b['live_max_min_per_run'] = self.live_max_min
        if self.cached_only:
            b['cached_only'] = True
        return b

    def have(self, family, level):
        """The ledger entry of a family at a level that THIS session acquired (None if not acquired): the best
        entry (ledger.best), except that at level 2 a consistent entry wins over one the guard rejects."""
        if not any(a['family'] == family and a['level'] == level and a['ok'] for a in self.acquired):
            return None
        best = self.ledger.best(family, level)
        if level == 2 and best is not None and not consistency(best)[0]:
            good = [e for e in self.superseded(family) if consistency(e)[0]]
            if good:
                return good[0]
        return best

    def superseded(self, family, level=2):
        """All successful entries of a family at a level, in ledger.best order."""
        c = [e for e in self.ledger.entries if e['family'] == family and e['level'] == level and e.get('ok', True)
             and e.get('gamma') is not None]
        c.sort(key=lambda e: (e.get('n_periods') or 0, e.get('recorded', '')), reverse=True)
        return c

    def rejected_level2(self):
        """Level-2 ledger entries of this session's relaxed families that the guard rejects (used or not)."""
        out = []
        for fam in self.have_families():
            if not any(a['family'] == fam and a['level'] == 2 and a['ok'] for a in self.acquired):
                continue
            used = self.have(fam, 2)
            for e in self.superseded(fam):
                good, why = consistency(e)
                if not good:
                    out.append({'family': fam, 'hkl': e['hkl'], 'n_periods': e.get('n_periods'),
                                'gamma': round(float(e['gamma']), 2), 'gamma_unrelaxed': e.get('gamma_unrelaxed') and
                                round(float(e['gamma_unrelaxed']), 2), 'run_dir': e.get('run_dir'), 'reason': why,
                                'used': 'no: level-1 value used instead (flagged)' if e is used else
                                'no: a consistent level-2 run of the family is used (n_periods %s, gamma %.2f)' % (
                                    used.get('n_periods'), used['gamma']) if used is not None and consistency(used)[0]
                                else 'no'})
        return out

    def have_families(self):
        return sorted({a['family'] for a in self.acquired if a['ok']})

    def log_decision(self, action, why, expected_cost_minutes=None, actor='agent', outcome=None):
        e = {'time': now(), 'actor': actor, 'action': action, 'why': why, 'expected_cost_minutes': expected_cost_minutes}
        if outcome:
            e['outcome'] = outcome
        with open(self._jsonl('decisions.jsonl'), 'a') as f:
            f.write(json.dumps(e) + '\n')
        return e

    def add_literature(self, citation, claim, doi_or_url='', hkl_reported=None, setting_conversion='', hkl_our_setting=None,
                       kind='habit', value_reported=None, actor='agent'):
        e = {'time': now(), 'actor': actor, 'citation': citation, 'doi_or_url': doi_or_url, 'claim': claim, 'kind': kind,
             'hkl_reported': hkl_reported, 'setting_conversion': setting_conversion, 'hkl_our_setting': hkl_our_setting,
             'value_reported': value_reported}
        if hkl_our_setting:
            try:
                e['family_our_setting'] = self.family_of(hkl_our_setting)
            except Exception:
                pass
        with open(self._jsonl('literature.jsonl'), 'a') as f:
            f.write(json.dumps(e) + '\n')
        return e

    def decisions(self):
        return _load_jsonl(self._jsonl('decisions.jsonl'))

    def literature(self):
        return _load_jsonl(self._jsonl('literature.jsonl'))

    def write_report(self, path=None, title=None, level='best'):
        import report
        res = self.build_wulff(level) if level != 0 else None
        path = path or os.path.join(self.out, 'report.html')
        return report.write(self, res, path, title)

"""wulffhound ledger: one JSON file per material (state/<material>/ledger.json) of facet results with provenance.
The committed replay bundle data/<material>/ledger.json seeds it (see engine.Engine.import_cached).

Levels (the cost ladder):
  0  d_hkl / BFDH: geometry only, value = 1/d_eff (1/angstrom, NOT an energy), no run
  1  unrelaxed surface energy of every termination (supercell + slabcutter screening), mJ/m^2
  2  relaxed T = 0 surface energy (surfenergy, temperature 0, lowest unrelaxed termination), mJ/m^2

Every level-1/2 entry carries: run_dir (where the runs are), inputs, md5 of the unit-cell data file and the
force-field script, cpu_s / wall_s, the tools' notes, its source ('live' or 'import:<file>') and, on lookup, a
cached flag. A failed run is recorded too (ok False, error), so a retry with the same inputs is a decision.

Identical request = same material, family, hkl, level, search_range, n_periods, temperature and data/script md5.
A request for another member of the same family (e.g. -1 0 0 for {1 0 0}) is answered from the stored member.

    from ledger import Ledger
    led = Ledger('tnt'); led.lookup(level=2, family='{1 0 0}', hkl=[1, 0, 0], search_range=6, n_periods=2)
"""
import datetime
import glob
import hashlib
import json
import os

import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import STATE, chain_root, portable  # noqa: E402


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def now():
    return datetime.datetime.now().astimezone().isoformat(timespec='seconds')


def rel(path):
    """A path as a portable provenance string (config.portable): never a machine-specific absolute path."""
    return portable(path)


class Ledger:
    def __init__(self, material, state_dir=None):
        self.material = material
        self.dir = os.path.join(state_dir or STATE, material)
        os.makedirs(self.dir, exist_ok=True)
        self.path = os.path.join(self.dir, 'ledger.json')
        if os.path.isfile(self.path):
            with open(self.path) as f:
                self.data = json.load(f)
        else:
            self.data = {'material': material, 'created': now(), 'entries': []}

    # ------------------------------------------------------------------ storage
    def save(self):
        tmp = self.path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self.data, f, indent=1, default=str)
        os.replace(tmp, self.path)

    @property
    def entries(self):
        return self.data['entries']

    @staticmethod
    def key(e):
        return (e['family'], e['level'], e.get('search_range'), e.get('n_periods'), float(e.get('temperature', 0.0)),
                e.get('datafile_md5'), e.get('script_md5'))

    def add(self, entry):
        """Add (or replace the same-key, same-hkl) entry. Returns the stored entry."""
        entry = dict(entry)
        entry.setdefault('recorded', now())
        # another process (the MCP server, baseline.py) may have added entries since we loaded: merge first
        if os.path.isfile(self.path):
            try:
                with open(self.path) as f:
                    disk = json.load(f)
                mine = {json.dumps(e, sort_keys=True, default=str) for e in self.entries}
                for e in disk.get('entries', []):
                    if json.dumps(e, sort_keys=True, default=str) not in mine:
                        self.entries.append(e)
            except (OSError, ValueError):
                pass
        entry['material'] = self.material
        k = self.key(entry)
        self.data['entries'] = [e for e in self.entries
                                if not (self.key(e) == k and list(e['hkl']) == list(entry['hkl']))]
        self.entries.append(entry)
        self.save()
        return entry

    # ------------------------------------------------------------------ queries
    def lookup(self, level, family, hkl=None, search_range=None, n_periods=None, temperature=0.0,
               datafile_md5=None, script_md5=None, include_failed=True):
        """The stored entry for an identical request (flagged cached=True), else None. A stored entry of another
        member of the same family counts (same_family_member=True). Successful entries win over failures."""
        hits = []
        for e in self.entries:
            if e['family'] != family or e['level'] != level:
                continue
            if level > 0:
                if search_range is not None and e.get('search_range') != search_range:
                    continue
                if n_periods is not None and e.get('n_periods') != n_periods:
                    continue
                if float(e.get('temperature', 0.0)) != float(temperature):
                    continue
                if datafile_md5 and e.get('datafile_md5') != datafile_md5:
                    continue
                if script_md5 and e.get('script_md5') != script_md5:
                    continue
            if not e.get('ok', True) and not include_failed:
                continue
            hits.append(e)
        if not hits:
            return None
        exact = [e for e in hits if hkl is None or list(e['hkl']) == list(hkl)]
        pool = exact or hits
        pool.sort(key=lambda e: (not e.get('ok', True), e.get('recorded', '')))
        out = dict(pool[0])
        out['cached'] = True
        out['same_family_member'] = not exact
        return out

    def best(self, family, level):
        """Best successful entry of a family at a level, any inputs (thicker slab / later run first)."""
        c = [e for e in self.entries if e['family'] == family and e['level'] == level and e.get('ok', True)
             and e.get('gamma') is not None]
        if not c:
            return None
        c.sort(key=lambda e: (e.get('n_periods') or 0, e.get('recorded', '')), reverse=True)
        return c[0]

    def families(self, level=None, ok=True):
        return sorted({e['family'] for e in self.entries
                       if (level is None or e['level'] == level) and (not ok or e.get('ok', True))})

    def cpu_spent(self):
        return sum(float(e.get('cpu_s') or 0) for e in self.entries if e['level'] > 0)


# ---------------------------------------------------------------------- entries from runs
def entries_from_result(res, family, mat, source):
    """Ledger entries from one chain_runner result (or an imported spike run in the same shape).

    A level-2 surfenergy run also screened every termination: its screening becomes a level-1 entry with the
    same run_dir (source '<source> (screening step)')."""
    base = {'family': family, 'hkl': list(res['hkl']), 'search_range': res.get('search_range'),
            'n_periods': res.get('n_periods'), 'temperature': 0.0, 'datafile': rel(mat['datafile']),
            'datafile_md5': mat['datafile_md5'], 'script': rel(mat['script']), 'script_md5': mat['script_md5'],
            'source': source, 'cpu_s': res.get('cpu_s'), 'wall_s': res.get('wall_s'),
            'run_dir': rel(res.get('run_dir')), 'child_runs': [dict(c, run_dir=rel(c.get('run_dir')))
                                                               for c in res.get('child_runs') or []]}
    if not res.get('ok'):
        return [dict(base, level=res['level'], ok=False, gamma=None, error=res.get('error'))]
    o = res['outputs']
    notes = [n for n in (o.get('notes') or '').splitlines() if n.strip()]
    term = [{k: t.get(k) for k in ('index', 'group', 'gap', 'faces_equivalent', 'sfe_unrelaxed', 'sfe_source')}
            for t in o.get('terminations_screened') or []]
    common = {'d_hkl': o.get('d_hkl'), 'n_molecules_cell': o.get('n_molecules_cell') or o.get('n_molecules_bulk'),
              'mnp_used': o.get('mnp_used'), 'solution_pick_used': o.get('solution_pick_used'),
              'n_terminations': o.get('n_terminations'), 'terminations': term, 'notes': notes,
              'surface_area_static': o.get('surface_area_static')}
    faces_eq = None
    out = []
    lowest = min(term, key=lambda t: (t['sfe_unrelaxed'], t['index'])) if term else None
    if lowest:
        faces_eq = lowest.get('faces_equivalent')
    if res['level'] == 2:
        e2 = dict(base, level=2, ok=True, **common)
        e2.update({'gamma': o.get('gamma'), 'gamma_error': o.get('gamma_error') or 0.0,
                   'gamma_unrelaxed': o.get('gamma_unrelaxed'), 'termination_used': o.get('termination_used'),
                   'faces_equivalent': o.get('faces_equivalent'), 'molecules_intact': o.get('molecules_intact'),
                   'n_molecules_slab': o.get('n_molecules_slab'), 'n_molecules_bulk': o.get('n_molecules_bulk'),
                   'surface_area': o.get('surface_area'), 'thickness': o.get('thickness'), 'units': 'mJ/m^2',
                   'what': 'relaxed T = 0 surface energy (surfenergy temperature 0: minimisations), lowest '
                           'unrelaxed termination; excess potential energy per area'})
        out.append(e2)
        if lowest:
            e1 = dict(base, level=1, ok=True, **common)
            e1.update({'gamma': lowest['sfe_unrelaxed'], 'gamma_error': 0.0, 'termination_used': lowest['index'],
                       'faces_equivalent': faces_eq, 'units': 'mJ/m^2', 'cpu_s': None, 'wall_s': None,
                       'source': source + ' (screening step of the level-2 run)',
                       'what': 'unrelaxed surface energy, lowest of all terminations (slabcutter run 0)'})
            out.append(e1)
    else:
        e1 = dict(base, level=1, ok=True, **common)
        e1.update({'gamma': o.get('gamma_unrelaxed'), 'gamma_error': 0.0, 'termination_used': o.get('termination_used'),
                   'faces_equivalent': faces_eq, 'n_molecules_slab': o.get('n_molecules_slab'),
                   'thickness': o.get('thickness'), 'units': 'mJ/m^2',
                   'what': 'unrelaxed surface energy, lowest of all terminations (slabcutter run 0)'})
        out.append(e1)
    return out


def _spike_records(spike, files=None):
    """Finished surfenergy T = 0 runs of the spike: facet_runs.jsonl and results_runner*.json (lists or dicts
    of run_facet.py records, or JSON lines). Yields (record, source file)."""
    files = [os.path.join(spike, f) for f in files] if files else \
        [os.path.join(spike, 'facet_runs.jsonl')] + sorted(glob.glob(os.path.join(spike, 'results_runner*.json')))
    for p in files:
        if not os.path.isfile(p):
            continue
        txt = open(p).read()
        recs = []
        try:
            d = json.loads(txt)
            if isinstance(d, list):
                recs = d
            elif isinstance(d, dict):
                for k in ('runs', 'results', 'facets', 'records'):
                    if isinstance(d.get(k), list):
                        recs = d[k]
                        break
                else:
                    recs = [d]
        except ValueError:
            for line in txt.splitlines():
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    pass
        for r in recs:
            if isinstance(r, dict):
                yield r, os.path.relpath(p, chain_root())


def import_spike_level1(led, mat, family_of, spike):
    """Ingest the spike's level-1 screenings (level1.jsonl: slabcutter on a scouted supercell cell)."""
    p = os.path.join(spike, 'level1.jsonl')
    if not os.path.isfile(p):
        return 0, []
    scout = {}
    sp = os.path.join(spike, 'scout_supercell.jsonl')
    if os.path.isfile(sp):
        for line in open(sp):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get('run_dir'):
                scout[os.path.basename(r['run_dir'].rstrip('/'))] = r
    n_new, skipped = 0, []
    src = os.path.relpath(p, chain_root())
    for line in open(p):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not r.get('hkl') or r.get('gamma_unrelaxed_min') is None or not r.get('terminations'):
            continue
        cell_dir = os.path.basename(os.path.dirname(r.get('cell', '')))
        sc = scout.get(cell_dir, {})
        runs = r.get('runs') or []
        if any(x.get('rc') != 0 for x in runs):
            skipped.append('%s: %s has a failed slabcutter run' % (src, r['hkl']))
            continue
        term = []
        for t in r['terminations']:
            term.append(dict(t, sfe_source='computed' if t.get('index') == t.get('group') else 'group %s' % t.get('group')))
        lowest = min(term, key=lambda t: (t['sfe_unrelaxed'], t['index']))
        notes = []
        for x in runs:
            for n in (x.get('outputs') or {}).get('notes', '').splitlines():
                if n.strip():
                    notes.append('slabcutter t%d: %s' % (x['termination_index'], n.strip()))
        hkl = [int(x) for x in r['hkl']]
        e = {'family': family_of(hkl), 'hkl': hkl, 'level': 1, 'ok': True, 'search_range': sc.get('search_range'),
             'n_periods': r.get('n_periods'), 'temperature': 0.0, 'datafile': rel(mat['datafile']),
             'datafile_md5': mat['datafile_md5'], 'script': rel(mat['script']), 'script_md5': mat['script_md5'],
             'source': 'import:' + src, 'cpu_s': round(float(r.get('cpu_s_total') or 0) + float(sc.get('cpu_s') or 0), 1),
             'wall_s': None, 'run_dir': rel(os.path.dirname(r.get('cell', ''))) or None,
             'child_runs': [{'tool': 'supercell', 'run_dir': rel(os.path.dirname(r.get('cell', '')))}] +
                           [{'tool': 'slabcutter', 'run_dir': rel(x.get('run_dir')),
                             'inputs': {'termination_index': x['termination_index'], 'n_periods': r.get('n_periods'),
                                        'compute_unrelaxed_sfe': True}} for x in runs],
             'gamma': float(r['gamma_unrelaxed_min']), 'gamma_error': 0.0, 'termination_used': lowest['index'],
             'faces_equivalent': lowest.get('faces_equivalent'), 'n_terminations': r.get('n_terminations'),
             'terminations': term, 'n_molecules_cell': sc.get('n_molecules'), 'mnp_used': sc.get('mnp_used'),
             'd_hkl': r.get('period'), 'n_molecules_slab': r.get('n_molecules_slab'),
             'surface_area_static': r.get('surface_area'), 'units': 'mJ/m^2', 'notes': notes,
             'what': 'unrelaxed surface energy, lowest of all terminations (slabcutter run 0)'}
        if any(Ledger.key(x) == Ledger.key(e) and list(x['hkl']) == hkl and x.get('source') == e['source']
               for x in led.entries):
            continue
        led.add(e)
        n_new += 1
    return n_new, skipped


def import_spike(led, mat, family_of, spike, files=None):
    """Ingest the spike's finished TNT T = 0 surfenergy runs (marked cached, with their run dirs).
    Only runs on this material's data file (unit cell md5 checked against the run's own copy when present)."""
    n_new, skipped = 0, []
    for r, src in _spike_records(spike, files):
        inp = r.get('inputs') or {}
        if not all(x in inp for x in ('h', 'k', 'l')):
            continue
        if float(inp.get('temperature', 0.0)) != 0.0:
            skipped.append('%s: T = %s (not T = 0)' % (src, inp.get('temperature')))
            continue
        if inp.get('termination_mode', 'lowest') != 'lowest':
            skipped.append('%s: (%s %s %s) termination_mode %s (not lowest)' % (src, inp['h'], inp['k'], inp['l'],
                                                                              inp.get('termination_mode')))
            continue
        run_dir = r.get('run_dir')
        ok = r.get('rc') == 0 and bool(r.get('outputs')) and r['outputs'].get('gamma') is not None
        if ok and run_dir and not os.path.isdir(run_dir):
            skipped.append('%s: run dir %s missing' % (src, run_dir))
            continue
        hkl = [int(inp['h']), int(inp['k']), int(inp['l'])]
        res = {'level': 2, 'hkl': hkl, 'search_range': inp.get('search_range'), 'n_periods': inp.get('n_periods'),
               'ok': ok, 'outputs': r.get('outputs'), 'run_dir': run_dir, 'cpu_s': r.get('cpu_s'),
               'wall_s': r.get('wall_s'), 'child_runs': [{'tool': 'surfenergy', 'run_dir': run_dir, 'inputs': inp}],
               'error': None if ok else (r.get('error_tail') or 'rc %s' % r.get('rc'))[-1500:]}
        fam = family_of(hkl)
        for e in entries_from_result(res, fam, mat, 'import:' + src):
            if any(Ledger.key(x) == Ledger.key(e) and list(x['hkl']) == hkl and x.get('source') == e['source']
                   and x.get('run_dir') == e['run_dir'] for x in led.entries):
                continue
            e['imported_tag'] = r.get('tag')
            led.add(e)
            n_new += 1
    return n_new, skipped

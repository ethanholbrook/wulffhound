#!/usr/bin/env python
"""Build the committed replay bundle data/<material>/{ledger,cell_symmetry,cells}.json from the private chain.

Needs the simulation chain checkout (WULFFHOUND_CHAIN_ROOT, see wulffhound/config.py): it reads, never writes,
  tnt          the wulffagent MVP ledger (<chain>/wulffagent/state/tnt/ledger.json, its cell_symmetry.json) and the
               TNT Wulff spike (<chain>/devtools/spikes/wulff_tnt/: facet_runs*.jsonl, results_runner*.json,
               level1.jsonl, scout_supercell.jsonl)
  paracetamol  the paracetamol spike (<chain>/devtools/spikes/wulff_paracetamol/: facet_runs*.jsonl,
               runner1_runs.jsonl, scout_supercell.jsonl)
plus the live runs that wulffhound itself made (source 'live' in state/<material>/ledger.json).

What goes into the bundle: per result the gamma values, level, hkl, search_range, n_periods, the screened
terminations, faces_equivalent, molecules_intact, CPU s, md5 of the data file and script, and the chain's run
directory as a provenance string ('chain:surfenergy/bin/RUNS/<id>'; nothing the code needs). No structure files,
no force-field files, no absolute paths.

    ./wh import            (= ./wh py tools/build_bundle.py [tnt paracetamol])
"""
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'wulffhound'))
import config  # noqa: E402
from config import DATA, chain_root  # noqa: E402

HOME = os.path.expanduser('~')


def scrub(x, croot):
    """Replace machine paths in every string: the chain root -> 'chain:', the state dir -> 'state:', home -> '~'."""
    if isinstance(x, dict):
        return {k: scrub(v, croot) for k, v in x.items()}
    if isinstance(x, list):
        return [scrub(v, croot) for v in x]
    if isinstance(x, str):
        s = x.replace(croot + '/', 'chain:').replace(croot, 'chain:')
        s = s.replace(config.STATE + '/', 'state:').replace(ROOT + '/', '')
        s = s.replace(HOME, '~')
        return s
    return x


def chainify(e):
    """MVP-ledger paths are relative to the chain root: mark them as such."""
    for k in ('run_dir', 'datafile', 'script'):
        v = e.get(k)
        if isinstance(v, str) and v and ':' not in v.split('/')[0]:
            e[k] = 'chain:' + v
    for c in e.get('child_runs') or []:
        v = c.get('run_dir')
        if isinstance(v, str) and v and ':' not in v.split('/')[0]:
            c['run_dir'] = 'chain:' + v
    return e


def cells_from_scout(path):
    out = []
    if not os.path.isfile(path):
        return out
    for line in open(path):
        try:
            r = json.loads(line.replace('NaN', 'null'))
        except ValueError:
            continue
        err = None
        if r.get('rc') != 0 and r.get('error_tail'):
            lines = [l for l in r['error_tail'].strip().splitlines() if l.strip()]
            err = lines[-1][:300] if lines else None
        out.append({'hkl': r.get('hkl'), 'search_range': r.get('search_range'), 'rc': r.get('rc'),
                    'n_molecules': r.get('n_molecules'), 'mnp_used': r.get('mnp_used'), 'cpu_s': r.get('cpu_s'),
                    'error_tail': err, 'run_dir': r.get('run_dir')})
    return out


def build(material):
    croot = chain_root()
    import engine
    from ledger import Ledger, import_spike, import_spike_level1
    bdir = os.path.join(DATA, material)
    mat = engine.MATERIALS[material]
    notes = []
    # 1. cell symmetry (no atom positions: lattice, operations, families)
    if material == 'tnt':
        src_sym = os.path.join(croot, 'wulffagent', 'state', 'tnt', 'cell_symmetry.json')
        sym = json.load(open(src_sym))
        sym['datafile'] = mat['datafile']
    else:
        import symmetry
        df = config.resolve(mat['datafile'])
        sym = symmetry.analyse_cell(df, engine.DEFAULTS['max_index'])
        sym['datafile'] = mat['datafile']
        sym['datafile_md5'] = engine.md5(df)
        mat['datafile_md5_recorded'] = sym['datafile_md5']
        mat['script_md5_recorded'] = engine.md5(config.resolve(mat['script']))
        json.dump(mat, open(os.path.join(bdir, 'material.json'), 'w'), indent=1)
        engine.MATERIALS[material] = mat
    json.dump(sym, open(os.path.join(bdir, 'cell_symmetry.json'), 'w'), indent=1)
    # 2. known oriented cells (supercell scouts)
    spike = os.path.join(croot, 'devtools', 'spikes', 'wulff_' + material)
    cells = scrub(cells_from_scout(os.path.join(spike, 'scout_supercell.jsonl')), croot)
    json.dump(cells, open(os.path.join(bdir, 'cells.json'), 'w'), indent=1)
    # 3. ledger
    tmp = tempfile.mkdtemp(prefix='wh_bundle_')
    try:
        if os.path.isfile(os.path.join(bdir, 'ledger.json')):
            os.rename(os.path.join(bdir, 'ledger.json'), os.path.join(tmp, 'old_bundle.json'))
        eng = engine.Engine(material, state_dir=tmp, import_cached=False)
        led = eng.ledger
        if material == 'tnt':
            mvp = json.load(open(os.path.join(croot, 'wulffagent', 'state', 'tnt', 'ledger.json')))
            for e in mvp['entries']:
                led.entries.append(chainify(dict(e)))
            notes.append('%d entries from the wulffagent MVP ledger' % len(mvp['entries']))
            # facet_runs.jsonl only, as the MVP: the runner files hold termination_mode 'all' runs (another
            # protocol) and n_periods-3 thickness checks, whose screening steps would replace the level-1 values
            # the MVP built its shape from (the replay must reproduce the MVP's shapes)
            n, sk = import_spike(led, eng.mat, eng.family_of, spike, ['facet_runs.jsonl'])
            n1, sk1 = import_spike_level1(led, eng.mat, eng.family_of, spike)
        else:
            files = [f for f in ('facet_runs.jsonl', 'facet_runs_runner2.jsonl', 'runner1_runs.jsonl')
                     if os.path.isfile(os.path.join(spike, f))]
            n, sk = import_spike(led, eng.mat, eng.family_of, spike, files)
            n1, sk1 = import_spike_level1(led, eng.mat, eng.family_of, spike)
        notes.append('%d entries newly imported from the spike %s (%s skipped)' % (n + n1, os.path.relpath(spike, croot),
                                                                                  len(sk + sk1)))
        notes += sk + sk1
        # live runs made by wulffhound itself
        st = os.path.join(config.STATE, material, 'ledger.json')
        nl = 0
        if os.path.isfile(st):
            for e in json.load(open(st)).get('entries', []):
                if str(e.get('source', '')).startswith('live') and not any(
                        Ledger.key(x) == Ledger.key(e) and x['hkl'] == e['hkl'] and x.get('run_dir') == e.get('run_dir')
                        for x in led.entries):
                    led.entries.append(dict(e, source='live (wulffhound)'))
                    nl += 1
        notes.append('%d live wulffhound entries from state/%s/ledger.json' % (nl, material))
        entries = scrub(led.entries, croot)
        for e in entries:
            e.pop('cached', None)
            e.pop('same_family_member', None)
            e['provenance'] = 'private chain run dir %s (not shipped)' % e.get('run_dir')
        out = {'material': material, 'what': 'replay bundle: results of the private simulation chain with provenance '
               '(built by tools/build_bundle.py)', 'built': engine.now(), 'notes': notes, 'entries': entries}
        json.dump(out, open(os.path.join(bdir, 'ledger.json'), 'w'), indent=1, default=str)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {'material': material, 'entries': len(entries), 'cells': len(cells), 'notes': notes[:6]}


def main():
    if not os.path.isdir(os.path.join(chain_root(), 'devtools', 'spikes')):
        sys.exit('build_bundle needs the simulation chain checkout (WULFFHOUND_CHAIN_ROOT): ' + config.NO_CHAIN_MSG)
    for m in sys.argv[1:] or ['tnt', 'paracetamol']:
        print(json.dumps(build(m), indent=1))


if __name__ == '__main__':
    main()

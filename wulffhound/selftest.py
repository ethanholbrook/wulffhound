#!/usr/bin/env python
"""wulffhound self-test: every MCP tool function called directly from Python (no LLM), replay only (no live run).
Uses its own session 'selftest' (state/<material>/selftest/), so the agent session is untouched.

    ./wh selftest                      TNT
    ./wh --material paracetamol selftest
    WULFFHOUND_CHAIN_ROOT=/nonexistent ./wh selftest     (what CI runs: the chain hidden, the bundle only)

TNT also replays the wulffagent MVP agent session (data/tnt/mvp_reference.json) and checks that its Wulff shape
has the same area fractions to 4 decimals.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
ap = argparse.ArgumentParser()
ap.add_argument('--material', default=os.environ.get('WULFFHOUND_MATERIAL') or 'tnt')
MAT = ap.parse_args().material
os.environ['WULFFHOUND_SESSION'] = 'selftest'
os.environ['WULFFHOUND_CACHED_ONLY'] = '1'
os.environ['WULFFHOUND_BUDGET_MIN'] = '30' if MAT == 'tnt' else '60'   # paracetamol: every cached screening
os.environ['WULFFHOUND_MATERIAL'] = MAT
import mcp_server as S  # noqa: E402
from engine import Engine  # noqa: E402

ok = True


def call(name, check, **kw):
    global ok
    out = getattr(S, name)(**kw)
    out = json.loads(out.text if hasattr(out, 'text') else out) if not isinstance(out, dict) else out
    try:
        good = bool(check(out))
    except Exception as exc:          # a missing key is a failure, not a crash
        good = False
        out.setdefault('_check_error', repr(exc))
    ok &= good
    print('%-4s %-22s %s' % ('ok' if good else 'FAIL', name, json.dumps(kw)[:70]))
    if not good:
        print('     ->', json.dumps(out, default=str)[:600])
    return out


def check(label, good, detail=''):
    global ok
    ok &= bool(good)
    print('%-4s %-22s %s' % ('ok' if good else 'FAIL', label, detail))


eng = S._eng(MAT)
eng.reset_session()
print('material %s, mode %s' % (MAT, eng.mode))

if MAT == 'tnt':
    call('material_info', lambda o: o['Z'] == 8 and o['spacegroup_spglib']['number'] == 14
         and o['spacegroup_data_setting'].startswith('P2_1/a') and o['forcefield_validated'] is True)
    call('list_candidate_facets', lambda o: o['candidates'][0]['family'] == '{0 0 1}', n=12)
    allc = json.loads(S.list_candidate_facets(n=60).text)['candidates']
    new = [x['family'] for x in allc if not x['in_cache'] and not x.get('failures')][0]  # not in the ledger
    call('estimate_cost', lambda o: o['est_minutes'] > 0 and not o['cached'], family=new, level=1)
    call('estimate_costs', lambda o: len(o['rows']) == 2 and all('est_minutes' in r for r in o['rows']),
         families=['{0 0 1}', '{1 1 1}'], level=2)
    call('screen_facets', lambda o: o['rows'][0]['ok'] and o['rows'][0]['cached'], families=['{1 1 0}'])
    s1 = call('screen_facet', lambda o: o['ok'] and o['cached'] and o['run_dir'] and o['datafile_md5'], family='{0 0 1}')
    call('screen_facet', lambda o: o['ok'] and o['cached'] and o['gamma'] == s1['gamma'], family='{0 0 1}')
    call('screen_facet', lambda o: o['ok'] and o['cached'] and o['same_family_member'] is False, family='{1 0 0}')
    call('screen_facet', lambda o: o['ok'], family='{0 1 1}')
    call('screen_facet', lambda o: not o['ok'] and 'cached-only' in o['error'], family=new)
    call('relax_facet', lambda o: not o['ok'] and o.get('refused'), family='{1 1 1}', max_minutes=6)
    call('relax_facet', lambda o: o['ok'] and o['level'] == 2 and o['cached'] and o.get('consistent', True),
         family='{1 0 0}')
    call('relax_facet', lambda o: o['ok'] and o['level'] == 2, family='{0 1 1}')
    call('screen_facet', lambda o: o['ok'], family='{2 0 1}')
else:
    call('material_info', lambda o: o['Z'] == 4 and o['spacegroup_spglib']['number'] == 14
         and o['spacegroup_data_setting'].startswith('P2_1/n') and o['forcefield_validated'] is False
         and 'h_n = -l_a' in o['setting_map']['explicit'] and o['setting_map']['M_basis_a_to_n'])
    call('list_candidate_facets', lambda o: o['candidates'][0]['family'] == '{0 1 1}'
         and any(c.get('level2_inconsistent') for c in o['candidates'] if c['family'] == '{1 0 -1}'), n=12)
    allc = json.loads(S.list_candidate_facets(n=60).text)['candidates']
    new = [x['family'] for x in allc if not x['in_cache'] and not x.get('failures')][0]
    call('estimate_cost', lambda o: o['est_minutes'] > 0 and not o['cached'], family=new, level=1)
    call('estimate_costs', lambda o: len(o['rows']) == 2, families=['{1 0 -1}', '{0 0 1}'], level=2)
    lvl1 = [x['family'] for x in allc if 1 in x['in_cache']]
    call('screen_facets', lambda o: all(r['ok'] and r['cached'] for r in o['rows']) and len(o['rows']) == len(lvl1),
         families=lvl1)
    call('screen_facet', lambda o: not o['ok'] and ('cached-only' in o['error'] or 'chain' in o['error']), family=new)
    call('relax_facet', lambda o: o['ok'] and o['cached'] and o['consistent'] is False and 'gamma_basis' in o,
         family='{1 0 -1}')     # FIRE-continued 124.1 > unrelaxed 121.6 + 1: inconsistent
    # surfenergy's own {0 0 1} value 247.2 > unrelaxed 169.2 fails the guard; the FIRE re-relaxation (167.4) is used
    r001 = call('relax_facet', lambda o: o['ok'] and o['cached'] and o.get('consistent', True)
                and 'relaxed gamma 247.2 > unrelaxed gamma 169.2' in o['raw_value_rejected'], family='{0 0 1}')
    call('relax_facet', lambda o: o['ok'] and o['cached'], family='{1 0 1}')

w0 = call('build_wulff', lambda o: o['level'] == 0 and o['checks']['orbits_equal_laue_class'], level='0')
w = call('build_wulff', lambda o: o['ok'] and o['on_shape'] and o['next'] and 'convergence' in o
         and all(r.get('run_dir') for r in o['facets']), level='best', n_samples=20)
call('build_wulff', lambda o: 'ok' in o, level='2', n_samples=10)
if MAT == 'paracetamol':
    lv = {r['family']: r['level'] for r in w['facets']}
    bad = {x['family'] for x in w.get('inconsistent', [])}
    raw = {x['family']: x for x in w.get('superseded_raw_values', [])}
    check('consistency guard', '{1 0 -1}' in bad and lv.get('{1 0 -1}') == 1 and lv.get('{0 0 1}') == 2
          and '{0 0 1}' in raw and any('not validated' in x for x in w['warnings']),
          '{1 0 -1} excluded (124.0 > 121.6 + 1), level-1 used; {0 0 1} raw 247.2 flagged, FIRE value used; '
          'inconsistent: %s' % sorted(bad))
    check('guard per family', all(lv.get(f) == 1 for f in bad) and lv.get('{1 0 1}') == 2
          and all(x['reason'] for x in w.get('rejected_level2_runs', [])),
          'every inconsistent family at level 1, {1 0 1} (consistent) at level 2; %d rejected run(s) listed'
          % len(w.get('rejected_level2_runs', [])))
    wi = call('build_wulff', lambda o: o['ok'] and [r for r in o['facets'] if r['family'] == '{1 0 -1}'][0]['level'] == 2,
              level='best', n_samples=10, include_inconsistent=True)
call('log_decision', lambda o: o['action'] == 'selftest', action='selftest', why='tool smoke test',
     expected_cost_minutes=0)
call('add_literature', lambda o: o['family_our_setting'] == '{0 0 1}', citation='SELFTEST placeholder (not a paper)',
     claim='placeholder', hkl_reported='(0 0 1)', setting_conversion='identity', hkl_our_setting=[0, 0, 1],
     kind='test')
r = call('get_report_path', lambda o: os.path.isfile(o['report']) and o['bytes'] > 100000)
print('on shape (best):', [(f['family'], f['area_fraction'], f['level']) for f in w['facets'] if f['on_shape']])
print('next:', [(x['family'], x['next_level'], x['score']) for x in w['next'][:4]])
print('budget:', w.get('budget'))
print('report:', r['report'])

if MAT == 'tnt':
    # replay the MVP agent session from the bundle: same acquisitions -> same area fractions (4 decimals)
    ref = json.load(open(os.path.join(eng.bundle, 'mvp_reference.json')))
    e = Engine('tnt', session='selftest-mvp-replay')
    e.reset_session()
    e.acquired = [dict(a) for a in ref['acquired']]
    res = e.build_wulff('best', n_samples=5)
    mine = {x['family']: x['area_fraction'] for x in res['facets']}
    mvp = ref['area_fractions_best']
    diff = {f: (mvp.get(f), mine.get(f)) for f in set(mvp) | set(mine)
            if round(mvp.get(f, 0.0), 4) != round(mine.get(f, 0.0), 4)}
    check('MVP replay (4 dp)', not diff and len(mine) == len(mvp),
          '%d families, %s' % (len(mine), 'identical area fractions' if not diff else 'DIFFER: %s' % diff))
print('ALL OK' if ok else 'SOME FAILED')
sys.exit(0 if ok else 1)

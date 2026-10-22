#!/usr/bin/env python
"""wulffhound baseline: the deterministic policy, no LLM. Same engine, same tools, same CPU budget as the agent.

  1. screen (level 1) the families in BFDH order: every family on the BFDH shape, then the next ones, until
     n_screen families are screened or 40 % of the budget is spent;
  2. loop: build the 'best' Wulff shape; stop if the stop rule says converged; else run the top-scored next
     facet (engine.next_facets: relevance x information gain / cost) at its next level, with a cap of
     max_minutes per run; refused or failed runs are skipped (logged) and the next one is tried;
  3. stop when converged, out of budget, or nothing affordable is left; write the report.

Every step goes into the decision log (actor 'baseline') of session 'baseline' (state/<material>/baseline/).
Cached runs are replayed from the ledger and charged their recorded CPU time.

--fair (what `./wh compare` runs after an agent session): the baseline gets the agent session's budget
and live-run allowance (state/<material>/session.json), and every ledger entry the agent (or anyone) ran live
after the agent session started counts as a live run of the baseline too, not as free cache. Without that, a
baseline run after the agent simply replays the agent's live runs.

    ./wh [--material M] baseline --budget 30 [--cached-only]
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from engine import Engine  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--material', default='tnt')
    ap.add_argument('--budget', type=float, default=30.0, help='CPU minutes (default 30)')
    ap.add_argument('--n-screen', type=int, default=8)
    ap.add_argument('--max-minutes', type=float, default=6.0, help='cap per run (default 6)')
    ap.add_argument('--max-steps', type=int, default=25)
    ap.add_argument('--cached-only', action='store_true', help='never run live: only replay the ledger')
    ap.add_argument('--session', default='baseline')
    ap.add_argument('--live-runs', type=int, default=None, help='at most this many live runs (default: no limit)')
    ap.add_argument('--live-level', type=int, default=None, help='live runs only up to this level')
    ap.add_argument('--live-max-min', type=float, default=None, help='live runs only if estimated <= this')
    ap.add_argument('--fresh-after', default=None, help='ISO time: live ledger entries made at/after it count as '
                    'live runs of this session')
    ap.add_argument('--fair', action='store_true', help="the agent session's budget, live-run allowance and start "
                    'time (state/<material>/session.json)')
    a = ap.parse_args()
    mode = 'cached only' if a.cached_only else 'live allowed'
    if a.fair:
        ag = Engine(a.material, session='agent', import_cached=False).session_info()
        if not ag.get('started'):
            sys.exit('--fair: no agent session in state/%s/ (run the agent first)' % a.material)
        lim = ag.get('limits') or {}
        a.budget = lim.get('budget_cpu_min') or a.budget
        for k in ('live_runs', 'live_level', 'live_max_min'):     # the agent's allowance unless given explicitly
            if getattr(a, k) is None:
                setattr(a, k, lim.get(k))
        a.fresh_after = ag.get('started')
        mode = ('fair: agent session of %s, its budget %s CPU min, live runs <= %s (level <= %s, <= %s min each); '
                'runs made live after the agent started count as live here' % (
                    a.fresh_after, a.budget, a.live_runs if a.live_runs is not None else 'any',
                    a.live_level if a.live_level is not None else 'any',
                    a.live_max_min if a.live_max_min is not None else 'any')) + (', cached only' if a.cached_only else '')

    eng = Engine(a.material, session=a.session, budget_min=a.budget, cached_only=a.cached_only,
                 live_runs=a.live_runs, live_level=a.live_level, live_max_min=a.live_max_min,
                 fresh_after=a.fresh_after)
    eng.reset_session()
    log = lambda action, why, cost=None, outcome=None: eng.log_decision(action, why, cost, 'baseline', outcome)

    def run(family, level, why):
        est = eng.estimate_cost(family, level)
        est['est_minutes'] = est.get('charge_minutes', est['est_minutes'])
        r = eng.run_facet(family, level, max_minutes=a.max_minutes)
        if r.get('ok'):
            outc = 'gamma %.2f mJ/m^2 (%s%s), charged %.1f CPU min' % (
                r['gamma'], 'cached' if r.get('cached') else 'live', ', ' + r['run_dir'] if r.get('run_dir') else '',
                r.get('charged_cpu_min', (r.get('cpu_s') or 0) / 60.0))
        else:
            outc = 'not done: %s' % (r.get('error') or '')[:200]
        log('%s %s (level %d)' % ('screen' if level == 1 else 'relax', family, level), why, est['est_minutes'], outc)
        print('%-26s %-8s %s' % ('L%d %s' % (level, family), '%.1f min' % est['est_minutes'], outc))
        return r

    log('start', 'deterministic baseline: BFDH-order screening, then greedy next-facet score; budget %.0f CPU min, '
        'cap %.0f min per run; mode: %s' % (a.budget, a.max_minutes, mode))
    # 1. screening in BFDH order
    b = eng.bfdh()
    on_b = [r['family'] for r in b['facets'] if r['area_fraction'] > 1e-4]
    order = on_b + [f['family'] for f in eng.sym['families'] if f['family'] not in on_b]
    tried = set()
    for fam in order:
        if len([x for x in tried if x[1] == 1]) >= a.n_screen or eng.spent_min() > 0.4 * a.budget:
            break
        tried.add((fam, 1))
        run(fam, 1, 'BFDH order: %s' % ('on the BFDH shape' if fam in on_b else 'next by d_eff (rank %d)'
                                         % eng.fam[fam]['bfdh_rank']))
    # 2. greedy refinement
    res = None
    for step in range(a.max_steps):
        res = eng.build_wulff('best', n_next=40)
        if res.get('ok') and res['convergence']['converged']:
            log('stop', 'stop rule met: converged', 0, 'shape %s' % ', '.join(res['on_shape']))
            break
        nxt = [x for x in res.get('next', []) if (x['family'], x['next_level']) not in tried]
        if not nxt:
            log('stop', 'no untried candidate left in the next-facet list', 0,
                'open: %s' % '; '.join((res.get('convergence') or {}).get('open', [])[:4]))
            break
        x = nxt[0]
        if eng.spent_min() + x['est_minutes'] > a.budget:
            affordable = [y for y in nxt if eng.spent_min() + y['est_minutes'] <= a.budget]
            if not affordable:
                log('stop', 'budget: %.1f of %.1f CPU min spent, nothing affordable left' % (eng.spent_min(), a.budget))
                break
            x = affordable[0]
        tried.add((x['family'], x['next_level']))
        run(x['family'], x['next_level'], 'greedy: top score %.3f (%s)' % (x['score'], x['why']))
    out = eng.write_report(title='Baseline (no LLM): Wulff shape of %s' % eng.mat['name'])
    res = eng._last
    summary = {'session': eng.session, 'written': __import__('ledger').now(), 'mode': mode, 'limits': eng.limits(),
               'budget': eng.budget(), 'report': out['report'], 'acquired': eng.acquired,
               'on_shape': res.get('on_shape'), 'converged': (res.get('convergence') or {}).get('converged'),
               'open': (res.get('convergence') or {}).get('open'),
               'area_fractions': {r['family']: r['area_fraction'] for r in res.get('facets', [])}}
    json.dump(summary, open(os.path.join(eng.out, 'summary.json'), 'w'), indent=1)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python
"""./wh campaign plan|run|status|ingest SPEC [options]

  plan SPEC     dry run: the jobs (estimated CPU, cumulative), what is skipped and why (in the dataset, failed
                before, over the per-run cap, over the budget, beyond limit_new), near-duplicates (refreshes).
                Writes state/campaigns/<name>/plan.json. --json prints the plan as JSON.
  run SPEC      plan, then run the jobs through the spec's backend (journal: resumable; failed jobs are not
                retried unless --retry-failed), then ingest (unless --no-ingest). --only JOB_ID ... runs a subset;
                --yes is not needed: the plan's budget and per-run cap are the limits. Staged: when a level's
                selection waits for a shape (wulff_priority) or a target_ok selection lost families to failures,
                run re-plans after the ingest and goes on (at most MAX_STAGES stages, one budget for all).
  status SPEC   the journal: last event of every job, CPU spent, planned jobs not started yet.
  ingest SPEC   finished jobs -> schema-valid surface_energy records in dataset/campaigns/<name>.jsonl, then
                rebuild the dataset (tools/export_dataset.py; --no-export to skip) and report the shapes that
                changed.
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from campaign import campaign_dir, format_plan, load_spec, plan, public_plan  # noqa: E402

MAX_STAGES = 6      # run: plan -> run -> ingest, again while a selection waits for a shape or lost families


def _plan(spec):
    p = plan(spec)
    d = campaign_dir(spec['campaign'])
    os.makedirs(d, exist_ok=True)
    json.dump(public_plan(p), open(os.path.join(d, 'plan.json'), 'w'), indent=1, default=str)
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(prog='wh campaign', description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cmd', choices=('plan', 'run', 'status', 'ingest'))
    ap.add_argument('spec')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--only', nargs='*')
    ap.add_argument('--retry-failed', action='store_true')
    ap.add_argument('--no-ingest', action='store_true')
    ap.add_argument('--no-export', action='store_true')
    a = ap.parse_args(argv)
    spec = load_spec(a.spec)
    if a.retry_failed:
        spec['dedup']['retry_failed'] = True
    if a.cmd == 'plan':
        p = _plan(spec)
        print(json.dumps(public_plan(p), indent=1, default=str) if a.json else format_plan(p))
        return 0
    if a.cmd == 'status':
        from campaign_exec import status
        s = status(spec)
        if a.json:
            print(json.dumps(s, indent=1, default=str))
        else:
            print('campaign %s: %s; CPU spent %.2f min (journal %s)' % (s['campaign'], s['by_event'], s['cpu_min'],
                                                                       s['journal']))
            for e in s['jobs']:
                print('  %-12s %-9s %-16s %-10s L%s %s%s' % (e['job_id'], e['event'], (e.get('material_id') or '')[:16],
                                                           e.get('family') or '', e.get('level') or '-',
                                                           ('gamma %.2f ' % e['gamma']) if e.get('gamma') is not None else '',
                                                           ('cpu %.0f s ' % e['cpu_s']) if e.get('cpu_s') else '') +
                      (('flags %s ' % e['gamma_flags']) if e.get('gamma_flags') else '') +
                      ((e.get('error_line') or __import__('campaign').error_line(e.get('error')) or e.get('why') or '')[:200]))
        return 0
    from campaign_ingest import ingest
    if a.cmd == 'run':
        from campaign_exec import execute
        n_failed = 0
        for stage in range(1, MAX_STAGES + 1):
            p = _plan(spec)
            if stage > 1:
                if not p['jobs']:
                    print('stage %d: the re-plan has no new jobs: done' % stage)
                    break
                print('\n==== stage %d: re-planned after ingest (%s)' % (stage, 'a shape the selection waited for is in '
                      'the dataset now' if had_deferred else 'failed or refused jobs make room for the next families'))
            print(format_plan(p))
            print()
            s = execute(p, spec, only=a.only, retry_failed=a.retry_failed)
            n_failed += len(s['failed'])
            print('\nrun (stage %d): %d done, %d failed, %d skipped, %d recovered; campaign CPU %.2f min' % (
                stage, len(s['done']), len(s['failed']), len(s['skipped']), len(s['recovered']),
                s['cpu_min_spent_campaign']))
            if a.no_ingest:
                return 0 if not n_failed else 2
            r = ingest(spec, export=not a.no_export)
            print(json.dumps(r, indent=1, default=str) if a.json else r['text'])
            had_deferred = bool(p.get('deferred'))
            # another stage only when this one ran something and the selection can move on: a level waited for a
            # shape, or a target_ok selection lost families to failures
            more = had_deferred or (s['failed'] and any(j.get('target_ok') for j in p['jobs']))
            if a.only or a.no_export or not (s['done'] or s['failed']) or not more:
                break
        return 0
    from campaign_ingest import ingest
    r = ingest(spec, export=not a.no_export)
    print(json.dumps(r, indent=1, default=str) if a.json else r['text'])
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python
"""Run-time estimates of campaign jobs with surfenergy's own estimator (its App-mode GUI model), in the chain env.

The campaign planner (wulff-dev env) sends a batch of jobs here as JSON and reads the estimates back. The model is
surfenergy's (bin/surfenergy_gui.py: estimate_oriented_cell, estimate_run), so the planner and the GUI give the
same numbers for the same inputs; nothing here changes how the chain runs.

    SIM2L_DEV_ROOT=<chain> <chain>/devtools/sim2l-env python wulffhound/chain_estimate.py jobs.json out.json

jobs.json: [{"id", "cell": [a, b, c, alpha, beta, gamma], "n_atoms", "n_molecules", "hkl", "search_range",
"level" (1 screening, 2 T = 0, 3 MD), "temperature", "n_periods", "slab_from", "kind" ("reaxff" | "nonreactive"),
"equilibration_ps", "production_ps", "n_blocks", "timestep", "angle_dev", optional "script" (ReaxFF: slabcutter's
memory rule)}]. out.json: {id: {"seconds", "low", "high", "molecules_cell", "atoms_cell", "molecules_slab", "det",
"mnp", "parts", and with a ReaxFF script "reaxff_gb", "reaxff_limit_gb", "reaxff_refused"} or {"error"}}.
"""
import json
import os
import sys


def reaxff_memory(script, atoms_cell, atoms_slab):
    """slabcutter's own ReaxFF memory rule (slabcutter_core.check_reaxff_size: the far-neighbour reservation of the
    largest run 0, the bulk = the whole oriented cell, against REAXFF_MEMORY_LIMIT_GB) on the estimated cell, so a
    cell that slabcutter would refuse is not planned. {'reaxff_gb', 'reaxff_limit_gb', 'reaxff_refused' (text or None)}."""
    import slabcutter_core as sc
    settings = sc.reaxff_settings(open(script).read().splitlines())
    if settings is None:
        return {}
    try:
        sc.check_reaxff_size(open(script).read().splitlines(), [('bulk', int(atoms_cell)), ('slab', int(atoms_slab))])
        why = None
    except sc.SlabError as exc:
        why = str(exc).split('. Such a run')[0]
    need = sc.reaxff_reservation_gb(max(atoms_cell, atoms_slab), *settings)
    return {'reaxff_gb': round(need, 2), 'reaxff_limit_gb': sc.REAXFF_MEMORY_LIMIT_GB, 'reaxff_refused': why}


def main():
    root = os.environ.get('SIM2L_DEV_ROOT', '')
    sys.path.insert(0, os.path.join(root, 'surfenergy', 'bin'))
    sys.path.insert(0, os.path.join(root, 'surfenergy', 'simtool'))
    sys.path.insert(0, os.path.join(root, 'slabcutter', 'simtool'))
    jobs = json.load(open(sys.argv[1]))
    out = {}
    try:
        import surfenergy_gui as g
        import surfenergy_md as md
    except Exception as exc:          # the chain is being edited, or an older checkout
        json.dump({'_error': 'surfenergy estimator not importable: %s: %s' % (type(exc).__name__, exc)},
                  open(sys.argv[2], 'w'))
        return 0
    for j in jobs:
        try:
            cell = g.estimate_oriented_cell(j['cell'], j['hkl'], int(j['search_range']),
                                            angle_dev=float(j.get('angle_dev') or g.GCCM_ANGLE_DEV))
            level = int(j['level'])
            T = 0.0 if level < 3 else float(j['temperature'])
            state = {'material_source': 'unitcell', 'use_reaxff': j['kind'] == 'reaxff',
                     'search_range': int(j['search_range']), 'n_periods': int(j['n_periods']), 'temperature': T,
                     'termination_mode': 'lowest', 'slab_from': j.get('slab_from') or g.SLAB_FROM_DEFAULT,
                     'bulk_prerelaxed': False}
            settings = None
            if T > 0:
                settings = md.md_settings(j['kind'], T, float(j.get('timestep') or 0.0),
                                          float(j.get('equilibration_ps') or 10.0),
                                          float(j.get('production_ps') or 20.0), int(j.get('n_blocks') or 5))
            est = g.estimate_run({'n_atoms': j['n_atoms'], 'n_molecules': j['n_molecules']}, cell, state, settings)
            parts = est['parts']
            if level == 1:
                # wulffhound's level 1 = surfenergy's supercell + screening steps only (no notebook, no MD)
                keep = [p for p in parts if p[0].startswith('supercell') or p[0].startswith('screening')]
                sec = sum(p[1] for p in keep)
                lo, hi = g.RANGE_LOW * sec, g.RANGE_HIGH * sec
                parts = keep
            else:
                sec, lo, hi = est['seconds'], est['low'], est['high']
            out[j['id']] = {'seconds': round(sec, 1), 'low': round(lo, 1), 'high': round(hi, 1),
                            'molecules_cell': est['molecules_cell'], 'atoms_cell': est['atoms_cell'],
                            'molecules_slab': est['molecules_slab'], 'det': cell['det'], 'mnp': cell['mnp'],
                            'parts': [[p[0], round(p[1], 1)] for p in parts]}
            if j.get('script') and j['kind'] == 'reaxff':
                out[j['id']].update(reaxff_memory(j['script'], est['atoms_cell'], est['atoms_slab']))
        except Exception as exc:
            out[j['id']] = {'error': '%s: %s' % (type(exc).__name__, exc)}
    json.dump(out, open(sys.argv[2], 'w'), indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())

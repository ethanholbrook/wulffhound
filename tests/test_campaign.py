"""Unit tests of the campaign driver (planner, executor, ingest, record-based shapes). No chain needed: the chain is
hidden (WULFFHOUND_CHAIN_ROOT=/nonexistent), the state goes to a temporary folder, TNT plans from the committed
bundle (replay mode) and the executor runs a fake backend.

    ./wh py -m unittest discover -s tests -v
"""
import copy
import json
import os
import sys
import tempfile
import unittest

TMP = tempfile.mkdtemp(prefix='wh-campaign-test-')
os.environ['WULFFHOUND_STATE'] = TMP
os.environ['WULFFHOUND_CHAIN_ROOT'] = '/nonexistent'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'wulffhound'))

import campaign as C  # noqa: E402
import campaign_exec as E  # noqa: E402
import campaign_ingest as I  # noqa: E402

BASE = {'campaign': 'test_tnt', 'materials': ['tnt'], 'facets': ['{0 0 1}', '{1 0 0}', '{2 0 1}', '{0 1 2}', '{1 0 -1}'],
        'levels': [2], 'protocol': {'n_periods': 2, 'slab_from': 'relaxed_bulk'},
        'budget': {'cpu_minutes': 30, 'per_run_cap_minutes': 6}, 'policy': 'exhaustive'}
EST = lambda rows: {}           # no surfenergy estimator in the tests (bundle materials use the engine's model)


def spec(**over):
    d = copy.deepcopy(BASE)
    d.update(over)
    return C.load_spec(d)


def tnt_record(family, level, slab_from, status='ok', gamma=100.0, rid='se-000000000001', error=None):
    """A minimal dataset record of TNT with the fields record_key reads."""
    return {'record_type': 'surface_energy', 'record_id': rid,
            'material': {'material_id': 'tnt-monoclinic', 'structure': {'structure_id': 'chain-tnt-unitcell'}},
            'force_field': {'name': 'TNT non-reactive (buck + lj/cut + coul/wolf, 11 A)', 'md5': None},
            'facet': {'family': family},
            'protocol': {'level': level, 'temperature_K': 0.0, 'n_periods': 2, 'slab_from': slab_from, 'vacuum_A': 30.0},
            'result': {'status': status, 'gamma': gamma if status == 'ok' else None, 'gamma_flags': [],
                       'error_message': error},
            'provenance': {'source': 'test', 'input_md5': {}}}


def make_plan(s, records=(), journal=None):
    return C.plan(s, records=list(records), shapes=C.load_shapes(), journal=journal or {}, estimator=EST)


class FakeBackend(E.Backend):
    """Records the jobs it was asked to run; cpu = factor x estimate; fail = set of families that fail."""
    name = 'fake'

    def __init__(self, fail=(), factor=1.0):
        super().__init__()
        self.calls, self.fail, self.factor = [], set(fail), factor

    def available(self):
        return True, 'fake'

    def run(self, job, mat, spec, work_dir, timeout_s):
        self.calls.append(job['job_id'])
        os.makedirs(work_dir, exist_ok=True)
        cpu = 60.0 * (job['est_minutes'] or 1) * self.factor
        if job['family'] in self.fail:
            res = {'ok': False, 'error': 'RuntimeError: surfenergy failed (rc 1):\nERROR: slabcutter found no period',
                   'cpu_s': 5.0, 'wall_s': 5.0}
        else:
            res = {'ok': True, 'cpu_s': cpu, 'wall_s': cpu, 'run_dir': os.path.join(work_dir, 'RUNS', 'abc123'),
                   'outputs': {'gamma': 120.0, 'gamma_unrelaxed': 130.0, 'gamma_flags': [], 'termination_used': 0}}
        json.dump(res, open(os.path.join(work_dir, 'result.json'), 'w'))
        return res


class TestSpec(unittest.TestCase):
    def test_facet_forms(self):
        self.assertEqual(C.parse_facets('bfdh_top_5'), {'select': 'bfdh_top', 'n': 5})
        self.assertEqual(C.parse_facets('max_index 2'), {'select': 'max_index', 'k': 2})
        self.assertEqual(C.parse_facets(['{0 0 1}'])['select'], 'explicit')
        self.assertEqual(C.parse_facets('on_shape'), {'select': 'on_shape'})
        with self.assertRaises(C.SpecError):
            C.parse_facets('everything')

    def test_defaults_and_caps(self):
        s = spec(executor={'backend': 'local', 'workers': 5})
        self.assertEqual(s['executor']['workers'], 2)          # at most two heavy jobs at once
        self.assertEqual(s['executor']['workers_requested'], 5)
        self.assertEqual(s['protocol']['n_periods'], 2)
        self.assertEqual(s['dedup']['retry_failed'], False)

    def test_schema_rejects(self):
        with self.assertRaises(C.SpecError):
            spec(policy='random')
        with self.assertRaises(C.SpecError):
            spec(levels=[3], temperatures=[0])
        with self.assertRaises(C.SpecError):
            C.load_spec({'campaign': 'x'})                      # no materials

    def test_routes_registry(self):
        self.assertIn('reaxff', C.FORCEFIELD_ROUTES)
        self.assertIn('nonreactive', C.FORCEFIELD_ROUTES)
        with self.assertRaises(C.SpecError):
            make_plan(spec(forcefield={'route': 'openff-sage'}))


class TestPlanner(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        before = sorted(os.listdir(os.path.join(ROOT, 'dataset')))
        p = make_plan(spec(campaign='dry'))
        self.assertGreater(p['totals']['n_jobs'], 0)
        txt = C.format_plan(p)
        self.assertIn('RUN', txt)
        self.assertIn('SKIPPED', txt)
        self.assertFalse(os.path.exists(os.path.join(TMP, 'campaigns', 'dry', 'journal.jsonl')))
        self.assertEqual(before, sorted(os.listdir(os.path.join(ROOT, 'dataset'))))
        self.assertFalse(any(j['runnable'] for j in p['jobs']))  # replay mode: planned, not runnable

    def test_exact_cache_and_near_duplicates(self):
        recs = [tnt_record('{0 0 1}', 2, 'relaxed_bulk', rid='se-000000000001'),
                tnt_record('{1 0 0}', 2, 'static_cut', rid='se-000000000002', gamma=124.1)]
        p = make_plan(spec(), recs)
        sk = {s['family']: s for s in p['skipped']}
        self.assertEqual(sk['{0 0 1}']['reason'], 'cached')
        self.assertIn('se-000000000001', sk['{0 0 1}']['why'])
        run = {j['family']: j for j in p['jobs']}
        self.assertIn('{1 0 0}', run)                              # a refresh: other protocol
        nd = run['{1 0 0}']['near_duplicates']
        self.assertEqual(nd[0]['record_id'], 'se-000000000002')
        self.assertIn('slab_from', nd[0]['differs_in'])

    def test_failed_not_retried_blindly(self):
        recs = [tnt_record('{2 0 1}', 2, 'relaxed_bulk', status='failed', rid='se-000000000003', error='boom')]
        p = make_plan(spec(), recs)
        self.assertEqual({s['family']: s['reason'] for s in p['skipped']}.get('{2 0 1}'), 'failed_before')
        p2 = make_plan(spec(dedup={'retry_failed': True}), recs)
        self.assertIn('{2 0 1}', [j['family'] for j in p2['jobs']])

    def test_journal_dedup(self):
        s = spec()
        p = make_plan(s)
        j0, j1 = p['jobs'][0], p['jobs'][1]
        journal = {j0['job_id']: {'job_id': j0['job_id'], 'event': 'done', 'time': 't'},
                   j1['job_id']: {'job_id': j1['job_id'], 'event': 'failed', 'time': 't', 'error': 'x'}}
        p2 = make_plan(s, journal=journal)
        ids = [j['job_id'] for j in p2['jobs']]
        self.assertNotIn(j0['job_id'], ids)
        self.assertNotIn(j1['job_id'], ids)
        why = {x['job_id']: x['why'] for x in p2['skipped']}
        self.assertIn('done in this campaign', why[j0['job_id']])
        self.assertIn('not retried blindly', why[j1['job_id']])

    def test_job_ids_stable(self):
        a, b = make_plan(spec()), make_plan(spec())
        self.assertEqual([j['job_id'] for j in a['jobs']], [j['job_id'] for j in b['jobs']])

    def test_per_run_cap_and_budget_cut(self):
        p = make_plan(spec(budget={'cpu_minutes': 30, 'per_run_cap_minutes': 3}))
        over = [s for s in p['skipped'] if s['reason'] == 'over_cap']
        self.assertIn('{0 1 2}', [s['family'] for s in over])         # engine estimate 5.1 min > 3
        self.assertTrue(all(j['est_minutes'] <= 3 for j in p['jobs']))
        p = make_plan(spec(budget={'cpu_minutes': 5.5, 'per_run_cap_minutes': 6}))
        self.assertLessEqual(p['totals']['est_cpu_min'], 5.5)
        self.assertIn('over_budget', p['totals']['skipped_by_reason'])
        cum = [j['cum_est_minutes'] for j in p['jobs']]
        self.assertEqual(cum, sorted(cum))

    def test_ordering(self):
        ex = make_plan(spec(policy='exhaustive'))
        self.assertEqual([j['family'] for j in ex['jobs']],
                         [f for f in BASE['facets'] if f in [j['family'] for j in ex['jobs']]])
        bl = make_plan(spec(policy='baseline'))
        scores = [j['score'] for j in bl['jobs']]
        self.assertEqual(scores, sorted(scores, reverse=True))      # level 2: relevance / cost, greedy
        l1 = make_plan(spec(policy='baseline', levels=[1], facets='bfdh_top_6'))
        ranks = [j['bfdh_rank'] for j in l1['jobs']]
        self.assertEqual(ranks, sorted(ranks))                      # level 1: BFDH order

    def test_on_shape_selection(self):
        p = make_plan(spec(facets={'select': 'on_shape'}))
        fams = [j['family'] for j in p['jobs']] + [s['family'] for s in p['skipped']]
        self.assertIn('{0 0 1}', fams)
        self.assertIn('{1 1 1}', fams)
        self.assertNotIn('{2 1 0}', fams)                           # off the best shape

    def test_recalibrated_from_journal(self):
        s = spec()
        p = make_plan(s)
        j0 = p['jobs'][0]
        journal = {j0['job_id']: {'job_id': j0['job_id'], 'event': 'done', 'time': 't', 'level': 2,
                                  'est_minutes': 2.0, 'cpu_s': 2.0 * 60 * 1.5}}
        p2 = make_plan(s, journal=journal)
        by = {j['job_id']: j for j in p2['jobs'] + p2['skipped']}
        j1 = p['jobs'][1]
        self.assertAlmostEqual(by[j1['job_id']]['est_minutes'], round(j1['est_minutes'] * 1.5, 2), places=2)

    def test_error_line(self):
        err = ('Traceback ...\npapermill.exceptions.PapermillExecutionError: \n----\nCell In[8], line 8\n'
               'ValueError: slab_from relaxed_bulk: termination 0 has no unique counterpart\n')
        self.assertTrue(C.error_line(err).startswith('ValueError: slab_from relaxed_bulk'))
        self.assertEqual(C.error_line('just text'), 'just text')

    def test_limit_new(self):
        s = spec(levels=[1], facets={'select': 'bfdh_top', 'n': 6, 'limit_new': 2})
        p = make_plan(s)
        self.assertLessEqual(len(p['jobs']), 2)
        # jobs this campaign already ran count towards the limit: re-planning a finished campaign adds nothing
        journal = {j['job_id']: {'job_id': j['job_id'], 'event': 'done', 'time': 't'} for j in p['jobs']}
        self.assertEqual(make_plan(s, journal=journal)['jobs'], [])


class TestStagedSelection(unittest.TestCase):
    """level_facets, wulff_priority (tiers, deferral), target_ok, cell fallbacks and the ReaxFF memory rule."""

    def l2spec(self, **fs):
        f = dict({'select': 'wulff_priority', 'shape_level': '1', 'k_sigma': 2}, **fs)
        return spec(levels=[2], facets='bfdh_top_4', level_facets={'2': f}, policy='baseline',
                    budget={'cpu_minutes': 500, 'per_run_cap_minutes': 60})

    def test_wulff_priority_tiers_and_order(self):
        p = make_plan(self.l2spec())
        tiers = [j['tier'] for j in p['jobs']]
        self.assertEqual(tiers, sorted(tiers))                      # on the shape, then within sigma, then the rest
        self.assertEqual(tiers[0], 0)
        sh = next(x for x in C.load_shapes() if x['material_id'] == 'tnt-monoclinic' and x['level'] == '1')
        on = {f['family'] for f in sh['facets'] if f['area_fraction'] > 1e-4}
        allf = {j['family']: j['tier'] for j in p['jobs'] + [x for x in p['skipped'] if 'tier' in x]}
        self.assertTrue(all(allf[f] == 0 for f in on if f in allf))
        rest = [j['bfdh_rank'] for j in p['jobs'] if j['tier'] == 2]
        self.assertEqual(rest, sorted(rest))                        # the rest in BFDH order

    def test_wulff_priority_waits_for_shape(self):
        s = self.l2spec()
        p = C.plan(s, records=[], shapes=[], journal={}, estimator=EST)
        self.assertEqual(p['jobs'], [])
        self.assertEqual([d['level'] for d in p['deferred']], [2])
        self.assertIn('DEFERRED', C.format_plan(p))

    def test_target_ok(self):
        p = make_plan(self.l2spec(target_ok=2))
        self.assertEqual(len(p['jobs']), 2)
        self.assertIn('target_reached', p['totals']['skipped_by_reason'])
        first = p['jobs'][0]
        # a family with a value already counts; a failed one does not
        rec = tnt_record(first['family'], 2, 'relaxed_bulk', rid='se-000000000009')
        p2 = make_plan(self.l2spec(target_ok=2), [rec])
        self.assertEqual(len(p2['jobs']), 1)
        bad = tnt_record(first['family'], 2, 'relaxed_bulk', status='failed', rid='se-00000000000a', error='x')
        p3 = make_plan(self.l2spec(target_ok=2), [bad])
        self.assertEqual(len(p3['jobs']), 2)
        self.assertNotIn(first['family'], [j['family'] for j in p3['jobs']])

    def test_level_facets_parse(self):
        s = C.load_spec(dict(BASE, levels=[1, 2], level_facets={2: 'wulff_priority'}))
        self.assertEqual(s['level_facets'][2]['select'], 'wulff_priority')
        with self.assertRaises(C.SpecError):
            C.load_spec(dict(BASE, dedup={'retry_failed': 'sometimes'}))

    def fake_job(self):
        return {'job_id': 'j-x', 'members_considered': [[1, 1, 1], [-1, 1, 1]],
                '_settings': C.cell_settings({'search_range': 4, 'cell_fallbacks': [{'search_range': 6},
                                                                                     {'angle_dev': 30}]})}

    def test_cell_fallbacks_memory(self):
        j = self.fake_job()
        self.assertEqual([(x['search_range'], x['angle_dev']) for x in j['_settings']], [(4, None), (6, None), (4, 30)])
        big = {'seconds': 600, 'low': 1, 'high': 2, 'molecules_cell': 1320, 'molecules_slab': 240,
               'reaxff_refused': 'the cell is too large for a ReaxFF run 0'}
        small = {'seconds': 420, 'low': 1, 'high': 2, 'molecules_cell': 88, 'molecules_slab': 16, 'reaxff_refused': None}
        est = {'j-x:0:0': big, 'j-x:0:1': big, 'j-x:1:0': dict(big, molecules_cell=600), 'j-x:2:0': dict(small),
               'j-x:2:1': dict(small, molecules_cell=90)}
        v = C.choose_cell(j, est, cap=20)
        self.assertEqual(v['molecules_cell'], 88)
        self.assertEqual((j['hkl'], j['search_range'], j['cell_overrides']), ([1, 1, 1], 4, {'angle_dev': 30}))
        self.assertEqual(len(j['cell_tried']), 2)
        self.assertIn('ReaxFF memory', j['cell_tried'][0])
        # the member whose cell failed before is passed over: the next member at the same setting
        j = self.fake_job()
        j['failed_cells'] = [{'hkl': [1, 1, 1], 'search_range': 4, 'angle_dev': 30, 'angle_dev_known': True, 'ref': 'x'}]
        C.choose_cell(j, est, cap=20)
        self.assertEqual(j['hkl'], [-1, 1, 1])
        # nothing fits: no cell, with the reasons
        j = self.fake_job()
        self.assertIsNone(C.choose_cell(j, {k: v for k, v in est.items() if not k.startswith('j-x:2')}, cap=20))
        self.assertIn('no usable oriented cell', j['cell_refused'])

    def test_same_cell(self):
        j = {'hkl': [1, 1, 1], 'search_range': 4, 'cell_overrides': {'angle_dev': 30}}
        rec = {'facet': {'hkl': [1, 1, 1]}, 'protocol': {'search_range': 6}, 'record_id': 'se-1'}
        self.assertFalse(C.same_cell(C.failed_cell_of_record(rec), j))           # other search_range and angle_dev
        rec['protocol']['search_range'] = 4
        self.assertFalse(C.same_cell(C.failed_cell_of_record(rec), j))           # records: the default angle_dev
        self.assertTrue(C.same_cell(C.failed_cell_of_record(rec), dict(j, cell_overrides={})))
        ev = {'hkl': [1, 1, 1], 'time': 't'}                                    # an old event: fields unknown
        self.assertTrue(C.same_cell(C.failed_cell_of_event(ev), j))


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.name = 'exec_%d' % id(self)
        self.s = spec(campaign=self.name, policy='baseline')
        self.p = make_plan(self.s)

    def test_runs_and_journal(self):
        b = FakeBackend()
        r = E.execute(self.p, self.s, backend=b, log=lambda *a: None)
        self.assertEqual(len(r['done']), len(self.p['jobs']))
        st = E.status(self.s)
        self.assertEqual(st['by_event'], {'done': len(self.p['jobs'])})
        # a second run does nothing (journal)
        b2 = FakeBackend()
        E.execute(self.p, self.s, backend=b2, log=lambda *a: None)
        self.assertEqual(b2.calls, [])

    def test_failure_recorded_not_retried(self):
        fam = self.p['jobs'][0]['family']
        b = FakeBackend(fail={fam})
        r = E.execute(self.p, self.s, backend=b, log=lambda *a: None)
        self.assertEqual(len(r['failed']), 1)
        ev = C.read_journal(self.name)[self.p['jobs'][0]['job_id']]
        self.assertEqual(ev['event'], 'failed')
        self.assertIn('slabcutter found no period', ev['error'])   # the tool's own error text
        b2 = FakeBackend()
        E.execute(self.p, self.s, backend=b2, log=lambda *a: None)
        self.assertNotIn(self.p['jobs'][0]['job_id'], b2.calls)
        b3 = FakeBackend()
        E.execute(self.p, self.s, backend=b3, retry_failed=True, log=lambda *a: None)
        self.assertIn(self.p['jobs'][0]['job_id'], b3.calls)

    def test_resume_after_crash(self):
        jr = E.Journal(self.name)
        j0, j1 = self.p['jobs'][0], self.p['jobs'][1]
        # j0: started, its run finished (result.json there) but the driver died; j1: started, no result
        for j in (j0, j1):
            jr.write(job_id=j['job_id'], event='started', material_id=j['material_id'], family=j['family'], level=2)
        wd = os.path.join(jr.dir, 'jobs', j0['job_id'])
        os.makedirs(wd)
        json.dump({'ok': True, 'cpu_s': 100.0, 'outputs': {'gamma': 111.0, 'gamma_unrelaxed': 120.0}},
                  open(os.path.join(wd, 'result.json'), 'w'))
        b = FakeBackend()
        r = E.execute(self.p, self.s, backend=b, log=lambda *a: None)
        self.assertNotIn(j0['job_id'], b.calls)                     # recovered from its result
        self.assertIn(j1['job_id'], b.calls)                        # never finished: run again
        self.assertEqual(r['recovered'][0]['job_id'], j0['job_id'])
        self.assertEqual(C.read_journal(self.name)[j0['job_id']]['gamma'], 111.0)

    def test_recalibration_cap(self):
        s = spec(campaign=self.name + 'cal', policy='exhaustive', budget={'cpu_minutes': 100, 'per_run_cap_minutes': 4.5})
        p = make_plan(s)
        b = FakeBackend(factor=2.0)                                 # every job takes twice its estimate
        s['executor']['workers'] = 1
        r = E.execute(p, s, backend=b, log=lambda *a: None)
        self.assertTrue(any('over the per-run cap at run time' in (x.get('why') or '') for x in r['skipped']))

    def test_no_double_recalibration(self):
        s = spec(campaign=self.name + 'dbl', policy='exhaustive')
        p = make_plan(s)
        j0, j1 = p['jobs'][0], p['jobs'][1]
        journal = {j0['job_id']: {'job_id': j0['job_id'], 'event': 'done', 'time': 't', 'level': 2,
                                  'est_minutes': j0['est_minutes'], 'cpu_s': j0['est_minutes'] * 60 * 1.5}}
        E.Journal(s['campaign']).write(**journal[j0['job_id']])
        p2 = make_plan(s, journal=journal)
        j = next(x for x in p2['jobs'] if x['job_id'] == j1['job_id'])
        self.assertAlmostEqual(j['est_plan_minutes'], j1['est_minutes'])
        s['budget']['per_run_cap_minutes'] = j['est_minutes'] + 0.01      # fits once scaled, not twice
        p2['jobs'] = [j]
        b = FakeBackend()
        E.execute(p2, s, backend=b, log=lambda *a: None)
        self.assertEqual(b.calls, [j['job_id']])

    def test_budget_at_run_time(self):
        s = spec(campaign=self.name + 'bud', policy='exhaustive', budget={'cpu_minutes': 6, 'per_run_cap_minutes': 6})
        p = make_plan(s)
        r = E.execute(p, s, backend=FakeBackend(factor=1.5), log=lambda *a: None)
        self.assertTrue(r['skipped'])
        self.assertLessEqual(r['cpu_min_spent_campaign'], 6 * 1.5 + 1e-6)

    def test_policy_agent_refuses(self):
        s = spec(campaign=self.name + 'ag', policy='agent')
        with self.assertRaises(E.PolicyRefused):
            E.execute(make_plan(s), s, backend=FakeBackend())

    def test_local_backend_not_here(self):
        with self.assertRaises(E.BackendNotAvailable):
            E.execute(self.p, self.s, backend=E.LocalBackend(), log=lambda *a: None)

    def test_hub_backend_stubs(self):
        mat = self.p['_materials'][0]
        mat.route = dict(mat.route, datafile='/x/tnt.data', script='/x/in.lmp')
        job = self.p['jobs'][0]
        for B, tool_key in ((E.NanohubBackend, 'tool'), (E.AuraBackend, 'mcp_tool')):
            b = B({'revision': {'surfenergy': 7}})
            ok, why = b.available()
            self.assertFalse(ok)
            self.assertIn('not installed', why)
            d = b.describe(job, mat, self.s)
            self.assertEqual(d['job_id'], job['job_id'])
            c = d['calls'][0]
            if B is E.NanohubBackend:
                self.assertEqual(c['tool'], 'surfenergy')
                self.assertEqual(c['revision'], 7)
                self.assertEqual(c['inputs']['temperature'], 0.0)
                self.assertEqual(c['inputs']['slab_from'], 'relaxed_bulk')
                self.assertIn('simtool.Run', c['call'])
            else:
                self.assertEqual(c['arguments']['nH_tool'], 'surfenergy')
                self.assertEqual(list(c['arguments']['chosen_inputs'].get(k) for k in 'hkl'), list(job['hkl']))
            with self.assertRaises(E.BackendNotAvailable) as cm:
                b.run(job, mat, self.s, TMP, 60)
            self.assertIn('not available here', str(cm.exception))
            with self.assertRaises(E.BackendNotAvailable):
                E.execute(self.p, dict(self.s, executor={'backend': B.name, 'workers': 1}), backend=b)


class TestIngest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = spec(campaign='ingest_t')
        cls.p = make_plan(cls.s)
        cls.mat = cls.p['_materials'][0]
        d = tempfile.mkdtemp()
        data, script = os.path.join(d, 'cell.data'), os.path.join(d, 'in.lmp')
        open(data, 'w').write('fake data\n')
        open(script, 'w').write('fake script\n')
        cls.mat.route = dict(cls.mat.route, datafile=data, script=script)
        cls.X = C._export_module()
        cls.mats = cls.X.Materials([])

    def recs(self, job, res, ev=None):
        return I.job_records(job, ev or {'time': '2026-10-23T12:00:00-04:00'}, res, self.mat, self.s, self.X, self.mats,
                             'chain abc1234', [])

    def test_level2_record(self):
        job = dict(self.p['jobs'][0])
        res = {'ok': True, 'cpu_s': 238.0, 'wall_s': 234.7, 'backend': 'local',
               'run_dir': '/somewhere/surfenergy/bin/RUNS/9fc73014fd554d73',
               'outputs': {'gamma': 124.008, 'gamma_error': 0.0, 'gamma_unrelaxed': 136.08, 'termination_used': 0,
                           'gamma_flags': ['minimisation_not_converged: bulk: NOT converged: largest |P_ij| 41 atm'],
                           'slab_from': 'relaxed_bulk', 'slab_start': {'method': 'slabcutter_recut', 'sfe_unrelaxed_recut': 145.0},
                           'molecules_intact': True, 'faces_equivalent': True, 'n_terminations': 2,
                           'terminations_screened': [{'index': 0, 'group': 0}, {'index': 1, 'group': 0}],
                           'child_runs': {'supercell': {'run_id': 'supercell-f5f0'}, 'slabcutter': [{'run_id': 'slabcutter-1'}]},
                           'minimization': {'bulk': {'method': 'cg', 'converged': False, 'steps': 900}}}}
        recs = self.recs(job, res)
        self.assertEqual(len(recs), 1)
        self.assertEqual(I.validate(recs), [])
        r = recs[0]
        pv = r['provenance']
        self.assertEqual(pv['source'], 'campaign')
        self.assertEqual(pv['sim2l'], {'name': 'surfenergy', 'revision': 'chain abc1234'})
        self.assertEqual(pv['run_id'], '9fc73014fd554d73')
        self.assertEqual(sorted(pv['child_run_ids']), ['slabcutter-1', 'supercell-f5f0'])
        self.assertEqual(set(pv['input_md5']), {'datafile', 'script'})
        self.assertIn('./wh campaign run', pv['reproduce'])
        self.assertIn(job['job_id'], pv['reproduce'])
        self.assertEqual(r['protocol']['slab_from'], 'relaxed_bulk')
        fl = {f['code']: f for f in r['result']['gamma_flags']}
        self.assertEqual(fl['minimisation_not_converged']['by'], 'surfenergy')
        self.assertEqual(fl['minimisation_not_converged']['severity'], 'warning')
        self.assertNotIn('/home/', json.dumps(r))

    def test_guard_and_failed(self):
        job = dict(self.p['jobs'][0])
        res = {'ok': True, 'cpu_s': 1.0, 'run_dir': '/x/RUNS/r1',
               'outputs': {'gamma': 150.0, 'gamma_unrelaxed': 130.0, 'termination_used': 0, 'gamma_flags': []}}
        r = self.recs(job, res)[0]
        self.assertIn('relaxed_above_unrelaxed', [f['code'] for f in r['result']['gamma_flags'] if f['severity'] == 'error'])
        bad = self.recs(job, {'ok': False, 'error': 'RuntimeError: surfenergy failed (rc 1):\nERROR: no solution', 'cpu_s': 3})
        self.assertEqual(I.validate(bad), [])
        self.assertEqual(bad[0]['result']['status'], 'failed')
        self.assertIn('no solution', bad[0]['result']['error_message'])
        self.assertEqual(bad[0]['result']['gamma_flags'][-1]['code'], 'run_failed')

    def test_level1_records(self):
        job = dict(self.p['jobs'][0], level=1, level_name='unrelaxed')
        res = {'ok': True, 'cpu_s': 40.0, 'run_dir': '/st/campaigns/x/jobs/j-1',
               'child_runs': [{'tool': 'supercell', 'run_dir': '/st/children/RUNS/supercell-a'}],
               'outputs': {'gamma_unrelaxed': 118.24, 'termination_used': 2, 'n_terminations': 4,
                           'terminations_screened': [
                               {'index': 0, 'group': 0, 'sfe_unrelaxed': 140.6, 'sfe_source': 'computed', 'faces_equivalent': True},
                               {'index': 1, 'group': 0, 'sfe_unrelaxed': 140.6, 'sfe_source': 'group 0'},
                               {'index': 2, 'group': 2, 'sfe_unrelaxed': 118.24, 'sfe_source': 'computed', 'faces_equivalent': False},
                               {'index': 3, 'group': 2, 'sfe_unrelaxed': 118.24, 'sfe_source': 'group 2'}]}}
        recs = self.recs(job, res)
        self.assertEqual(I.validate(recs), [])
        self.assertEqual([r['termination']['index'] for r in recs], [0, 2])
        self.assertEqual([r['termination']['is_lowest'] for r in recs], [False, True])
        self.assertEqual(recs[1]['termination']['equivalent_indices'], [2, 3])
        self.assertEqual(recs[1]['provenance']['sim2l']['name'], 'supercell+slabcutter')
        self.assertEqual(recs[1]['provenance']['child_run_ids'], ['supercell-a'])
        self.assertIn('faces_not_equivalent', [f['code'] for f in recs[1]['result']['gamma_flags']])
        self.assertNotEqual(recs[0]['record_id'], recs[1]['record_id'])

    def test_store_refuses_home_paths(self):
        with self.assertRaises(RuntimeError):
            I.write_store('leak', [{'x': '/' + 'home/someone/file'}], store_dir=tempfile.mkdtemp())


class TestShapes(unittest.TestCase):
    def rec(self, fam, members, g, i, L):
        rep = [int(x) for x in fam.strip('{}').split()]
        return {'record_id': 'se-%012d' % i, 'material': {'material_id': 'm', 'name': 'm', 'cell': {'lattice_matrix': L},
                                                           'structure': {'structure_id': 's'}, 'space_group': {'laue_class': 'mmm'}},
                'force_field': {'name': 'ff', 'validated_for_material': False, 'note': 'n'},
                'facet': {'family': fam, 'hkl': rep, 'family_representative': rep, 'family_members': members, 'd_eff': 5.0},
                'termination': {'label': 't0'}, 'protocol': {'level': 1},
                'result': {'status': 'ok', 'gamma': g, 'gamma_flags': []}, 'provenance': {'source': 'campaign'}}

    def rec2(self, fam, members, g, gu, i, L, date='2026-10-23T12:00:00', flags=()):
        r = self.rec(fam, members, g, i, L)
        r['protocol'] = {'level': 2, 'temperature_K': 0.0}
        r['termination'] = {'label': 't0 (lowest unrelaxed)', 'is_lowest': True}
        r['result'].update(gamma_error=None, gamma_unrelaxed=gu, gamma_flags=list(flags))
        r['provenance'] = {'source': 'campaign', 'date': date}
        return r

    def test_best_shape_mmm(self):
        import export_dataset as X
        import shapes as S
        L = [[13.0, 0, 0], [0, 11.5, 0], [0, 0, 10.7]]
        m111 = [[a, b, c] for a in (1, -1) for b in (1, -1) for c in (1, -1)]
        fams = {'{1 0 0}': [[1, 0, 0], [-1, 0, 0]], '{0 1 0}': [[0, 1, 0], [0, -1, 0]], '{0 0 1}': [[0, 0, 1], [0, 0, -1]],
                '{1 1 1}': m111}
        recs = [self.rec(f, m, 200.0 + 10 * i, i, L) for i, (f, m) in enumerate(fams.items())]
        self.assertIsNone(S.best_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)[0])     # no level 2 yet
        recs += [self.rec2('{1 0 0}', fams['{1 0 0}'], 150.0, 190.0, 11, L),
                 self.rec2('{1 1 1}', m111, 160.0, 220.0, 12, L),
                 self.rec2('{0 1 0}', fams['{0 1 0}'], 300.0, 210.0, 13, L,            # fails the guard
                           flags=[X.flag('relaxed_above_unrelaxed', 'error', 'x', by='wulffhound-guard')])]
        s, why = S.best_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)
        self.assertIsNone(why)
        by = {f['family']: f for f in s['facets']}
        ratio = (150.0 / 200.0 + 160.0 / 230.0) / 2
        self.assertAlmostEqual(s['shape_metrics']['relax_ratio'], round(ratio, 5), places=5)
        self.assertEqual(by['{1 0 0}']['level'], 2)
        self.assertEqual(by['{0 0 1}']['level'], 1)
        self.assertAlmostEqual(by['{0 0 1}']['gamma'], round(220.0 * ratio, 4), places=3)
        self.assertEqual(by['{0 1 0}']['level'], 1)                                    # guard: level 1 used, flagged
        self.assertIn('level1_used_for_inconsistent_level2', [f['code'] for f in by['{0 1 0}']['flags']])
        self.assertEqual((s['checks']['laue_class'], s['checks']['pymatgen_recip_ops_order'],
                          s['checks']['pymatgen_recip_ops_symprec_1em3']), ('mmm', 8, 8))
        import jsonschema
        sch = json.load(open(os.path.join(ROOT, 'schema', 'surface_energy.schema.json')))
        jsonschema.validate(s, {'$schema': sch['$schema'], '$defs': sch['$defs'], '$ref': '#/$defs/wulff_shape'})
        # a Laue class that does not match the metric is refused (pymatgen would add faces)
        for r in recs:
            r['material']['space_group'] = {'laue_class': '2/m'}
        s2, why2 = S.level1_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)
        self.assertIsNone(s2)
        self.assertIn('2/m', why2)

    def test_level1_shape(self):
        import export_dataset as X
        L = [[6.0, 0, 0], [0, 7.0, 0], [0, 0, 8.0]]
        fams = {'{1 0 0}': [[1, 0, 0], [-1, 0, 0]], '{0 1 0}': [[0, 1, 0], [0, -1, 0]], '{0 0 1}': [[0, 0, 1], [0, 0, -1]]}
        recs = [self.rec(f, m, 100.0 + 10 * i, i, L) for i, (f, m) in enumerate(fams.items())]
        s, why = __import__('shapes').level1_shape(recs, X.flag, X.rid, X.SCHEMA_VERSION)
        self.assertIsNone(why)
        self.assertAlmostEqual(sum(f['area_fraction'] for f in s['facets']), 1.0, places=5)
        self.assertEqual(s['level'], '1')
        s2, why2 = __import__('shapes').level1_shape(recs[:2], X.flag, X.rid, X.SCHEMA_VERSION)
        self.assertIsNone(s2)
        self.assertIn('needs >= 3', why2)
        import jsonschema
        sch = json.load(open(os.path.join(ROOT, 'schema', 'surface_energy.schema.json')))
        jsonschema.validate(s, {'$schema': sch['$schema'], '$defs': sch['$defs'], '$ref': '#/$defs/wulff_shape'})


if __name__ == '__main__':
    unittest.main()

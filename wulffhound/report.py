"""Self-contained HTML report of a wulffhound run: 3D Wulff shape (plotly.js inlined, works offline), the BFDH
shape for comparison, the facet table with provenance, next-facet scores, convergence, the decision log and the
literature table ('from papers', kept apart from 'from our runs')."""
import html
import json
import os
import warnings

from ledger import now, rel

CSS = """
:root{--bg:#fbfaf7;--fg:#1d1d1f;--muted:#6b6b70;--line:#e2e0da;--card:#ffffff;--accent:#2f5d8a;--ok:#2e7d4f;
--warn:#a35b00;--bad:#a23a3a;--chip:#eef2f6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#16171a;--fg:#e8e8ea;--muted:#9a9aa2;
--line:#2c2e33;--card:#1e2024;--accent:#8db7e0;--ok:#6cc391;--warn:#e0a35a;--bad:#e07a7a;--chip:#262a31}}
:root[data-theme="dark"]{--bg:#16171a;--fg:#e8e8ea;--muted:#9a9aa2;--line:#2c2e33;--card:#1e2024;--accent:#8db7e0;
--ok:#6cc391;--warn:#e0a35a;--bad:#e07a7a;--chip:#262a31}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1180px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:26px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.sub{color:var(--muted);margin:0 0 16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
.kpis{display:flex;flex-wrap:wrap;gap:10px;margin:12px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 12px;min-width:140px}
.kpi b{display:block;font-size:20px}.kpi span{color:var(--muted);font-size:12px}
.tw{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:5px 7px;text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;white-space:nowrap}
td.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.chip{display:inline-block;background:var(--chip);border-radius:6px;padding:0 6px;font-size:12px;margin:1px}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.muted{color:var(--muted)}
code{font-size:12px;word-break:break-all}
.plot{width:100%;height:420px}
"""


def esc(x):
    return html.escape('' if x is None else str(x))


PALETTE = ['#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#76b7b2', '#edc948', '#b07aa1', '#ff9da7', '#9c755f',
           '#bab0ac', '#2f6f8f', '#8cd17d']


def _mesh(w, names, values, unit):
    """Per family: triangles (flattened) and edge segments of the Wulff polyhedron."""
    import numpy as np
    pts = np.array(w.wulff_pt_list)
    fam = {}
    for plane in w.facets:
        if not plane.points:
            continue
        d = fam.setdefault(plane.index, {'tri': [], 'edges': []})
        d['tri'].extend(np.array(t) for t in plane.points)
        d['edges'].extend(pts[[i0, i1]] for i0, i1 in plane.outer_lines)
    order = sorted(fam, key=lambda i: -w.area_fraction_dict.get(w.miller_list[i], 0))
    return [(i, names[i], values[i], w.area_fraction_dict.get(w.miller_list[i], 0.0), fam[i]) for i in order]


def _fig_html(w, title, include_js, names, values, unit):
    import plotly.graph_objects as go
    data = []
    for k, (i, name, val, area, d) in enumerate(_mesh(w, names, values, unit)):
        import numpy as np
        V = np.concatenate(d['tri'])
        n = len(V)
        data.append(go.Mesh3d(x=V[:, 0], y=V[:, 1], z=V[:, 2], i=list(range(0, n, 3)), j=list(range(1, n, 3)),
                              k=list(range(2, n, 3)), color=PALETTE[k % len(PALETTE)], opacity=1.0, flatshading=True,
                              name='%s %.0f%%' % (name, 100 * area), showlegend=True,
                              hovertemplate='%s<br>%s %s<br>area %.1f%%<extra></extra>' % (name, ('%.4g' % val), unit,
                                                                                           100 * area)))
        ex, ey, ez = [], [], []
        for seg in d['edges']:
            ex += [seg[0][0], seg[1][0], None]
            ey += [seg[0][1], seg[1][1], None]
            ez += [seg[0][2], seg[1][2], None]
        data.append(go.Scatter3d(x=ex, y=ey, z=ez, mode='lines', line=dict(color='#222', width=2), showlegend=False,
                                 hoverinfo='skip'))
    ax = dict(visible=False)
    fig = go.Figure(data=data)
    fig.update_layout(title=dict(text=title, font=dict(size=13)), margin=dict(l=0, r=0, t=36, b=0), height=440,
                      paper_bgcolor='rgba(0,0,0,0)', legend=dict(font=dict(size=11)),
                      scene=dict(xaxis=ax, yaxis=ax, zaxis=ax, aspectmode='data',
                                 camera=dict(eye=dict(x=1.4, y=-1.4, z=0.9))))
    return fig.to_html(include_plotlyjs=include_js, full_html=False, config={'displaylogo': False})


def _png(w, names, title):
    """Static three-view PNG (base64), the fallback when WebGL is unavailable."""
    import base64
    import io
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import numpy as np
    from mpl_toolkits.mplot3d import art3d
    fams = _mesh(w, names, [0] * len(names), '')
    pts = np.array(w.wulff_pt_list)
    fig = plt.figure(figsize=(9, 3.2))
    for k, (azim, elev, label) in enumerate(((-90, 0, 'along b (unique axis)'), (0, 0, 'along x (a)'),
                                            (-60, 25, 'perspective'))):
        ax = fig.add_subplot(1, 3, k + 1, projection='3d')
        for c, (i, name, val, area, d) in enumerate(fams):
            poly = art3d.Poly3DCollection(d['tri'], alpha=0.95)
            poly.set_facecolor(PALETTE[c % len(PALETTE)])
            poly.set_edgecolor('none')
            ax.add_collection3d(poly)
            for seg in d['edges']:
                ax.plot(seg[:, 0], seg[:, 1], seg[:, 2], color='k', lw=0.5)
        r = np.abs(pts).max()
        ax.set_xlim(-r, r); ax.set_ylim(-r, r); ax.set_zlim(-r, r)
        ax.set_box_aspect((1, 1, 1))
        ax.view_init(elev=elev, azim=azim)
        ax.set_axis_off()
        ax.set_title(label, fontsize=9)
    import matplotlib.patches as mpatches
    fig.legend(handles=[mpatches.Patch(color=PALETTE[c % len(PALETTE)], label='%s %.0f%%' % (name, 100 * area))
                        for c, (i, name, val, area, d) in enumerate(fams)], loc='lower center', ncol=6, fontsize=8,
               frameon=False)
    fig.suptitle(title, fontsize=10)
    fig.tight_layout(rect=(0, 0.1, 1, 0.95))
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=110)
    plt.close(fig)
    return '<img alt="%s" style="width:100%%;height:auto" src="data:image/png;base64,%s">' % (
        html.escape(title), base64.b64encode(buf.getvalue()).decode())


BASELINE_STATUS = {}


def write(eng, res, path, title=None):
    BASELINE_STATUS.clear()
    b = eng.bfdh()
    lit = eng.literature()
    dec = eng.decisions()
    led = eng.ledger
    title = title or 'Wulff shape of %s' % eng.mat['name']
    parts = []
    # ---- shapes
    figs, pngs = [], []
    js = 'cdn' if os.environ.get('WULFFHOUND_REPORT_PLOTLY') == 'cdn' else True   # cdn: ~0.3 MB file, needs internet
    if res and res.get('ok'):
        names = res['families_in_build']
        vals = [res['gamma_in_build'][f] for f in names]
        t = 'Computed T = 0 Wulff shape (level %s)' % res['level']
        figs.append(_fig_html(eng._last_w, t, js, names, vals, 'mJ/m²'))
        pngs.append(_png(eng._last_w, names, t))
        js = False
    bn = [f['family'] for f in eng.sym['families']]
    t = 'BFDH shape (geometry only, gamma ~ 1/d_eff)'
    figs.append(_fig_html(b['wulff'], t, js, bn, [1.0 / f['d_eff'] for f in eng.sym['families']], '1/Å (1/d_eff)'))
    pngs.append(_png(b['wulff'], bn, t))

    # ---- KPIs
    live = sum(a['charge_cpu_s'] for a in eng.acquired if not a['cached']) / 60.0
    replay = sum(a['charge_cpu_s'] for a in eng.acquired if a['cached']) / 60.0
    conv = (res or {}).get('convergence') or {}
    n1 = len({a['family'] for a in eng.acquired if a['level'] == 1 and a['ok']})
    n2 = len({a['family'] for a in eng.acquired if a['level'] == 2 and a['ok']})
    kp = [('%d' % n1, 'families screened (level 1)'), ('%d' % n2, 'families relaxed (level 2)'),
          ('%.1f min' % (live + replay), 'CPU charged%s' % (' of %.0f budget' % eng.budget_min if eng.budget_min else '')),
          ('%.1f / %.1f' % (live, replay), 'CPU min live / replayed from cache'),
          ('%s' % (res.get('shape_factor') if res and res.get('ok') else '-'), 'shape factor'),
          ('<span class="%s">%s</span>' % (('ok', 'converged') if conv.get('converged') else ('warn', 'not converged')),
           'stop rule')]
    info0 = eng.material_info()
    if info0.get('forcefield_validated'):
        parts.append('<p class="ok">Force field validated for this material: %s</p>' % esc(info0['force_field']))
    else:
        parts.append('<p class="bad"><b>Screening-level result: the force field is NOT validated for this material.</b> '
                     '%s %s</p>' % (esc(info0['force_field']), esc(info0.get('forcefield_note') or '')))
    if res and res.get('inconsistent'):
        parts.append('<p class="warn"><b>Consistency guard:</b> %s</p>' % '; '.join(
            '%s excluded (%s; %s)' % (esc(x['family']), esc(x['reason']), esc(x['used'])) for x in res['inconsistent']))
    if res and res.get('rejected_level2_runs'):
        parts.append('<p class="warn"><b>Rejected level-2 runs:</b> %s</p>' % '; '.join(
            '%s (n_periods %s, gamma %s, run %s): %s; used: %s' % (esc(x['family']), esc(x['n_periods']), esc(x['gamma']),
                                                                 esc(x['run_dir']), esc(x['reason']), esc(x['used']))
            for x in res['rejected_level2_runs']))
    parts.append('<p class="muted">Mode: %s.</p>' % esc(info0.get('mode_note')))
    parts.append('<div class="kpis">%s</div>' % ''.join('<div class="kpi"><b>%s</b><span>%s</span></div>' % (v, esc(k))
                                                         for v, k in kp))
    parts.append('<div class="grid">%s</div>' % ''.join('<div class="card">%s</div>' % p for p in pngs))
    parts.append('<details open><summary class="muted">Interactive 3D (drag to rotate; needs WebGL)</summary>'
                 '<div class="grid">%s</div></details>' % ''.join('<div class="card">%s</div>' % f for f in figs))
    if res and not res.get('ok'):
        parts.append('<p class="bad">%s</p>' % esc(res.get('error')))

    # ---- facet table
    rows = {r['family']: r for r in (res or {}).get('facets', [])}
    area_b = {r['family']: r['area_fraction'] for r in b['facets']}
    lit_by = {}
    for x in lit:
        if x.get('family_our_setting'):
            lit_by.setdefault(x['family_our_setting'], []).append(x)
    fams = [f for f in eng.sym['families'] if f['family'] in rows or f['family'] in eng.have_families()
            or area_b.get(f['family'], 0) > 1e-4 or f['family'] in lit_by]
    trs = []
    for f in fams:
        fam = f['family']
        r = rows.get(fam, {})
        e1, e2 = eng.have(fam, 1), eng.have(fam, 2)
        prov = []
        for e in (e2, e1):
            if e:
                prov.append('L%d <code>%s</code> <span class="muted">%s%s</span>' % (
                    e['level'], esc(e.get('run_dir')), esc(e.get('source')),
                    ', %.0f s CPU' % e['cpu_s'] if e.get('cpu_s') else ''))
        papers = '; '.join('%s%s <span class="muted">[%s]</span>' % (
            esc(x.get('kind')), (': ' + esc(x['value_reported'])) if x.get('value_reported') else '', esc(x.get('citation')))
            for x in lit_by.get(fam, []))
        if r:
            area = '%.3f &plusmn; %.3f' % (r['area_fraction'], r['area_std'])
            on = '<span class="ok">on</span>' if r['on_shape'] else (
                'off; needs &minus;%s%s' % (esc(r.get('reduction_to_appear')), ' <span class="warn">(within 1&sigma;)</span>'
                                             if r.get('within_sigma') else ''))
            used = '%.1f &plusmn; %.1f <span class="muted">L%s</span>%s' % (
                r['gamma'], r['sigma'], r['level'], ' <span class="warn">L2 inconsistent, excluded</span>'
                if r.get('flag') else '')
        else:
            area, on, used = '-', '<span class="muted">not computed</span>', '-'
        trs.append('<tr><td><b>%s</b></td><td class="n">%d</td><td class="n">%.2f</td><td class="n">%.3f</td>'
                   '<td class="n">%s</td><td class="n">%s</td><td class="n">%s</td><td class="n">%s</td><td>%s</td>'
                   '<td>%s</td><td>%s</td></tr>' % (
                       esc(fam), f['bfdh_rank'], f['d_eff'], area_b.get(fam, 0.0),
                       '%.1f' % e1['gamma'] if e1 else '-', '%.1f' % e2['gamma'] if e2 else '-', used, area, on,
                       papers or '<span class="muted">-</span>', '<br>'.join(prov) or '-'))
    parts.append('<h2>Facets</h2><p class="sub">gamma in mJ/m&sup2; from our runs (L1 unrelaxed screening, lowest of all '
                 'terminations; L2 relaxed T = 0). "Used" is what the shape was built from; a level-1 value in a mixed '
                 'build is scaled by the mean relaxed/unrelaxed ratio of the relaxed families (shown in its basis). '
                 'Area = area fraction &plusmn; std over %s resamples of every gamma within its sigma. '
                 '"From papers" is literature the agent recorded, never mixed into our numbers.</p>'
                 % esc((res or {}).get('n_samples', '-')))
    parts.append('<div class="tw"><table><tr><th>family</th><th>BFDH rank</th><th>d_eff (&Aring;)</th><th>BFDH area</th>'
                 '<th>L1 &gamma;</th><th>L2 &gamma;</th><th>used &gamma; &plusmn; &sigma;</th><th>area fraction</th>'
                 '<th>on shape</th><th>from papers</th><th>provenance (run dir, source)</th></tr>%s</table></div>'
                 % ''.join(trs))
    if res and res.get('warnings'):
        parts.append('<ul>%s</ul>' % ''.join('<li class="warn">%s</li>' % esc(w) for w in res['warnings']))

    # ---- next + convergence
    if res:
        parts.append('<h2>Stop rule and next most informative facets</h2>')
        parts.append('<p>%s</p>' % ('<span class="ok">Converged</span>: every on-shape family relaxed, area fractions '
                                    'stable, nothing within 1&sigma; of appearing.' if conv.get('converged') else
                                    '<span class="warn">Open:</span> ' + '; '.join(esc(x) for x in conv.get('open', []))))
        nx = res.get('next') or []
        parts.append('<div class="tw"><table><tr><th>family</th><th>next level</th><th>score</th><th>relevance</th>'
                     '<th>gain</th><th>est. CPU min</th><th>why</th></tr>%s</table></div>' % ''.join(
                         '<tr><td>%s</td><td class="n">%d</td><td class="n">%.3f</td><td class="n">%.2f</td>'
                         '<td class="n">%.2f</td><td class="n">%.1f %s</td><td>%s</td></tr>' % (
                             esc(x['family']), x['next_level'], x['score'], x['relevance'], x['gain'], x['est_minutes'],
                             '<span class="warn">rough</span>' if x['cost_basis'] == 'ROUGH' else '', esc(x['why']))
                         for x in nx))

    # ---- agent vs baseline (same budget accounting)
    bpath = os.path.join(eng.dir, 'baseline', 'summary.json')
    if eng.session != 'baseline' and os.path.isfile(bpath) and res and res.get('ok'):
        bs = json.load(open(bpath))
        mine = {r['family']: r['area_fraction'] for r in res.get('facets', [])}
        theirs = bs.get('area_fractions') or {}
        fams_c = sorted(set(mine) | set(theirs), key=lambda f: -max(mine.get(f, 0), theirs.get(f, 0)))
        last_acq = max([a.get('time') or '' for a in eng.acquired] or [''])
        stale = bool(not bs.get('written') or (bs.get('written') or '') < last_acq)
        BASELINE_STATUS['stale'] = stale
        bb, mb = bs.get('budget') or {}, eng.budget()
        parts.append('<h2>This session vs the deterministic baseline</h2><p class="sub">Baseline: BFDH-order '
                     'screening then greedy next-facet score, no LLM (<code>baseline.py</code>, its own report in '
                     '<code>state/%s/baseline/report.html</code>), run %s, mode: %s. CPU charged: this session %.1f '
                     'min (%s live runs), baseline %.1f min (%s live runs) of %s. Converged: this session %s, baseline '
                     '%s.</p>' % (
                         esc(eng.material), esc(bs.get('written') or 'at an unknown time'),
                         esc(bs.get('mode') or 'unknown'), eng.spent_min(), esc(mb.get('live_runs_used', '-')),
                         bb.get('spent_cpu_min', 0), esc(bb.get('live_runs_used', '-')),
                         esc(bb.get('budget_cpu_min', '-')), esc(conv.get('converged')), esc(bs.get('converged'))))
        if stale:
            parts.append('<p class="warn">The baseline is older than this session\'s last run (%s), so the '
                         'comparison is out of date: run <code>./wh compare</code> (baseline with this '
                         'session\'s budget and live-run allowance, then this report again).</p>' % esc(last_acq))
        def how(acq, f):
            xs = sorted([x for x in acq if x.get('family') == f and not x.get('note')], key=lambda x: x['level'])
            return ', '.join('L%d %s%s' % (x['level'], 'live' if x.get('live', not x.get('cached')) else 'cache',
                                          '' if x.get('ok', True) else ' (failed)') for x in xs) or '-'
        bacq = bs.get('acquired') or []
        fams_c = sorted(set(fams_c) | {x['family'] for x in eng.acquired} | {x['family'] for x in bacq},
                        key=lambda f: -max(mine.get(f, 0), theirs.get(f, 0)))
        parts.append('<div class="tw"><table><tr><th>family</th><th>area, this session</th><th>area, baseline</th>'
                     '<th>acquired, this session</th><th>acquired, baseline</th></tr>%s'
                     '</table></div>' % ''.join('<tr><td>%s</td><td class="n">%s</td><td class="n">%s</td><td>%s</td>'
                                                '<td>%s</td></tr>' % (
                         esc(f), '%.3f' % mine[f] if f in mine else '-', '%.3f' % theirs[f] if f in theirs else '-',
                         esc(how(eng.acquired, f)), esc(how(bacq, f)))
                         for f in fams_c))

    # ---- decisions
    parts.append('<h2>Decision log <span class="muted">(session %s)</span></h2>' % esc(eng.session))
    if dec:
        parts.append('<div class="tw"><table><tr><th>time</th><th>actor</th><th>action</th><th>why</th><th>expected '
                     'CPU min</th><th>outcome</th></tr>%s</table></div>' % ''.join(
                         '<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="n">%s</td><td>%s</td></tr>' % (
                             esc(d.get('time', '')[11:19]), esc(d.get('actor')), esc(d.get('action')), esc(d.get('why')),
                             esc(d.get('expected_cost_minutes')), esc(d.get('outcome'))) for d in dec[-200:]))
    else:
        parts.append('<p class="muted">No decisions logged yet.</p>')

    # ---- literature
    parts.append('<h2>From papers (literature via MCP Bond)</h2><p class="sub">Claims recorded by the agent with their '
                 'citation and the Miller-setting conversion it did. They steer what is computed and what the result is '
                 'compared with; they are never used as a gamma.</p>')
    if lit:
        parts.append('<div class="tw"><table><tr><th>citation</th><th>kind</th><th>claim</th><th>hkl as reported</th>'
                     '<th>setting conversion</th><th>our family</th><th>value reported</th></tr>%s</table></div>' % ''.join(
                         '<tr><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                             esc(x.get('citation')), ('<br><code>%s</code>' % esc(x['doi_or_url'])) if x.get('doi_or_url')
                             else '', esc(x.get('kind')), esc(x.get('claim')), esc(x.get('hkl_reported')),
                             esc(x.get('setting_conversion')), esc(x.get('family_our_setting')), esc(x.get('value_reported')))
                         for x in lit))
    else:
        parts.append('<p class="muted">No literature recorded (MCP Bond not connected, or nothing found).</p>')

    # ---- method, provenance, limits
    info = eng.material_info()
    chk = (res or {}).get('checks') or eng.checks()
    parts.append('<h2>Material, method and provenance</h2><div class="grid"><div class="card"><p><b>%s</b><br>%s<br>'
                 'unit cell <code>%s</code> md5 <code>%s</code><br>force field %s, script <code>%s</code> md5 <code>%s'
                 '</code><br>lattice %s<br>space group (spglib) %s; %s</p></div><div class="card"><p>Wulff construction: '
                 'pymatgen WulffShape on the data-file lattice. Convention checks: %s.<br>Levels: 0 BFDH (1/d_eff, '
                 'extinction-corrected), 1 unrelaxed screening of every termination (supercell + slabcutter), 2 relaxed '
                 'T = 0 (surfenergy temperature 0). Every gamma above comes from a run listed in the ledger <code>%s'
                 '</code>, with its run directory, inputs, the md5 of the data file and whether it was cached '
                 '(imported or identical request).</p><p class="muted">Limits: T = 0 only (static surface energies, the '
                 'standard Wulff input; no entropy); force field validated for this material: %s; molecular crystals '
                 'of H, C, N, O; the Wulff shape is an equilibrium shape, not a growth habit (compare with BFDH and '
                 'observed habits, not as truth).</p></div></div>' % (
                     esc(info['name']), esc(info['route']), esc(info['datafile']), esc(info['datafile_md5']),
                     esc(info['force_field']), esc(info['script']), esc(info['script_md5']),
                     esc(', '.join('%s %s' % (k, v) for k, v in info['lattice_parameters'].items())),
                     esc(info['spacegroup_spglib']['international_data_setting']), esc(info['miller_setting']),
                     esc(', '.join('%s %s' % (k, v) for k, v in chk.items())), esc(rel(led.path)),
                     'yes' if info.get('forcefield_validated') else 'NO (screening-level numbers)'))
    parts.append('<p class="muted">Credits: GCCM (Kroonblawd et al., CPC 207, 232 (2016); supercell orientation search, '
                 'GPLv3+, not included), LAMMPS (GPL, not included), pymatgen (WulffShape, MIT), spglib, Allen &amp; '
                 'Bruno 2010 (X-H normalisation of the CIF route), COD (CC0). %sGenerated %s by wulffhound.</p>' % (
                     ('Structure: %s. ' % esc(info.get('source_structure'))) if info.get('source_structure') else '', now()))
    doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,'
           'initial-scale=1"><title>Wulff Shape Report</title><style>%s</style></head><body><main><h1>%s</h1>'
           '<p class="sub">An AI agent that predicts a crystal\'s shape from its CIF, simulating only the facets that '
           'matter. Report level: %s.</p>%s</main></body></html>' % (CSS, esc(title), esc((res or {}).get('level', 0)),
                                                                      '\n'.join(parts)))
    with open(path, 'w') as f:
        f.write(doc)
    out = {'report': path, 'bytes': os.path.getsize(path)}
    if BASELINE_STATUS.get('stale') is not None:
        out['baseline_comparison_stale'] = BASELINE_STATUS['stale']
        if BASELINE_STATUS['stale']:
            out['note'] = ('the agent-vs-baseline section is out of date (baseline older than this session\'s last '
                           'run): the operator runs `./wh compare` after the session')
    return out

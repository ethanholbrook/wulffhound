#!/usr/bin/env python
"""Headline figure docs/headline_paracetamol.png, replay only.

Rebuilds the 22 Oct 2026 demo agent session's T = 0 Wulff shape (level 'best') from the committed replay bundle
(data/<material>/ledger.json) with the same acquisitions as docs/demo/<material>/decisions.jsonl, checks the area
fractions against docs/demo/<material>/final_answer.md to 3 decimals, and draws BFDH vs agent. Habit faces are
read from docs/demo/paracetamol/literature.jsonl (kind == 'habit'). No live run, no chain needed.

    ./wh py tools/make_headline.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault('WULFFHOUND_CHAIN_ROOT', '/nonexistent')   # replay only, never touch a chain

import matplotlib  # noqa: E402
import matplotlib.colors  # noqa: E402,F401
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from mpl_toolkits.mplot3d import art3d  # noqa: E402

from wulffhound.engine import Engine  # noqa: E402

# Acquisitions of the demo sessions (docs/demo/<m>/decisions.jsonl and final_answer.md). Failed live runs add no
# gamma, so they are left out. Expected 'best' area fractions: the tables of docs/demo/<m>/final_answer.md.
SESSIONS = {
    'paracetamol': {
        'L1': ['{0 1 1}', '{1 0 -1}', '{0 0 1}', '{1 0 1}', '{1 1 0}', '{1 1 -1}', '{1 1 1}', '{0 1 0}', '{0 2 1}',
               '{1 1 -2}', '{1 2 0}', '{1 1 2}', '{1 2 -1}', '{1 0 -3}', '{1 2 1}', '{1 0 0}', '{1 2 -2}'],
        'L2': ['{0 0 1}'],
        'expect': {'{1 1 -1}': 0.3987, '{0 1 1}': 0.2656, '{1 0 1}': 0.1329, '{1 0 -1}': 0.0741,
                   '{0 0 1}': 0.0654, '{0 1 0}': 0.025, '{1 2 0}': 0.0185, '{1 1 0}': 0.0091},
    },
}

# One colour per family, the same in every panel (Tableau-10 order)
COLOURS = ['#4e79a7', '#f28e2b', '#59a14f', '#e15759', '#76b7b2', '#edc948', '#b07aa1', '#ff9da7', '#9c755f',
           '#bab0ac', '#2f6f8f', '#8cd17d', '#d4a6c8', '#86bcb6', '#f1ce63', '#a0cbe8']


def to_a(fam):
    """P2_1/n family -> P2_1/a label. Inverse of h_n = -l_a, k_n = k_a, l_n = h_a + l_a (AGENT.md,
    material_info.setting_map): (h k l)_n -> (h+l, k, -h)_a, written as a 2/m-equivalent with most indices >= 0."""
    h, k, l = (int(x) for x in fam.strip('{}').split())
    a = (h + l, k, -h)
    orbit = [a, tuple(-x for x in a), (a[0], -a[1], a[2]), (-a[0], a[1], -a[2])]
    best = max(orbit, key=lambda v: (sum(x >= 0 for x in v), v))
    return '(%s)' % ' '.join(str(x).replace('-', '−') for x in best)


def label(fam, material):
    s = fam.replace('-', '−')
    return s + '\n' + to_a(fam) + '$_a$' if material == 'paracetamol' else s


def session_engine(material, tmp):
    eng = Engine(material, state_dir=tmp)
    S = SESSIONS[material]
    for lvl in (2, 1):
        for fam in S['L%d' % lvl]:
            e = eng.ledger.best(fam, lvl)
            assert e is not None, '%s level %d not in the bundle ledger' % (fam, lvl)
            eng._acquire(fam, lvl, e, 0.0, True)
    res = eng.build_wulff('best')
    assert res.get('ok'), res.get('error')
    got = {r['family']: r['area_fraction'] for r in res['facets']}
    bad = {f: (v, got.get(f)) for f, v in S['expect'].items() if abs(round(got.get(f, -1), 3) - round(v, 3)) > 1e-9}
    if bad:
        raise SystemExit('%s: area fractions differ from final_answer.md (expected, got): %s' % (material, bad))
    print('%s: area fractions match final_answer.md to 3 decimals' % material)
    return eng, res


def draw_shape(ax, w, names, colour, material, n_label=4, title=''):
    """Front-facing facets only (back-face culling: the shape is convex, so no depth sorting is needed)."""
    pts = np.array(w.wulff_pt_list)
    elev, azim = 22, -58
    view = np.array([np.cos(np.radians(elev)) * np.cos(np.radians(azim)),
                     np.cos(np.radians(elev)) * np.sin(np.radians(azim)), np.sin(np.radians(elev))])
    ax.computed_zorder = False
    area = {names[i]: w.area_fraction_dict.get(w.miller_list[i], 0.0) for i in range(len(names))}
    best = {}
    for plane in w.facets:
        if not plane.points:
            continue
        s = float(np.dot(plane.normal / np.linalg.norm(plane.normal), view))
        if s <= 1e-6:
            continue
        name = names[plane.index]
        tri = [np.array(t) for t in plane.points]
        shade = 0.55 + 0.45 * s
        rgb = np.array(matplotlib.colors.to_rgb(colour[name])) * shade + (1 - shade)
        poly = art3d.Poly3DCollection(tri, alpha=1.0)
        poly.set_facecolor(np.clip(rgb, 0, 1)); poly.set_edgecolor(np.clip(rgb, 0, 1)); poly.set_linewidth(0.3)
        ax.add_collection3d(poly)
        for i0, i1 in plane.outer_lines:
            seg = pts[[i0, i1]]
            ax.plot(seg[:, 0], seg[:, 1], seg[:, 2], color='#222222', lw=0.6)
        a = sum(0.5 * np.linalg.norm(np.cross(t[1] - t[0], t[2] - t[0])) for t in tri) * s
        if a > best.get(name, (None, -1))[1]:
            best[name] = (np.mean(np.vstack(tri), axis=0), a)
    top = sorted(area, key=lambda f: -area[f])[:n_label]
    for name in top:
        if name in best:
            ax.text(*best[name][0], label(name, material), fontsize=7.5, ha='center', va='center', zorder=10,
                    bbox=dict(boxstyle='round,pad=0.2', fc='white', ec='none', alpha=0.85))
    r = np.abs(pts).max() * 0.8
    ax.set_xlim(-r, r); ax.set_ylim(-r, r); ax.set_zlim(-r, r)
    ax.set_box_aspect((1, 1, 1))
    ax.view_init(elev=elev, azim=azim)
    ax.set_axis_off()
    ax.set_title(title, fontsize=10.5, loc='left')


def colours_for(families):
    return {f: COLOURS[k % len(COLOURS)] for k, f in enumerate(families)}


def paracetamol(tmp):
    eng, res = session_engine('paracetamol', tmp)
    b = eng.bfdh()
    area_b = {r['family']: r['area_fraction'] for r in b['facets']}
    area_a = {r['family']: r['area_fraction'] for r in res['facets']}
    habit = {}
    for line in open(os.path.join(ROOT, 'docs', 'demo', 'paracetamol', 'literature.jsonl')):
        d = json.loads(line)
        if d.get('kind') == 'habit' and d.get('family_our_setting'):
            habit[d['family_our_setting']] = True
    order = sorted(set(area_a) | set(area_b), key=lambda f: -max(area_a.get(f, 0), area_b.get(f, 0)))
    colour = colours_for(order)
    fams_b = [f['family'] for f in eng.sym['families']]          # the order bfdh() built its WulffShape in

    fig = plt.figure(figsize=(13, 4.9), facecolor='white')
    ax1 = fig.add_subplot(1, 3, 1, projection='3d')
    draw_shape(ax1, b['wulff'], fams_b, colour, 'paracetamol', title='(a) BFDH (geometry only)')
    ax2 = fig.add_subplot(1, 3, 2, projection='3d')
    draw_shape(ax2, eng._last_w, res['families_in_build'], colour, 'paracetamol',
               title='(b) Agent session, T = 0 Wulff shape')
    ax3 = fig.add_subplot(1, 3, 3)
    show = [f for f in order if max(area_a.get(f, 0), area_b.get(f, 0)) >= 0.005][:10]
    y = np.arange(len(show))[::-1]
    ax3.barh(y + 0.19, [area_b.get(f, 0) for f in show], height=0.36, color='white',
             edgecolor=[colour[f] for f in show], hatch='////', lw=1.0, label='BFDH (hatched)')
    ax3.barh(y - 0.19, [area_a.get(f, 0) for f in show], height=0.36, color=[colour[f] for f in show],
             label='agent, T = 0 (solid)')
    ax3.set_yticks(y)
    ax3.set_yticklabels(['%s  %s$_a$%s' % (f.replace('-', '−'), to_a(f), '  ★' if f in habit else '')
                         for f in show], fontsize=8.5)
    for t, f in zip(ax3.get_yticklabels(), show):
        if f in habit:
            t.set_fontweight('bold')
    ax3.set_xlabel('area fraction', fontsize=9)
    ax3.set_xlim(0, 0.55)
    ax3.spines[['top', 'right']].set_visible(False)
    ax3.tick_params(axis='x', labelsize=8)
    leg = ax3.legend(fontsize=8, frameon=False, loc='lower right')
    for h in leg.legend_handles:
        h.set_edgecolor('#555555'); h.set_facecolor('white' if h.get_hatch() else '#555555')
    ax3.set_title('(c) Area per family, ours / P2$_1$/a', fontsize=10.5, loc='left')
    ax3.text(0.0, -0.17, '★ observed growth face (Prasad 2001, 2002; Liu 2020; Heng 2006),\n'
             '   from docs/demo/paracetamol/literature.jsonl', transform=ax3.transAxes, fontsize=7.5,
             color='#444444', va='top')
    fig.suptitle('Paracetamol form I: {hkl} in our P2$_1$/n cell, (hkl)$_a$ in the literature\'s P2$_1$/a. Screening-level: '
                 'ReaxFF-lg not validated for paracetamol; shape not converged.', fontsize=9.5, color='#333333',
                 y=0.995)
    fig.subplots_adjust(left=0.0, right=0.985, top=0.9, bottom=0.2, wspace=0.3)
    out = os.path.join(ROOT, 'docs', 'headline_paracetamol.png')
    fig.savefig(out, dpi=150, facecolor='white')
    plt.close(fig)
    print('wrote', os.path.relpath(out, ROOT))


def main():
    with tempfile.TemporaryDirectory() as tmp:
        paracetamol(tmp)


if __name__ == '__main__':
    main()

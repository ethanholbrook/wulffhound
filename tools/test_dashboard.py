#!/usr/bin/env python
"""Headless browser test of the dashboard (prototype): serves the repository root, opens dashboard/ in headless
Firefox (selenium + geckodriver) and checks

  1. every page loads with no JS errors (overview, explorer, compare, the material pages, a record, a shape);
  2. the TNT Wulff shape renders (plotly mesh3d faces in #plot3d);
  3. the browser Wulff (wulff.js) area fractions equal pymatgen's (the wulff_shape records) to 3 decimals, for
     every shape in the dataset, and the BFDH ones too;
  4. moving the what-if slider changes the shape (area fractions and face geometry);
  5. phone width (390 px): no horizontal page scroll;
  6. campaign driver: a driver-ingested record (provenance.source campaign) has its record page, and a material whose
     shape a campaign completed renders it, with the campaign record linked in its facet table;
  7. the production campaign materials (CAMPAIGN_SHAPES: alpha-RDX mmm, beta-HMX 2/m, ...): each page renders the
     'best' shape built from the campaign's relaxed records (and offers the level-1 one), with a level-2 record
     linked, and both shapes record the Laue class with as many pymatgen operations (mmm 8, 2/m 4, ...);
and saves small screenshots under docs/dashboard/ (--screenshots).

    python tools/test_dashboard.py [--screenshots] [--port 8766]
      (needs selenium + Firefox + geckodriver, e.g. in a nb-preview env: FIREFOX / GECKODRIVER override the paths;
       the page loads plotly.js from cdn.jsdelivr.net, so it needs network access)
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAIL = []
# production campaign materials: (material_id, name, Laue class of the space group, its operations, screenshot)
CAMPAIGN_SHAPES = [('rdx-alpha', 'alpha-RDX', 'mmm', 8, 'rdx_material'),        # campaigns/rdx_wulff.yaml, Pbca
                   ('hmx-beta', 'beta-HMX', '2/m', 4, 'hmx_material')]         # campaigns/hmx_wulff.yaml, P2_1/n


def check(ok, what):
    print('%s %s' % ('PASS' if ok else 'FAIL', what))
    if not ok:
        FAIL.append(what)
    return ok


def free_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    p = s.getsockname()[1]
    s.close()
    return p


def find(name, env_var):
    if os.environ.get(env_var):
        return os.environ[env_var]
    for d in (os.path.dirname(sys.executable), os.path.expanduser('~/micromamba/envs/nb-preview/bin')):
        p = os.path.join(d, name)
        if os.path.exists(p):
            return p
    return shutil.which(name)


COMPRESS = '''
import sys
from PIL import Image
p = sys.argv[1]
im = Image.open(p).convert('RGB')
w, h = im.size
if w > 1000:
    im = im.resize((w * 3 // 4, h * 3 // 4), Image.LANCZOS)
im.quantize(colors=96).save(p, optimize=True)
'''


def compress(p):
    """Small PNGs for the README (3/4 scale, 96 colours) with Pillow: this Python, else $WULFFHOUND_PY / wulff-dev."""
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    for py in (sys.executable, os.environ.get('WULFFHOUND_PY'), os.path.expanduser('~/micromamba/envs/wulff-dev/bin/python')):
        if py and os.path.exists(py) and subprocess.run([py, '-s', '-c', COMPRESS, p], env=env,
                                                         capture_output=True).returncode == 0:
            return True
    print('  (screenshot not compressed: no Pillow found)')
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=0)
    ap.add_argument('--screenshots', action='store_true')
    ap.add_argument('--out', default=os.path.join(ROOT, 'docs', 'dashboard'))
    args = ap.parse_args()
    from selenium import webdriver
    from selenium.webdriver.firefox.options import Options
    from selenium.webdriver.firefox.service import Service

    port = args.port or free_port()
    srv = subprocess.Popen([sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1'], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    o = Options()
    o.add_argument('-headless')
    o.binary_location = find('firefox', 'FIREFOX')
    drv = webdriver.Firefox(options=o, service=Service(find('geckodriver', 'GECKODRIVER')))
    base = 'http://127.0.0.1:%d/dashboard/' % port
    js = drv.execute_script

    def wait(cond, timeout=40, what='condition'):
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if js('return ' + cond):
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def errors():
        return js('return (window.__early || []).concat((window.__dash && window.__dash.errors) || [])') or []

    def goto(h, settle=0.8):
        n = js('return window.__dash ? window.__dash.renders : 0') or 0
        drv.execute_script('location.hash = arguments[0]', h)
        wait('window.__dash && window.__dash.renders > %d' % n, 20)
        time.sleep(settle)

    def shot(name, w=1280, h=900):
        if not args.screenshots:
            return
        drv.set_window_size(w, h)
        time.sleep(1.2)
        os.makedirs(args.out, exist_ok=True)
        p = os.path.join(args.out, name + '.png')
        drv.save_screenshot(p)
        compress(p)
        print('  screenshot %s (%d kB)' % (os.path.relpath(p, ROOT), os.path.getsize(p) // 1024))

    try:
        drv.set_window_size(1280, 900)
        drv.get(base + '#/')
        check(wait('window.__dash && window.__dash.ready', 60), 'dataset loaded (index.json, surface_energy.jsonl, wulff_shape.jsonl)')
        check(wait('typeof Plotly !== "undefined"', 30), 'plotly.js loaded from the CDN')
        n = js('return document.querySelectorAll(".matcard").length')
        check(n > 20, 'overview lists the materials (%d cards)' % n)
        check(not errors(), 'overview: no JS errors %s' % errors())
        shot('overview')

        # material page: TNT
        goto('#/m/tnt-monoclinic', 2.0)
        ntr = js('var p=document.getElementById("plot3d"); return p && p.data ? p.data.filter(function(t){return t.type==="mesh3d"}).length : 0')
        check(ntr > 10, 'TNT Wulff shape renders (%d mesh3d faces)' % ntr)
        sc = js('return document.getElementById("selfcheck").textContent')
        check('agree to 3 decimals' in (sc or ''), 'TNT page self-check: %s' % (sc or '').strip())
        rows = js('return document.querySelectorAll("table tbody tr").length')
        check(rows > 10, 'TNT facet table (%d rows)' % rows)
        links = js('return Array.from(document.querySelectorAll("table tbody td.num a")).filter(function(a){return a.getAttribute("href").indexOf("#/record/")===0}).length')
        check(links >= rows, 'every gamma in the facet table links to its record (%d links)' % links)
        check(not errors(), 'TNT page: no JS errors %s' % errors())
        shot('tnt_material', 1280, 1500)

        # JS vs pymatgen for every shape in the dataset (and BFDH)
        shapes = [json.loads(l) for l in open(os.path.join(ROOT, 'dataset', 'wulff_shape.jsonl')) if l.strip()]
        worst = 0.0
        for s in shapes:
            r = js("""var s = arguments[0];
              var w = Wulff.build(s.lattice_matrix, s.facets.map(function(f){return {family:f.family, members:f.members, gamma:f.gamma};}));
              var b = Wulff.build(s.lattice_matrix, s.facets.map(function(f){return {family:f.family, members:f.members, gamma:1/f.d_eff};}));
              return {ok: w.ok, af: w.areaFraction, bf: b.areaFraction, m: Wulff.metrics(w)};""", s)
            d = max(abs(r['af'][f['family']] - f['area_fraction']) for f in s['facets'])
            db = max(abs(r['bf'][f['family']] - f['bfdh_area_fraction']) for f in s['facets'])
            dsf = abs(r['m']['shape_factor'] - s['shape_metrics']['shape_factor'])
            worst = max(worst, d)
            same3 = all(round(r['af'][f['family']], 3) == round(f['area_fraction'], 3) or abs(r['af'][f['family']] - f['area_fraction']) < 5e-4
                        for f in s['facets'])
            check(r['ok'] and d < 5e-4 and db < 5e-4 and same3,
                  'JS vs pymatgen %s level %s: %d families, max |dA| %.1e, BFDH %.1e, shape factor %.5f vs %.5f'
                  % (s['material_id'], s['level'], len(s['facets']), d, db, r['m']['shape_factor'], s['shape_metrics']['shape_factor']))
            print('    ' + ', '.join('%s %.4f/%.4f' % (f['family'], r['af'][f['family']], f['area_fraction']) for f in s['facets'][:6]) + ' ...')

        # what-if slider
        before = js('return JSON.stringify(window.__dash.lastBuild.areaFraction)')
        x0 = js('var p=document.getElementById("plot3d"); return JSON.stringify(p.data.filter(function(t){return t.type==="mesh3d"}).map(function(t){return t.x}))')
        fam = js('var s=document.getElementById("wf-fam"); s.value="{1 0 0}"; s.dispatchEvent(new Event("change")); return s.value')
        js('var r=document.getElementById("wf-slider"); r.value="0.8"; r.dispatchEvent(new Event("input"));')
        time.sleep(1.0)
        after = js('return JSON.stringify(window.__dash.lastBuild.areaFraction)')
        x1 = js('var p=document.getElementById("plot3d"); return JSON.stringify(p.data.filter(function(t){return t.type==="mesh3d"}).map(function(t){return t.x}))')
        a0, a1 = json.loads(before), json.loads(after)
        check(after != before and x1 != x0 and a1[fam] > a0[fam] + 0.01,
              'what-if slider: %s gamma x 0.8 -> area fraction %.4f -> %.4f, face geometry changed' % (fam, a0[fam], a1[fam]))
        unc = js('var p=document.getElementById("unc"); var d=p.data[1]; var i=d.y.indexOf(arguments[0]); return d.x[i]', fam)
        check(abs(unc - a1[fam]) < 1e-9, 'uncertainty view follows the slider (browser dot %.4f)' % unc)
        js('document.getElementById("chk-bfdh").click()')
        time.sleep(1.5)
        nb = js('var p=document.getElementById("plot3d"); return p.data.filter(function(t){return t.name==="BFDH"}).length')
        check(nb == 1, 'BFDH overlay toggles on')
        check(not errors(), 'what-if / BFDH: no JS errors %s' % errors())
        shot('tnt_whatif_bfdh', 1280, 1500)
        js('document.getElementById("wf-reset").click(); document.getElementById("chk-bfdh").click()')
        time.sleep(1.0)

        # other pages
        for h, sel, what in [('#/m/paracetamol-i', '#plot3d', 'paracetamol material page'),
                             ('#/m/urea', '#strip', 'urea (zoo, no shape) material page'),
                             ('#/explore', '#scatter', 'explorer'),
                             ('#/compare/tnt-monoclinic/paracetamol-i', '#c3d-a', 'compare'),
                             ('#/explore?q=UZGLIIJVICEWHF', '#scatter', 'explorer search by InChIKey')]:
            goto(h, 2.0)
            ok = js('var e=document.querySelector(arguments[0]); return !!(e && (e.data || e.innerHTML))', sel)
            check(ok and not errors(), '%s loads (%s), no JS errors %s' % (what, sel, errors()))
            if h.startswith('#/m/paracetamol'):
                sc = js('return document.getElementById("selfcheck").textContent')
                check('agree to 3 decimals' in sc, 'paracetamol page self-check: %s' % sc.strip())
                shot('paracetamol_material', 1280, 1500)
            if h == '#/explore':
                ncov = js('return document.querySelectorAll(".cov .chip").length')
                check(ncov > 40, 'coverage map chips: %d' % ncov)
                shot('explorer', 1280, 1300)
            if h.startswith('#/compare'):
                shot('compare', 1280, 1100)
            if 'UZGLIIJVICEWHF' in h:
                nm = js('return document.querySelectorAll(".cov .name").length')
                check(nm >= 1, 'InChIKey search finds HMX (%d rows)' % nm)
        # campaign driver: a driver-ingested record (provenance.source campaign) is in the dashboard, its record page
        # shows it, and a material that got its shape from a campaign renders it with that record in its facet table
        allrecs = [json.loads(l) for l in open(os.path.join(ROOT, 'dataset', 'surface_energy.jsonl')) if l.strip()]
        camp = [r for r in allrecs if r['provenance']['source'] == 'campaign' and r['result']['status'] == 'ok']
        if check(bool(camp), 'dataset has campaign-driver records (%d)' % len(camp)):
            c0 = camp[0]
            goto('#/record/' + c0['record_id'], 1.0)
            body = js('return document.body.textContent')
            check(c0['record_id'] in js('return document.querySelector("h1").textContent') and 'campaign' in body
                  and c0['provenance']['run_id'] in body and not errors(),
                  'campaign record page %s (source campaign, run id %s, %s)' % (c0['record_id'], c0['provenance']['run_id'],
                                                                             c0['provenance']['sim2l']['revision']))
            withshape = {s['material_id'] for s in shapes}
            cm = sorted([r for r in camp if r['material']['material_id'] in withshape and r['protocol']['level'] == 1
                         and r['termination']['is_lowest']], key=lambda r: r['material']['material_id'] != 'naphthalene')
            if check(bool(cm), 'a campaign material has a shape'):
                m = cm[0]['material']['material_id']
                goto('#/m/' + m, 2.0)
                ntr = js('var p=document.getElementById("plot3d"); return p && p.data ? p.data.filter(function(t){return t.type==="mesh3d"}).length : 0')
                lk = js('var id=arguments[0]; return Array.from(document.querySelectorAll("table tbody a")).filter(function(a){return a.getAttribute("href")==="#/record/"+id}).length', cm[0]['record_id'])
                sc = js('var e=document.getElementById("selfcheck"); return e ? e.textContent : ""')
                check(ntr > 3 and lk >= 1 and 'agree to 3 decimals' in sc and not errors(),
                      '%s (shape from a campaign) renders: %d mesh3d faces, the campaign record %s linked in the facet '
                      'table, self-check: %s' % (m, ntr, cm[0]['record_id'], sc.strip()[:80]))
                shot('campaign_material', 1280, 1500)
        # production campaigns (rdx_wulff, hmx_wulff, ...): each material's page renders its 'best' shape (relaxed
        # T = 0 values from the campaign) and offers the level-1 one, with a level-2 campaign record linked in the
        # facet table, and both shapes record the Laue class of the space group with as many pymatgen operations
        for mid, name, laue, nops, png in CAMPAIGN_SHAPES:
            ms = [s for s in shapes if s['material_id'] == mid]
            if not check({s['level'] for s in ms} >= {'best', '1'}, '%s has a best and a level-1 shape (%s)' % (
                    name, sorted(s['level'] for s in ms))):
                continue
            sb = next(s for s in ms if s['level'] == 'best')
            l2 = [f['input_record_ids'][0] for f in sb['facets'] if f['level'] == 2]
            goto('#/m/' + mid, 2.5)
            ntr = js('var p=document.getElementById("plot3d"); return p && p.data ? p.data.filter(function(t){return t.type==="mesh3d"}).length : 0')
            sc = js('var e=document.getElementById("selfcheck"); return e ? e.textContent : ""')
            lv = js('var s=document.getElementById("sel-level"); return s ? Array.from(s.options).map(function(o){return o.value}) : []')
            lk = js('var ids=arguments[0]; return Array.from(document.querySelectorAll("table tbody a")).filter(function(a){return ids.indexOf(a.getAttribute("href").replace("#/record/",""))>=0}).length', l2)
            check(ntr >= 4 and 'agree to 3 decimals' in sc and set(lv) >= {'best', '1'} and lk >= 1 and not errors(),
                  '%s page renders the best shape: %d mesh3d faces, levels %s, %d of %d level-2 records linked, self-check: %s'
                  % (name, ntr, lv, lk, len(l2), sc.strip()[:80]))
            for sh in ms:
                c = sh['checks']
                check(c.get('laue_class') == laue and c.get('pymatgen_recip_ops_order') == nops
                      and c.get('pymatgen_recip_ops_symprec_1em3') == nops,
                      '%s shape level %s: Laue %s, %d pymatgen operations at symprec 1e-5 and 1e-3 (%s)' % (
                          name, sh['level'], laue, nops, {k: c.get(k) for k in (
                              'laue_class', 'pymatgen_recip_ops_order', 'pymatgen_recip_ops_symprec_1em3')}))
            shot(png, 1280, 1500)
        rid = js('return document.querySelector(".cov a") ? null : null')
        first = json.loads(open(os.path.join(ROOT, 'dataset', 'surface_energy.jsonl')).readline())
        goto('#/record/' + first['record_id'], 1.0)
        check(first['record_id'] in js('return document.querySelector("h1").textContent') and not errors(), 'record page %s' % first['record_id'])
        goto('#/shape/' + shapes[0]['shape_id'], 1.0)
        check(shapes[0]['shape_id'] in js('return document.querySelector("h1").textContent') and not errors(), 'shape page %s' % shapes[0]['shape_id'])

        # dark mode + phone width
        js('window.__dash.setTheme("dark")')
        goto('#/m/tnt-monoclinic', 2.0)
        bg = js('return getComputedStyle(document.body).backgroundColor')
        check(bg in ('rgb(13, 13, 13)',), 'dark theme applies (%s)' % bg)
        shot('tnt_material_dark', 1280, 1500)
        # phone width: the page in a 390 x 844 iframe (headless Firefox windows do not go below ~500 px)
        drv.set_window_size(1280, 1000)
        for h in ('#/m/tnt-monoclinic', '#/explore', '#/', '#/compare/tnt-monoclinic/paracetamol-i'):
            js("""var f = document.getElementById('phone'); if (f) f.remove();
              f = document.createElement('iframe'); f.id = 'phone'; f.style.cssText = 'width:390px;height:844px;border:0;position:fixed;top:0;left:0;z-index:99;background:#fff';
              f.src = location.pathname + arguments[0]; document.body.appendChild(f);""", h)
            ok = wait('(function(){var f=document.getElementById("phone"); return f.contentWindow.__dash && f.contentWindow.__dash.ready;})()', 40)
            time.sleep(2.5)
            sw = js('var d=document.getElementById("phone").contentDocument.documentElement; return [d.scrollWidth, d.clientWidth]')
            ferr = js('var w=document.getElementById("phone").contentWindow; return (w.__early||[]).concat(w.__dash.errors)')
            check(ok and sw[0] <= sw[1] and not ferr, 'phone width 390 px %s: no horizontal page scroll (%d <= %d), no JS errors %s' % (h, sw[0], sw[1], ferr))
            if args.screenshots and h == '#/m/tnt-monoclinic':
                os.makedirs(args.out, exist_ok=True)
                p = os.path.join(args.out, 'tnt_phone_dark.png')
                js('var w=document.getElementById("phone").contentWindow; var e=w.document.getElementById("plot3d"); w.scrollTo(0, e.getBoundingClientRect().top + w.scrollY - 70);')
                time.sleep(1.0)
                drv.find_element('id', 'phone').screenshot(p)
                compress(p)
                print('  screenshot %s (%d kB)' % (os.path.relpath(p, ROOT), os.path.getsize(p) // 1024))
        js("var f = document.getElementById('phone'); if (f) f.remove();")
        js('window.__dash.setTheme("light")')
        check(not errors(), 'whole session: no JS errors %s' % errors())
    finally:
        drv.quit()
        srv.terminate()
    print('%d failure(s)' % len(FAIL))
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())

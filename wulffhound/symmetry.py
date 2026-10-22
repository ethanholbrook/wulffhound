"""Cell setting, space group, Laue class, extinction-corrected d_hkl and BFDH families of a unit cell.

Copied (with small changes: a function instead of a script, no printing) from the Wulff spike
devtools/spikes/wulff_tnt/cell_symmetry.py (same repository, same author). The space group is found with
spglib *in the data file's own a, b, c setting*, the systematic absences are derived from the symmetry
operations of that setting (no table lookup, so P2_1/a vs P2_1/c cannot go wrong), (h k l) are grouped
into families under the Laue class, and the families are ranked by d_eff = d_hkl / n (n the smallest
order that is not systematically absent): BFDH / Donnay-Harker.
"""
import itertools
import math
from fractions import Fraction

import numpy as np
import spglib


def read_data(path):
    """LAMMPS data file (atom_style full) -> lattice rows a, b, c; origin; atoms (id, mol, type, x, y, z); masses."""
    box = {}
    atoms = []
    with open(path) as f:
        lines = f.readlines()
    i = 0
    while i < len(lines):
        s = lines[i].split('#')[0].split()
        if len(s) >= 4 and s[2] == 'xlo':
            box['xlo'], box['xhi'] = float(s[0]), float(s[1])
        elif len(s) >= 4 and s[2] == 'ylo':
            box['ylo'], box['yhi'] = float(s[0]), float(s[1])
        elif len(s) >= 4 and s[2] == 'zlo':
            box['zlo'], box['zhi'] = float(s[0]), float(s[1])
        elif len(s) >= 6 and s[3] == 'xy':
            box['xy'], box['xz'], box['yz'] = float(s[0]), float(s[1]), float(s[2])
        elif lines[i].startswith('Atoms'):
            i += 2
            while i < len(lines) and lines[i].strip():
                t = lines[i].split()
                atoms.append((int(t[0]), int(t[1]), int(t[2]), float(t[4]), float(t[5]), float(t[6])))
                i += 1
            continue
        i += 1
    masses = {}
    for j, l in enumerate(lines):
        if l.startswith('Masses'):
            k = j + 2
            while lines[k].strip():
                t = lines[k].split()
                masses[int(t[0])] = float(t[1])
                k += 1
    a = np.array([box['xhi'] - box['xlo'], 0.0, 0.0])
    b = np.array([box.get('xy', 0.0), box['yhi'] - box['ylo'], 0.0])
    c = np.array([box.get('xz', 0.0), box.get('yz', 0.0), box['zhi'] - box['zlo']])
    origin = np.array([box['xlo'], box['ylo'], box['zlo']])
    return np.array([a, b, c]), origin, atoms, masses


def element_of(mass):
    for el, m in (('H', 1.008), ('C', 12.011), ('N', 14.007), ('O', 15.999)):
        if abs(mass - m) < 0.1:
            return el
    raise ValueError('mass %g is not H, C, N or O (molecular crystals of HCNO only)' % mass)


def lattice_params(L):
    a, b, c = (np.linalg.norm(v) for v in L)
    al = math.degrees(math.acos(np.dot(L[1], L[2]) / (b * c)))
    be = math.degrees(math.acos(np.dot(L[0], L[2]) / (a * c)))
    ga = math.degrees(math.acos(np.dot(L[0], L[1]) / (a * b)))
    return dict(a=float(a), b=float(b), c=float(c), alpha=al, beta=be, gamma=ga, volume=float(abs(np.linalg.det(L))))


def d_spacing(L, hkl):
    recip = np.linalg.inv(L).T          # rows a*, b*, c* (no 2 pi)
    g = np.dot(hkl, recip)
    return float(1.0 / np.linalg.norm(g))


def analyse_cell(datafile, max_index=3, symprec=0.05):
    L, origin, atoms, masses = read_data(datafile)
    cart = np.array([a[3:] for a in atoms]) - origin
    fracs = (cart @ np.linalg.inv(L)) % 1.0
    elements = [element_of(masses[a[2]]) for a in atoms]
    znum = {'H': 1, 'C': 6, 'N': 7, 'O': 8}
    species = [znum[e] for e in elements]
    n_mol = len({a[1] for a in atoms})
    formula = {}
    for e in elements:
        formula[e] = formula.get(e, 0) + 1
    ds = spglib.get_symmetry_dataset((L, fracs, species), symprec=symprec)
    rots, trans = ds.rotations, ds.translations
    laue = []
    for R in rots:
        for s in (1, -1):
            M = s * np.array(R)
            if not any((M == X).all() for X in laue):
                laue.append(M)

    def absent(h):
        h = np.array(h)
        for R, t in zip(rots, trans):
            if (h @ np.array(R) == h).all():
                ph = float(np.dot(h, t))
                if abs(ph - round(ph)) > 1e-3:
                    return True
        return False

    zones = {'h00 absent for h =': [n for n in range(1, 7) if absent((n, 0, 0))],
             '0k0 absent for k =': [n for n in range(1, 7) if absent((0, n, 0))],
             '00l absent for l =': [n for n in range(1, 7) if absent((0, 0, n))],
             'h0l absent examples': [[h, 0, l] for h in range(-3, 4) for l in range(-3, 4)
                                     if (h, l) != (0, 0) and absent((h, 0, l))][:8],
             'hkl general absences': any(absent(h) for h in itertools.product(range(1, 4), repeat=3))}
    seen = set()
    fams = []
    m = max_index
    for h in itertools.product(range(-m, m + 1), repeat=3):
        if h == (0, 0, 0) or math.gcd(math.gcd(abs(h[0]), abs(h[1])), abs(h[2])) != 1 or h in seen:
            continue
        orbit = sorted({tuple(int(x) for x in np.array(h) @ M) for M in laue}, reverse=True)
        seen.update(orbit)
        d = d_spacing(L, h)
        n = 1
        while absent(tuple(n * x for x in h)):
            n += 1
        rep = sorted(orbit, key=lambda x: (-(sum(1 for v in x if v >= 0)), [-v for v in x]))[0]
        fams.append({'family': '{%s}' % ' '.join(str(v) for v in rep), 'representative': list(rep),
                     'members': [list(o) for o in orbit], 'multiplicity': len(orbit),
                     'd_hkl': d, 'extinction_factor': n, 'd_eff': d / n, 'max_abs_index': max(abs(v) for v in h)})
    fams.sort(key=lambda f: -f['d_eff'])
    for i, f in enumerate(fams):
        f['bfdh_rank'] = i + 1
    ops = []
    for R, t in zip(rots, trans):
        t = np.where(np.abs(t - np.round(t)) < 1e-3, np.round(t), t) % 1.0
        ops.append({'R': np.asarray(R).tolist(), 't': [str(Fraction(float(x)).limit_denominator(24)) for x in t]})
    return {'datafile': datafile, 'n_atoms': len(atoms), 'n_molecules': n_mol,
            'atoms_per_molecule': len(atoms) // n_mol,
            'formula_cell': formula, 'formula_molecule': {k: v // n_mol for k, v in formula.items()},
            'lattice_rows_abc': L.tolist(), 'lattice_parameters': lattice_params(L),
            'spacegroup': {'international_data_setting': ds.international, 'number': int(ds.number),
                           'hall': ds.hall, 'pointgroup': ds.pointgroup, 'symprec': symprec},
            'operations_in_data_setting': ops, 'laue_order': len(laue),
            'laue_ops_on_miller': [M.tolist() for M in laue],
            'has_inversion': any((np.array(R) == -np.eye(3, dtype=int)).all() for R in rots),
            'systematic_absences_data_setting': zones, 'max_index': max_index, 'families': fams}

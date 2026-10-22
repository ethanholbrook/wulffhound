I've finished the run, but the shape is **not converged**: the stop rule was not met. I stopped because no permitted run was left, not because the shape had settled. Report: `state/paracetamol/report.html`

**The force field is not validated for paracetamol** (`forcefield_validated: false`; ReaxFF-lg 2011 was fitted to nitramine explosives). Everything below is screening-level: the order of the facets means more than the absolute gammas.

## Predicted T = 0 Wulff shape (level "best", P2_1/n setting)

| Family | gamma (mJ/m²) | basis | area fraction (± std) | p_on_shape | BFDH area |
|---|---|---|---|---|---|
| {1 1 -1} | 112.11 | L1 × 0.989 | 0.3987 (0.0853) | 1.0 | 0.0079 |
| {0 1 1} | 137.82 | L1 × 0.989 | 0.2656 (0.0674) | 1.0 | 0.4959 |
| {1 0 1} | 153.26 | L1 × 0.989 | 0.1329 (0.0259) | 1.0 | 0.1078 |
| {1 0 -1} | 120.21 | L1 × 0.989 | 0.0741 (0.0395) | 0.97 | 0.1829 |
| {0 0 1} | 167.37 | **L2** | 0.0654 (0.0162) | 1.0 | 0.0675 |
| {0 1 0} | 152.08 | L1 × 0.989 | 0.025 | 0.78 | 0.0 |
| {1 2 0} | 150.96 | L1 × 0.989 | 0.0185 | 0.75 | 0.0 |
| {1 1 0} | 156.34 | L1 × 0.989 | 0.0091 | 0.5 | 0.138 |

Six more families are on the shape with area fractions of 0.0058 or less: {0 2 1}, {1 0 -3}, {1 0 0}, {1 2 1}, {1 1 1}. Four are off the shape but within 1 σ of appearing: {1 2 -1} needs a 0.4% reduction, {1 2 -2} 1.0%, {1 1 2} 1.6%, {1 1 -2} 1.7%. The tool gives weighted surface energy 131.24 mJ/m² and anisotropy 0.1423.

- **Level 2 is thin.** Only {0 0 1} has a relaxed value. The other families are their level-1 values scaled by a ratio taken from that one pair.
- **{1 1 -2} carries a warning:** the top and bottom faces of its slab differ, so its gamma is an average of the two.

## Comparison with BFDH
- **{1 1 -1}** is the biggest change: from almost nothing in BFDH (0.0079) to the dominant face (0.3987).
- **{0 1 1}** drops from 0.4959 to 0.2656, and {1 0 -1} from 0.1829 to 0.0741.
- **{1 1 0}** nearly disappears, from 0.138 to 0.0091.
- **{1 0 1}** and **{0 0 1}** stay about where BFDH puts them.

## Literature (Bond), converted P2_1/a → P2_1/n
Conversion: h_n = −l_a, k_n = k_a, l_n = h_a + l_a. None of the abstracts states lattice parameters, so I assumed the usual P2_1/a (Haisa) cell. That check is still open.

| Paper (face as reported, P2_1/a) | Conversion | Ours |
|---|---|---|
| [Prasad 2001](https://pubmed.ncbi.nlm.nih.gov/11250089/), [Prasad 2002](https://pubmed.ncbi.nlm.nih.gov/11996808/): (1 1 0), dominant in the columnar habit | (−0, 1, 1+0) = (0 1 1) | {0 1 1} |
| Prasad 2001/2002: (0 0 1), dominant in the plate habit | (−1, 0, 0+1) = (−1 0 1) | {1 0 −1} |
| Prasad 2002: (2 0 −1) (abstract prints "[201;]", read as 20-1 with the bar lost) | (1, 0, 2−1) = (1 0 1) | {1 0 1} |
| [Liu 2020](https://doi.org/10.1021/jacs.0c00321), [Heng 2006](https://pubmed.ncbi.nlm.nih.gov/16519480/): {0 1 1}, width/thickness faces | (−1, 1, 0+1) = (−1 1 1) | {1 1 −1} |
| Heng 2006: (2 0 1) as printed | (−1, 0, 2+1) = (−1 0 3) | {1 0 −3}; or {1 0 1} if it is really (2 0 −1) |
| Heng 2006, [Heng 2006 milling](https://pubmed.ncbi.nlm.nih.gov/16850263/): (0 1 0), cleavage plane, not a growth face | (0 1 0) | {0 1 0} |

- **Agreement:** the four observed growth faces, {1 1 −1}, {0 1 1}, {1 0 1} and {1 0 −1} in our setting, are four of our five largest. In BFDH, {1 1 −1} has almost no area.
- **Differences:** the face that dominates the observed plate habit, {1 0 −1}, is only fourth here. Our {0 0 1}, which is (1 0 0) in P2_1/a, is not reported as a habit face. These are growth shapes that depend on supersaturation, so they don't compare directly with an equilibrium shape.
- **Heng's 34 and 45 mJ/m²** are only the dispersive part of the surface energy, measured by contact angle. They are recorded as "from papers" and are not comparable to our gammas.
- **Beyer, Day & Price 2001** (attachment-energy morphology prediction) is recorded as a method reference only; its abstract lists no faces.

## Results the consistency guard rejected
1. **{1 0 −1}, level 2 (already in the ledger):** rejected because the relaxed gamma, 124.5 mJ/m², is higher than the unrelaxed 121.5 mJ/m². A T = 0 minimisation can't raise the energy, so the bulk and slab references disagree. I didn't replay it (same inputs, deterministic). The Wulff build uses its level-1 value instead. It doesn't show under `inconsistent_level2` because it wasn't acquired this session.
2. **{0 0 1}, surfenergy's own value:** 247.16 mJ/m² was rejected (relaxed 247.2 > unrelaxed 169.2). It was replaced by a FIRE-continued relaxation, 167.37 mJ/m², which passes the guard with molecules intact. That is the value used.

## CPU budget
- **Total:** 18.2 of 30 minutes spent, 11.8 left.
- **Live:** 0.91 minutes, both runs failed.
- **Cached replays:** 17.29 minutes (17 level-1 screens and {0 0 1} level 2).
- **Live-run limit:** the operator allowed only 2 live runs, level 1 only, at most 1.5 minutes each. Both are used.

## Open questions
- **{1 0 1} level 2** is cached, but replaying it is charged 6.24 minutes, over your 6-minute cap, so I skipped it. If you allow it, it fits in the 11.8 minutes left.
- **{1 1 −1} and {0 1 1}** are the largest faces and have no level 2. Their estimates (13.7 and 15.1 minutes) are over the cap, and live level-2 runs aren't allowed.
- **{0 1 3} and {1 1 −3}** got past the r4 supercell failure at search_range 6. Both then crashed (segfault, exit −11) in the slabcutter bulk LAMMPS run. This looks like a problem in the simulation chain, so it needs the operator.
- **{0 1 2}** (BFDH rank 7) was never screened: its 1.8-minute estimate is over the 1.5-minute live limit.
- **{1 0 −1}** needs consistent bulk and slab references before its level 2 can be used.
- **Literature:** the lattice parameters behind the converted faces are unconfirmed, and so is the Heng (201) vs (20−1) index.

---
turns 32, cost USD 1.03, wall 255 s

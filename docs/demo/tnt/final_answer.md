I have a predicted T = 0 Wulff shape for monoclinic TNT, but the tool says it has **not converged**. I stopped because nothing more could be computed within the limits: the live-run allowance allowed only 2 level-1 runs and no live level-2 runs. Only 3 of the 15 facets on the shape are relaxed (level 2).

**Report:** `state/tnt/report.html`

## Shape (the "best" build)
Facets use the relaxed value where one exists. The rest use the level-1 value multiplied by the mean relaxed/unrelaxed ratio, 0.917 from 3 pairs. γ is in mJ/m².

| Family | Area fraction | γ used | Basis | p_on_shape |
|---|---|---|---|---|
| {0 0 1} | 0.2194 | 108.48 | level 1 × 0.917 | 1.0 |
| {1 1 1} | 0.1418 | 128.09 | level 1 × 0.917 | 0.93 |
| {1 0 0} | 0.0993 | 124.11 | **level 2** | 1.0 |
| {1 0 -1} | 0.0966 | 125.61 | level 1 × 0.917 | 0.97 |
| {1 1 0} | 0.0787 | 131.21 | level 1 × 0.917 | 0.80 |
| {1 1 -2} | 0.0751 | 139.17 | level 1 × 0.917 | 0.95 |
| {2 0 -3} | 0.0718 | 127.15 | level 1 × 0.917 | 0.80 |
| {0 1 1} | 0.0663 | 132.51 | level 1 × 0.917 | 0.90 |
| {2 0 1} | 0.0657 | 125.42 | **level 2** | 1.0 |

- **Small facets:** {1 1 -1} 0.0239, {1 1 2} 0.0221, {2 1 -2} 0.0206, {2 1 -1} 0.0107, {0 1 2} 0.008 (level 2, γ 141.94) and {2 1 0} 0.0002.
- **Just off the shape:** {2 0 -1}, {1 0 1} and {1 0 -2}, each within 1σ of appearing.
- **Overall:** shape factor 5.1912, area-weighted γ 125.39.
- **Relaxation checks:** all three relaxed results pass the consistency check (relaxed γ below unrelaxed, molecules intact). In two of them, {1 0 0} and {2 0 1}, the bulk cell relaxation stopped above its pressure target (41 and 68 atm).

The prediction is a fairly round, many-faceted crystal, with {0 0 1} the largest family at about 22% of the area.

## Comparison with BFDH
BFDH predicts a plate: {0 0 1} 0.592, {2 0 -1} 0.1949, {0 1 1} 0.0718, {1 1 0} 0.0702, {1 1 -1} 0.0657, {1 0 0} 0.0053 (shape factor 6.60). Both agree that {0 0 1} dominates. The energy-based shape differs in three ways:
- {2 0 -1}, second in BFDH, drops off the shape, though only by 0.43 mJ/m².
- {1 0 0} grows to about 10% of the area.
- {1 1 1}, {1 0 -1}, {2 0 -3}, {2 0 1} and {1 1 -2} appear; BFDH gives them no area.

## Comparison with literature
I found **no published per-face surface energies, indexed habit or morphology prediction for pure monoclinic TNT** in the literature search, so there is nothing to compare facet by facet and no Miller indices were converted.
- **Conversion I would have applied:** if a P2_1/c paper turns up, its cell has a_c = c_ours, b_c = −b_ours, c_c = a_ours, so (h k l)_c → (l −k h)_ours. For example, (1 0 0)_c → (0 0 1)_ours and (0 1 1)_c → (1 −1 0)_ours, which is the {1 1 0} family. Its lattice parameters should be checked against ours first (a 14.911, b 6.034, c 20.882 Å, β 110.37°).
- **Recorded with `add_literature`:**
  - [Holbrook et al. 2025](https://arxiv.org/abs/2503.23680) predicts and ranks TNT surface energies with a TNT force field. The abstract gives no faces or values, so the full text is needed.
  - [Vrcelj et al. 2001](https://pubmed.ncbi.nlm.nih.gov/11456877/): solution-grown TNT forms the orthorhombic polymorph first, then converts to the monoclinic one. Flagged as a different crystal, not converted, and a caveat for any comparison with observed habits.
  - [Li et al. 2026](https://doi.org/10.1007/s00894-026-06672-0) is about a TNT/ATL cocrystal. Flagged and not converted.
  - [Hikal et al. 2022](https://doi.org/10.3390/molecules27175482) and [Kumar et al. 2019](https://doi.org/10.1021/acsomega.8b03214) give habit context only: layered growth islands and nanorods, no face indices.

## CPU spent
**23.59 of 30 min**, of which **1.9 min live** (2 runs) and **21.69 min cached replays**; 6.41 min remain.
- **Live runs (level 1):** {1 1 2} γ 148.16 and {2 1 −2} γ 160.53.
- **Cached replays:** 16 level-1 screens and 3 level-2 relaxations ({1 0 0}, {2 0 1}, {0 1 2}). Replaying a cached result is charged its recorded CPU time, which is why caching still used most of the budget.

## Open questions
1. **Missing relaxations:** {0 0 1}, {1 0 −1}, {2 0 −3} and {2 0 −1} rank highest among facets worth relaxing next (estimated 2.1–2.8 min each), but none is in the cache and live level-2 runs weren't allowed. Their relaxed values could change the shape, especially for {0 0 1}.
2. **Thin correction factor:** the 0.917 ratio comes from 3 facets and is applied to 12 facets that weren't relaxed.
3. **Unsettled facets:** {1 1 1} has the least stable area (spread 0.086 across resamples), and {2 0 −1}, {1 0 1} and {1 0 −2} sit within 1σ of appearing.
4. **One relaxation skipped:** the cached {0 1 1} relaxation would be charged 6.74 min, more than both the 6-min per-run cap and the remaining budget.
5. **Unscreened families:** {1 1 −3} and {0 1 3} (BFDH ranks 19–20) were never screened.
6. **Averaged faces:** {0 0 1}, {2 0 −1}, {1 1 −2}, {1 0 1}, {1 0 −2}, {2 1 0}, {1 1 2} and {2 1 −2} have different top and bottom slab faces, so their γ is an average of two faces.
7. **Experimental comparison:** observed TNT habits come from solution growth (via the orthorhombic form) or kinetic precipitation. Neither is the T = 0 equilibrium shape, so they are a weak test of this prediction.

---
turns 40, cost USD 1.59, wall 296 s

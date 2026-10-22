# wulffhound: instructions for the agent

You drive a set of deterministic simulation tools (MCP server `wulffhound`) to predict the **T = 0 Wulff
shape** of a molecular crystal, and stop when the shape is converged, within a CPU budget (default 30 CPU
minutes, shown as `budget` in every run result). The operator may also set a **live-run allowance** (at most N live
runs, up to a level, up to M estimated minutes each; `budget.live_runs_allowed`, `live_level_max`,
`live_max_min_per_run`): the tools enforce it, and a cached replay never counts against it. Optionally you have **MCP Bond** (`bond`), a literature
search over ~44.5 M papers.

## Hard rules

1. **You never produce a number.** Every surface energy, area fraction, cost and score comes from a tool
   result. Quote them with their provenance (run_dir, cached or live). If a tool did not give a number,
   say that it is unknown. Do not add up or convert numbers yourself: `budget` already gives spent, live
   and cached-replay CPU minutes; quote those.
2. **Literature only steers and compares.** A paper's surface energy or habit goes into `add_literature`
   (the report's "from papers" column) and may make you compute a facet, or compare at the end. It never
   replaces a gamma of ours, and you never pass it to a Wulff build.
3. **Log every decision** with `log_decision` (what, why, expected CPU minutes; afterwards the outcome):
   every live run on its own entry, before (why, expected cost) and with its outcome (gamma, CPU, run_dir);
   every skip of a facet that looked relevant, every recovery from a failure, the stop. A batch of cached
   replays may share one entry, but give its outcome (the gammas) once the batch is back.
4. **Miller settings.** Our indices are relative to the data-file cell (`material_info.miller_setting`,
   `material_info.setting_map`). Before you use a paper's (hkl): compare its lattice parameters with ours,
   write the transformation, show it in your answer, and record both the reported hkl and the converted one in
   `add_literature(setting_conversion=...)`.
   - **TNT** (`material="tnt"`): monoclinic in the P2_1/a setting (h0l absent for h odd). Papers often use
     P2_1/c (a and c exchanged): a_c = c_ours, b_c = -b_ours, c_c = a_ours, so (h k l)_c -> (l -k h)_ours. The
     orthorhombic polymorph is a different crystal: do not convert, flag it.
   - **Paracetamol** (`material="paracetamol"`): our cell is the COD cell in **P2_1/n** (a 7.09, b 9.21,
     c 11.60 A, beta 97.9). The morphology literature mostly uses **P2_1/a** (a ~12.87, b ~9.40, c ~7.10 A,
     beta ~115.9). Conversion (`setting_map`): **h_n = -l_a, k_n = k_a, l_n = h_a + l_a** (inverse: h_a = h_n +
     l_n, k_a = k_n, l_a = -h_n). Examples: (1 1 0)_a -> (0 1 1)_n; (0 0 1)_a -> (-1 0 1)_n = {1 0 -1};
     (2 0 -1)_a -> (1 0 1)_n; (0 1 1)_a -> (-1 1 1)_n = {1 1 -1}; (1 0 0)_a -> (0 0 1)_n. Write the conversion
     out for every face you take from a paper (it is the agent's job to show it, the tool only checks the family).
5. **Consistency guard and force-field status.** The engine marks a level-2 result as **inconsistent** when
   its relaxed gamma exceeds its unrelaxed gamma by more than 1 mJ/m^2 (a T = 0 minimisation cannot raise the
   energy), when its gamma is not positive, or when molecules did not stay intact. Such a result is left out of `build_wulff` (the level-1
   value is used, flagged) and listed under `inconsistent` with the reason; `relax_facet` returns
   `consistent: false` and `inconsistent_reason`. Report it with the reason; do not re-run it with the same
   inputs (deterministic) and do not use its number. `material_info.forcefield_validated` says whether the
   force field is validated for the material (TNT: yes; paracetamol: **no**, ReaxFF-lg 2011 is
   nitramine-tuned): when false, call the result **screening-level** in your answer and give the ranking of
   facets more weight than absolute gammas. A level-2 result may carry `gamma_basis` (e.g. paracetamol: a FIRE
   re-relaxation replaced surfenergy's minimiser-limited value, kept as `gamma_surfenergy_raw`; when that raw value
   itself failed the guard, the result says `raw_value_rejected` and `build_wulff.superseded_raw_values` lists
   it): quote the basis.
   `build_wulff.rejected_level2_runs` lists every rejected run (also thickness checks); mention them.
6. **Replay mode.** Without the simulation chain (`material_info.mode` = "replay") only the cached results in
   the ledger can be acquired; a run that is not cached is refused with a clear message. Say which facets
   would need a live run and what they would cost (`estimate_cost`).

## Loop

1. `material_info`, then `list_candidate_facets` (BFDH order, what the cache has, cost of the next level).
   Use the batch tools to save turns: `estimate_costs(families, level)` for many families at once,
   `screen_facets(families)` to screen a list (default member and search_range each; cached ones are
   replayed). Use `screen_facet` / `relax_facet` for one family with a specific hkl or search_range.
2. **Literature (if `bond` is available)**: search for the material's observed growth habit (vapour,
   solution, melt), published morphology predictions (BFDH, attachment energy) and published surface
   energies. Check whether Bond gives full text or only abstracts (habit faces are often in the full text).
   Record each claim with `add_literature` (citation, DOI, claim, hkl as reported, conversion). Literature
   habit faces that are not in the BFDH top list join the candidates.
3. **Screen broadly (level 1, `screen_facet`)**: every family on the BFDH shape, the literature faces, and the
   next few by BFDH rank. Cheap (~0.5-1 min). Then `build_wulff level="1"` and `level="best"`.
4. **Refine where it matters (level 2, `relax_facet`, max_minutes 6)**: families on the shape, or off it but
   `within_sigma` of appearing. Skip families far off the shape and log why (their `reduction_to_appear`).
   `build_wulff` lists `next` facets with a score (relevance x information gain / CPU minutes): use it as
   advice, not as an order; explain when you deviate.
5. **Stop rule** (`build_wulff(...).convergence`): every on-shape family at level 2, area fractions stable
   (max area std < 0.02 under resampling), nothing within 1 sigma of appearing below level 2. Or stop when
   the budget is spent and report which facet is uncertain and what settling it would cost.
6. `get_report_path` and a short final answer: the shape (families and area fractions, from the tool), how it
   compares with BFDH and with the literature (from papers, cited), CPU spent (live vs replayed from cache),
   and the open questions.

## Recovering from failures (read the tool's `error`, `notes` and `recovery_hints`)

- **GCCM / supercell finds no usable cell**: retry with another member of the family (e.g. -2 0 1 instead of
  2 0 -1; `list_candidate_facets` shows the known working cells) or a larger `search_range` (8, 10).
- **Refused by the live-run allowance**: keep the facet at the level you have, say what the run would cost
  (`estimate_cost`) and list it as open.
- **Refused, estimate over max_minutes or over budget**: pick a smaller cell (other member / search_range),
  or keep the facet at level 1 and say so.
- **Slab with non-equivalent faces** (`faces_equivalent` false): gamma is the average of two faces; say so.
- **Broken molecules / not intact**: do not use the number; log it.
- A failed run is recorded in the ledger: the same inputs fail the same way (deterministic), so change an
  input before retrying.

## Honest limits to state in the answer

T = 0 only (static surface energies; no entropy); TNT with a validated non-reactive force field, paracetamol
with ReaxFF-lg 2011 (not validated: screening-level); molecular crystals of H, C, N, O; the Wulff shape is the equilibrium shape, not the growth habit, so differences from observed
habits are expected and are a finding, not an error.

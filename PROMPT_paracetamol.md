Predict the T = 0 Wulff shape of paracetamol form I (material "paracetamol") with the wulffhound tools,
following the server instructions (AGENT.md): material_info first (note forcefield_validated and setting_map),
candidates, broad level-1 screening, Wulff, level-2 relaxations of the facets on or near the shape, Wulff with
sensitivity, stop rule. Budget 30 CPU minutes, at most 6 minutes per run. Report every result the consistency
guard marks inconsistent, with its reason, and say that the force field is not validated for paracetamol (the
result is screening-level; the facet ranking matters more than absolute gammas). If the bond tools are available,
look up paracetamol form I's observed habit and published morphology predictions; the literature uses the P2_1/a
cell, ours is P2_1/n: convert every (hkl) with the setting_map (h_n = -l_a, k_n = k_a, l_n = h_a + l_a), show the
conversion, and record it with add_literature. Log every decision. Never state a number that a tool did not
return. Finish with get_report_path and a short summary: shape, comparison with BFDH and literature, CPU spent
(live vs cached), inconsistent results, open questions.

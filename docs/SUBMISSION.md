# Submission text

## Description (about 120 words)

wulffhound is an AI agent (Claude Code over MCP) that predicts the equilibrium shape of a molecular crystal and
spends expensive surface-energy simulations only on the facets that matter. Deterministic tools hold all the
physics, and the LLM never produces a number. It chooses facets, recovers from failures, pulls
literature through MCP Bond, converts Miller indices between cell settings, and writes a cited report. For
paracetamol form I, the four observed growth faces are four of the five largest computed families, while BFDH
gives one of them, {1 1 -1} = (0 1 1) in P2_1/a, almost no area. A deterministic baseline under the same budget
is the control. It found the same on-shape facets (TNT: all but a 0.0002 sliver), so the LLM adds grounding and explanation, not allocation.
Screening-level, not converged.

## Highlights

- **The physics is in the tools.** Every surface energy in the report comes from a run with provenance, and a
  consistency guard rejects relaxations that raise the energy.
- **Paracetamol form I.** {1 1 -1} goes from 0.0079 of the area in BFDH to 0.3987 on the T = 0 shape. It
  is (0 1 1) in P2_1/a, an observed growth face (Liu 2020), after the P2_1/a to P2_1/n conversion
  (h_n = -l_a, k_n = k_a, l_n = h_a + l_a). An equilibrium shape is not a growth habit, so this is a
  consistency check, not a habit prediction.
- **Cheap and honest.** Demo sessions cost USD 1.03 (32 turns, 18.2 CPU min) and USD 1.59 (40 turns, 23.6 CPU
  min). Both say `converged: false` and explain why.

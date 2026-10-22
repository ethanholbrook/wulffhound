#!/usr/bin/env bash
# Copy a finished agent session into docs/demo/<material>/ for committing: readable transcript (Bond results cut to
# 600 chars: third-party abstracts), the final answer, the light report, the compare summary, decisions and literature.
# Local absolute paths are replaced by repo-relative ones. Usage: tools/publish_demo.sh MATERIAL [MATERIAL...]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHAIN="${WULFFHOUND_CHAIN_ROOT:-${SIM2L_DEV_ROOT:-$(cd "$HERE/.." && pwd)/surface-energy-sim2ls}}"
for M in "$@"; do
  S="$HERE/state/$M"; D="$HERE/docs/demo/$M"; mkdir -p "$D"
  "$HERE/wh" transcript "$S/agent_transcript.jsonl" "$D/agent_transcript.txt" --short-prefix mcp__bond__ --short-max 600 >/dev/null
  "$HERE/wh" py -c "import json,sys
for l in open(sys.argv[1]):
    d = json.loads(l)
    if d.get('type') == 'result':
        open(sys.argv[2], 'w').write(d['result'] + '\n\n---\nturns %s, cost USD %.2f, wall %.0f s\n' % (d['num_turns'], d['total_cost_usd'], d['duration_ms'] / 1000))" \
    "$S/agent_transcript.jsonl" "$D/final_answer.md"
  cp "$S/report_light.html" "$D/report.html"
  cp "$S/compare.json" "$D/compare.json" 2>/dev/null || true
  cp "$S/decisions.jsonl" "$S/literature.jsonl" "$D/"
  for f in "$D"/*; do
    sed -i -e "s#$HERE/##g" -e "s#$HERE#.#g" -e "s#$CHAIN#\$WULFFHOUND_CHAIN_ROOT#g" -e "s#$HOME#~#g" "$f"
  done
  echo "$M -> docs/demo/$M ($(du -sh "$D" | cut -f1))"
done

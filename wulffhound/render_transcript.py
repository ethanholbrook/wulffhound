#!/usr/bin/env python
"""Readable transcript of a `claude -p --output-format stream-json --verbose` run:
render_transcript.py IN.jsonl OUT.txt [--max-result N] [--short-prefix mcp__bond__ --short-max M]
--max-result: truncate every tool result to N chars (default 6000); --short-prefix/--short-max: results of tools
whose name starts with the prefix are cut to M chars (docs/demo: Bond results carry third-party abstracts)."""
import json, sys
args = sys.argv[1:]
def _opt(name, default):
    if name in args:
        i = args.index(name); v = args[i + 1]; del args[i:i + 2]; return v
    return default
MAXR = int(_opt('--max-result', 6000)); SHORTP = _opt('--short-prefix', None); SHORTM = int(_opt('--short-max', 600))
src, dst = args[0], args[1]
names = {}
out = []
for line in open(src):
    try: d = json.loads(line)
    except Exception: continue
    t = d.get('type')
    if t == 'system' and d.get('subtype') == 'init':
        out.append('=== INIT model=%s tools=%s mcp=%s' % (d.get('model'), d.get('tools'), d.get('mcp_servers')))
    elif t == 'assistant':
        for c in d['message'].get('content', []):
            if c.get('type') == 'text': out.append('\n[ASSISTANT]\n' + c['text'])
            elif c.get('type') == 'tool_use':
                names[c.get('id')] = c['name']; out.append('\n[TOOL CALL] %s %s' % (c['name'], json.dumps(c.get('input'))))
    elif t == 'user':
        cont = d['message'].get('content', [])
        if isinstance(cont, str): out.append('\n[USER]\n' + cont); continue
        for c in cont:
            if c.get('type') == 'tool_result':
                r = c.get('content')
                if isinstance(r, list): r = ''.join(x.get('text', '') for x in r if isinstance(x, dict))
                r = str(r)
                m = SHORTM if SHORTP and names.get(c.get('tool_use_id'), '').startswith(SHORTP) else MAXR
                out.append('[TOOL RESULT%s] %s' % (' ERROR' if c.get('is_error') else '', r if len(r) <= m else r[:m] + ' ...[truncated %d chars]' % (len(r) - m)))
    elif t == 'result':
        out.append('\n=== RESULT subtype=%s turns=%s cost_usd=%s duration_s=%.0f denials=%s terminal=%s' % (
            d.get('subtype'), d.get('num_turns'), d.get('total_cost_usd'), (d.get('duration_ms') or 0) / 1000,
            d.get('permission_denials'), d.get('terminal_reason')))
open(dst, 'w').write('\n'.join(out) + '\n')
print(len(out), 'blocks')

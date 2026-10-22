#!/usr/bin/env python
"""Readable transcript of a `claude -p --output-format stream-json --verbose` run: render_transcript.py IN.jsonl OUT.txt"""
import json, sys
src, dst = sys.argv[1], sys.argv[2]
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
            elif c.get('type') == 'tool_use': out.append('\n[TOOL CALL] %s %s' % (c['name'], json.dumps(c.get('input'))))
    elif t == 'user':
        cont = d['message'].get('content', [])
        if isinstance(cont, str): out.append('\n[USER]\n' + cont); continue
        for c in cont:
            if c.get('type') == 'tool_result':
                r = c.get('content')
                if isinstance(r, list): r = ''.join(x.get('text', '') for x in r if isinstance(x, dict))
                r = str(r)
                out.append('[TOOL RESULT%s] %s' % (' ERROR' if c.get('is_error') else '', r if len(r) < 6000 else r[:6000] + ' ...[truncated %d chars]' % (len(r) - 6000)))
    elif t == 'result':
        out.append('\n=== RESULT subtype=%s turns=%s cost_usd=%s duration_s=%.0f denials=%s terminal=%s' % (
            d.get('subtype'), d.get('num_turns'), d.get('total_cost_usd'), (d.get('duration_ms') or 0) / 1000,
            d.get('permission_denials'), d.get('terminal_reason')))
open(dst, 'w').write('\n'.join(out) + '\n')
print(len(out), 'blocks')

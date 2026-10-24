#!/usr/bin/env bash
# Public-repo hygiene over the tracked files: no tokens or secrets, no absolute home paths, no force-field or
# CCDC-derived structure files. Exit 1 on a finding. Run before every push (CI runs it too).
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail=0
files=$(git ls-files 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null)
files=$(echo "$files" | grep -v '^tools/check_public.sh$' | sort -u)
hit() { echo "FAIL: $1"; echo "$2" | head -20; fail=1; }
# 1. secrets: private keys, common token formats, literal bearer tokens, assigned secrets
out=$(echo "$files" | xargs -r grep -nIE -- '-----BEGIN [A-Z ]*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}|Bearer [A-Za-z0-9._-]{20,}|(TOKEN|SECRET|PASSWORD|API_KEY)[A-Z_]*["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9._/+-]{12,}' 2>/dev/null)
[ -n "$out" ] && hit "possible secret" "$out"
# 2. absolute home paths (a user's machine)
out=$(echo "$files" | xargs -r grep -nIE '/home/[a-z][a-z0-9_-]+/|/Users/[A-Za-z]' 2>/dev/null | grep -v 'home/coder/svn-repositories/_RELOADED_COD')
[ -n "$out" ] && hit "absolute home path" "$out"
# 3. force-field files and non-distributable (CCDC-derived: TNT, TATB) structure files
out=$(echo "$files" | grep -iE '\.ff$|ffield|reax.*\.(ff|txt)$|(tnt|tatb).*\.(data|cif|lmp)$|\.(res|ccdc)$')
[ -n "$out" ] && hit "force-field or restricted structure file" "$out"
[ $fail -eq 0 ] && echo "check_public: OK ($(echo "$files" | wc -l) files)"
exit $fail

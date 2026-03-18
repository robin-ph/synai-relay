#!/bin/bash
# Pre-push gate: blocks git push unless tests pass and dependencies are consistent.
# Called by Claude Code PreToolUse hook on Bash commands.

set -euo pipefail

INPUT=$(cat)
CMD=$(echo "$INPUT" | jq -r '.tool_input.command // ""')

# Only gate git push commands
if ! echo "$CMD" | grep -qE '^git push'; then
  exit 0
fi

cd /Users/labrinyang/projects/synai-relay

# --- Gate 1: Full test suite ---
echo "Running pytest..." >&2
pytest_output=$(python -m pytest --tb=short -q 2>&1) || true
pytest_exit=${PIPESTATUS[0]:-$?}

if [ "$pytest_exit" -ne 0 ]; then
  jq -n --arg reason "pytest failed. Fix all tests before pushing.\n$pytest_output" '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": $reason
    }
  }'
  exit 0
fi

# --- Gate 2: Dependency consistency (pip check) ---
echo "Checking dependency consistency..." >&2
dep_output=$(python -m pip check 2>&1) || true
dep_exit=${PIPESTATUS[0]:-$?}

if [ "$dep_exit" -ne 0 ]; then
  jq -n --arg reason "Dependency issues found. Fix before pushing.\n$dep_output" '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": $reason
    }
  }'
  exit 0
fi

# --- Gate 3: Check for new imports missing from requirements.txt ---
echo "Scanning for undeclared imports..." >&2
missing=$(python3 -c "
import ast, sys, os, importlib.util

# Collect all top-level imports from project .py files
project_imports = set()
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in ('tests', '.git', 'migrations', '__pycache__', '.claude', 'contracts', 'audio', 'scripts')]
    for f in files:
        if not f.endswith('.py'):
            continue
        try:
            tree = ast.parse(open(os.path.join(root, f)).read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        project_imports.add(alias.name.split('.')[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    project_imports.add(node.module.split('.')[0])
        except:
            pass

# Read declared deps from requirements.txt
declared = set()
if os.path.exists('requirements.txt'):
    for line in open('requirements.txt'):
        line = line.strip()
        if line and not line.startswith('#'):
            pkg = line.split('==')[0].split('>=')[0].split('<=')[0].split('[')[0].split('<')[0].split('>')[0].strip()
            declared.add(pkg.lower().replace('-', '_'))

# Known stdlib + local modules to skip
skip = {
    'os','sys','json','time','datetime','hashlib','hmac','secrets','threading',
    'logging','re','uuid','decimal','functools','collections','traceback','atexit',
    'concurrent','urllib','base64','io','pathlib','contextlib','typing','dataclasses',
    'unittest','textwrap','copy','string','math','random','signal','socket','struct',
    'tempfile','shutil','http','abc','enum','warnings','configparser','csv','glob',
    'importlib','subprocess','multiprocessing','queue','itertools','operator',
    'inspect','pickle','gzip','zipfile','tarfile','platform','ctypes','array',
    'binascii','codecs','pprint','argparse','getpass','locale','calendar',
    # Local modules
    'server','models','config','services','migrations','templates','static','tests','manage',
    # Flask sub-deps (installed transitively)
    'werkzeug','jinja2','markupsafe','click','itsdangerous','blinker',
    # Other transitive deps
    'pkg_resources','setuptools','pip','distutils','_distutils_hack',
}

missing = []
for imp in sorted(project_imports):
    imp_lower = imp.lower().replace('-', '_')
    if imp_lower in skip:
        continue
    if imp_lower in declared:
        continue
    # Check if importable (might be transitive dep)
    if importlib.util.find_spec(imp) is not None:
        continue
    missing.append(imp)

if missing:
    print(' '.join(missing))
" 2>&1) || true

if [ -n "$missing" ]; then
  jq -n --arg reason "Undeclared imports found: $missing. Add to requirements.txt or verify they are transitive deps." '{
    "hookSpecificOutput": {
      "hookEventName": "PreToolUse",
      "permissionDecision": "deny",
      "permissionDecisionReason": $reason
    }
  }'
  exit 0
fi

echo "All pre-push gates passed." >&2

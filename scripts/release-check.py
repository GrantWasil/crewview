#!/usr/bin/env python3
"""Conservative checks on the distributable source; no account or history access."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARTS = {'state', 'backups', '.verification', 'tasks', '__pycache__', '.venv'}
REQUIRED = ['README.md', 'AGENTS.md', 'LICENSE', 'docs/SETUP.md', 'docs/PRIVACY.md',
            'docs/WORKFLOW.md', 'docs/TROUBLESHOOTING.md', 'install.py', 'uninstall.py',
            'settings.py', 'crewview.example.json', 'server.py', 'dashboard.py', 'demo.py']
ALLOWED = set(REQUIRED) | {
    '.gitignore', '.github/workflows/tests.yml', 'CONTRIBUTING.md',
    'activity.py', 'codex_history.py', 'smoke_test.py', 'launch-dashboard.command',
    'web/index.html', 'web/app.js', 'web/app.css', 'scripts/release-check.py',
    'examples/demo.png',
}
PATTERNS = [
    ('personal absolute path', re.compile(r'/' + r'Users/(?!demo(?:/|\b)|example(?:/|\b)|you(?:/|\b)|USERNAME(?:/|\b))[^/\s"\']+/')),
    ('likely API credential', re.compile(r'(?:sk-ant-|sk-proj-)[A-Za-z0-9_-]{20,}')),
    ('private key', re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')),
]

def candidates():
    result = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                            text=True, capture_output=True)
    if result.returncode == 0:
        return sorted(set(p for p in result.stdout.split('\0') if p))
    return sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob('*')
                  if p.is_file() and '.git' not in p.parts and '__pycache__' not in p.parts)

def main():
    errors = ['Missing required file: ' + p for p in REQUIRED if not (ROOT / p).is_file()]
    files = candidates()
    for name in files:
        path = Path(name)
        if name not in ALLOWED and not re.fullmatch(r'tests/test_[a-z_]+\.py', name):
            errors.append('Not in the distribution allowlist: ' + name)
            continue
        if FORBIDDEN_PARTS.intersection(path.parts) or path.name == 'crewview.json' or path.suffix == '.jsonl' or path.name.startswith('.env'):
            errors.append('Private/generated file in release candidate: ' + name)
            continue
        actual = ROOT / name
        if actual.is_symlink():
            errors.append('Review symlink before release: ' + name)
            continue
        try:
            body = actual.read_text()
        except UnicodeDecodeError:
            if path.suffix not in {'.png', '.jpg', '.svg', '.webp'}:
                errors.append('Unexpected binary file: ' + name)
            continue
        for label, pattern in PATTERNS:
            if pattern.search(body):
                errors.append(label + ' in ' + name)
    if errors:
        print('\n'.join(errors), file=sys.stderr)
        return 1
    print(f'Release checks passed for {len(files)} source files. Manually inspect the diff and any media before publishing.')
    return 0

if __name__ == '__main__':
    raise SystemExit(main())

"""Audit tracked source only. Prints filenames/reasons, never matched secrets."""
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
paths = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode().split('\0')
allowed_md = {'README.md', 'G8-Source-code-LLM-tu-van-hanh-chinh-cong/README.md',
              'G8.5-He-thong-LLM-reusable/README.md'}
token_patterns = [r'gh[pousr]_[A-Za-z0-9]{30,}', r'github_pat_[A-Za-z0-9_]{40,}',
                  r'sk-(?:proj-)?[A-Za-z0-9_-]{30,}', r'tskey-[A-Za-z0-9_-]{20,}',
                  r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
errors, count, total = [], 0, 0
for rel in filter(None, paths):
    path = ROOT / rel
    count += 1
    data = path.read_bytes()
    total += len(data)
    parts = Path(rel).parts
    if set(parts) & {'.runtime', '.venv', 'node_modules', '__pycache__', 'dist', 'private', 'external'}:
        errors.append((rel, 'runtime/private/generated directory'))
    if path.name.startswith('.env') and path.name != '.env.example':
        errors.append((rel, 'non-example environment file'))
    if path.suffix.lower() == '.md' and rel not in allowed_md:
        errors.append((rel, 'only distribution READMEs may be published'))
    if path.suffix.lower() in {'.gguf', '.safetensors', '.dump', '.sql', '.db', '.log'}:
        errors.append((rel, 'weights, history or log artifact'))
    if len(data) > 20_000_000:
        errors.append((rel, 'unexpectedly large file'))
    text = data.decode('utf-8', errors='replace')
    if path.name not in {'README.md', 'check_public_tree.py'} and ('recifine' in text.lower() or 'recifine' in rel.lower()):
        errors.append((rel, 'excluded recipe dataset reference/content'))
    if any(re.search(pattern, text) for pattern in token_patterns):
        errors.append((rel, 'credential pattern'))
    if re.search(r'\b[D-Z]:[\\/]CODEFPT\b', text, re.I):
        errors.append((rel, 'author-machine absolute path'))
    if 'G8.5-' in rel and path.name in {'administrative_policy.py', 'legacy_administrative_bridge.py',
                                       'g85-profile.json', 'g85-metadata.json'}:
        errors.append((rel, 'domain-specific serving artifact'))
print(json.dumps({'tracked_files': count, 'bytes': total, 'errors': errors, 'pass': not errors}))
raise SystemExit(bool(errors))

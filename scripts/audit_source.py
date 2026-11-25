"""Audit kandidat/berkas terlacak tanpa mencetak nilai kredensial."""
import argparse
import ast
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_DIRS = {'outputs', 'manifests', 'masukan_audit', '.venv', 'venv', '.local', '.paddlex', '.paddleocr',
                'models', 'cache', 'cache_ocr', 'uploads', 'customer_data', 'arsip_pelanggan'}
SECRET_PATTERNS = {
    'private_key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github_token': re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}\b'),
    'cloud_access_key': re.compile(r'\bAKIA[A-Z0-9]{16}\b'),
    'api_token': re.compile(r'\bsk-[A-Za-z0-9_-]{32,}\b'),
}


def manifest(root):
    data = json.loads((Path(root) / 'demo-manifest.json').read_text(encoding='utf-8-sig'))
    files = data['files']
    if len(files) != len(set(files)) or any(Path(p).is_absolute() or '..' in Path(p).parts for p in files):
        raise ValueError('Manifest sumber tidak valid.')
    return data


def sensitive(path, allowed):
    path = Path(path)
    return (bool(set(path.parts) & PRIVATE_DIRS) or path.name.startswith('.env')
            or path.suffix.lower() in {'.key', '.pem', '.p12', '.pfx', '.pdiparams', '.zip', '.rar', '.7z', '.pdf', '.csv'}
            or (path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp'} and path.as_posix() not in allowed))


def audit(root=ROOT):
    root = Path(root).resolve()
    allowed = manifest(root)['files']
    command = subprocess.run(['git', '-C', str(root), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    git_root = Path(command.stdout.strip()).resolve() if command.returncode == 0 else None
    is_repo = git_root == root
    tracked = []
    if is_repo:
        tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
        tracked = [p for p in tracked if p]
    findings, path_notes, import_missing = [], [], []
    candidates = sorted(set(allowed) | set(tracked))
    for name in candidates:
        path = root / name
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        try:
            content = path.read_text(encoding='utf-8-sig')
        except (UnicodeError, OSError):
            continue
        for number, line in enumerate(content.splitlines(), 1):
            for kind, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    findings.append({'file': name, 'line': number, 'kind': kind})
            if re.search(r'\b[A-Za-z]:[\\/]', line):
                path_notes.append({'file': name, 'line': number, 'kind': 'absolute_path_review'})
        if path.suffix == '.py':
            tree = ast.parse(content)
            modules = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules.extend(item.name for item in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    modules.append(node.module)
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == '__import__' and node.args and isinstance(node.args[0], ast.Constant):
                    modules.append(node.args[0].value)
            for module in modules:
                local = module.replace('.', '/') + '.py'
                if (root / local).is_file() and local not in allowed:
                    import_missing.append({'file': name, 'dependency': local})
    result = {'git_status': 'repository' if is_repo else 'not_a_git_repository',
              'tracked_audit': 'performed' if is_repo else 'unknown_no_index_or_history',
              'tracked_count': len(tracked) if is_repo else None,
              'tracked_sensitive': [p for p in tracked if sensitive(p, allowed)],
              'tracked_outside_manifest': sorted(set(tracked) - set(allowed)),
              'missing_manifest_files': [p for p in allowed if not (root / p).is_file()],
              'missing_local_imports': import_missing, 'credential_pattern_findings': findings,
              'absolute_path_review': path_notes,
              'legacy_customer_examples_review': [{'file': 'baca_pdf_digital.py', 'symbol': 'NAMA_DOKUMEN',
                 'note': 'Contoh nama pelanggan pada helper lama; mesin dipertahankan, tinjau sebelum publikasi.'}],
              'local_private_categories_present': sorted(p.name for p in root.iterdir() if p.is_dir() and p.name in PRIVATE_DIRS),
              'scope': 'index/current candidates only; not a complete credential or remote history audit'}
    result['critical_findings'] = any(result[k] for k in ['tracked_sensitive', 'tracked_outside_manifest', 'missing_manifest_files', 'missing_local_imports', 'credential_pattern_findings'])
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = audit()
    content = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding='utf-8')
    print(content)
    if args.check:
        if result['critical_findings']:
            return 1
        if result['git_status'] != 'repository':
            return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

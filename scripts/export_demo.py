"""Salin allowlist demo ke folder baru, tanpa .git, data, model atau environment."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.audit_source import audit, manifest


def export_demo(destination, source=ROOT):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists():
        raise ValueError('Gunakan folder tujuan baru; tidak menimpa file lokal.')
    report = audit(source)
    if report['critical_findings']:
        raise ValueError('Audit kandidat memiliki temuan kritis; periksa scripts/audit_source.py.')
    destination.mkdir(parents=True)
    for name in manifest(source)['files']:
        path = source / name
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError('Sumber di luar workspace: ' + name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return {name: hashlib.sha256((destination / name).read_bytes()).hexdigest() for name in manifest(source)['files']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    hashes = export_demo(args.destination)
    print(json.dumps({'destination': str(args.destination.resolve()), 'files': len(hashes), 'uploaded': False}))


if __name__ == '__main__':
    main()

"""Tambahkan hanya sesi berlabel sintetis untuk demo Data; tidak memanggil OCR."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.support import SOURCES, make_session


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'outputs/web_lokal')
    args = parser.parse_args()
    for index, session_id in enumerate(SOURCES):
        folder = args.output / 'jobs' / session_id
        if folder.exists():
            print('Tidak menimpa sesi yang sudah ada:', session_id)
            continue
        make_session(args.output, session_id, partial=index == 0)
        print('Sesi sintetis dibuat:', session_id)
    print('Tanpa OCR. Data demo: 3 faktur, 7 detail; bukan data pelanggan.')


if __name__ == '__main__':
    main()

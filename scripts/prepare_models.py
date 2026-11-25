"""Periksa model lokal; --prepare menyiapkan model tanpa membaca faktur/predict."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uji_ocr_gambar import CONFIG


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare', action='store_true', help='Memuat/mengunduh model konfigurasi mesin; tidak melakukan inferensi faktur.')
    args = parser.parse_args()
    if args.prepare:
        os.environ['PADDLE_PDX_MODEL_SOURCE'] = 'BOS'
        from paddleocr import PaddleOCR
        PaddleOCR(**CONFIG)
    packages = {}
    for name in ['paddleocr', 'paddlepaddle', 'paddlex']:
        try: packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError: packages[name] = None
    models = []
    for key in ['text_detection_model_name', 'text_recognition_model_name']:
        folder = Path.home() / '.paddlex/official_models' / CONFIG[key]
        missing = [name for name in ['inference.json', 'inference.pdiparams', 'inference.yml'] if not (folder / name).is_file()]
        models.append({'name': CONFIG[key], 'folder': str(folder), 'missing': missing})
    ready = all(packages.values()) and not any(row['missing'] for row in models)
    print(json.dumps({'packages': packages, 'models': models, 'ready': bool(ready), 'invoice_inference': False}, indent=2))
    return 0 if ready else 1


if __name__ == '__main__':
    raise SystemExit(main())

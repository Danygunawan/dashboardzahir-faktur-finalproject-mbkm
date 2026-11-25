"""Tes terkurasi demo tanpa data pelanggan dan dengan OCR nyata diblokir."""
import importlib.abc
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class NoOCR(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'paddle', 'paddleocr', 'paddlex'}:
            raise AssertionError('OCR/model tidak boleh diimpor pada CI: ' + fullname)
        return None


def main():
    guard = NoOCR()
    sys.meta_path.insert(0, guard)
    import batch_faktur
    import web_lokal.worker
    from web_lokal.app import JobStore
    modules = ['test_web_lokal', 'test_unduhan_web_lokal', 'test_tiga_layar', 'test_data_web_lokal', 'tests.test_preparation']
    suite = unittest.defaultTestLoader.loadTestsFromNames(modules)
    with patch.object(batch_faktur, 'run_batch', side_effect=AssertionError('run_batch nyata dilarang pada CI')), \
         patch.object(web_lokal.worker, 'run_batch', side_effect=AssertionError('Worker nyata dilarang pada CI')), \
         patch.object(JobStore, '_run', side_effect=AssertionError('Subproses ekstraksi dilarang pada CI')):
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(json.dumps({'scope': 'local_synthetic_tests', 'tests': result.testsRun, 'success': result.wasSuccessful(),
                      'real_ocr': False, 'customer_data_required': False}))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())

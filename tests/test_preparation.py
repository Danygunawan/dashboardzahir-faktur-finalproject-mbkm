"""Verifikasi source preparation memakai repo sementara, tanpa pelanggan/OCR."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts.audit_source import audit, manifest
from scripts.export_demo import export_demo

ROOT = Path(__file__).resolve().parents[1]


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='zahir_preparation_')
        self.root = Path(self.tmp.name) / 'clean'
        export_demo(self.root)
        subprocess.run(['git', 'init', '--quiet', str(self.root)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, text=True)

    def test_clean_export_closure_and_exact_git_candidates(self):
        self.assertFalse(audit(self.root)['critical_findings'])
        self.assertEqual(self.git('add', '--all').returncode, 0)
        indexed = set(self.git('ls-files').stdout.splitlines())
        self.assertEqual(indexed, set(manifest(self.root)['files']))
        self.assertEqual(audit(self.root)['tracked_sensitive'], [])
        for name in manifest(ROOT)['runtime_python']:
            self.assertEqual((ROOT/name).read_bytes(), (self.root/name).read_bytes())

    def test_private_probes_are_ignored_but_assets_are_trackable(self):
        private = ['outputs/web_lokal/jobs/test/job.json', 'manifests/private.json', 'masukan_audit/data.csv',
                   '.venv/lib/module.py', '.paddlex/official_models/model/inference.pdiparams',
                   'customer.pdf', 'customer.jpg', 'arsip.zip', '.env', 'credentials.json',
                   'web_lokal/static/customer.png', 'uji_batch_private/source.txt', '.local/result.log']
        for name in private:
            file = self.root / name; file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text('synthetic ignore probe', encoding='utf-8')
            self.assertEqual(self.git('check-ignore', '--no-index', name).returncode, 0, name)
        for name in ['web_lokal/static/zahir-one.png', 'web_lokal/static/InterVariable.woff2', '.github/workflows/ci.yml']:
            self.assertEqual(self.git('check-ignore', '--no-index', name).returncode, 1, name)
        self.assertEqual(self.git('add', '--all').returncode, 0)
        self.assertEqual(set(self.git('ls-files').stdout.splitlines()), set(manifest(self.root)['files']))

    def test_forced_tracking_and_token_are_reported_without_value(self):
        file = self.root / '.env'
        fake = 'sk-' + 'a' * 48
        file.write_text(fake, encoding='utf-8')
        self.assertEqual(self.git('add', '-f', '.env').returncode, 0)
        result = audit(self.root)
        self.assertIn('.env', result['tracked_sensitive'])
        self.assertTrue(result['credential_pattern_findings'])
        self.assertNotIn(fake, json.dumps(result))

    def test_no_git_history_is_not_reported_as_a_tracking_pass(self):
        with tempfile.TemporaryDirectory(prefix='zahir_no_git_') as folder:
            root = Path(folder) / 'copy'
            export_demo(root)
            result = audit(root)
            self.assertEqual(result['tracked_audit'], 'unknown_no_index_or_history')
            self.assertIsNone(result['tracked_count'])

    def test_export_never_overwrites_destination(self):
        sentinel = self.root / 'keep.txt'
        sentinel.write_text('keep this file', encoding='utf-8')
        with self.assertRaises(ValueError):
            export_demo(self.root)
        self.assertEqual(sentinel.read_text(), 'keep this file')


if __name__ == '__main__':
    unittest.main()

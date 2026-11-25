"""Satu proses Python untuk satu sesi unggahan; seluruh output terisolasi."""
import argparse
import traceback
from pathlib import Path

from batch_common import read_json, now
import batch_faktur
from batch_faktur import run_batch
from web_lokal.io import atomic_json


def execute(job_dir):
    job_dir = Path(job_dir).resolve()
    state_path = job_dir / 'job.json'
    state = read_json(state_path)
    try:
        state.update(status='diproses', mulai=now(), pesan='Menyiapkan mesin ekstraksi.')
        atomic_json(state_path, state)
        manifest = read_json(job_dir / 'manifest.json')
        # Tidak memakai manifest produksi, output batch lama, atau cache tulis bersama.
        # Hanya binding I/O dalam proses worker ini: parser/konfigurasi tetap asli.
        # os.replace bisa ditolak sementara ketika reader/file scanner Windows aktif.
        original_writer = batch_faktur.atomic_json
        batch_faktur.atomic_json = atomic_json
        try:
            run_batch(job_dir / 'manifest.json', run_dir=job_dir / 'run',
                      limit=len(manifest['dokumen']), cache_dir=job_dir / 'cache_ocr',
                      roi_mode='cache')
        finally:
            batch_faktur.atomic_json = original_writer
        progress = read_json(job_dir / 'run/progres.json')
        statuses = [d['status'] for d in progress['dokumen']]
        failures = statuses.count('gagal')
        if failures == len(statuses):
            status = 'gagal'
        elif failures:
            status = 'sebagian_gagal'
        elif 'perlu_review' in statuses:
            status = 'perlu_review'
        else:
            status = 'selesai'
        state.update(status=status, selesai=now(), pesan='Ekstraksi selesai; periksa hasil dan catatan review.')
        atomic_json(state_path, state)
        return 1 if failures else 0
    except Exception as exc:
        traceback.print_exc()
        state.update(status='gagal', selesai=now(), pesan=f'{type(exc).__name__}: {exc}')
        atomic_json(state_path, state)
        return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_dir', type=Path)
    raise SystemExit(execute(parser.parse_args().job_dir))

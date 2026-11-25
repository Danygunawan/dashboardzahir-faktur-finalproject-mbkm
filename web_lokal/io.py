"""Penulisan status dengan retry singkat untuk reader/file scanner Windows."""
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from batch_common import read_json


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Nama sementara pendek: target + UUID dapat melewati MAX_PATH Windows.
    temporary = path.with_name('.tmp_' + uuid4().hex)
    try:
        with temporary.open('x', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + 3
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.025)
    finally:
        temporary.unlink(missing_ok=True)

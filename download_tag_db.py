"""Install the independently published, frozen Danbooru metadata snapshot.

Downloads only from Hugging Face, never Danbooru; no API key or browser cookies.
Snapshot is pinned and SHA-256 validated before replacing any previous file.
"""
import hashlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from huggingface_hub import hf_hub_download

from offline_tags import OfflineTags, SOURCE_URL

BASE = Path(__file__).resolve().parent
TARGET = BASE / 'danbooru_tags.sqlite'
DATASET = 'hlibr/danbooru-tag-metadata-snapshot'
REVISION = 'b5f0fb5dd0d344d6dad09f1405f8b740b04ace0d'
SHA256 = 'd24f6ff78fae40f18cb1e404f278393ddf0d165fd5cdbc16fa30ebe2d11a58e5'


def validated(path):
    path = Path(path)
    if not path.is_file():
        return False
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(4*1024*1024), b''):
            h.update(block)
    if h.hexdigest() != SHA256:
        return False
    return OfflineTags(path).ready


def install():
    if validated(TARGET):
        print('Lokalna baza tagow jest juz zainstalowana i zweryfikowana.')
        return
    print('Pobieram opcjonalna baze tagow (~175 MB) z:', SOURCE_URL)
    downloaded = Path(hf_hub_download(repo_id=DATASET, repo_type='dataset', revision=REVISION,
                                       filename='booru_snapshot.sqlite'))
    if not validated(downloaded):
        raise RuntimeError('Pobrana baza nie przeszla kontroli SHA-256/schematu. Nie instaluję.')
    # Atomic replacement on the user's local filesystem. Keep a prior database on failure.
    temp = TARGET.with_suffix('.sqlite.tmp')
    try:
        shutil.copyfile(downloaded, temp)
        if not validated(temp):
            raise RuntimeError('Nieudana weryfikacja kopii bazy')
        os.replace(temp, TARGET)
    finally:
        temp.unlink(missing_ok=True)
    print('Gotowe: offline SQLite z 2026-04-08. Rozmiar:', TARGET.stat().st_size)


if __name__ == '__main__':
    install()

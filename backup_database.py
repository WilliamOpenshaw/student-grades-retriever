'''
Database Backup
Makes a safe copy of grades.db (even while DataGrip or another script has it
open), zips it, keeps it in the local "backups" folder, and uploads it to
Google Drive ("Student Grades Retriever/Database Backups").

Old backups are cleaned up automatically:
    local:  newest KEEP_LOCAL zips are kept
    Drive:  newest KEEP_DRIVE zips are kept (older ones go to Drive's trash)

Usage:
    python backup_database.py               # back up locally and to Google Drive
    python backup_database.py --local-only  # skip the upload

Restore:
    1. Close DataGrip and stop any running scripts.
    2. Rename the current grades.db (and grades.db-wal / grades.db-shm if present) to keep it aside.
    3. Unzip the backup into the project folder; it contains grades.db.
'''

import os
import glob
import sqlite3
import zipfile
import argparse
import tempfile
from datetime import datetime

from db import get_db_path, BASE_DIR

LOCAL_BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
KEEP_LOCAL = 14
KEEP_DRIVE = 30
PREFIX = 'grades_'


def make_local_backup():
    db_path = get_db_path()
    if not os.path.exists(db_path):
        raise SystemExit(f"Database not found: {db_path}")
    os.makedirs(LOCAL_BACKUP_DIR, exist_ok=True)

    stamp = datetime.now().strftime('%Y-%m-%d_%H%M')
    zip_path = os.path.join(LOCAL_BACKUP_DIR, f'{PREFIX}{stamp}.zip')
    if os.path.exists(zip_path):   # two backups in the same minute: keep both (add seconds)
        zip_path = os.path.join(LOCAL_BACKUP_DIR, f"{PREFIX}{datetime.now().strftime('%Y-%m-%d_%H%M%S')}.zip")

    with tempfile.TemporaryDirectory() as tmp:
        snapshot = os.path.join(tmp, 'grades.db')
        # SQLite's backup API gives a consistent copy even if the database is in use.
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(snapshot)
        try:
            src.backup(dst)
            ok = dst.execute('PRAGMA integrity_check').fetchone()[0]
        finally:
            dst.close()
            src.close()
        if ok != 'ok':
            raise SystemExit(f"Integrity check failed on the backup copy: {ok}")

        with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(snapshot, arcname='grades.db')

    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    print(f"  ✅ Local backup: {zip_path} ({size_mb:.1f} MB)")

    old = sorted(glob.glob(os.path.join(LOCAL_BACKUP_DIR, f'{PREFIX}*.zip')), reverse=True)[KEEP_LOCAL:]
    for path in old:
        os.remove(path)
    if old:
        print(f"  Removed {len(old)} old local backup(s) (keeping {KEEP_LOCAL}).")
    return zip_path


def upload_to_drive(zip_path):
    from google_drive import get_drive_service, ensure_folders, upload, list_folder, move_to_trash

    service = get_drive_service()
    config = ensure_folders(service)
    folder_id = config['backups_folder_id']

    upload(service, folder_id, os.path.basename(zip_path), path=zip_path, mimetype='application/zip')
    print(f"  ✅ Uploaded to Google Drive: Student Grades Retriever/Database Backups/{os.path.basename(zip_path)}")

    old = list_folder(service, folder_id, PREFIX)[KEEP_DRIVE:]
    for f in old:
        move_to_trash(service, f['id'])
    if old:
        print(f"  Moved {len(old)} old Drive backup(s) to the trash (keeping {KEEP_DRIVE}).")


def main():
    parser = argparse.ArgumentParser(description="Back up grades.db locally and to Google Drive.")
    parser.add_argument('--local-only', action='store_true', help="Don't upload to Google Drive.")
    args = parser.parse_args()

    print("--- Backing up the database ---")
    zip_path = make_local_backup()
    if not args.local_only:
        upload_to_drive(zip_path)
    print("Done.")


if __name__ == '__main__':
    main()

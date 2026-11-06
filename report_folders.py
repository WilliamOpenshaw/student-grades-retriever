r'''
Where grade reports live, shared by generate_grade_reports.py, audit_emails.py and
send_emails_hs.py / send_emails_jh.py.

Every report run gets its own folder, so nothing is ever cleared or overwritten:
    grade_reports\<YYYY-MM-DD_HHMM>_<HS|JH|ALL>\        e.g. grade_reports\2026-10-05_1430_HS\
    grade_reports\<YYYY-MM-DD_HHMM>_<HS|JH|ALL>_SELECTED\  runs for selected students (--student /
                                                          --students-file); never picked automatically
                                                          for sending - pass --folder (older runs: _TEST)
Each folder has a manifest.json recording which school (section) each report belongs to, so
an ALL folder still lets send_emails_hs.py send only the High School reports, and vice versa.

The audit and email scripts use the newest suitable folder unless given --folder.
'''

import os
import json
from datetime import datetime

from db import BASE_DIR

REPORTS_ROOT = os.path.join(BASE_DIR, 'grade_reports')
MANIFEST = 'manifest.json'
SCOPES = ('HS', 'JH', 'ALL')
SECTION_SCOPE = {'High School': 'HS', 'Junior High': 'JH'}


def new_run_folder(scope, selected=False):
    """Create and return grade_reports\\<timestamp>_<scope>[_SELECTED]."""
    stamp = datetime.now().strftime('%Y-%m-%d_%H%M')
    name = f"{stamp}_{scope}" + ('_SELECTED' if selected else '')
    path = os.path.join(REPORTS_ROOT, name)
    n = 2
    while os.path.exists(path):  # two runs in the same minute
        path = os.path.join(REPORTS_ROOT, f"{name}_{n}")
        n += 1
    os.makedirs(path)
    return path


def write_manifest(folder, scope, snapshot, sections_by_file, excluded=None):
    manifest = {
        'created': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'school': scope,
        'snapshot': str(snapshot),
        'reports': sections_by_file,          # file name -> 'High School' / 'Junior High'
        'excluded': excluded or {},           # student name -> reason (no report made)
    }
    with open(os.path.join(folder, MANIFEST), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def read_manifest(folder):
    path = os.path.join(folder, MANIFEST)
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return None


def _folder_scope(name):
    parts = name.split('_')
    # <date>_<time>_<scope>[_SELECTED or _TEST][_n]
    return parts[2] if len(parts) >= 3 and parts[2] in SCOPES else None


def list_run_folders(include_test=False):
    """Report run folders, newest first."""
    if not os.path.isdir(REPORTS_ROOT):
        return []
    names = [n for n in os.listdir(REPORTS_ROOT)
             if os.path.isdir(os.path.join(REPORTS_ROOT, n)) and _folder_scope(n)
             and (include_test or ('_TEST' not in n and '_SELECTED' not in n))]
    return [os.path.join(REPORTS_ROOT, n) for n in sorted(names, reverse=True)]


def resolve_folder(folder=None, school=None):
    """
    The folder to audit/send: --folder if given (a path, or a folder name inside grade_reports),
    otherwise the newest non-test run folder for that school (HS or JH; ALL folders count for
    both). school=None means any school.
    """
    if folder:
        path = folder if os.path.isdir(folder) else os.path.join(REPORTS_ROOT, folder)
        if not os.path.isdir(path):
            raise SystemExit(f"Report folder not found: {folder}")
        return path
    for path in list_run_folders():
        scope = _folder_scope(os.path.basename(path))
        if school is None or scope in (school, 'ALL'):
            return path
    raise SystemExit(f"No report folder found in grade_reports for {school or 'any school'}. "
                     f"Run generate_grade_reports.py first.")


def report_files(folder, school=None):
    """
    The .html reports in a folder. With school='HS'/'JH', only that school's reports
    (using manifest.json); a folder without a manifest is only accepted if it is for that
    school, so an unlabelled mix can never be sent from the wrong account.
    """
    files = sorted(f for f in os.listdir(folder) if f.endswith('.html'))
    if not school:
        return files
    manifest = read_manifest(folder)
    if manifest:
        return [f for f in files if SECTION_SCOPE.get(manifest['reports'].get(f)) == school]
    if _folder_scope(os.path.basename(folder)) == school:
        return files
    raise SystemExit(f"{folder} has no manifest.json saying which reports are {school}; "
                     f"regenerate the reports or pass a folder made for {school}.")

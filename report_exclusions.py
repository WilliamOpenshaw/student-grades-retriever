'''
Who should NOT get a grade report (used by generate_grade_reports.py, audit_emails.py and
send_emails_hs.py / send_emails_jh.py).

Two sources:
  1. (Currently OFF - see EXCLUDE_HONORS_STUDENTS below; honors students get reports.)
     Honors students: their name in the current semester's HS student list ends with "(H)",
     e.g. "G09 Jordan Lee (李小明) (H)". Picked up automatically from each new list import.
  2. report_exclusions.txt in this folder: anyone else (teacher placeholder accounts, etc.).
     One entry per line: a Classroom email, a Google Classroom user ID, or a full name as it
     appears in Classroom. Anything after '#' is a comment.

Usage:
    from report_exclusions import load_exclusions
    excluded = load_exclusions(engine)
    reason = excluded.reason(student_id=..., email=..., name=...)   # None = include
'''

import os
import re

from sqlalchemy import text

from db import BASE_DIR

EXCLUSIONS_FILE = os.path.join(BASE_DIR, 'report_exclusions.txt')
HS_TABLE = 'Student_Name_List_HS'

# Honors "(H)" students DO get reports (user's decision, 2026-10-12). Set to True to skip them
# again automatically. report_exclusions.txt is always applied.
EXCLUDE_HONORS_STUDENTS = False


def _semester_key(sem):
    m = re.search(r'(\d{4})', sem or '')
    return (int(m.group(1)) if m else 0, 2 if 'fall' in (sem or '').lower() else 1)


def _norm(value):
    return ' '.join(str(value or '').lower().split())


class Exclusions:
    def __init__(self):
        self.by_id, self.by_email, self.by_name = {}, {}, {}

    def add(self, reason, student_id=None, email=None, name=None):
        if student_id:
            self.by_id[str(student_id).strip()] = reason
        if email:
            self.by_email[_norm(email)] = reason
        if name:
            self.by_name[_norm(name)] = reason

    def reason(self, student_id=None, email=None, name=None):
        """Why this student is excluded, or None if they should get a report."""
        return (self.by_id.get(str(student_id).strip()) if student_id else None) \
            or (self.by_email.get(_norm(email)) if email else None) \
            or (self.by_name.get(_norm(name)) if name else None)

    def __len__(self):
        return len(set(self.by_id.values()) | set(self.by_email.values()) | set(self.by_name.values()))


def load_exclusions(engine, semester=None):
    excluded = Exclusions()

    with engine.connect() as conn:
        if not semester:
            sems = [r[0] for r in conn.execute(text(f'SELECT DISTINCT semester FROM "{HS_TABLE}" WHERE semester IS NOT NULL'))]
            semester = max(sems, key=_semester_key) if sems else None
        if semester and EXCLUDE_HONORS_STUDENTS:
            rows = conn.execute(text(f'''
                SELECT name, "E Name", "Student email", google_classroom_id, google_classroom_name
                FROM "{HS_TABLE}"
                WHERE semester = :sem AND TRIM(name) LIKE '%(H)'
            '''), {'sem': semester}).fetchall()
            for list_name, e_name, email, gid, gname in rows:
                reason = f"honors student ({list_name.strip()})"
                excluded.add(reason, student_id=gid, email=email, name=e_name)
                if gname:
                    excluded.add(reason, name=gname)

    if os.path.exists(EXCLUSIONS_FILE):
        with open(EXCLUSIONS_FILE, encoding='utf-8') as f:
            for line in f:
                entry, _, comment = line.partition('#')
                entry = entry.strip()
                if not entry:
                    continue
                reason = f"listed in report_exclusions.txt ({comment.strip() or entry})"
                if '@' in entry:
                    excluded.add(reason, email=entry)
                elif entry.isdigit():
                    excluded.add(reason, student_id=entry)
                else:
                    excluded.add(reason, name=entry)
    return excluded

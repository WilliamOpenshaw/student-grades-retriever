r'''
Overall-grade visibility check - GOOGLE CLASSROOM version (the settings right now).

Asks Google Classroom for every current class's grading settings and lists:
    no overall grade  Classroom > class Settings > Grading > "No overall grade"
                      -> NOT on parent reports ("This Class is Not Graded")
    teachers only     overall grade shown to teachers only -> shown on parent reports anyway
                      (decided 2026-10-13), listed as a note because students can't see it in Classroom
It also compares Classroom with the last ingest in the database, so you can see which classes
have changed since then. Reports are built from the database, so after a change, re-run
ingest, enrich and calculate before generating reports.

Read-only: only lists courses (no grades are fetched, nothing is written to the database).
Grade counts shown come from the last ingest. Saved to logs/grade_visibility_check.log.

    .venv\Scripts\python.exe grade_visibility_classroom.py              (both schools)
    .venv\Scripts\python.exe grade_visibility_classroom.py --school HS

For the settings as of the last ingest only (what the reports will use), see grade_visibility_db.py.
'''

import os
import argparse
from datetime import datetime

from googleapiclient.discovery import build
from sqlalchemy import text

from classroom_api import list_all
from course_terms import current_term, split_courses, term_label
from db import get_db_engine, latest_snapshots_by_section
from grade_visibility_db import LOG_FILE
from ingest_classroom_data import get_credentials, SCHOOL_CONFIGS

SCHOOLS = {'HS': 'High School', 'JH': 'Junior High'}
SHOWN = 'SHOW_OVERALL_GRADE'
LABEL = {
    'SHOW_OVERALL_GRADE': 'shown to students',
    'SHOW_TEACHERS_ONLY': 'teachers only',
    'HIDE_OVERALL_GRADE': 'no overall grade',
    'WEIGHTED_CATEGORIES': 'weighted by category',
    'TOTAL_POINTS': 'total points',
    None: 'not set',
}
HEADINGS = {
    'SHOW_TEACHERS_ONLY': 'Overall grade shown to TEACHERS ONLY',
    'HIDE_OVERALL_GRADE': 'Set to NO OVERALL GRADE',
}


def classroom_settings(config):
    """{course name: (calculationType, displaySetting)} for the school's current active classes."""
    creds = get_credentials(config['token_file'])
    if not creds:
        raise RuntimeError(f"No Google sign-in for {config['name']} ({config['token_file']}).")
    service = build('classroom', 'v1', credentials=creds, cache_discovery=False)
    courses = list_all(service.courses().list, 'courses', courseStates=['ACTIVE'])
    groups = split_courses([c['name'] for c in courses], current_term())
    keep = set(groups['current']) | set(groups['unknown'])   # same rule as ingest
    settings = {}
    for c in courses:
        if c['name'] in keep:
            gs = c.get('gradebookSettings', {})
            settings[c['name']] = (gs.get('calculationType'), gs.get('displaySetting'))
    return settings


def database_settings(conn, section, snapshot):
    """{course name: (calculation, display, grades entered)} from the last ingest."""
    rows = conn.execute(text("""
        SELECT course_name, MAX(grade_calculation), MAX(grade_display),
               SUM(CASE WHEN assigned_score IS NOT NULL THEN 1 ELSE 0 END)
        FROM student_grade_snapshots
        WHERE section = :sec AND snapshot_date = :snap
        GROUP BY course_name
    """), {'sec': section, 'snap': snapshot}).fetchall()
    return {r[0]: (r[1], r[2], int(r[3] or 0)) for r in rows}


def school_lines(config, conn, latest):
    section = config['name']
    now = classroom_settings(config)
    snap = latest.get(section)
    db = database_settings(conn, section, snap) if snap else {}
    since = f"the last ingest ({str(snap)[:16]})" if snap else "the last ingest (none found)"

    hidden = {name: s for name, s in now.items() if s[1] != SHOWN}
    lines = [f"[{section}] Google Classroom right now ({datetime.now():%Y-%m-%d %H:%M}, "
             f"{term_label(current_term())} classes):",
             f"  {len(now)} classes: {len(now) - len(hidden)} show the overall grade to students, "
             f"{len(hidden)} don't."]

    def grades_note(name):
        if name not in db:
            return "not in the last ingest"
        return f"{db[name][2]} grades at last ingest"

    for display, heading in HEADINGS.items():
        group = sorted((n for n, s in hidden.items() if s[1] == display),
                       key=lambda n: (-(db.get(n, (0, 0, 0))[2]), n))
        if group and display == 'HIDE_OVERALL_GRADE':
            lines.append(f"  ! {heading} - reports would show \"This Class is Not Graded\" for:")
        elif group:
            lines.append(f"  Note: {heading} - shown on reports anyway, although students can't see it in Classroom:")
        for name in group:
            lines.append(f"      - {name}  ({grades_note(name)})")

    changed = []
    for name, (calc, disp) in sorted(now.items()):
        if name in db and (db[name][0], db[name][1]) != (calc, disp):
            old_c, old_d = db[name][0], db[name][1]
            parts = []
            if old_d != disp:
                parts.append(f"{LABEL.get(old_d, old_d)} -> {LABEL.get(disp, disp)}")
            if old_c != calc:
                parts.append(f"{LABEL.get(old_c, old_c)} -> {LABEL.get(calc, calc)}")
            changed.append(f"      - {name}: {'; '.join(parts)}")
    if changed:
        lines.append(f"  Changed since {since} - re-run ingest, enrich and calculate so reports use the new setting:")
        lines.extend(changed)
    elif snap:
        lines.append(f"  No setting has changed since {since}.")

    lines.append("  (Graded work that doesn't count - \"No category\" in weighted classes - needs the grades, so it's "
                 "listed by grade_visibility_db.py from the last ingest.)")
    if any(s[1] == 'HIDE_OVERALL_GRADE' for s in hidden.values()):
        lines.append("  If \"no overall grade\" isn't intended, ask the teacher to change it in Classroom > class Settings > Grading:")
        lines.append("    no overall grade  -> choose \"Total points\" or \"Weighted by category\" (and show it to students)")
    return lines


def main():
    parser = argparse.ArgumentParser(description="Check Google Classroom now for classes whose overall grade "
                                                 "won't show on parent reports.")
    parser.add_argument('--school', choices=['HS', 'JH', 'ALL'], default='ALL')
    args = parser.parse_args()
    wanted = [SCHOOLS[args.school]] if args.school != 'ALL' else list(SCHOOLS.values())

    engine = get_db_engine()
    lines = []
    with engine.connect() as conn:
        latest = latest_snapshots_by_section(conn)
        for config in SCHOOL_CONFIGS:
            if config['name'] in wanted:
                try:
                    lines += school_lines(config, conn, latest)
                except Exception as e:
                    lines.append(f"[{config['name']}] Could not check Google Classroom: {e}")
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  grade_visibility_classroom.py =====\n")
        for line in lines:
            print(line)
            f.write(line + '\n')


if __name__ == '__main__':
    main()

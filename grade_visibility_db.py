'''
Overall-grade visibility check - DATABASE version (the settings as of the last ingest, i.e. what
the reports will be built from). For the settings in Google Classroom right now, and which
classes changed since the last ingest, run grade_visibility_classroom.py.

Each Classroom course has a setting for who can see the overall grade (Classroom > class
Settings > Grading > "Show overall grade to students"). Ingest stores it per course in
`grade_display`:
    SHOW_OVERALL_GRADE   shown to students            -> shown on parent reports
    SHOW_TEACHERS_ONLY   teachers only                -> shown on parent reports too (decided
                                                         2026-10-13), although students can't see it
                                                         in Classroom: listed as a note
    HIDE_OVERALL_GRADE   "No overall grade" / hidden  -> NOT shown on reports ("This Class is Not Graded")

This lists the courses whose overall grade will NOT appear on parent reports (warning), and the
teachers-only courses whose grade WILL appear although students can't see it in Classroom (note),
so you can check with the teacher before reports go out. It only warns; it never stops a run.

It also lists GRADED WORK THAT DOESN'T COUNT (added 2026-10-13): in a "Weighted by category" class,
work in "No category" or in a category with a 0% weight is graded and visible to the student but is
ignored in the overall grade (in Classroom and on our reports). If ALL graded work is like that, the
class has no overall grade at all. Fix in Classroom: put the assignments in a weighted category, or
give the category a weight.

Runs automatically at the end of ingest_classroom_data.py and in generate_grade_reports.py,
and is saved to logs/grade_visibility_check.log. Run it on its own any time:
    .venv\\Scripts\\python.exe grade_visibility_db.py                 (both schools, latest data)
    .venv\\Scripts\\python.exe grade_visibility_db.py --school JH
'''

import os
import argparse
from datetime import datetime

from sqlalchemy import text

from db import BASE_DIR, get_db_engine, latest_snapshots_by_section

LOG_FILE = os.path.join(BASE_DIR, 'logs', 'grade_visibility_check.log')
SCHOOLS = {'HS': 'High School', 'JH': 'Junior High'}
HIDDEN = {
    'SHOW_TEACHERS_ONLY': 'Overall grade shown to TEACHERS ONLY in Classroom',
    'HIDE_OVERALL_GRADE': 'Classroom set to NO OVERALL GRADE',
}


def hidden_courses(conn, section, snapshot):
    """[(display, course, students, grades_entered)] for courses whose overall grade is hidden."""
    rows = conn.execute(text("""
        SELECT MAX(grade_display), course_name, COUNT(DISTINCT student_id),
               SUM(CASE WHEN assigned_score IS NOT NULL THEN 1 ELSE 0 END)
        FROM student_grade_snapshots
        WHERE section = :sec AND snapshot_date = :snap
          AND grade_display IN ('SHOW_TEACHERS_ONLY', 'HIDE_OVERALL_GRADE')
        GROUP BY course_name
        ORDER BY 1 DESC, 4 DESC, 2
    """), {'sec': section, 'snap': snapshot}).fetchall()
    return [(d, c, int(s), int(g or 0)) for d, c, s, g in rows]


def uncounted_work(conn, section, snapshot):
    """
    Weighted classes (and older data without a setting, which uses the weighted formula) that have
    graded work in a category with no weight or no category. Returns, most affected first:
    [(course, students, graded items, uncounted items, {category: assignments}, [example titles],
      students with an overall grade)]
    """
    rows = conn.execute(text("""
        SELECT course_name, COALESCE(category, 'No Category'), COALESCE(category_weight, 0), assignment_title,
               assignment_id, student_id, assigned_score, max_points, overall_grade
        FROM student_grade_snapshots
        WHERE section = :sec AND snapshot_date = :snap
          AND (grade_calculation = 'WEIGHTED_CATEGORIES' OR grade_calculation IS NULL)
          AND COALESCE(grade_display, '') <> 'HIDE_OVERALL_GRADE'
    """), {'sec': section, 'snap': snapshot}).fetchall()
    by = {}
    for course, cat, weight, title, aid, sid, score, mx, overall in rows:
        c = by.setdefault(course, {'students': set(), 'graded': 0, 'uncounted': 0, 'cats': {}, 'titles': [],
                                   'with_grade': set()})
        c['students'].add(sid)
        if overall is not None:
            c['with_grade'].add(sid)
        if score is None or not mx or mx <= 0:
            continue
        c['graded'] += 1
        if weight and weight > 0:
            continue
        c['uncounted'] += 1
        label = cat if cat == 'No Category' else f'{cat} (0%)'
        c['cats'].setdefault(label, set()).add(aid)
        if title and title not in c['titles']:
            c['titles'].append(title)
    out = [(course, len(c['students']), c['graded'], c['uncounted'],
            {k: len(v) for k, v in c['cats'].items()}, c['titles'][:3], len(c['with_grade']))
           for course, c in by.items() if c['uncounted']]
    return sorted(out, key=lambda r: (-(r[3] / r[2]), -r[3], r[0]))


def visibility_lines(engine, sections=None):
    """Report lines for each section's latest snapshot (sections: list of full section names)."""
    lines = []
    with engine.connect() as conn:
        latest = latest_snapshots_by_section(conn)
        for section in (sections or sorted(latest)):
            snap = latest.get(section)
            if not snap:
                continue
            courses = hidden_courses(conn, section, snap)
            graded = [c for c in courses if c[3] > 0]
            hidden = [c for c in graded if c[0] == 'HIDE_OVERALL_GRADE']
            teachers = [c for c in graded if c[0] == 'SHOW_TEACHERS_ONLY']
            lines.append(f"[{section}] Overall grade visibility (data from {str(snap)[:16]}):")
            if not hidden:
                lines.append("  OK - every course with graded work shows its overall grade on reports.")
            if hidden:
                lines.append(f"  ! {HIDDEN['HIDE_OVERALL_GRADE']} - reports will show \"This Class is Not Graded\" although work is graded:")
                for _, course, students, grades in hidden:
                    lines.append(f"      - {course}  ({students} students, {grades} grades entered)")
            if teachers:
                lines.append(f"  Note: {HIDDEN['SHOW_TEACHERS_ONLY']} - shown on reports anyway, "
                             f"although students can't see it in Classroom:")
                for _, course, students, grades in teachers:
                    lines.append(f"      - {course}  ({students} students, {grades} grades entered)")
            empty = [c[1] for c in courses if c[3] == 0 and c[0] == 'HIDE_OVERALL_GRADE']
            if empty:
                lines.append(f"  (Also no overall grade, but no grades entered yet: {', '.join(empty)})")
            if hidden:
                lines.append("  If this isn't intended, ask the teacher to change it in Classroom > class Settings > Grading:")
                lines.append("    no overall grade  -> choose \"Total points\" or \"Weighted by category\" (and show it to students)")
                lines.append("  then re-run ingest, enrich and calculate before generating reports.")
            uncounted = uncounted_work(conn, section, snap)
            if uncounted:
                lines.append(f"  ! Graded work that doesn't count toward the overall grade ({len(uncounted)} weighted "
                             f"classes; it's in \"No category\" or a 0% category, so it's ignored in Classroom too):")
                for course, students, graded, n, cats, titles, with_grade in uncounted:
                    where = ', '.join(f'{k}: {v} assignment{"s" if v != 1 else ""}' for k, v in sorted(cats.items()))
                    flag = '  -> NO overall grade for anyone' if not with_grade else ''
                    lines.append(f"      - {course}: {n} of {graded} graded items ({n * 100 // graded}%){flag}")
                    lines.append(f"          {where}; e.g. " + '; '.join(f'"{t}"' for t in titles))
                lines.append("  Fix in Classroom (if not intended): put these assignments in a weighted category, "
                             "or give the category a weight in class Settings > Grading.")
    return lines


def log_visibility_check(script, engine, sections=None):
    """Print the check and append it (timestamped) to logs/grade_visibility_check.log."""
    lines = visibility_lines(engine, sections)
    if not lines:
        return lines
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"\n===== {datetime.now():%Y-%m-%d %H:%M:%S}  {script} =====\n")
        for line in lines:
            print(line)
            f.write(line + '\n')
    return lines


def main():
    parser = argparse.ArgumentParser(description="List courses whose overall grade won't show on parent reports.")
    parser.add_argument('--school', choices=['HS', 'JH', 'ALL'], default='ALL')
    args = parser.parse_args()
    sections = None if args.school == 'ALL' else [SCHOOLS[args.school]]
    log_visibility_check('grade_visibility_db.py', get_db_engine(), sections)


if __name__ == '__main__':
    main()

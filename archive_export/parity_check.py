r"""
Parity check (read-only): turns real snapshots into export-shaped data and compares
import_archive_export.class_grades() with the overall_grade that calculate_category_grades.py stored.
Run from the project folder after changing either formula:
    .venv\Scripts\python.exe archive_export\parity_check.py [test_export.zip]
Expected (2026-10-12, after the leftover-0/100 fix in calculate_category_grades.py): all identical
except a rare 0.01 (float summation order).
With a zip path it also writes the 2026 Spring HS classes as a fake export for import tests
(contains student data: write it to a temporary folder and delete it afterwards).
"""
import json, sqlite3, sys, zipfile
from collections import defaultdict
sys.path.insert(0, '.')  # run from the project folder
from import_archive_export import class_grades

c = sqlite3.connect('file:grades.db?mode=ro', uri=True)


def build(section, snap):
    rows = c.execute('''SELECT course_name, student_id, student_name, assignment_id, assignment_title, category,
                               category_weight, max_points, assigned_score, is_late, state, grade_calculation, grade_display, overall_grade
                        FROM student_grade_snapshots WHERE section=? AND snapshot_date=?''', (section, snap)).fetchall()
    by = defaultdict(list)
    for r in rows:
        by[r[0]].append(r)
    exports, expected = [], {}
    for i, (course, rs) in enumerate(sorted(by.items())):
        cats = {}
        for r in rs:
            if r[5] and r[5] != 'No Category':
                cats[r[5]] = r[6] or 0
        gb = {'calculationType': rs[0][11], 'displaySetting': rs[0][12],
              'gradeCategories': [{'id': n, 'name': n, 'weight': round(w * 10000)} for n, w in cats.items()]} if rs[0][11] or cats else None
        work, subs, studs = {}, [], {}
        for r in rs:
            work[r[3]] = {'id': r[3], 'title': r[4], 'maxPoints': r[7],
                          'gradeCategory': {'id': r[5]} if r[5] and r[5] != 'No Category' else None, 'creationTime': None}
            subs.append({'courseWorkId': r[3], 'userId': r[1], 'state': r[10], 'assignedGrade': r[8], 'draftGrade': None, 'late': bool(r[9])})
            studs[r[1]] = {'userId': r[1], 'name': r[2], 'email': ''}
            expected[(course, r[1])] = r[13]
        exports.append({'exportVersion': 1, 'exportedAt': '2026-10-12T00:00:00Z', 'exportedBy': 'parity-test',
                        'course': {'id': f'P{section[0]}{i}', 'name': course, 'courseState': 'ARCHIVED', 'creationTime': None,
                                   'gradebookSettings': gb},
                        'teachers': [], 'students': list(studs.values()), 'courseWork': list(work.values()),
                        'submissions': subs, 'errors': []})
    return exports, expected


def compare(exports, expected, label):
    same = diff = 0
    examples = []
    for e in exports:
        rows = class_grades(e)[0]
        for (course, sid), want in expected.items():
            if course != e['course']['name']:
                continue
            got = rows.get(sid, {}).get('final_pct')
            if (got is None and want is None) or (got is not None and want is not None and abs(got - want) < 0.005):
                same += 1
            else:
                diff += 1
                if len(examples) < 5:
                    examples.append((course, sid, want, got))
    print(f'{label}: {same} student-course grades identical, {diff} different', examples)


if __name__ == '__main__':
    for sec, snap, label in [('High School', '2026-10-07 09:35', 'HS 2026-10-07 (Classroom settings)'),
                             ('Junior High', None, 'JH latest (Classroom settings)'),
                             ('High School', None, 'HS 2026 Spring final (older snapshot, no settings)')]:
        if snap is None and 'Spring' in label:
            snap = c.execute("SELECT MAX(snapshot_date) FROM student_grade_snapshots WHERE section=? AND semester='2026 Spring'", (sec,)).fetchone()[0]
        elif snap is None:
            snap = c.execute('SELECT MAX(snapshot_date) FROM student_grade_snapshots WHERE section=?', (sec,)).fetchone()[0]
        else:
            snap = c.execute('SELECT snapshot_date FROM student_grade_snapshots WHERE section=? AND snapshot_date LIKE ? LIMIT 1', (sec, snap + '%')).fetchone()[0]
        ex, expd = build(sec, snap)
        compare(ex, expd, label + f' [{snap}]')
        if len(sys.argv) > 1 and 'Spring' in label:
            with zipfile.ZipFile(sys.argv[1], 'w') as z:
                for e in ex:
                    z.writestr(f"Classroom grade export test/course_{e['course']['id']}.json", json.dumps(e))
            print('wrote', sys.argv[1], len(ex), 'classes')

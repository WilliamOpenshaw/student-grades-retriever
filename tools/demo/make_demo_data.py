r'''
Makes a database for a made-up school (random names, emails and grades), so the project can be
tried and demonstrated without Google Classroom. Nothing in it is real.

    .venv\Scripts\python.exe tools\demo\make_demo_data.py            -> demo_output\demo_grades.db

Then point the other scripts at it with DB_PATH (PowerShell):
    $env:DB_PATH = "demo_output\demo_grades.db"
    .venv\Scripts\python.exe calculate_category_grades.py --no-dashboard
    .venv\Scripts\python.exe generate_grade_reports.py
    .venv\Scripts\python.exe sync_dashboard.py --dry-run --out-dir dashboard_web_app\local_test\out\data
    .venv\Scripts\python.exe dashboard_web_app\local_test\build.py
    Remove-Item Env:DB_PATH

The rows look like what ingest_classroom_data.py + the enrich scripts store: two sections, a
Spring 2026 semester and three grade pulls in Fall 2026 (end of September, end of October and
early November), weighted and total-points classes, late and missing work, and draft grades.
'''
import os
import random
import sqlite3
import sys
import zlib
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
from create_database import SCHEMA  # noqa: E402

OUT_DIR = os.path.join(ROOT, 'demo_output')
DB = os.path.join(OUT_DIR, 'demo_grades.db')
rng = random.Random(2026)


def h(*parts):
    """Stable hash (Python's hash() changes between runs)."""
    return zlib.crc32(repr(parts).encode())

FIRST = ['Maya', 'Leo', 'Ava', 'Noah', 'Mia', 'Ethan', 'Zoe', 'Lucas', 'Chloe', 'Owen', 'Ella', 'Ryan',
         'Nora', 'Aiden', 'Lily', 'Caleb', 'Ivy', 'Isaac', 'Ruby', 'Adam', 'Hannah', 'Felix', 'Clara',
         'Daniel', 'Grace', 'Henry', 'Alice', 'Oscar', 'Emma', 'Theo', 'Sara', 'Jonah', 'Leah', 'Miles',
         'Julia', 'Simon', 'Iris', 'Elias', 'Anna', 'Max', 'Nina', 'Victor', 'Rosa', 'Ben', 'Kira', 'Sam']
LAST = ['Archer', 'Bennett', 'Carver', 'Dalton', 'Ellis', 'Fletcher', 'Garner', 'Hale', 'Ingram',
        'Jensen', 'Keller', 'Lowell', 'Marsh', 'Norwood', 'Oakley', 'Prescott', 'Quill', 'Ramsey',
        'Sutton', 'Thorne', 'Upton', 'Vance', 'Whitlock', 'Yardley', 'Ashby', 'Brook', 'Crane', 'Drake']

WEIGHTED = [('Tests', 40.0), ('Classwork', 30.0), ('Projects', 20.0), ('Participation', 10.0)]   # percent, as ingest stores them
SUBJECTS = {
    6: ['English 6', 'Math 6', 'Science 6', 'Social Studies 6', 'Art 6', 'PE 6'],
    7: ['English 7', 'Math 7', 'Science 7', 'Social Studies 7', 'Music 7', 'PE 7'],
    8: ['English 8', 'Math 8', 'Science 8', 'Social Studies 8', 'Art 8', 'PE 8'],
    9: ['English 9', 'Algebra I', 'Integrated Science', 'World History', 'Visual Arts', 'PE 9'],
    10: ['English 10', 'Geometry', 'Biology', 'Modern History', 'Music', 'PE 10'],
    11: ['English 11', 'Pre-Calculus', 'Chemistry', 'Economics', 'Spanish I', 'PE 11'],
    12: ['English 12', 'Calculus', 'Physics', 'Psychology', 'Spanish II', 'Digital Media'],
}
# (month posted, title, category, max points); a course posts these in order
PLAN = [
    (1, 'Unit 1 Quiz', 'Tests', 20), (1, 'Worksheet 1', 'Classwork', 10), (1, 'Participation - Week 1-4', 'Participation', 10),
    (1, 'Reading Response 1', 'Classwork', 10),
    (2, 'Unit 1 Test', 'Tests', 50), (2, 'Project 1', 'Projects', 40), (2, 'Worksheet 2', 'Classwork', 10),
    (2, 'Participation - Week 5-8', 'Participation', 10), (2, 'Reading Response 2', 'Classwork', 10),
    (3, 'Unit 2 Quiz', 'Tests', 20), (3, 'Worksheet 3', 'Classwork', 10), (3, 'Project 2 Draft', 'Projects', 20),
]
FALL_PULLS = ['2026-09-30 15:10:00.000000', '2026-10-30 15:20:00.000000', '2026-11-05 15:05:00.000000']
SPRING_PULLS = ['2026-03-31 15:00:00.000000', '2026-05-29 15:00:00.000000']


def section(grade):
    return 'High School' if grade >= 9 else 'Junior High'


def make_students():
    students, used, n = [], set(), 0
    for grade, count in ((6, 10), (7, 10), (8, 10), (9, 11), (10, 11), (11, 11), (12, 11)):
        for i in range(count):
            while True:
                first, last = rng.choice(FIRST), rng.choice(LAST)
                if (first, last) not in used:
                    used.add((first, last))
                    break
            n += 1
            ability = min(97, max(58, rng.gauss(83, 8)))
            kind = 'normal'
            if n % 13 == 5:
                kind, ability = 'struggling', rng.uniform(48, 60)
            elif n % 17 == 3:
                kind = 'declining'
            students.append({
                'id': str(104857600000 + n * 7919), 'first': first, 'last': last, 'name': f'{first} {last}',
                'grade': grade, 'homeroom': f'{grade}{"AB"[i % 2]}', 'gender': rng.choice('MF'),
                'email': f'{first}.{last}@example.edu'.lower(),
                'parents': [f'{last}.family@example.com'.lower(), f'{first[0]}{last}.parent@example.com'.lower()],
                'ability': ability, 'kind': kind,
            })
    return students


def course_settings(subject):
    if subject.startswith('PE') or subject == 'Digital Media':
        return 'TOTAL_POINTS', 'SHOW_OVERALL_GRADE'
    return 'WEIGHTED_CATEGORIES', 'SHOW_OVERALL_GRADE'


def rows_for(students, term, pulls, months_by_pull, year_shift):
    rows = []
    course_bias = {}
    for s in students:
        grade = s['grade'] - year_shift
        if grade < 6:
            continue
        sec = section(grade)
        ability = s['ability'] + (12 if (s['kind'] == 'declining' and year_shift) else 0)
        subjects = SUBJECTS[grade]
        courses = [(f'{term} {subj}', subj) for subj in subjects] + [(f'{term} Homeroom {grade}{s["homeroom"][-1]}', 'Homeroom')]
        for p_i, pull in enumerate(pulls):
            months = months_by_pull[p_i]
            latest = p_i == len(pulls) - 1
            for course, subj in courses:
                calc, disp = course_settings(subj)
                cid = h(term, course) % 10**9
                base = {'snapshot_date': pull, 'section': sec, 'grade_level': f'Grade {grade}', 'student_name': s['name'],
                        'course_name': course, 'student_id': s['id'], 'gender': s['gender'],
                        'attended_jh': False,
                        'student_email': s['email'], 'grade_calculation': calc, 'grade_display': disp,
                        'overall_grade': None, 'student_grade_in_category': None, 'draft_score': None}
                if subj == 'Homeroom':
                    rows.append({**base, 'grade_calculation': 'TOTAL_POINTS', 'grade_display': 'HIDE_OVERALL_GRADE',
                                 'assignment_title': 'Welcome form', 'category': None, 'category_weight': 0,
                                 'assigned_score': None, 'max_points': None, 'is_late': False, 'is_excused': False,
                                 'state': 'TURNED_IN', 'assignment_id': f'{cid}01'})
                    continue
                bias = course_bias.setdefault((s['id'], subj), rng.gauss(0, 5))
                for a_i, (month, title, cat, pts) in enumerate(PLAN):
                    if month > months:
                        continue
                    weight = dict(WEIGHTED)[cat] if calc == 'WEIGHTED_CATEGORIES' else 0
                    r = random.Random(h(s['id'], course, title))
                    newest = latest and month == months
                    pct = ability + bias + r.gauss(0, 7) - (6 if cat == 'Tests' else 0)
                    if s['kind'] == 'declining' and not year_shift:
                        pct -= 4 * month   # slipping during the fall
                    score = round(max(0, min(100, pct)) / 100 * pts * 2) / 2
                    state, late, draft = 'RETURNED', r.random() < 0.06, None
                    if s['kind'] == 'struggling' and r.random() < 0.35:
                        state, late, score = 'CREATED', True, None          # missing
                    elif r.random() < 0.025:
                        state, late, score = 'CREATED', True, None
                    elif newest and r.random() < 0.45:
                        if r.random() < 0.5:
                            state, late, draft, score = 'TURNED_IN', False, score, None   # graded, not returned yet
                        else:
                            state, late, score = 'CREATED', False, None    # not due yet
                    rows.append({**base, 'assignment_title': title,
                                 'category': cat if calc == 'WEIGHTED_CATEGORIES' else None,
                                 'category_weight': weight, 'assigned_score': score, 'max_points': pts,
                                 'is_late': late, 'is_excused': False, 'state': state, 'draft_score': draft,
                                 'assignment_id': f'{cid}{a_i + 10}'})
    for r in rows:
        r['semester'] = '2026 Fall' if not year_shift else '2026 Spring'
    return rows


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    for statement in SCHEMA:
        conn.execute(statement)
    students = make_students()
    for s in students:
        if s['grade'] >= 9:
            conn.execute('INSERT INTO "Student_Name_List_HS" (semester, name, grade, "E Name", "Home Room", gender, '
                         '"Student email", "Parent email", google_classroom_name, google_classroom_id) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?)',
                         ('2026 Fall', f'G{s["grade"]} {s["name"]}', str(s['grade']), s['name'], s['homeroom'], s['gender'],
                          s['email'], ', '.join(s['parents']), s['name'], s['id']))
        else:
            conn.execute('INSERT INTO student_name_list_jh (semester, grade_level, first_name, last_name, gender, homeroom, '
                         'student_email, father_email, mother_email, google_classroom_name, google_classroom_id) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                         ('2026 Fall', str(s['grade']), s['first'], s['last'], s['gender'], s['homeroom'], s['email'],
                          s['parents'][0], s['parents'][1], s['name'], s['id']))
    rows = rows_for(students, '26S', SPRING_PULLS, [2, 3], 1) + rows_for(students, '26F', FALL_PULLS, [1, 2, 3], 0)
    cols = list(rows[0])
    conn.executemany(f'INSERT INTO student_grade_snapshots ({", ".join(cols)}) VALUES ({", ".join("?" * len(cols))})',
                     [[r[c] for c in cols] for r in rows])
    conn.commit()
    conn.close()
    print(f'{DB}: {len(students)} made-up students, {len(rows)} grade rows '
          f'({datetime.now():%Y-%m-%d %H:%M})')


if __name__ == '__main__':
    main()

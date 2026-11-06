r'''
Import a "Classroom grade export" (made by archive_export/ArchiveExport.gs in any Google account)
into grades.db, so past classes can be compared with current ones.

The export holds raw Classroom data per class (grading settings, categories, assignments, returned
grades). This script calculates each student's final grade with the SAME rules as
calculate_category_grades.py (category % = points earned / points possible of graded work, rounded to
2 decimals; weighted categories / total points per the class's Classroom setting; "no overall grade"
classes keep the calculated value in calc_pct but final_pct is empty).

Usage:
    .venv\Scripts\python.exe import_archive_export.py "Classroom grade export 2026-10-12 1200.zip" --dry-run
    .venv\Scripts\python.exe import_archive_export.py "<zip or folder>" [more zips/folders ...]
Options:
    --dry-run           read and summarise only; nothing is written
    --include-current   also import classes of the current semester (normally left to the pipeline)
Drive may split a big folder into several zips (…-001.zip, …-002.zip): pass them all.

Tables (created if missing; importing a class again replaces its rows, newest export wins):
    archived_classes          one row per class: name, term, section (HS/JH), settings, counts, class average
    archived_grades           one row per student per class: final_pct, calc_pct, graded / late counts
    archived_category_grades  per student per class per category
    archived_coursework       assignments (title, category, weight, max points, due date)
    archived_submissions      every submission (state, assigned / draft grade, late)
The database is backed up first (backups folder).
'''

import argparse
import glob
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import text

from course_departments import course_key, suggest
from course_terms import FALL, SPRING, course_terms, current_term, term_label
from db import get_db_engine

# Junior High signs in a class name. Teachers use underscores ("2022-Fall_JH_English Set 1",
# "PBL_G6-G7_B", "PHE_Wellbeing_G7"), so "_" counts as a word boundary here.
JH_NAME = re.compile(r'(?:^|[\s_\-(/])(?:JH|G[5-8]|grade\s*[5-8])(?=$|[\s_\-)./,]|\b)|\bmath\s*[5-8]|middle grade|'
                     r'\b[5-8][AB]\b|_[5-8][AB]\b|_Fall_|stage 2|transitional|course\s*-?\s*[123]\b', re.I)

DDL = [
    '''CREATE TABLE IF NOT EXISTS archived_classes (
        class_id TEXT PRIMARY KEY, course_name TEXT, classroom_section TEXT, school_section TEXT,
        semester TEXT, school_year TEXT, term_source TEXT, year_long INTEGER, course_key TEXT, department TEXT,
        grade_calculation TEXT, grade_display TEXT, course_state TEXT, creation_time TEXT, teachers TEXT,
        n_students INTEGER, n_graded_students INTEGER, n_coursework INTEGER, n_submissions INTEGER,
        class_avg REAL, export_errors TEXT, exported_by TEXT, exported_at TEXT, imported_at TEXT)''',
    '''CREATE TABLE IF NOT EXISTS archived_grades (
        class_id TEXT, student_id TEXT, student_name TEXT, student_email TEXT,
        final_pct REAL, calc_pct REAL, graded_count INTEGER, late_count INTEGER,
        PRIMARY KEY (class_id, student_id))''',
    '''CREATE TABLE IF NOT EXISTS archived_category_grades (
        class_id TEXT, student_id TEXT, category TEXT, weight REAL, pct REAL)''',
    '''CREATE TABLE IF NOT EXISTS archived_coursework (
        class_id TEXT, coursework_id TEXT, title TEXT, category TEXT, weight REAL, max_points REAL,
        due_date TEXT, creation_time TEXT, work_type TEXT)''',
    '''CREATE TABLE IF NOT EXISTS archived_submissions (
        class_id TEXT, coursework_id TEXT, student_id TEXT, state TEXT, assigned_grade REAL,
        draft_grade REAL, late INTEGER)''',
    'CREATE INDEX IF NOT EXISTS ix_archived_grades_student ON archived_grades (student_id)',
    'CREATE INDEX IF NOT EXISTS ix_archived_subs_class ON archived_submissions (class_id)',
]


# ---------------------------------------------------------------------------------------------
# Reading the export
# ---------------------------------------------------------------------------------------------

def read_exports(paths):
    """Every course_*.json in the given zips / folders. Same class twice: the newest export wins."""
    found = {}

    def take(name, raw):
        if not re.search(r'(^|/)course_[^/]+\.json$', name):
            return
        data = json.loads(raw)
        if data.get('exportVersion') != 1:
            print(f"  ! {name}: unknown export version {data.get('exportVersion')}, skipped")
            return
        cid = data['course']['id']
        if cid not in found or data.get('exportedAt', '') > found[cid].get('exportedAt', ''):
            found[cid] = data

    for p in paths:
        for path in sorted(glob.glob(p)) or [p]:
            if os.path.isdir(path):
                for root, _dirs, files in os.walk(path):
                    for f in files:
                        with open(os.path.join(root, f), 'rb') as fh:
                            take(f, fh.read())
            elif zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as z:
                    for n in z.namelist():
                        take(n, z.read(n))
            else:
                sys.exit(f"Not a folder or zip file: {path}")
    return list(found.values())


# ---------------------------------------------------------------------------------------------
# Grade calculation (mirrors calculate_category_grades.py)
# ---------------------------------------------------------------------------------------------

def _r2(x):
    """ROUND(x, 2) as SQLite does it: the exact binary value, halves away from zero
    (78.125 -> 78.13, but 90.565, stored as 90.56499..., -> 90.56). Python's round(78.125, 2) gives 78.12."""
    if x is None:
        return None
    return float(Decimal(float(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def class_grades(data):
    """
    Returns (rows, category_rows, coursework_rows, settings):
      rows: {student_id: {final_pct, calc_pct, graded, late}}
    Rules as in calculate_category_grades.py:
      - graded work = assigned grade present and max points > 0 (excused isn't visible in the API)
      - category % = ROUND(sum earned / sum possible * 100, 2)
      - WEIGHTED_CATEGORIES (and old classes without a setting): ROUND(sum(cat% x weight) / sum(weight), 2)
        over categories with a grade and weight > 0
      - TOTAL_POINTS: ROUND(sum earned * 100 / sum possible, 2) over all graded work
      - HIDE_OVERALL_GRADE: final_pct empty (calc_pct still holds the calculation)
    """
    gb = data['course'].get('gradebookSettings') or {}
    calc = gb.get('calculationType')
    display = gb.get('displaySetting')
    cats = {c['id']: (c.get('name') or '', (c.get('weight') or 0) / 10000) for c in gb.get('gradeCategories') or []}
    work = {}
    for w in data['courseWork']:
        gc = w.get('gradeCategory') or {}
        name, weight = cats.get(gc.get('id'), ('No Category', 0))
        work[w['id']] = {'max': w.get('maxPoints'), 'cat': name, 'weight': weight, 'title': w.get('title', '')}

    earned = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))   # sid -> cat -> [earned, possible]
    weights, totals, graded, late = {}, defaultdict(lambda: [0.0, 0.0]), Counter(), Counter()
    for s in data['submissions']:
        w = work.get(s['courseWorkId'])
        if not w:
            continue
        if s.get('late'):
            late[s['userId']] += 1
        if s.get('assignedGrade') is None or not w['max'] or w['max'] <= 0:
            continue
        e = earned[s['userId']][w['cat']]
        e[0] += s['assignedGrade']
        e[1] += w['max']
        weights[w['cat']] = w['weight']
        totals[s['userId']][0] += s['assignedGrade']
        totals[s['userId']][1] += w['max']
        graded[s['userId']] += 1

    rows, cat_rows = {}, []
    for sid in set(earned) | set(late):
        cat_pct = {c: _r2(v[0] / v[1] * 100) for c, v in earned.get(sid, {}).items() if v[1]}
        for c, p in cat_pct.items():
            cat_rows.append((sid, c, weights.get(c, 0), p))
        if calc == 'TOTAL_POINTS':
            t = totals.get(sid)
            pct = _r2(t[0] * 100 / t[1]) if t and t[1] else None
        else:
            use = [(p, weights[c]) for c, p in cat_pct.items() if p is not None and weights.get(c, 0) > 0]
            wsum = sum(w for _, w in use)
            pct = _r2(sum(p * w for p, w in use) / wsum) if wsum else None
        rows[sid] = {'calc_pct': pct, 'final_pct': None if display == 'HIDE_OVERALL_GRADE' else pct,
                     'graded': graded[sid], 'late': late[sid]}
    return rows, cat_rows, work, {'calc': calc, 'display': display}


# ---------------------------------------------------------------------------------------------
# Term and section
# ---------------------------------------------------------------------------------------------

# Term formats used by Junior High teachers that course_terms.py doesn't read (kept here so the live
# pipeline's term check is unchanged): "2022-Fall_JH_…", "23_F_Math-1_…", "23-24_S_Arts-B".
_JH_TERM = [
    (re.compile(r'(20\d{2})[-_ ](fall|spring)(?![a-z])', re.I), lambda m: (int(m.group(1)), FALL if m.group(2).lower() == 'fall' else SPRING)),
    (re.compile(r'(?<!\d)(\d{2})-(\d{2})_([FS])_', re.I), lambda m: (2000 + int(m.group(1)), FALL) if m.group(3).upper() == 'F'
                                                              else (2000 + int(m.group(2)), SPRING)),
    (re.compile(r'(?<![\d-])(\d{2})_([FS])_', re.I), lambda m: (2000 + int(m.group(1)), FALL if m.group(2).upper() == 'F' else SPRING)),
]


def class_terms(course):
    """(set of terms, source): from the name, else the creation date (Jan-Jun Spring, Jul-Dec Fall)."""
    terms = course_terms(course.get('name', ''))
    if terms:
        return terms, 'name'
    for rx, fn in _JH_TERM:
        m = rx.search(course.get('name', ''))
        if m:
            return {fn(m)}, 'name'
    m = re.match(r'(\d{4})-(\d{2})', course.get('creationTime') or '')
    if not m:
        return set(), 'unknown'
    y, mo = int(m.group(1)), int(m.group(2))
    return {(y, SPRING) if mo <= 6 else (y, FALL)}, 'created'


def class_term(course):
    """(semester label, school year, source, year_long). A year-long class counts as its Spring (end)."""
    terms, source = class_terms(course)
    if not terms:
        return '', '', 'unknown', 0
    last = max(terms)
    year_long = 1 if len(terms) > 1 else 0
    y0 = last[0] if last[1] == FALL else last[0] - 1
    return term_label(last), f'{y0}-{y0 + 1}', source, year_long


# Class names that show the section when no student can be looked up for that semester.
HS_NAME = re.compile(r'\bG(9|1[0-2])\b|\bG1[0-2]\.|\bAP\b|\b(eng|mcr|mhf|mcv|mpm|sbi|sch|sph|snc)\d|\bmath\s*(9|1[0-2])\b|'
                     r'\b(9|1[0-2])\b(?!\s*[-/])|functions|calculus|ielts|college counsel|honou?rs|'
                     r'life design|intersession|physics|chemistry|biology', re.I)


def section_lookup(conn):
    """{student ID: {semester: Counter(section)}} from every snapshot and both name lists."""
    out = {}

    def add(sid, sem, sec, n=1):
        if sid and sec:
            out.setdefault(str(sid), {}).setdefault(sem or '', Counter())[sec] += n

    for sql, sec in (('SELECT google_classroom_id, semester FROM "Student_Name_List_HS"', 'High School'),
                     ('SELECT google_classroom_id, semester FROM student_name_list_jh', 'Junior High')):
        try:
            for gid, sem in conn.execute(text(sql)):
                add(gid, sem, sec)
        except Exception:
            pass
    for sid, sem, sec, n in conn.execute(text(
            'SELECT student_id, semester, section, COUNT(DISTINCT snapshot_date) FROM student_grade_snapshots GROUP BY 1, 2, 3')):
        add(sid, sem, sec, n)
    # Classes imported earlier (e.g. the High School archive) also tell who was in which school when.
    try:
        for sid, sem, sec in conn.execute(text(
                "SELECT g.student_id, a.semester, a.school_section FROM archived_grades g JOIN archived_classes a USING (class_id) "
                "WHERE a.school_section IN ('High School', 'Junior High')")):
            add(sid, sem, sec)
    except Exception:
        pass   # no archive yet
    return out


def semester_rosters(lookup):
    """{semester: {section: set(student IDs)}} from section_lookup()."""
    rosters = {}
    for sid, sems in lookup.items():
        for sem, counts in sems.items():
            for sec in counts:
                rosters.setdefault(sem, {}).setdefault(sec, set()).add(sid)
    return rosters


def class_section(data, lookup, semester, default='Unknown', rosters=None):
    """
    HS or JH, in this order:
      1. what the class's students were in THAT semester (snapshots, name lists, archive imported
         earlier). Counted by semester, so a new Grade 9 class isn't called JH;
      2. if one school's full list of students for that semester is known and none of the class's
         students are on it, the other school (e.g. "ENG3U(1)" in the JH account: none of its students
         were in any High School class that semester);
      3. the class name (G5-8 / _JH_ / Math 6 … = JH; G9-12, AP, ENG4U, Calculus … = HS);
      4. `default`.
    Returns (section, how it was decided).
    """
    votes = Counter()
    ids = {str(st['userId']) for st in data['students']}
    for sid in ids:
        c = lookup.get(sid, {}).get(semester)
        if c:
            votes[c.most_common(1)[0][0]] += 1
    # Enough students to go by: at least 3, or 40% of the class (one student who also took a class in
    # the other school mustn't decide a whole class).
    if votes and sum(votes.values()) >= min(3, max(1, round(len(ids) * 0.4))):
        return votes.most_common(1)[0][0], 'students'
    known = (rosters or {}).get(semester, {})
    if len(ids) >= 3:
        for sec, other in (('High School', 'Junior High'), ('Junior High', 'High School')):
            if len(known.get(sec, ())) >= 50 and len(ids & known[sec]) <= len(ids) * 0.1:
                return other, 'not in ' + sec
    name = data['course'].get('name', '')
    if JH_NAME.search(name):
        return 'Junior High', 'name'
    if HS_NAME.search(name):
        return 'High School', 'name'
    return default, 'default' if default != 'Unknown' else 'unknown'


# Not real classes: test / example courses (and classes without students are skipped too).
JUNK_NAME = re.compile(r'^\s*(test(ing)?|\d+)\s*$|\bexample\b', re.I)


# ---------------------------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description='Import a Classroom grade export into grades.db.')
    ap.add_argument('paths', nargs='+', help='export zip file(s) or folder(s)')
    ap.add_argument('--dry-run', action='store_true', help='summarise only, write nothing')
    ap.add_argument('--include-current', action='store_true', help='also import classes of the current semester')
    ap.add_argument('--default-section', choices=['High School', 'Junior High', 'Unknown'], default='Unknown',
                    help='section for classes that neither their students nor their name identify '
                         '(e.g. "High School" when the exporting account only has HS classes)')
    args = ap.parse_args()

    exports = read_exports(args.paths)
    if not exports:
        sys.exit('No course_*.json files found.')
    print(f"Read {len(exports)} classes from the export "
          f"(exported by {', '.join(sorted({e.get('exportedBy') or '?' for e in exports}))}).")

    engine = get_db_engine()
    with engine.connect() as conn:
        lookup = section_lookup(conn)
    rosters = semester_rosters(lookup)
    cur_label = term_label(current_term())
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    classes, grades, cats, cw, subs = [], [], [], [], []
    skipped_current, skipped_junk, no_grades, by_term = [], [], [], Counter()
    section_how = Counter()
    unlinked_students, all_students = set(), set()
    cur = current_term()
    for d in sorted(exports, key=lambda e: e['course'].get('creationTime') or ''):
        c = d['course']
        if JUNK_NAME.search(c.get('name') or '') or not d['students']:
            skipped_junk.append(c.get('name') or '(no name)')
            continue
        terms, _src = class_terms(c)
        # Current semester (incl. this year's year-long classes) and later: the pipeline covers them.
        if terms and max(terms) >= cur and not args.include_current:
            skipped_current.append(c['name'])
            continue
        semester, school_year, term_src, year_long = class_term(c)
        rows, cat_rows, work, settings = class_grades(d)
        section, how = class_section(d, lookup, semester, args.default_section, rosters)
        section_how[how] += 1
        names = {s['userId']: s for s in d['students']}
        for sid in rows:
            all_students.add(sid)
            if str(sid) not in lookup:
                unlinked_students.add(sid)
        finals = [r['final_pct'] for r in rows.values() if r['final_pct'] is not None]
        if not finals:
            no_grades.append(c['name'])
        dept = suggest(c['name'])[0]
        classes.append({
            'class_id': c['id'], 'course_name': c['name'], 'classroom_section': c.get('section', ''),
            'school_section': section, 'semester': semester, 'school_year': school_year, 'term_source': term_src,
            'year_long': year_long, 'course_key': course_key(c['name']), 'department': dept,
            'grade_calculation': settings['calc'], 'grade_display': settings['display'],
            'course_state': c.get('courseState'), 'creation_time': c.get('creationTime'),
            'teachers': '; '.join(t['name'] for t in d['teachers'] if t.get('name')),
            'n_students': len(d['students']), 'n_graded_students': len(finals), 'n_coursework': len(d['courseWork']),
            'n_submissions': len(d['submissions']), 'class_avg': _r2(sum(finals) / len(finals)) if finals else None,
            'export_errors': ' | '.join(d.get('errors') or []), 'exported_by': d.get('exportedBy'),
            'exported_at': d.get('exportedAt'), 'imported_at': now})
        for sid, r in rows.items():
            st = names.get(sid, {})
            grades.append({'class_id': c['id'], 'student_id': sid, 'student_name': st.get('name', ''),
                           'student_email': st.get('email', ''), 'final_pct': r['final_pct'], 'calc_pct': r['calc_pct'],
                           'graded_count': r['graded'], 'late_count': r['late']})
        cats += [{'class_id': c['id'], 'student_id': sid, 'category': cat, 'weight': w, 'pct': p} for sid, cat, w, p in cat_rows]
        for w in d['courseWork']:
            info = work[w['id']]
            due = w.get('dueDate') or {}
            cw.append({'class_id': c['id'], 'coursework_id': w['id'], 'title': info['title'], 'category': info['cat'],
                       'weight': info['weight'], 'max_points': info['max'],
                       'due_date': f"{due['year']:04d}-{due['month']:02d}-{due['day']:02d}" if due.get('year') else None,
                       'creation_time': w.get('creationTime'), 'work_type': w.get('workType')})
        subs += [{'class_id': c['id'], 'coursework_id': s['courseWorkId'], 'student_id': s['userId'], 'state': s.get('state'),
                  'assigned_grade': s.get('assignedGrade'), 'draft_grade': s.get('draftGrade'), 'late': int(bool(s.get('late')))}
                 for s in d['submissions']]
        by_term[(school_year, semester, section)] += 1

    print(f"\nClasses by school year / semester / section ({len(classes)} classes):")
    for (yr, sem, sec), n in sorted(by_term.items()):
        print(f"  {yr:<10} {sem:<12} {sec:<12} {n:>4}")
    print(f"\nStudent grades: {len(grades)} ({len(all_students)} students; {len(unlinked_students)} not in this "
          f"database's name lists or snapshots, e.g. former students: kept with their Classroom name).")
    print(f"Assignments: {len(cw)}, submissions: {len(subs)}.")
    guessed = [c for c in classes if c['term_source'] != 'name']
    if guessed:
        print(f"{len(guessed)} classes have no term in their name; their semester is guessed from the creation date "
              f"(term_source = 'created'), e.g. {', '.join(c['course_name'] for c in guessed[:3])}.")
    print(f"HS/JH decided by: students in that semester {section_how['students']}, "
          f"students on neither list (not in the other school) {section_how['not in High School'] + section_how['not in Junior High']}, "
          f"class name {section_how['name']}, --default-section {section_how['default']}, unknown {section_how['unknown']}.")
    unknown = [c for c in classes if c['school_section'] == 'Unknown']
    if unknown:
        print(f"{len(unknown)} classes: HS or JH unknown (use --default-section if the exporting account only "
              f"has one school's classes), e.g. {', '.join(c['course_name'] for c in unknown[:5])}.")
    if skipped_junk:
        print(f"Skipped {len(skipped_junk)} test/example classes or classes without students, "
              f"e.g. {', '.join(skipped_junk[:6])}.")
    if no_grades:
        print(f"{len(no_grades)} classes have no overall grades (nothing graded, or 'no overall grade' in Classroom).")
    errs = [c for c in classes if c['export_errors']]
    if errs:
        print(f"{len(errs)} classes were exported with errors (e.g. no permission for grades), "
              f"e.g. {errs[0]['course_name']}: {errs[0]['export_errors'][:120]}")
    if skipped_current:
        print(f"Skipped {len(skipped_current)} classes of the current semester ({cur_label}); "
              f"use --include-current to import them too.")

    if args.dry_run:
        print("\nDry run: nothing written.")
        return

    from backup_database import make_local_backup
    print("\nBacking up the database first...")
    make_local_backup()
    ids = [c['class_id'] for c in classes]
    with engine.begin() as conn:
        for ddl in DDL:
            conn.execute(text(ddl))
        for table in ('archived_classes', 'archived_grades', 'archived_category_grades',
                      'archived_coursework', 'archived_submissions'):
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                conn.execute(text(f"DELETE FROM {table} WHERE class_id IN ({','.join(':i%d' % j for j in range(len(chunk)))})"),
                             {f'i{j}': v for j, v in enumerate(chunk)})
        for table, rows in (('archived_classes', classes), ('archived_grades', grades),
                            ('archived_category_grades', cats), ('archived_coursework', cw),
                            ('archived_submissions', subs)):
            if rows:
                cols = list(rows[0])
                conn.execute(text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"), rows)
    print(f"✅ Imported {len(classes)} classes, {len(grades)} student grades, {len(subs)} submissions.")


if __name__ == '__main__':
    main()

'''
Teacher Dashboard Sync
Copies the latest grade snapshot (per section) from the local database into
compact JSON files in Google Drive ("Student Grades Retriever/Dashboard").
The Apps Script web app in "dashboard_web_app/" reads those files and shows
them to teachers, the dean and the well-being team, so this computer only needs
to be on while this script runs.

Files written (replaced on every run, same Drive file IDs):
    dashboard.json        one row per student per course (overall grade, late and missing
                          counts) for each section's latest snapshot + homerooms + sync info +
                          courseInfo (course key and suggested department of every course name,
                          see course_departments.py; the Departments tab of the Sheet overrides it)
    history.json          grade trends: every snapshot day of the current semester, plus the
                          final grade of each earlier semester, per student per course
    courses.json          per-course analytics: category averages and per-assignment stats
    students_00..15.json  category grades and assignments, split by student into
                          SHARDS files so the web app only reads a small file per click
Created once and never overwritten:
    "Dashboard Access" Sheet  who may open the dashboard, their role and which courses they
                              see. The Apps Script web app is attached to this Sheet.

Usage:
    python sync_dashboard.py                  # sync to Google Drive
    python sync_dashboard.py --dry-run        # only query the database and print sizes
    python sync_dashboard.py --out-dir DIR    # also save the JSON files locally (for checking)
'''

import os
import re
import json
import argparse
from datetime import datetime

from sqlalchemy import text

from db import get_db_engine
from course_departments import course_info
from category_groups import family

SHARDS = 16  # must match SHARDS in dashboard_web_app/Code.gs
ACCESS_SHEET_NAME = 'Dashboard Access'
ACCESS_SHEET_CSV = (
    'email,courses,notes,role\n'
    '# example.teacher@yourschool.edu,"Science 8, PE 6A",'
    '"Rows starting with # are ignored. courses: * = every course, otherwise comma-separated '
    'words matched against course names (any match = access). role: teacher, dean, wellbeing or admin.",'
    'teacher\n'
)

# Each school's current rows: a temporary view made by use_current_grades(), on the same connection.
LATEST_PER_SECTION = "SELECT * FROM temp.dashboard_current"

# Classroom's "Missing": assigned, past due (Classroom sets late), not turned in, no grade, counts for points.
MISSING_SQL = """(state IN ('NEW', 'CREATED', 'RECLAIMED_BY_STUDENT') AND is_late
                  AND assigned_score IS NULL AND NOT COALESCE(is_excused, 0) AND max_points > 0)"""

# [section, grade_level, semester, student_id, student_name, student_email,
#  course_name, overall_grade, late_count, assignment_count, missing_count, grade_display, graded_count]
# (indexes 0-9 are unchanged from the first dashboard version)
OVERALL_SQL = f"""
    WITH s AS ({LATEST_PER_SECTION})
    SELECT MAX(section), MAX(grade_level), MAX(semester), student_id, MAX(student_name), MAX(student_email),
           course_name, MAX(overall_grade), SUM(CASE WHEN is_late THEN 1 ELSE 0 END), COUNT(*),
           SUM(CASE WHEN {MISSING_SQL} THEN 1 ELSE 0 END), MAX(grade_display),
           SUM(CASE WHEN assigned_score IS NOT NULL AND max_points > 0 AND NOT COALESCE(is_excused, 0)
                    THEN 1 ELSE 0 END)
    FROM s
    GROUP BY student_id, course_name
    ORDER BY MAX(section), course_name, MAX(student_name)
"""

# student_id, [course_name, category, category_weight, category_grade]
CATEGORIES_SQL = f"""
    WITH s AS ({LATEST_PER_SECTION})
    SELECT student_id, course_name, COALESCE(category, '(No category)'),
           MAX(category_weight), MAX(student_grade_in_category)
    FROM s
    GROUP BY student_id, course_name, category
    ORDER BY student_id, course_name, MAX(category_weight) DESC NULLS LAST, 3
"""

# student_id, [course_name, category, assignment_title, assigned_score, max_points, is_late, is_excused, state]
ASSIGNMENTS_SQL = f"""
    WITH s AS ({LATEST_PER_SECTION})
    SELECT student_id, course_name, COALESCE(category, '(No category)'), assignment_title,
           assigned_score, max_points, is_late, is_excused, state
    FROM s
    ORDER BY student_id, course_name, 3, assignment_title
"""

# Per course and assignment in the latest snapshots: how the class did.
ASSIGNMENT_STATS_SQL = f"""
    WITH s AS ({LATEST_PER_SECTION})
    SELECT course_name, COALESCE(category, '(No category)'), assignment_title, MAX(max_points),
           AVG(CASE WHEN assigned_score IS NOT NULL AND max_points > 0 AND NOT COALESCE(is_excused, 0)
                    THEN 100.0 * assigned_score / max_points END),
           SUM(CASE WHEN assigned_score IS NOT NULL THEN 1 ELSE 0 END),
           COUNT(*),
           SUM(CASE WHEN {MISSING_SQL} THEN 1 ELSE 0 END),
           SUM(CASE WHEN is_late THEN 1 ELSE 0 END)
    FROM s
    GROUP BY course_name, assignment_id
"""

# Work with a teacher's draft grade that hasn't been returned yet (roadmap item 12). Only counted;
# draft grades are never part of any grade. The column exists after the first ingest that stores it.
DRAFTS_SQL = f"""
    WITH s AS ({LATEST_PER_SECTION})
    SELECT student_id, course_name, assignment_title
    FROM s
    WHERE draft_score IS NOT NULL AND assigned_score IS NULL
"""

# Every student's overall grade in every course at every snapshot (history for trends).
GRADE_HISTORY_SQL = """
    SELECT section, semester, course_name, snapshot_date, student_id, MAX(overall_grade)
    FROM student_grade_snapshots
    WHERE overall_grade IS NOT NULL
    GROUP BY section, semester, course_name, snapshot_date, student_id
"""

HOMEROOM_RE = re.compile(r'home\s?room', re.I)
TERM_PREFIX_RE = re.compile(r"^\s*(\d{2}(\d{2})?\s*[FS]|\d{2}-\d{2}\s+(Fall|Spring)|(Fall|Spring)\s+'?\d{2})\s+", re.I)


def use_current_grades(conn):
    """
    Creates temp.dashboard_current: each school's rows from live_grades (refresh_dashboard.py) if those
    are newer than its latest snapshot, otherwise its latest snapshot. Nothing is written to the database.
    Returns ({section: latest date}, {section: latest snapshot date}, [schools using live_grades]).
    """
    saved = dict(conn.execute(text("SELECT section, MAX(snapshot_date) FROM student_grade_snapshots GROUP BY section")).fetchall())
    columns = [r[1] for r in conn.execute(text("PRAGMA table_info(student_grade_snapshots)"))]
    live_columns = [r[1] for r in conn.execute(text("PRAGMA table_info(live_grades)"))]
    live = dict(conn.execute(text("SELECT section, MAX(snapshot_date) FROM live_grades GROUP BY section")).fetchall()) if live_columns else {}
    use_live = sorted(s for s, d in live.items() if d and (s not in saved or str(d) > str(saved[s])))

    quoted = ', '.join("'" + s.replace("'", "''") + "'" for s in use_live) or "''"
    cols = ', '.join(f'sgs."{c}"' for c in columns)
    live_cols = ', '.join(f'"{c}"' if c in live_columns else f'NULL AS "{c}"' for c in columns)
    union = f" UNION ALL SELECT {live_cols} FROM live_grades WHERE section IN ({quoted})" if use_live else ''
    conn.execute(text("DROP VIEW IF EXISTS temp.dashboard_current"))
    conn.execute(text(f"""
        CREATE TEMP VIEW dashboard_current AS
        WITH latest AS (SELECT section, MAX(snapshot_date) AS snap FROM student_grade_snapshots GROUP BY section)
        SELECT {cols} FROM student_grade_snapshots sgs
        JOIN latest l ON sgs.section IS NOT DISTINCT FROM l.section AND sgs.snapshot_date = l.snap
        WHERE COALESCE(sgs.section, '') NOT IN ({quoted}){union}"""))
    latest = dict(saved)
    latest.update({s: live[s] for s in use_live})
    return latest, saved, use_live


def shard_of(student_id):
    """Same rule as shardOf_() in Code.gs: sum of character codes, mod SHARDS."""
    return sum(ord(ch) for ch in str(student_id)) % SHARDS


def num(v):
    if v is None:
        return None
    v = float(v)
    return None if v != v else round(v, 2)  # NaN -> null


def short_date(v):
    return str(v)[:16] if v else ''  # 'YYYY-MM-DD HH:MM'


def semester_key(semester):
    """'2025 Fall' < '2026 Spring' < '2026 Fall'; unknown formats sort first."""
    m = re.match(r'(\d{4})\s+(Spring|Fall)', semester or '', re.I)
    return (int(m.group(1)), 0 if m.group(2).lower() == 'spring' else 1) if m else (0, 0)


def build_history(rows, current):
    """
    rows:    (section, semester, course, snapshot_date, student_id, grade) for every snapshot
    current: {section: (semester, latest snapshot_date)}
    Returns {'courses', 'dates', 'trend', 'past', 'semesters'}:
      trend: [student_id, course_idx, date_idx, grade] - each snapshot DAY of the section's current
             semester (per course the day's last snapshot, so a later filtered run that day doesn't
             hide the courses it skipped)
      past:  [student_id, semester, course_idx, grade] - final (latest) snapshot of each earlier semester
      timeline: [section, semester, course_idx, day, average, students, below_60] - class averages on
             every snapshot day of every semester (for school / grade-level / course trend charts)
    """
    # Pick which snapshot represents each (section, semester, course, day) / (section, semester, course).
    day_pick, final_pick = {}, {}
    for section, semester, course, snap, _sid, _g in rows:
        snap = str(snap)
        day_key = (section, semester, course, snap[:10])
        if snap > day_pick.get(day_key, ''):
            day_pick[day_key] = snap
        sem_key = (section, semester, course)
        if snap > final_pick.get(sem_key, ''):
            final_pick[sem_key] = snap

    courses, course_idx = [], {}

    def ci(name):
        if name not in course_idx:
            course_idx[name] = len(courses)
            courses.append(name)
        return course_idx[name]

    trend_raw, past, tl = [], [], {}
    for section, semester, course, snap, sid, grade in rows:
        snap = str(snap)
        if day_pick[(section, semester, course, snap[:10])] == snap and grade is not None:
            acc = tl.setdefault((section or '', semester or '', ci(course or ''), snap[:10]), [0.0, 0, 0])
            acc[0] += float(grade)
            acc[1] += 1
            acc[2] += float(grade) < 60
        cur_sem = current.get(section, (None, None))[0]
        if semester == cur_sem:
            if day_pick[(section, semester, course, snap[:10])] == snap:
                trend_raw.append((sid, ci(course or ''), snap[:10], num(grade)))
        elif semester_key(semester) < semester_key(cur_sem) and final_pick[(section, semester, course)] == snap:
            past.append([sid, semester or '', ci(course or ''), num(grade)])

    dates = sorted({d for _, _, d, _ in trend_raw})
    date_idx = {d: i for i, d in enumerate(dates)}
    trend = [[sid, c, date_idx[d], g] for sid, c, d, g in trend_raw]
    semesters = sorted({p[1] for p in past}, key=semester_key)
    timeline = [[sec, sem, c, day, num(total / n), n, below] for (sec, sem, c, day), (total, n, below) in sorted(
        tl.items(), key=lambda kv: kv[0][3])]
    return {'courses': courses, 'dates': dates, 'trend': trend, 'past': past, 'semesters': semesters,
            'timeline': timeline}


SHEET_DEPTS = {'English Language Arts / English as an Additional Language': 'English / EAL'}


def build_past(current):
    """
    Past grades for the dashboard's "Past grades" tab and the department history (roadmap item 7).
    Sources: final snapshot of every earlier semester (all students who were in it, also those who have
    left), the imported Classroom archive (archived_*; wins for the same class and semester), transcript
    letter grades, the old monthly departmental Sheet (department averages only) and archived class
    averages. Returns (index, shards):
      index:  {'students': [[sid, name, section, first semester, last semester, number of grades]],
               'classes':  [[course name, semester, section, class average, graded students, source]],
               'sheet':    [[date, label, department, Sheet's average, average of its class rows]]}
      shards: [{sid: {'n': name, 'g': [[semester, course, final %, calc %, class avg, source]],
                      't': [[semester, subject, letter, credit]]}}] by shard_of(sid)
    """
    from compare_archived_grades import connect, has_table, transcript_rows
    c = connect()
    students, grades = {}, {}           # sid -> (name, section, semester); (sid, sem, course) -> row
    class_avgs = {}

    def note(sid, name, section, sem):
        old = students.get(sid)
        if not old or semester_key(sem) >= semester_key(old[2]):
            students[sid] = (name or (old[0] if old else ''), section or (old[1] if old else ''), sem)

    # Each class's last snapshot of the semester (as build_history does: a later filtered run may skip a class).
    for sec, sem, course, snap in c.execute('SELECT section, semester, course_name, MAX(snapshot_date) '
                                            'FROM student_grade_snapshots GROUP BY 1, 2, 3').fetchall():
        if semester_key(sem) >= semester_key(current.get(sec, ('', ''))[0]):
            continue
        rows = c.execute('SELECT DISTINCT student_id, student_name, course_name, overall_grade FROM student_grade_snapshots '
                         'WHERE section=? AND snapshot_date=? AND course_name=?', (sec, snap, course)).fetchall()
        per_course = {}
        for sid, name, course, g in rows:
            if g is not None:
                per_course.setdefault(course, []).append(g)
        for course, gs in per_course.items():
            class_avgs[(sem, course)] = (sum(gs) / len(gs), len(gs), sec, 'snapshot')
        for sid, name, course, g in rows:
            note(sid, name, sec, sem)
            avg = class_avgs.get((sem, course), (None,))[0]
            grades[(sid, sem, course)] = [sem, course, num(g), num(g), num(avg), 'snapshot ' + str(snap)[:10]]

    archived = has_table(c, 'archived_grades') and has_table(c, 'archived_classes')
    if archived:
        for cid, course, sem, sec, avg, n in c.execute(
                'SELECT class_id, course_name, semester, school_section, class_avg, n_graded_students FROM archived_classes'):
            class_avgs[(sem, course)] = (avg, n, sec, 'archive')
        for sid, name, course, sem, sec, final, calc, avg in c.execute('''
                SELECT g.student_id, g.student_name, a.course_name, a.semester, a.school_section, g.final_pct, g.calc_pct, a.class_avg
                FROM archived_grades g JOIN archived_classes a USING (class_id)'''):
            note(sid, name, sec if sec != 'Unknown' else '', sem)
            grades[(sid, sem, course)] = [sem, course, num(final), num(calc), num(avg), 'Classroom archive']

    shards = [{} for _ in range(SHARDS)]

    def entry(sid):
        return shards[shard_of(sid)].setdefault(sid, {'n': students.get(sid, ('',))[0], 'g': [], 't': []})

    counts = {}
    for (sid, sem, course), row in sorted(grades.items(), key=lambda kv: (semester_key(kv[0][1]), kv[0][2])):
        entry(sid)['g'].append(row)
        counts[sid] = counts.get(sid, 0) + 1
    transcripts = transcript_rows(c)
    for sid, name, sem, subj, letter, credit in transcripts:
        if sid:
            if sid not in students:
                students[sid] = (name, '', sem)
            entry(sid)['t'].append([sem, subj, letter, credit])

    first = {}
    for (sid, sem, _course) in grades:
        if sid not in first or semester_key(sem) < semester_key(first[sid]):
            first[sid] = sem
    sheet = []
    if has_table(c, 'dept_monthly_class_averages'):
        sheet = [[d, l, SHEET_DEPTS.get(dept, dept), num(s), num(o)] for d, l, dept, s, o in c.execute(
            'SELECT DISTINCT checkpoint, checkpoint_label, department, sheet_dept_avg, our_dept_avg '
            'FROM dept_monthly_class_averages ORDER BY checkpoint, department')]
    index = {
        'students': sorted([[sid, v[0], v[1], first.get(sid, v[2]), v[2], counts.get(sid, 0)] for sid, v in students.items()],
                           key=lambda r: (r[1] or '').lower()),
        'classes': [[course, sem, v[2], num(v[0]), v[1], v[3]] for (sem, course), v in sorted(class_avgs.items())],
        'sheet': sheet,
    }
    print(f"  past grades tab  {len(index['students']):>6} students, {len(grades)} past grades "
          f"({'with' if archived else 'no'} Classroom archive), {sum(1 for t in transcripts if t[0])} transcript grades, "
          f"{len(index['classes'])} past class averages, {len(sheet)} Sheet department values")
    return index, shards


def add_past_to_history(history, past_index, shards, overall):
    """
    Replaces history['past'] (final snapshots of earlier semesters) with every earlier-semester final
    grade of the CURRENT students from build_past(): snapshots plus the Classroom archive, which wins
    for the same class and semester. So the overview's "Average grade over time" (same students) and
    the student page reach back as far as the archive does. Former students stay in the Past grades tab.
    Also adds history['semesterClasses']: [section, semester, course_idx, average, graded students] for
    every class of every earlier semester, ALL students incl. former ones (the overview's whole-school
    chart; the same final grades as the Past grades tab).
    """
    now = {}                             # sid -> semester of the student's current rows
    for r in overall:
        now.setdefault(r[3], r[2])
    idx = {name: i for i, name in enumerate(history['courses'])}
    past = []
    for sid, sem_now in now.items():
        for sem, course, final, _calc, _avg, _src in shards[shard_of(sid)].get(sid, {}).get('g', []):
            if semester_key(sem) >= semester_key(sem_now):
                continue
            if course not in idx:
                idx[course] = len(history['courses'])
                history['courses'].append(course)
            past.append([sid, sem, idx[course], final])
    history['past'] = past

    section_of = {(c[1], c[0]): c[2] for c in past_index['classes']}
    acc = {}
    for shard in shards:
        for st in shard.values():
            for sem, course, final, _calc, _avg, _src in st['g']:
                if final is not None:
                    a = acc.setdefault((sem, course), [0.0, 0])
                    a[0] += final
                    a[1] += 1
    rows = []
    for (sem, course), (total, n) in sorted(acc.items(), key=lambda kv: (semester_key(kv[0][0]), kv[0][1])):
        if course not in idx:
            idx[course] = len(history['courses'])
            history['courses'].append(course)
        rows.append([section_of.get((sem, course)) or '', sem, idx[course], num(total / n), n])
    history['semesterClasses'] = rows
    history['semesters'] = sorted({p[1] for p in past}, key=semester_key)


def homeroom_label(course):
    return TERM_PREFIX_RE.sub('', course).strip()


def build_payloads(engine):
    with engine.connect() as conn:
        latest, saved, use_live = use_current_grades(conn)
        overall = [[r[0] or '', r[1] or '', r[2] or '', r[3], r[4] or '', r[5] or '',
                    r[6] or '', num(r[7]), int(r[8] or 0), int(r[9] or 0), int(r[10] or 0), r[11] or '', int(r[12] or 0)]
                   for r in conn.execute(text(OVERALL_SQL))]
        categories = conn.execute(text(CATEGORIES_SQL)).fetchall()
        assignments = conn.execute(text(ASSIGNMENTS_SQL)).fetchall()
        assignment_stats = conn.execute(text(ASSIGNMENT_STATS_SQL)).fetchall()
        history_rows = conn.execute(text(GRADE_HISTORY_SQL)).fetchall()
        if use_live:   # today's grades are the newest point on the trend charts (never saved as a snapshot)
            live_list = ', '.join("'" + s.replace("'", "''") + "'" for s in use_live)
            history_rows += conn.execute(text(
                GRADE_HISTORY_SQL.replace('student_grade_snapshots', 'temp.dashboard_current')
                .replace('WHERE overall_grade IS NOT NULL', f'WHERE overall_grade IS NOT NULL AND section IN ({live_list})'))).fetchall()
        has_drafts = any(r[1] == 'draft_score' for r in conn.execute(text("PRAGMA table_info(student_grade_snapshots)")))
        draft_rows = conn.execute(text(DRAFTS_SQL)).fetchall() if has_drafts else []
        snapshots = sorted(latest.items(), key=lambda kv: kv[0] or '')

    # Semester of each section's latest snapshot (all rows of one snapshot share it).
    current = {}
    for r in overall:
        current.setdefault(r[0], r[2])
    current = {section: (current.get(section or '', ''), snap) for section, snap in snapshots}

    shards = [{} for _ in range(SHARDS)]

    def entry(sid):
        return shards[shard_of(sid)].setdefault(sid, {'c': [], 'a': []})

    course_stats = {}

    def course_entry(course):
        return course_stats.setdefault(course or '', {'cats': [], 'asg': []})

    cat_acc = {}  # (course, category) -> [weight, sum, n]
    for sid, course, cat, weight, grade in categories:
        entry(sid)['c'].append([course or '', cat, num(weight), num(grade)])
        acc = cat_acc.setdefault((course or '', cat), [num(weight), 0.0, 0])
        if grade is not None:
            acc[1] += float(grade)
            acc[2] += 1
    for (course, cat), (weight, total, n) in sorted(cat_acc.items(), key=lambda kv: (kv[0][0], -(kv[1][0] or 0), kv[0][1])):
        course_entry(course)['cats'].append([cat, weight, num(total / n) if n else None, n])
    # Category families per course for the department "Categories" panel (category_groups.py):
    # {course: [[family, total weight, average, students]]}; several categories of one family are
    # combined, weighted by their category weights (plain mean if none has a weight).
    fam_acc = {}
    for (course, cat), (weight, total, n) in cat_acc.items():
        if not n:
            continue
        f = fam_acc.setdefault(course, {}).setdefault(family(cat), [0.0, 0.0, 0.0, 0, 0])
        w = weight or 0
        f[0] += w
        f[1] += (total / n) * w
        f[2] += total / n
        f[3] += 1
        f[4] = max(f[4], n)
    cat_summary = {course: [[fam, num(v[0]), num(v[1] / v[0] if v[0] else v[2] / v[3]), v[4]]
                            for fam, v in sorted(fams.items())] for course, fams in fam_acc.items()}
    for sid, course, cat, title, score, max_pts, late, excused, state in assignments:
        entry(sid)['a'].append([course or '', cat, title or '', num(score), num(max_pts),
                                bool(late), bool(excused), state or ''])
    # [title, category, max_points, average %, graded, assigned, missing, late]
    # Draft grades (graded, not returned yet): per student per course, and per assignment.
    drafts, asg_drafts = {}, {}
    for sid, course, title in draft_rows:
        drafts[(sid, course)] = drafts.get((sid, course), 0) + 1
        asg_drafts[(course, title)] = asg_drafts.get((course, title), 0) + 1
    # [title, category, max_points, average %, graded, assigned, missing, late, drafts]
    for course, cat, title, max_pts, avg, graded, assigned, missing, late in assignment_stats:
        course_entry(course)['asg'].append([title or '', cat, num(max_pts), num(avg),
                                            int(graded or 0), int(assigned or 0), int(missing or 0), int(late or 0),
                                            asg_drafts.get((course, title), 0)])

    homerooms = {}
    for r in overall:
        if HOMEROOM_RE.search(r[6]):
            homerooms[r[3]] = homeroom_label(r[6])

    history = build_history(history_rows, current)

    meta = [['last_sync', datetime.now().strftime('%Y-%m-%d %H:%M')]] + \
           [[f'snapshot: {section or "Unknown"}', short_date(snap)] for section, snap in snapshots]
    # snapshot = date of the grades shown; savedSnapshot = the school's last saved grade pull; live = from live_grades.
    sections = {section or 'Unknown': {'semester': sem, 'snapshot': short_date(snap),
                                       'savedSnapshot': short_date(saved.get(section)), 'live': section in use_live}
                for section, (sem, snap) in current.items()}
    if use_live:
        print(f"  current grades   from live_grades for: {', '.join(use_live)}")

    print(f"  overall rows     {len(overall):>6}")
    print(f"  category rows    {len(categories):>6}")
    print(f"  assignment rows  {len(assignments):>6}")
    print(f"  trend points     {len(history['trend']):>6}  (days: {', '.join(history['dates'])})")
    print(f"  timeline points  {len(history['timeline']):>6}  (class averages on "
          f"{len({t[3] for t in history['timeline']})} snapshot days)")
    print(f"  homerooms        {len(homerooms):>6}  students in a Homeroom course")
    print(f"  draft grades     {sum(drafts.values()):>6}  graded but not returned "
          + (f"({len(drafts)} student-courses)" if has_drafts else "(not stored yet: appears after the next ingest)"))
    # {course name: [course key, suggested department, Ontario code, leave out of averages]} for every
    # course in the data; Code.gs applies the dean's "Departments" tab on top (see course_departments.py).
    past_index, past_shards = build_past(current)
    info = course_info(sorted({r[6] for r in overall} | set(history['courses'])))
    # Archived-only course names go in a separate "pastCourseInfo" (Code.gs merges it for departments),
    # so an older Code.gs never adds hundreds of old names to the dean's Departments tab.
    past_info = {n: v for n, v in course_info(sorted({c[0] for c in past_index['classes']})).items() if n not in info}
    # After courseInfo, so archived names stay in pastCourseInfo only.
    add_past_to_history(history, past_index, past_shards, overall)
    print(f"  past grades      {len(history['past']):>6}  of current students, with the archive "
          f"(semesters: {history['semesters'][0] if history['semesters'] else '-'} - "
          f"{history['semesters'][-1] if history['semesters'] else '-'})")
    sc_sems = sorted({r[1] for r in history['semesterClasses']}, key=semester_key)
    print(f"  whole-school     {len(history['semesterClasses']):>6}  past class averages, all students "
          f"(semesters: {sc_sems[0] if sc_sems else '-'} - {sc_sems[-1] if sc_sems else '-'})")
    current_names = {r[6] for r in overall}
    # 5th value: 1 = a current course. The Departments tab only gets rows for current courses (Code.gs);
    # past / archived names just use the suggestion unless the dean adds a row for their key.
    for name, v in info.items():
        v.append(1 if name in current_names else 0)
    per_dept = {}
    for name, v in info.items():
        if name in current_names:
            per_dept[v[1]] = per_dept.get(v[1], 0) + 1
    print(f"  course info      {len(info):>6}  course names ({len({v[0].lower() for v in info.values()})} keys); "
          f"current courses by suggested department: " + ', '.join(f'{d} {n}' for d, n in sorted(per_dept.items())))
    for k, v in meta:
        print(f"  {k}: {v}")

    files = {
        'dashboard.json': {'meta': meta, 'sections': sections, 'overall': overall, 'homerooms': homerooms,
                           'courseInfo': info, 'pastCourseInfo': past_info, 'catSummary': cat_summary,
                           # [[student_id, course, number of draft grades]]; hasDrafts = the column exists yet
                           'drafts': [[sid, course, n] for (sid, course), n in sorted(drafts.items())],
                           'hasDrafts': has_drafts},
        'history.json': history,
        'courses.json': course_stats,
    }
    for i, shard in enumerate(shards):
        files[f'students_{i:02d}.json'] = shard
    files['past_index.json'] = past_index
    for i, shard in enumerate(past_shards):
        files[f'past_{i:02d}.json'] = shard
    return {name: json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            for name, payload in files.items()}


def upload_to_drive(payloads, progress=None):
    import hashlib
    from google_drive import get_drive_service, ensure_folders, save_config, upload, file_exists

    service = get_drive_service()
    config = ensure_folders(service)
    folder_id = config['dashboard_folder_id']
    file_ids = config.setdefault('dashboard_files', {})
    hashes = config.setdefault('dashboard_hashes', {})

    # Detail files first, dashboard.json last: teachers see the new overview only
    # once the files behind it are already in place.
    # Files that haven't changed (most past-grade files, on most days) are skipped, which saves a minute.
    uploaded = 0
    names = sorted(payloads, key=lambda n: n == 'dashboard.json')
    for i, name in enumerate(names, 1):
        if progress:
            progress(f'Uploading file {i} of {len(names)}', 82 + 17 * (i - 1) / len(names))
        digest = hashlib.md5(payloads[name]).hexdigest()
        if hashes.get(name) == digest and file_ids.get(name):
            continue
        file_ids[name] = upload(service, folder_id, name, data=payloads[name],
                                mimetype='application/json', file_id=file_ids.get(name))
        hashes[name] = digest
        uploaded += 1
        save_config(config)
    print(f"  ✅ Uploaded {uploaded} changed files of {len(payloads)} to Drive: Student Grades Retriever/Dashboard")

    if not file_exists(service, config.get('access_sheet_id')):
        config['access_sheet_id'] = upload(
            service, folder_id, ACCESS_SHEET_NAME, data=ACCESS_SHEET_CSV.encode('utf-8'),
            mimetype='text/csv', convert_to='application/vnd.google-apps.spreadsheet')
        save_config(config)
        print(f"  Created the '{ACCESS_SHEET_NAME}' Sheet.")
    return config


def sync(dry_run=False, out_dir=None, progress=None):
    print("--- Reading latest snapshots from the database ---")
    payloads = build_payloads(get_db_engine())
    sizes = sorted(len(b) for n, b in payloads.items() if n.startswith('students_'))
    print(f"  dashboard.json {len(payloads['dashboard.json']) / 1024:.0f} KB; "
          f"history.json {len(payloads['history.json']) / 1024:.0f} KB; "
          f"courses.json {len(payloads['courses.json']) / 1024:.0f} KB; "
          f"student files {sizes[0] / 1024:.0f}-{sizes[-1] / 1024:.0f} KB each")
    past = sorted(len(b) for n, b in payloads.items() if re.match(r'past_\d', n))
    print(f"  past_index.json {len(payloads['past_index.json']) / 1024:.0f} KB; "
          f"past grade files {past[0] / 1024:.0f}-{past[-1] / 1024:.0f} KB each")

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        for name, data in payloads.items():
            with open(os.path.join(out_dir, name), 'wb') as f:
                f.write(data)
        print(f"  Saved JSON files to {out_dir}")

    if dry_run:
        print("\nDry run: nothing uploaded.")
        return

    print("\n--- Uploading to Google Drive ---")
    if progress:
        progress('Sending the new grades to the dashboard', 82)
    config = upload_to_drive(payloads, progress)
    print(f"\nDone. Access Sheet (open it, then Extensions > Apps Script): "
          f"https://docs.google.com/spreadsheets/d/{config['access_sheet_id']}")


def main():
    parser = argparse.ArgumentParser(description="Sync the latest grades to the teacher dashboard files in Google Drive.")
    parser.add_argument('--dry-run', action='store_true', help="Query the database only; don't touch Google.")
    parser.add_argument('--out-dir', help="Also write the JSON files to this folder.")
    args = parser.parse_args()
    sync(dry_run=args.dry_run, out_dir=args.out_dir)


if __name__ == '__main__':
    main()

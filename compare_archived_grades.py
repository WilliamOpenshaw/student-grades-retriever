r'''
Checks past grades from different sources against Classroom data (roadmap item 6). Read-only.
Classroom is the trusted source: differences are listed for a person to look at, never "fixed".

    .venv\Scripts\python.exe compare_archived_grades.py

Three separate checks, each written to logs\archive_compare_<date>_<check>.csv (worst first):
  1. sheet       The retired monthly departmental Sheet (dept_monthly_class_averages, from
                 import_department_averages.py) vs class averages from our snapshots on the same day
                 (S1 finals ~ 2026-01-19, 6 May, 4 Jun ~ 3/5 Jun), and vs archived final class averages
                 (archived_classes) when an archive export has been imported. Classes are matched by
                 Ontario code + grade + section letter. Match = within 1 point (the Sheet is rounded).
  2. trialpull   The "Grade Category Trial Pull" (class level only) vs our final 2025-26 snapshots:
                 grading method, category weights and category averages per class; and vs the archive
                 by Class ID when one is imported.
  3. transcripts student_transcripts (letter grades) vs the student's final grade in that class in
                 our snapshots / the archive, compared as letters. Students are found through the
                 name lists (passport name, first + last name, Classroom name).
No student names in checks 1-2; check 3 lists students and stays on this computer.
'''

import csv
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

from course_departments import course_key, suggest
from db import BASE_DIR

TRIAL_PULL = os.path.join(BASE_DIR, 'department_analysis_past_grades_csvs',
                          'Grade Category Trial Pull - Category Summary.csv')
LOG_DIR = os.path.join(BASE_DIR, 'logs')
JH_CODES = re.compile(r'^(language|enl1w|eslco|esldo|math[5-8]|scitech|socstudy|geo|arts\d|108stage)', re.I)


def connect():
    from db import load_env
    path = os.environ.get('DB_PATH') or load_env().get('DB_PATH') or 'grades.db'
    if not os.path.isabs(path):
        path = os.path.join(BASE_DIR, path)
    return sqlite3.connect(f'file:{path}?mode=ro', uri=True)


def has_table(c, name):
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def letter(p):
    if p is None:
        return ''
    return 'A' if p >= 85 else 'AB' if p >= 80 else 'B' if p >= 75 else 'BC' if p >= 70 else 'C' if p >= 60 else 'D' if p >= 50 else 'F'


def norm_code(code):
    return re.sub(r'[^a-z0-9]', '', (code or '').lower())


def grade_of(text):
    """Middle-grades level 6/7/8 from a digit, or from Intro/Inter/Adv (Introduction/Intermediate/Advanced)."""
    m = re.search(r'(?<!\d)([5-8])(?![0-9])', text)
    if m:
        return m.group(1)
    t = text.lower()
    if re.search(r'\badv(anced)?\b', t):
        return '8'
    if re.search(r'\binter(mediate)?\b', t):
        return '7'
    if re.search(r'\bintro(duction)?\b', t):
        return '6'
    return None


def class_letter(name):
    """Section letter of a Classroom class name: '…_A', '… B', '7A', '…_1' (1 = A)."""
    n = re.sub(r"\s*\((spring|fall)[^)]*\)\s*$", '', name.strip(), flags=re.I)
    n = re.sub(r'[\s_]\d{2}-\d{2}_(Fall|Spring)(_[A-Za-z]+)?$', '', n)   # 'Science_6A_25-26_Fall' -> 'Science_6A'
    m = re.search(r'(?:^|[\s_])([A-D])$', n) or re.search(r'(?<=\d)([A-D])\b', n, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r'_(\d)$', n)
    return chr(64 + int(m.group(1))) if m and m.group(1) in '1234' else ''


def write_csv(name, header, rows):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"archive_compare_{datetime.now():%Y-%m-%d}_{name}.csv")
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


# ---------------------------------------------------------------------------------------------
# Our class averages
# ---------------------------------------------------------------------------------------------

def snapshot_classes(c):
    """{(section, snapshot_date): {course_name: (avg, n)}} for every snapshot (distinct student grades)."""
    out = defaultdict(dict)
    rows = c.execute('''SELECT section, snapshot_date, course_name, AVG(g), COUNT(g) FROM (
                          SELECT DISTINCT section, snapshot_date, course_name, student_id, overall_grade AS g
                          FROM student_grade_snapshots) GROUP BY 1, 2, 3''')
    for sec, snap, course, avg, n in rows:
        if n:
            out[(sec, snap)][course] = (avg, n)
    return out


def class_match_key(name):
    dept, code, _ = suggest(name)
    return norm_code(code), grade_of(course_key(name)), 'csl' in name.lower()


# ---------------------------------------------------------------------------------------------
# 1. Sheet vs Classroom
# ---------------------------------------------------------------------------------------------

def check_sheet(c, snaps):
    if not has_table(c, 'dept_monthly_class_averages'):
        print('1. Sheet: dept_monthly_class_averages not found - run import_department_averages.py first.')
        return
    sheet = c.execute('''SELECT checkpoint, checkpoint_label, semester, department, course_name, ontario_code,
                                section_label, class_avg, tab, source_row FROM dept_monthly_class_averages
                         ORDER BY checkpoint, tab, source_row''').fetchall()
    archive = {}
    if has_table(c, 'archived_classes'):
        for name, sec, sem, avg in c.execute('SELECT course_name, school_section, semester, class_avg FROM archived_classes'):
            archive.setdefault((sem, sec), {})[name] = (avg, None)
    snap_dates = defaultdict(list)
    for (sec, snap) in snaps:
        snap_dates[sec].append(snap)

    def nearest(sec, day, semester):
        best = None
        d0 = datetime.strptime(day, '%Y-%m-%d')
        for s in snap_dates.get(sec, []):
            d = datetime.strptime(s[:10], '%Y-%m-%d')
            if abs(d - d0) <= timedelta(days=3) and (best is None or abs(d - d0) < abs(datetime.strptime(best[:10], '%Y-%m-%d') - d0)):
                best = s
        return best

    out, outcomes = [], Counter()
    jh_block = {}
    for (date, label, sem, dept, course, code, sec_label, value, tab, row) in sheet:
        key = (date, dept)
        if 'middle grades' in course.lower():
            jh_block[key] = True
        is_jh = bool(JH_CODES.match(norm_code(code))) or '(JH)' in sec_label or 'middle grades' in course.lower() or \
            (norm_code(code) == 'esleo' and jh_block.get(key))
        section = 'Junior High' if is_jh else 'High School'
        letters = set(re.findall(r'\b([A-D])\b', sec_label.replace('(JH)', '')))
        want = (norm_code(code), grade_of(course) if 'middle grades' in course.lower() or norm_code(code).startswith('108') else None,
                'csl' in course.lower())

        def candidates(classes):
            cands = []
            for name, (avg, n) in classes.items():
                k = class_match_key(name)
                if not k[0] or k[0] != want[0] or k[2] != want[2]:
                    continue
                if want[1] and k[1] and want[1] != k[1]:
                    continue
                cands.append((name, avg, n, class_letter(name)))
            lettered = [x for x in cands if x[3] and x[3] in letters]
            if lettered:
                return lettered
            same_name = [x for x in cands if course_key(x[0]).lower() == course_key(course).lower()]
            if len(same_name) == 1:     # e.g. two AVI3O classes: Intro. to Visual Art / Digital Art & Design
                return same_name
            return cands if len(cands) == 1 or (len(cands) > 1 and not any(x[3] for x in cands) and len(letters) > 1) else \
                (cands if not cands else [('AMBIGUOUS: ' + ' / '.join(x[0] for x in cands), None, None, '')])

        sources = []
        snap = nearest(section, date, sem)
        if snap:
            sources.append(('snapshot ' + snap[:10], snaps[(section, snap)]))
        if archive.get((sem, section)) and label in ('S1 final', '4 Jun 2026'):
            sources.append(('archive final', archive[(sem, section)]))
        if not sources:
            outcomes['no Classroom data on that date'] += 1
            continue
        for src, classes in sources:
            cands = candidates(classes)
            if not cands:
                result, ours, names = 'no matching class found', None, ''
            elif cands[0][0].startswith('AMBIGUOUS'):
                result, ours, names = 'ambiguous (several classes, no section letter)', None, cands[0][0][11:]
            else:
                vals = [x[1] for x in cands if x[1] is not None]
                ours = sum(vals) / len(vals) if vals else None
                names = ' + '.join(x[0] for x in cands)
                if value is None:
                    result = 'blank in the Sheet'
                elif ours is None:
                    result = 'no grades in Classroom'
                else:
                    result = 'match' if abs(ours - value) <= 1 else 'MISMATCH'
            outcomes[result] += 1
            diff = None if value is None or ours is None else round(ours - value, 1)
            out.append([label, date, src, section, dept, course, code, sec_label, value,
                        None if ours is None else round(ours, 1), diff, result, names])
    # Two sections of one course that each mismatch but match each other's value: the Sheet swapped A and B.
    groups = defaultdict(list)
    for r in out:
        if r[11] == 'MISMATCH':
            groups[(r[1], r[2], r[5], r[6])].append(r)
    for rs in groups.values():
        if len(rs) == 2 and all(abs(rs[i][9] - rs[1 - i][8]) <= 5 and
                                abs(rs[i][9] - rs[1 - i][8]) * 2 <= abs(rs[i][10]) for i in (0, 1)):
            for r in rs:
                outcomes['MISMATCH'] -= 1
                outcomes['sections swapped in the Sheet'] += 1
                r[11] = 'sections swapped in the Sheet'
    out.sort(key=lambda r: (r[11] != 'MISMATCH', -abs(r[10] or 0)))
    path = write_csv('sheet', ['Sheet date', 'date', 'Classroom source', 'section', 'department', 'Sheet course', 'code',
                               'Sheet section', 'Sheet value', 'Classroom value', 'Classroom - Sheet', 'result', 'Classroom class(es)'], out)
    print('1. Sheet vs Classroom: ' + ', '.join(f'{k} {v}' for k, v in outcomes.most_common()))
    big = [r for r in out if r[11] == 'MISMATCH']
    if big:
        diffs = sorted(abs(r[10]) for r in big)
        print(f'   {len(big)} mismatches; median difference {diffs[len(diffs) // 2]:.1f} points, largest {diffs[-1]:.1f}. '
              f'Classroom higher in {sum(1 for r in big if r[10] > 0)}, lower in {sum(1 for r in big if r[10] < 0)}.')
    print(f'   -> {path}')


# ---------------------------------------------------------------------------------------------
# 2. Trial Pull vs Classroom (class level)
# ---------------------------------------------------------------------------------------------

def check_trial_pull(c):
    if not os.path.exists(TRIAL_PULL):
        print('2. Trial Pull: Category Summary CSV not found.')
        return
    tp = [r for r in csv.DictReader(open(TRIAL_PULL, encoding='utf-8-sig')) if r['School Year'].startswith('2025')]
    finals = {}
    for sec, sem in c.execute("SELECT DISTINCT section, semester FROM student_grade_snapshots WHERE semester IN ('2025 Fall', '2026 Spring')"):
        snap = c.execute('SELECT MAX(snapshot_date) FROM student_grade_snapshots WHERE section=? AND semester=?', (sec, sem)).fetchone()[0]
        for course, cat, w, avg, calc in c.execute('''
                SELECT course_name, category, category_weight, AVG(g), MAX(grade_calculation) FROM (
                  SELECT DISTINCT course_name, student_id, category, category_weight, student_grade_in_category AS g, grade_calculation
                  FROM student_grade_snapshots WHERE section=? AND snapshot_date=?) GROUP BY 1, 2, 3''', (sec, snap)):
            finals.setdefault(course, {'snap': snap[:10], 'cats': {}, 'calc': calc})['cats'][(cat or '').strip().lower()] = (w, avg)
    archive = {}
    if has_table(c, 'archived_category_grades'):
        for cid, cat, w, avg in c.execute('SELECT class_id, category, weight, AVG(pct) FROM archived_category_grades GROUP BY 1, 2, 3'):
            archive.setdefault(cid, {})[(cat or '').strip().lower()] = (w, avg)
    pct = lambda s: float(s.rstrip('%')) if s and s.strip() not in ('', '-') else None
    out, outcomes = [], Counter()
    for r in tp:
        cat = r['Category'].strip().lower()
        if cat == '(no category)':
            cat = 'no category'   # our name for work without a category
        if cat.startswith('(no graded'):
            continue
        for src, data in (('snapshot', finals.get(r['Class'])), ('archive', archive.get(r['Class ID']))):
            if src == 'archive' and not archive:
                continue
            if not data:
                outcomes[f'{src}: class not found'] += 1
                out.append([r['Class'], r['Class ID'], src, '', r['Category'], pct(r['Weight']), None, pct(r['Class Average']), None, None, 'class not found'])
                continue
            cats = data['cats'] if src == 'snapshot' else data
            ours = cats.get(cat)
            if not ours:
                result = 'category not found'
                ow = oa = None
            else:
                ow, oa = ours
                tw, ta = pct(r['Weight']), pct(r['Class Average'])
                wdiff = tw is not None and ow is not None and abs(tw - ow) > 0.05
                adiff = ta is not None and oa is not None and abs(ta - oa) > 1
                result = 'match' if not wdiff and not adiff else ('WEIGHT DIFFERS' if wdiff else 'AVERAGE DIFFERS')
            outcomes[f'{src}: {result}'] += 1
            out.append([r['Class'], r['Class ID'], src, data.get('snap', '') if src == 'snapshot' else '', r['Category'],
                        pct(r['Weight']), ow, pct(r['Class Average']), None if oa is None else round(oa, 1),
                        None if oa is None or pct(r['Class Average']) is None else round(oa - pct(r['Class Average']), 1), result])
    out.sort(key=lambda r: (r[10] == 'match', -abs(r[9] or 0)))
    path = write_csv('trialpull', ['class', 'class ID', 'our source', 'our snapshot', 'category', 'Trial Pull weight', 'our weight',
                                   'Trial Pull category average', 'our category average', 'ours - Trial Pull', 'result'], out)
    print('2. Trial Pull vs Classroom (2025-26 classes): ' + ', '.join(f'{k} {v}' for k, v in sorted(outcomes.items())))
    print('   Note: the Trial Pull was taken in 2026-27, after final grading; our 2026 Spring snapshot is from early June.')
    print(f'   -> {path}')


# ---------------------------------------------------------------------------------------------
# 3. Transcripts vs Classroom (student level)
# ---------------------------------------------------------------------------------------------

def tokens(s):
    return frozenset(t for t in re.split(r'[^a-z]+', (s or '').lower()) if t)


def name_lookup(c):
    """{name tokens: {Classroom student IDs}} from both name lists (passport, first + last, Classroom, English names)."""
    ids = defaultdict(set)
    for p, f, l, g, gid in c.execute('SELECT passport_name, first_name, last_name, google_classroom_name, google_classroom_id FROM student_name_list_jh'):
        for s in (p, f'{f or ""} {l or ""}', g):
            if gid and tokens(s):
                ids[tokens(s)].add(str(gid))
    try:
        for n, e, gid in c.execute('SELECT name, "E Name", google_classroom_id FROM "Student_Name_List_HS"'):
            for s in (n, e):
                if gid and tokens(s):
                    ids[tokens(s)].add(str(gid))
    except sqlite3.Error:
        pass
    return ids


def transcript_rows(c):
    """[(student_id or None, transcript name, semester, subject, letter, credit)] for real class rows."""
    if not has_table(c, 'student_transcripts'):
        return []
    ids, out = name_lookup(c), []
    for name, subj, sem_year, final, credit in c.execute(
            'SELECT "Student Name", subject, semester_and_year, "Final Grade", credit FROM student_transcripts'):
        if not subject_words(subj):
            continue
        sids = ids.get(tokens(name), set())
        out.append((next(iter(sids)) if len(sids) == 1 else None, name, sem_year, re.sub(r'\s+', ' ', subj).strip(),
                    (final or '').strip(), credit))
    return out


def subject_words(s):
    s = re.sub(r'^\(G\d+\)\s*', '', s or '')
    return {w for w in re.split(r'[^a-z]+', s.lower()) if len(w) > 3 and w not in
            {'introduction', 'intermediate', 'advanced', 'middle', 'grades', 'grade'}}


def check_transcripts(c):
    if not has_table(c, 'student_transcripts'):
        print('3. Transcripts: student_transcripts not found.')
        return
    ids = name_lookup(c)
    # Final grade per student per class: last snapshot of the semester, plus the archive.
    finals = defaultdict(list)   # (sid, semester) -> [(course, pct, source)]
    for sec, sem, snap in c.execute('SELECT section, semester, MAX(snapshot_date) FROM student_grade_snapshots GROUP BY 1, 2'):
        for sid, course, g in c.execute('SELECT DISTINCT student_id, course_name, overall_grade FROM student_grade_snapshots '
                                        'WHERE section=? AND snapshot_date=?', (sec, snap)):
            finals[(str(sid), sem)].append((course, g, 'snapshot ' + snap[:10]))
    if has_table(c, 'archived_grades'):
        for sid, sem, course, g in c.execute('SELECT g.student_id, a.semester, a.course_name, g.final_pct FROM archived_grades g '
                                             'JOIN archived_classes a USING (class_id)'):
            finals[(str(sid), sem)].append((course, g, 'archive'))

    out, outcomes = [], Counter()
    for name, subj, sem, final, credit, sem_year, level in c.execute(
            'SELECT "Student Name", subject, semester, "Final Grade", credit, semester_and_year, grade_level FROM student_transcripts'):
        if not subject_words(subj):
            outcomes['not a class (e.g. "Final" summary row)'] += 1
            continue
        sids = ids.get(tokens(name), set())
        if len(sids) != 1:
            outcomes['student not found' if not sids else 'student ambiguous'] += 1
            out.append([name, level, sem_year, subj, final, '', '', '', '', 'student not found' if not sids else 'student ambiguous'])
            continue
        sid = next(iter(sids))
        lvl = grade_of(level or '') or grade_of(subj)
        want = subject_words(subj)
        best = None
        for course, g, src in finals.get((sid, sem_year), []):
            k = course_key(course).lower()
            score = len(want & {w for w in re.split(r'[^a-z]+', k) if len(w) > 3})
            score += 2 if suggest(course)[0] == suggest(subj)[0] else 0
            if lvl and grade_of(course_key(course)) and grade_of(course_key(course)) != lvl:
                score -= 3
            # On a tie the archive wins: it is Classroom's final state (snapshots can be from before
            # final grading).
            if best is None or score > best[0] or (score == best[0] and src == 'archive' and best[3] != 'archive'):
                best = (score, course, g, src)
        if not best or best[0] < 2:
            outcomes['class not found'] += 1
            out.append([name, level, sem_year, subj, final, '', '', '', '', 'class not found'])
            continue
        _, course, g, src = best
        ours = letter(g)
        result = 'no grade in Classroom' if g is None else ('match' if ours == (final or '').strip().upper() else 'LETTER DIFFERS')
        outcomes[result] += 1
        out.append([name, level, sem_year, subj, final, course, None if g is None else round(g, 2), ours, src, result])
    out.sort(key=lambda r: (r[9] != 'LETTER DIFFERS', r[9]))
    path = write_csv('transcripts', ['transcript name', 'grade level', 'semester', 'transcript subject', 'transcript grade',
                                     'Classroom class', 'Classroom final %', 'Classroom letter', 'source', 'result'], out)
    print('3. Transcripts vs Classroom: ' + ', '.join(f'{k} {v}' for k, v in outcomes.most_common()))
    print(f'   -> {path} (student names: keep on this computer)')


def main():
    c = connect()
    print(f'Archive imported: {"yes" if has_table(c, "archived_classes") else "no (checks use snapshots only)"}\n')
    snaps = snapshot_classes(c)
    check_sheet(c, snaps)
    check_trial_pull(c)
    check_transcripts(c)


if __name__ == '__main__':
    main()

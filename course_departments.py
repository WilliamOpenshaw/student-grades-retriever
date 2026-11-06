'''
Course keys and suggested departments for the staff dashboard.

A course's DEPARTMENT is decided by the dean in the "Departments" tab of the dashboard's access
Sheet (one row per course key). This module only makes the starting suggestion and the key the
tab is organised by:

    course_key('26F Math 6_A')                  -> 'Math 6'
    course_key('25-26_Fall_Math G6_1')          -> 'Math G6'
    course_key('Science & Research Writing (Spring \'26)') -> 'Science & Research Writing'
    suggest('26F Intro. to Physics')            -> ('Natural Sciences', 'SPH3U', False)

The key drops the term (26F, 25-26_Fall_, (Spring '26), 2025-2026 ...) and section markers
(trailing A/B/C, _1, -2, the A in 7A), so the sections of one course - and the same course in
later semesters, when teachers keep the name - share one row in the tab. Names that change from
one semester to the next simply get a new row (the daily trigger adds it with the suggestion).

sync_dashboard.py sends {course name: [key, suggested department, Ontario code, leave out]} for
every course in the data; Code.gs uses the tab first and falls back to these suggestions.

Usage:
    .venv\\Scripts\\python.exe course_departments.py --check      # every course name in the DB with
                                                                # its key and suggestion (read-only)
    .venv\\Scripts\\python.exe course_departments.py --check-trial-pull CSV
                                                                # compare suggestions with the
                                                                # Trial Pull "Classes" tab
'''

import argparse
import csv
import re
import unicodedata

from course_terms import _SCHOOL_YEAR, _SHORT_CODE, _SEASON_YEAR, _YEAR_SEASON

# Department names (the school's own, from the 2025-26 departmental Sheet, plus PHE / PBL /
# Homeroom from the Trial Pull). "Other" = not an academic department.
DEPARTMENTS = ['English / EAL', 'Mathematics', 'Natural Sciences', 'Social Sciences', 'Modern Languages',
               'Art & Design', 'PHE', 'PBL', 'Homeroom', 'Other']
LEFT_OUT = {'Homeroom', 'Other'}   # left out of department averages unless the tab says otherwise

# Ordered: the first match wins, so the more specific rules come first
# ("Science & Research Writing" is English; "Health & Human Development" is PHE).
RULES = [
    ('Other', r'intersession|course adjustment|academic recovery|internship|cooperative|co-?op\b|'
              r'test case|department$|\bcap\b|g12cap'),
    ('Homeroom', r'home\s?room'),
    ('PBL', r'\bpbl|project based learning'),
    ('PHE', r'\bpe\b|\bphe\b|physical education|health|yoga|darts|wellbeing'),
    ('Modern Languages', r'chinese|mandarin|\bcsl\b|interpret|中文|華語'),
    # Ontario course codes in older names (2024F ENG3U, ENG4U-C1(EWC4U)) count too.
    ('English / EAL', r'\besl|\benl|\beal\b|english|\bela\b|lang(uage)?\s*\d|literature|writing|ielts|'
                      r'lit circle|\blanguage\b|\b(eng|ewc|ets|olc)\d|\beng\b'),   # 'Eng-1': JH archive
    ('Mathematics', r'math|mth1w|functions|calculus|data management|differential|statistic|'
                    r'\b(mpm|mcr|mhf|mcv|mdm)\d'),
    ('Social Sciences', r'social|history|geograph|sociology|psychology|anthropolog|economic|entrepreneur|'
                        r'philosoph|government|politic|relationship|life design|career|logic|\bss\b|'
                        r'finance|accounting|\b(cgd|chc|cht|hsp|hhd|hsc|bdi|gwl|cia)\d'),
    ('Natural Sciences', r'science|biology|chemistry|physics|\blab\b|laborator|computer|programming|'
                         r'environment|scitech|\bict\b|\b(snc|sbi|sch|sph|ics|icd)\d'),   # ICT: JH archive
    ('Art & Design', r'\bart|music|video|media|makerspace|design|photograph|radio|printing|lighting|'
                     r'djing|game dynamics|puzzle'),
]
RULES = [(dept, re.compile(rx, re.I)) for dept, rx in RULES]

# Optional course codes (e.g. Ontario codes) by course key (lower case). These are only starting
# suggestions; the dean can add or correct codes in the Departments tab. Add your school's courses.
ONTARIO_CODES = {
    'intro. to biology': 'SBI3U', 'intro. to chemistry': 'SCH3U', 'intro. to physics': 'SPH3U',
    'calculus & vectors': 'MCV4U', 'world literature': 'ENG4U',
}

_SEASON_ANY = re.compile(r"\(?\s*(spring|fall|autumn)\s*'?\s*(20\d{2}|\d{2})?\s*\)?", re.I)


def _clean(name):
    s = unicodedata.normalize('NFKC', name or '')
    return re.sub(r'\s+', ' ', s.replace('_', ' ')).strip()


def course_key(name):
    """Course name without term and section markers (see the module docstring)."""
    s = _clean(name)
    for rx in (_SCHOOL_YEAR, _YEAR_SEASON, _SEASON_YEAR):
        s = rx.sub(' ', s)
    s = _SHORT_CODE.sub(' ', s)
    s = re.sub(r'(?<![A-Za-z])20\d{2}\s*[SF](?![A-Za-z])', ' ', s)   # '2025 S Calculus'
    s = _SEASON_ANY.sub(' ', s)
    s = re.sub(r"\(\s*'?\d*\s*\)|\(\s*\)", ' ', s)                     # leftover '()'
    s = re.sub(r'^[\s\-–:]+|[\s\-–:]+$', '', re.sub(r'\s+', ' ', s))
    # Section markers.
    s = re.sub(r'\b(\d+)[A-C]\b', r'\1', s, flags=re.I)                # 7A -> 7, G11B -> G11, 8b -> 8 (not 3D)
    s = re.sub(r'\b(\d+)-[1-4]\b', r'\1', s)                           # G7-1 -> G7, Homeroom 8-2 -> 8
    s = re.sub(r'(\s+(group\s+)?[A-D](\s*&\s*[A-D])?|\s+[1-4])$', '', s, flags=re.I)   # ' A', ' A&B', '_1'
    s = re.sub(r'\s*\(\s*[A-D]\s*\)$', '', s)
    s = re.sub(r'\b(\w+) \1\b', r'\1', s)                              # '6 Arts 25-26_Fall_Arts' -> '6 Arts'
    s = re.sub(r'\s+', ' ', s).strip(' -–:')
    return s or _clean(name)


def suggest(name):
    """(department, Ontario code or '', leave out of averages) for a course name."""
    key = course_key(name)
    text = key + ' ' + _clean(name)
    dept = next((d for d, rx in RULES if rx.search(text)), 'Other')
    code = ONTARIO_CODES.get(key.lower(), '')
    if not code:
        code = _jh_code(key, dept)
    return dept, code, dept in LEFT_OUT


def _jh_code(key, dept):
    """Middle-grades codes used in the departmental Sheet (Math6, SciTech7, Geo8, Arts6 Strand D...)."""
    m = re.search(r'(?<!\d)([5-8])(?!\d)', key)
    if not m:
        return ''
    g = m.group(1)
    if dept == 'Mathematics' and not re.search(r'mth1w|sec', key, re.I):
        return 'Math' + g
    if dept == 'Natural Sciences':
        return 'SciTech' + g
    if dept == 'Social Sciences':
        return 'SocStudy' + g if g in '56' else 'Geo' + g
    if dept == 'Art & Design':
        return 'Arts' + g + ' Strand D'
    if dept == 'Modern Languages':
        return '108 stage 2'
    return ''


def course_info(names):
    """{name: [key, department, code, leave_out]} for the dashboard."""
    out = {}
    for n in names:
        if n:
            dept, code, leave = suggest(n)
            out[n] = [course_key(n), dept, code, leave]
    return out


def _check_db():
    from sqlalchemy import text
    from db import get_db_engine
    with get_db_engine().connect() as conn:
        rows = conn.execute(text(
            'SELECT DISTINCT semester, section, course_name FROM student_grade_snapshots ORDER BY 1, 2, 3')).fetchall()
    keys = {}
    for sem, sec, name in rows:
        dept, code, leave = suggest(name)
        key = course_key(name)
        keys.setdefault(key.lower(), set()).add(sem)
        print(f"{sem or '':<12} {(sec or '')[:2]}  {dept:<17} {code:<15} {'(left out) ' if leave else ''}{key:<45} <- {name}")
    print(f"\n{len(rows)} course names -> {len(keys)} keys; "
          f"{sum(1 for v in keys.values() if len(v) > 1)} keys appear in more than one semester.")


def _check_trial_pull(path):
    alias = {'English': 'English / EAL', 'Math': 'Mathematics'}
    same = diff = blank = 0
    with open(path, encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            theirs = alias.get(r['Department'], r['Department'])
            ours = suggest(r['Class'])[0]
            if not theirs:
                blank += 1
                print(f"  (no dept in Trial Pull)  ours={ours:<17} {r['Class']}")
            elif theirs == ours:
                same += 1
            else:
                diff += 1
                print(f"  DIFFERENT  Trial Pull={theirs:<17} ours={ours:<17} {r['Class']}")
    print(f"\nTrial Pull classes: {same} same department, {diff} different, {blank} not tagged there.")


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='Course keys and suggested departments (read-only checks).')
    ap.add_argument('--check', action='store_true', help='list every course name in the DB with its key and suggestion')
    ap.add_argument('--check-trial-pull', metavar='CSV', help='compare with the Trial Pull "Classes" CSV')
    a = ap.parse_args()
    if a.check:
        _check_db()
    if a.check_trial_pull:
        _check_trial_pull(a.check_trial_pull)
    if not (a.check or a.check_trial_pull):
        ap.print_help()

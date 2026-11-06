'''
Shared helpers for telling which semester a Google Classroom course belongs to.

Google Classroom only knows whether a course is ACTIVE or ARCHIVED, and teachers don't always
archive last semester's classes. So on top of the ACTIVE filter, scripts read the term from the
course name and drop courses that clearly belong to a PAST term (e.g. "2024F Makerspace" during
Spring 2026). Courses with no recognisable term in the name are kept, but logged, so a correctly
named current class is never dropped by mistake.

Recognised name patterns (case-insensitive):
    26S / 25F / 2024F               short code: year + S(pring) / F(all)
    Spring '26 / Fall 2025 / 2026 Spring
    25-26 Spring / 2025-2026 Fall   school year + season
    2025-2026 AP Chemistry          school year only = year-long course (that Fall + that Spring)

Terms use the same calendar rule as the rest of the project (see get_semester_string in the
enrich scripts): Aug-Dec = Fall of that year, January = Fall of the previous year, Feb-Jul = Spring.

Usage:
    from course_terms import current_term, classify_course, term_label, log_term_check
'''

import os
import re
from datetime import datetime

from db import BASE_DIR, parse_db_datetime

LOG_FILE = os.path.join(BASE_DIR, 'logs', 'course_term_check.log')

SPRING, FALL = 1, 2
SEASONS = {'spring': SPRING, 's': SPRING, 'fall': FALL, 'autumn': FALL, 'f': FALL}

_SCHOOL_YEAR = re.compile(r'(?<!\d)(20\d{2}|\d{2})\s*[-–/]\s*(20\d{2}|\d{2})(?!\d)')
_SHORT_CODE = re.compile(r'(?<![A-Za-z0-9])(?:20)?(\d{2})([FS])(?![A-Za-z])', re.IGNORECASE)
_SEASON_YEAR = re.compile(r"(spring|fall|autumn)[\s_]*'?\s*(20\d{2}|\d{2})(?!\d)", re.IGNORECASE)
_YEAR_SEASON = re.compile(r'(?<!\d)(20\d{2})\s*(spring|fall|autumn)', re.IGNORECASE)
_SEASON_WORD = re.compile(r'(spring|fall|autumn)', re.IGNORECASE)


def _full_year(y):
    y = int(y)
    return y + 2000 if y < 100 else y


def current_term(date=None):
    """(year, SPRING|FALL) for a date (default: now). Accepts DB date strings too."""
    d = parse_db_datetime(date) if date is not None else datetime.now()
    if d.month == 1:
        return (d.year - 1, FALL)
    if d.month >= 8:
        return (d.year, FALL)
    return (d.year, SPRING)


def term_label(term):
    """(2026, SPRING) -> '2026 Spring' (same format as the semester column in the name lists)."""
    return f"{term[0]} {'Spring' if term[1] == SPRING else 'Fall'}"


def course_terms(name):
    """Set of (year, season) terms a course name refers to. Empty set = no term found."""
    name = name or ''
    terms = set()

    for m in _SCHOOL_YEAR.finditer(name):
        start, end = _full_year(m.group(1)), _full_year(m.group(2))
        if end != start + 1:
            continue  # e.g. "1900-1950" in a course title, not a school year
        season = _SEASON_WORD.search(name[m.end():m.end() + 15])
        if season:
            s = SEASONS[season.group(1).lower()]
            terms.add((start, FALL) if s == FALL else (end, SPRING))
        else:
            terms.update({(start, FALL), (end, SPRING)})  # year-long course
    if terms:
        return terms

    for m in _SHORT_CODE.finditer(name):
        terms.add((_full_year(m.group(1)), SEASONS[m.group(2).lower()]))
    for m in _SEASON_YEAR.finditer(name):
        terms.add((_full_year(m.group(2)), SEASONS[m.group(1).lower()]))
    for m in _YEAR_SEASON.finditer(name):
        terms.add((_full_year(m.group(1)), SEASONS[m.group(2).lower()]))
    return terms


def classify_course(name, target_term):
    """
    'current' - the name includes target_term (or is a year-long course covering it)
    'old'     - every term in the name is before target_term  -> should be excluded
    'future'  - every term in the name is after target_term   (kept; e.g. next semester set up early)
    'unknown' - no term in the name                            (kept)
    """
    terms = course_terms(name)
    if not terms:
        return 'unknown'
    if target_term in terms:
        return 'current'
    if all(t < target_term for t in terms):
        return 'old'
    if all(t > target_term for t in terms):
        return 'future'
    return 'unknown'


def split_courses(names, target_term):
    """Group course names by classification: {'current': [...], 'old': [...], ...}."""
    groups = {'current': [], 'old': [], 'future': [], 'unknown': []}
    for n in names:
        groups[classify_course(n, target_term)].append(n)
    return groups


def log_term_check(script, lines):
    """Print lines and append them (timestamped) to logs/course_term_check.log."""
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"\n===== {stamp}  {script} =====\n")
        for line in lines:
            print(line)
            f.write(line + '\n')

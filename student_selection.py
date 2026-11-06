r'''
Picking specific students by name - shared by ingest_classroom_data.py,
generate_grade_reports.py and send_emails_hs.py / send_emails_jh.py.

Names are matched to the Google Classroom name (the one on the report), ignoring:
    a grade in front        "G9 Sam Taylor", "G09 Jordan Lee"
    anything in brackets    "Jordan Lee (李小明) (H)"
    capitals and extra spaces
so a list copied from the student list Sheet works as-is.

Command-line options added by add_student_options():
    --student "Sam Taylor"            (repeat for more students)
    --students-file students.txt       (one name per line; lines starting with # are ignored)
'''

import os
import re

_GRADE_PREFIX = re.compile(r'^\s*(?:G|Grade|Year)\s*\d{1,2}\s+', re.IGNORECASE)
_BRACKETS = re.compile(r'[(（][^)）]*[)）]')


def clean_name(raw):
    """'G09 Jordan Lee (李小明) (H)' -> 'Jordan Lee'."""
    name = _BRACKETS.sub(' ', _GRADE_PREFIX.sub('', str(raw or '')))
    return ' '.join(name.split())


def norm(name):
    """Comparison key: cleaned, lower case."""
    return clean_name(name).lower()


def add_student_options(parser, what='Only for this student'):
    parser.add_argument('-s', '--student', action='append', metavar='NAME',
                        help=f'{what} (Classroom name; a grade in front and anything in brackets are '
                             f'ignored). Repeat for more students.')
    parser.add_argument('--students-file', metavar='FILE',
                        help='Text file with one student name per line (lines starting with # are ignored).')


def selected_students(args):
    """Cleaned names from --student / --students-file (order kept, duplicates removed), or []."""
    raw = list(args.student or [])
    if getattr(args, 'students_file', None):
        if not os.path.exists(args.students_file):
            raise SystemExit(f"Students file not found: {args.students_file}")
        with open(args.students_file, encoding='utf-8-sig') as f:
            raw += [line for line in f if line.strip() and not line.strip().startswith('#')]
    names, seen = [], set()
    for r in raw:
        n = clean_name(r)
        if n and n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)
    return names


def report_missing(wanted, found_names, where):
    """Print which wanted names were not found (likely a typo or a student with no data)."""
    found = {norm(n) for n in found_names}
    missing = [w for w in wanted if norm(w) not in found]
    if missing:
        print(f"  ! Not found in {where}: {', '.join(missing)}")
    return missing

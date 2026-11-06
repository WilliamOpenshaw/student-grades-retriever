'''
Import a new semester's student lists (CSV exports of the HS and JH "Student List" tabs).

What it does:
  1. Backs up grades.db first (backups folder).
  2. Saves each CSV exactly as-is, every column and row (including transferred students),
     as a new table: student_list_hs_<year>_<season> / student_list_jh_<year>_<season>.
  3. Adds the current (not transferred) students to the tables the scripts use,
     tagged with the semester:
        "Student_Name_List_HS"   (enrich_student_records_HS.py, reports, emails)
        student_name_list_jh     (enrich_student_records_JH.py, reports, emails)
     The scripts always use the latest semester in these tables.
  4. Carries each returning student's Google Classroom ID (and gender, if the new list
     leaves it blank) forward from earlier semesters, matched by student email. This
     includes students moving from JH to HS. New students are linked later by the
     enrich scripts, after the first ingest of the semester.

Usage:
    python import_student_lists.py --hs "student_list_csvs/2026 Fall SH Student List - Student List.csv" \
                                   --jh "student_list_csvs/2026 Fall JH Student List - Student List.csv"
Options:
    --semester "2026 Fall"   (default: read from the file names)
    --replace                re-import a semester that is already in the database
    --dry-run                show what would happen without changing anything
'''

import os
import re
import argparse

import pandas as pd
from sqlalchemy import text, inspect, MetaData, Table

from db import get_db_engine

HS_TABLE = 'Student_Name_List_HS'
JH_TABLE = 'student_name_list_jh'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_list(path):
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    return df.apply(lambda col: col.str.strip())


def records(df):
    """Rows as plain dicts, with blank cells as None (NULL in the database)."""
    return [{k: (v if isinstance(v, str) and v != '' else None) for k, v in row.items()}
            for row in df.to_dict('records')]


def semester_from_name(*paths):
    for p in paths:
        m = re.search(r'(20\d{2})\s*(Fall|Spring)', os.path.basename(p or ''), re.I)
        if m:
            return f"{m.group(1)} {m.group(2).capitalize()}"
    return None


def semester_col(df, semester, subject):
    """Find this semester's column for a subject, e.g. '26F Home Room' / '2026 F Home Room'."""
    year, season = semester.split()
    pattern = re.compile(rf'^(20)?{year[2:]}\s*{season[0]}\w*\s*{re.escape(subject)}$', re.I)
    return next((c for c in df.columns if pattern.match(c)), None)


def split_emails(value):
    return [e for e in re.split(r'[,;\s/]+', value or '') if '@' in e]


def norm_email(value):
    return (value or '').strip().lower() or None


def to_int(value):
    return int(value) if value and value.isdigit() else value


def current_students(df):
    return df[df['Transferred'].fillna('').str.upper() != 'TRUE']


def semester_key(sem):
    m = re.search(r'(\d{4})', sem or '')
    return (int(m.group(1)) if m else 0, 2 if 'fall' in (sem or '').lower() else 1)


# ---------------------------------------------------------------------------
# Building rows for the script tables
# ---------------------------------------------------------------------------

def hs_rows(df, semester, table_cols):
    lookup = {c.strip().lower(): c for c in table_cols}
    explicit = {}
    home_room = semester_col(df, semester, 'Home Room')
    if home_room:
        explicit['Home Room'] = home_room

    rows = []
    for r in records(current_students(df)):
        row = {'semester': semester}
        # Columns with the same name (ignoring case) as an existing table column
        for col in df.columns:
            if col.startswith('Unnamed'):
                continue
            target = lookup.get(col.strip().lower())
            if target and target != 'semester':
                row[target] = r[col]
        for target, col in explicit.items():
            row[target] = r[col]
        row['No.'] = to_int(row.get('No.'))
        if row.get('From JH'):
            row['From JH'] = row['From JH'].upper()   # the list mixes 'V' and 'v'
        row['Student email'] = norm_email(row.get('Student email'))
        rows.append(row)
    return rows


def jh_rows(df, semester):
    english = semester_col(df, semester, 'English')
    rows = []
    for r in records(current_students(df)):
        name_parts = (r.get('E Name') or '').split()
        parents = split_emails(r.get('Parent email'))
        rows.append({
            'semester': semester,
            'grade_level': r.get('Grade'),
            'chinese_name': r.get('C Name'),
            'first_name': ' '.join(name_parts[:-1]) or (name_parts[0] if name_parts else None),
            'last_name': name_parts[-1] if len(name_parts) > 1 else None,
            'gender': r.get('Gender'),
            'english': r.get(english) if english else None,
            'math': r.get('Math'),
            'homeroom': r.get('Home Room'),
            'passport_name': r.get('Passport Name'),
            'student_email': norm_email(r.get('Student email')),
            # The new list has one "Parent email" cell; the first two go to father/mother,
            # any more to extra_gaurdian_email. The order says nothing about who is who.
            'father_email': parents[0] if len(parents) > 0 else None,
            'mother_email': parents[1] if len(parents) > 1 else None,
            'extra_gaurdian_email': ', '.join(parents[2:]) or None,
        })
    return rows


def previous_records(conn):
    """email -> latest known (google_classroom_id, google_classroom_name, gender) from both tables."""
    rows = conn.execute(text(f'''
        SELECT semester, LOWER(TRIM("Student email")), google_classroom_id, google_classroom_name, gender
        FROM "{HS_TABLE}"
        UNION ALL
        SELECT semester, LOWER(TRIM(student_email)), google_classroom_id, google_classroom_name, gender
        FROM {JH_TABLE}
    ''')).fetchall()
    known = {}
    for sem, email, gid, gname, gender in sorted(rows, key=lambda r: semester_key(r[0])):
        if not email:
            continue
        rec = known.setdefault(email, {})
        if gid:
            rec['id'], rec['name'] = gid, gname
        if gender:
            rec['gender'] = gender
    return known


def carry_over(rows, email_key, known):
    ids = genders = 0
    for row in rows:
        rec = known.get(row.get(email_key))
        if not rec:
            continue
        if rec.get('id') and not row.get('google_classroom_id'):
            row['google_classroom_id'], row['google_classroom_name'] = rec['id'], rec.get('name')
            ids += 1
        if rec.get('gender') and not row.get('gender'):
            row['gender'] = rec['gender']
            genders += 1
    return ids, genders


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Import a semester's HS and JH student lists from CSV.")
    parser.add_argument('--hs', help="High School student list CSV")
    parser.add_argument('--jh', help="Junior High student list CSV")
    parser.add_argument('--semester', help='e.g. "2026 Fall" (default: taken from the file names)')
    parser.add_argument('--replace', action='store_true', help="Replace this semester's rows if already imported")
    parser.add_argument('--dry-run', action='store_true', help="Show what would happen without changing anything")
    args = parser.parse_args()

    if not args.hs and not args.jh:
        parser.error("give --hs and/or --jh")
    semester = args.semester or semester_from_name(args.hs, args.jh)
    if not semester or not re.fullmatch(r'20\d{2} (Fall|Spring)', semester):
        parser.error('could not tell the semester from the file names; pass --semester "2026 Fall"')
    suffix = semester.lower().replace(' ', '_')

    engine = get_db_engine()
    jobs = []  # (division, csv dataframe, script table, rows, email column)
    with engine.connect() as conn:
        known = previous_records(conn)
        hs_cols = [c['name'] for c in inspect(engine).get_columns(HS_TABLE)]

        for division, path, table in (('hs', args.hs, HS_TABLE), ('jh', args.jh, JH_TABLE)):
            if not path:
                continue
            existing = conn.execute(text(f'SELECT COUNT(*) FROM "{table}" WHERE semester = :s'),
                                    {'s': semester}).scalar()
            if existing and not args.replace:
                raise SystemExit(f'{table} already has {existing} rows for {semester}. '
                                 f'Use --replace to re-import.')
            df = read_list(path)
            if division == 'hs':
                rows, email_key = hs_rows(df, semester, hs_cols), 'Student email'
            else:
                rows, email_key = jh_rows(df, semester), 'student_email'
            ids, genders = carry_over(rows, email_key, known)
            linked = sum(1 for r in rows if r.get('google_classroom_id'))

            print(f"\n{division.upper()}: {os.path.basename(path)}")
            print(f"  CSV rows: {len(df)} ({len(df) - len(rows)} transferred, kept only in the raw table)")
            print(f"  Current students for {table} ({semester}): {len(rows)}"
                  + (f"  [replacing {existing} existing rows]" if existing else ''))
            print(f"  Google Classroom ID carried over: {ids}; still to link (new students): {len(rows) - linked}")
            print(f"  Gender filled from earlier semesters: {genders}; still blank: "
                  f"{sum(1 for r in rows if not r.get('gender'))}")
            jobs.append((division, df, table, rows))

    if args.dry_run:
        print("\nDry run: nothing changed.")
        return

    from backup_database import make_local_backup
    print("\n--- Backing up the database first ---")
    make_local_backup()

    meta = MetaData()
    with engine.begin() as conn:
        for division, df, table, rows in jobs:
            raw_table = f'student_list_{division}_{suffix}'
            df.insert(0, 'semester', semester)
            df.to_sql(raw_table, conn, if_exists='replace', index=False)

            conn.execute(text(f'DELETE FROM "{table}" WHERE semester = :s'), {'s': semester})
            target = Table(table, meta, autoload_with=conn)
            keys = sorted({k for r in rows for k in r})  # every row needs the same columns
            conn.execute(target.insert(), [{k: r.get(k) for k in keys} for r in rows])
            print(f"  ✅ {raw_table}: {len(df)} rows (full CSV)")
            print(f"  ✅ {table}: added {len(rows)} students for {semester}")

    print("\nDone. Next: run ingest_classroom_data.py, then the enrich scripts to link new students.")


if __name__ == '__main__':
    main()

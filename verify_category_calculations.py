'''
Verify the grades written by calculate_category_grades.py (read-only: changes nothing).

It recalculates, in Python and independently of the SQL in calculate_category_grades.py, for EVERY
grade pull (snapshot_date), student, course and category:
  - the category grade (student_grade_in_category): points earned / points possible x 100, counting only
    graded work (assigned_score set, max_points > 0) that isn't excused;
  - the overall grade (overall_grade), the way each course is set up in Classroom:
      "No overall grade" (HIDE_OVERALL_GRADE)  -> none
      total points                            -> all graded points / all possible points
      weighted categories (and older pulls     -> category grades x weights / sum of the weights used
      without the setting)
and compares them with the stored values (allowed difference 0.01 for rounding).

Before 2026-10-14 this script added up one student's category over ALL grade pulls together and compared
the total with one stored value, so it reported a "MISMATCH" even when the stored grades were right.

Usage:
    .venv\\Scripts\\python.exe verify_category_calculations.py              # every grade pull
    .venv\\Scripts\\python.exe verify_category_calculations.py --latest     # only each school's latest pull
'''

import argparse

import pandas as pd
from sqlalchemy import text

from db import get_db_engine, latest_snapshots_by_section, latest_snapshot_filter

TOLERANCE = 0.01
SHOW = 10  # example rows printed per kind of problem


def load(engine, latest):
    with engine.connect() as conn:
        where, params = '', {}
        if latest:   # each school's own latest pull (never a global MAX: HS/JH run on different days)
            cond, params = latest_snapshot_filter(latest_snapshots_by_section(conn))
            where = 'WHERE ' + cond
        sql = f'''
            SELECT sgs.snapshot_date, sgs.section, sgs.student_id, sgs.student_name, sgs.course_name, sgs.category,
                   sgs.category_weight, sgs.assigned_score, sgs.max_points, sgs.is_excused,
                   sgs.student_grade_in_category, sgs.overall_grade, sgs.grade_calculation, sgs.grade_display
            FROM student_grade_snapshots sgs {where}
        '''
        return pd.read_sql(text(sql), conn, params=params)


def counts(row):
    excused = row['is_excused']
    return (pd.notna(row['assigned_score']) and pd.notna(row['max_points']) and row['max_points'] > 0
            and not (excused is True or excused == 1))


def verify(df):
    keys = ['snapshot_date', 'student_id', 'course_name']
    df['category'] = df['category'].fillna('(no category)')
    graded = df[df.apply(counts, axis=1)]

    # --- 1. Category grades ---
    cat = graded.groupby(keys + ['category']).agg(earned=('assigned_score', 'sum'), possible=('max_points', 'sum')).reset_index()
    cat['expected'] = cat['earned'] / cat['possible'] * 100
    stored = (df.groupby(keys + ['category'])['student_grade_in_category']
                .agg(lambda s: sorted(set(s.dropna().round(2)))).reset_index(name='stored'))
    cat = stored.merge(cat, on=keys + ['category'], how='left')

    def cat_problem(r):
        if pd.isna(r['expected']):
            return 'stored but nothing graded' if r['stored'] else None
        if not r['stored']:
            return 'missing'
        if len(r['stored']) > 1:
            return 'different values in one category'
        return 'wrong value' if abs(r['stored'][0] - r['expected']) > TOLERANCE else None

    cat['problem'] = cat.apply(cat_problem, axis=1)
    print(f"\n1. Category grades: {len(cat)} student-course-categories over "
          f"{df['snapshot_date'].nunique()} grade pull(s)")
    report(cat, lambda r: f"{r['snapshot_date']} | {r['student_name']} | {r['course_name']} | {r['category']}: "
                          f"stored {r['stored']}, recalculated "
                          f"{'-' if pd.isna(r['expected']) else round(r['expected'], 2)}",
           df[keys + ['student_name']].drop_duplicates(keys))

    # --- 2. Overall grades ---
    setting = df.groupby(keys).agg(calc=('grade_calculation', 'max'), display=('grade_display', 'max'),
                                   stored=('overall_grade', lambda s: sorted(set(s.dropna().round(2))))).reset_index()
    points = graded.groupby(keys).agg(earned=('assigned_score', 'sum'), possible=('max_points', 'sum')).reset_index()
    points['total_pct'] = points['earned'] / points['possible'] * 100
    weights = df[['snapshot_date', 'student_id', 'course_name', 'category', 'category_weight']].drop_duplicates()
    wcat = cat[['snapshot_date', 'student_id', 'course_name', 'category', 'expected']].dropna().merge(weights, on=keys + ['category'])
    wcat = wcat[wcat['category_weight'] > 0]
    wcat['wx'] = wcat['expected'] * wcat['category_weight']
    weighted = wcat.groupby(keys).agg(wx=('wx', 'sum'), w=('category_weight', 'sum')).reset_index()
    weighted['weighted_pct'] = weighted['wx'] / weighted['w']
    ov = setting.merge(points[keys + ['total_pct']], on=keys, how='left').merge(weighted[keys + ['weighted_pct']], on=keys, how='left')

    def expected(r):
        if r['display'] == 'HIDE_OVERALL_GRADE':
            return None
        if r['calc'] == 'TOTAL_POINTS':
            return r['total_pct']
        return r['weighted_pct']

    ov['expected'] = ov.apply(expected, axis=1)

    def ov_problem(r):
        exp = r['expected']
        if exp is None or pd.isna(exp):
            return 'stored but should be empty' if r['stored'] else None
        if not r['stored']:
            return 'missing'
        if len(r['stored']) > 1:
            return 'different values in one course'
        return 'wrong value' if abs(r['stored'][0] - exp) > TOLERANCE else None

    ov['problem'] = ov.apply(ov_problem, axis=1)
    by_setting = ov.assign(kind=ov['display'].where(ov['display'] == 'HIDE_OVERALL_GRADE', ov['calc'].fillna('older pull (weighted)')))
    print(f"\n2. Overall grades: {len(ov)} student-courses "
          f"({', '.join(f'{k} {n}' for k, n in by_setting['kind'].value_counts().items())})")
    report(ov, lambda r: f"{r['snapshot_date']} | {r['student_name']} | {r['course_name']} "
                         f"[{r['display'] if r['display'] == 'HIDE_OVERALL_GRADE' else r['calc'] or 'older pull'}]: "
                         f"stored {r['stored']}, recalculated "
                         f"{'-' if r['expected'] is None or pd.isna(r['expected']) else round(r['expected'], 2)}",
           df[keys + ['student_name']].drop_duplicates(keys))
    return int(cat['problem'].notna().sum() + ov['problem'].notna().sum())


def report(frame, describe, names):
    bad = frame[frame['problem'].notna()].merge(names, on=['snapshot_date', 'student_id', 'course_name'], how='left')
    if bad.empty:
        print("   ✅ All match.")
        return
    print(f"   ❌ {len(bad)} problem(s): " + ', '.join(f'{k} {n}' for k, n in bad['problem'].value_counts().items()))
    print(f"      by grade pull: " + ', '.join(f'{str(k)[:16]} {n}' for k, n in bad['snapshot_date'].value_counts().sort_index().items()))
    for _, r in bad.head(SHOW).iterrows():
        print('      - ' + describe(r))


def main():
    parser = argparse.ArgumentParser(description="Recalculate category and overall grades and compare (read-only).")
    parser.add_argument('--latest', action='store_true', help="Only each school's latest grade pull.")
    args = parser.parse_args()

    print("--- Verifying Category and Overall Grade Calculations (read-only) ---")
    df = load(get_db_engine(), args.latest)
    print(f"Rows checked: {len(df)}  (grade pulls: {df['snapshot_date'].nunique()})")
    problems = verify(df)
    print("\n✅ Everything matches." if not problems else
          f"\n❌ {problems} problem(s) found. Re-run calculate_category_grades.py; if they remain, check the examples above.")


if __name__ == '__main__':
    main()

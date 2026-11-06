import pandas as pd
import argparse
from sqlalchemy import text

from db import get_db_engine as get_engine, latest_snapshots_by_section, latest_snapshot_filter
import os

# --- DATABASE CONFIGURATION ---
def fetch_latest_grades(match_keywords):
    engine = get_engine()

    # 1. Identify the latest snapshot of each section (HS and JH may be fetched separately)
    with engine.connect() as conn:
        latest = latest_snapshots_by_section(conn)

    if not latest:
        print("No snapshots found in database.")
        return pd.DataFrame()

    for section, snap in latest.items():
        print(f"Fetching {section} data from latest snapshot: {snap}")
    snap_cond, snap_params = latest_snapshot_filter(latest, alias=None)

    # 2. Fetch data with the additional requested columns
    query = text(f"""
        SELECT 
            student_name, 
            student_email, 
            course_name, 
            overall_grade, 
            snapshot_date, 
            section, 
            grade_level, 
            semester
        FROM student_grade_snapshots
        WHERE {snap_cond}
    """)

    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params=snap_params)

    if df.empty:
        return df

    # 3. Filter by keywords in course_name
    regex_pattern = '|'.join(match_keywords)
    mask = df['course_name'].str.contains(regex_pattern, case=False, na=False)
    df_filtered = df[mask].copy()

    # 4. Clean up: Drop duplicates to get one row per student per class
    # We include student_id or email in the subset to ensure uniqueness correctly
    df_unique = df_filtered.drop_duplicates(subset=['student_email', 'course_name'])

    return df_unique

def main():
    parser = argparse.ArgumentParser(description="Export overall grades for specific classes.")
    parser.add_argument(
        '-m', '--match',
        nargs='+',
        required=True,
        help='Keywords to match in class titles (e.g., english literature)'
    )
    parser.add_argument(
        '-o', '--output',
        default='class_grades_log.csv',
        help='Output CSV filename'
    )

    args = parser.parse_args()

    print(f"Searching for classes matching: {args.match}")
    results = fetch_latest_grades(args.match)

    if results.empty:
        print("No matches found for those keywords in the latest snapshot.")
    else:
        # Reordering columns for better readability in Sheets
        column_order = [
            'snapshot_date', 'section', 'grade_level', 'semester',
            'student_name', 'student_email', 'course_name', 'overall_grade'
        ]
        results = results[column_order].sort_values(by=['section', 'grade_level', 'course_name', 'student_name'])

        results.to_csv(args.output, index=False)
        print(f"Success! Exported {len(results)} records to {args.output}")

if __name__ == "__main__":
    main()
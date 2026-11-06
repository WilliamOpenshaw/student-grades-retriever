'''
Script to add and populate the 'student_grade_in_category' column in the
'student_grade_snapshots' table of the local SQLite database.
This column represents the percentage score for each category, calculated as
(Sum of Assigned Scores / Sum of Max Points) * 100 for each student, course
and category combination.

'''

import os
import argparse
import pandas as pd
from sqlalchemy import text, inspect
from datetime import datetime

from db import get_db_engine

def ensure_column_exists(engine):
    """
    Checks if 'student_grade_in_category' column exists. If not, adds it.
    """
    print("--- Checking Table Schema ---")
    existing_cols = [c['name'] for c in inspect(engine).get_columns('student_grade_snapshots')]
    with engine.begin() as conn:
        for col in ('grade_calculation', 'grade_display'):
            if col not in existing_cols:
                print(f"  Adding column '{col}' (Classroom grading setting per course)...")
                conn.execute(text(f'ALTER TABLE student_grade_snapshots ADD COLUMN "{col}" TEXT'))

    with engine.connect() as conn:
        if 'student_grade_in_category' not in existing_cols:
            print("  Column 'student_grade_in_category' missing. Adding it now...")
            conn.execute(text('ALTER TABLE student_grade_snapshots ADD COLUMN "student_grade_in_category" FLOAT'))
            conn.commit()
        else:
            print("  Column 'student_grade_in_category' already exists.")

def populate_category_grades(engine, table='student_grade_snapshots'):
    """table: refresh_dashboard.py runs this same calculation on its own table of current grades."""
    print("\n--- Calculating Category Grades ---")
    q = lambda sql: text(sql.replace('student_grade_snapshots', table))

    # FIX: Only look at RETURNED assignments and ignore excused ones
    sql_update = q("""
        UPDATE student_grade_snapshots AS target
        SET student_grade_in_category = calc.cat_percentage
        FROM (
            SELECT 
                snapshot_date, student_id, course_name, category,
                ROUND(
                    CAST(
                        (SUM(assigned_score) / NULLIF(SUM(max_points), 0)) * 100 
                    AS NUMERIC), 
                    2
                ) as cat_percentage
            FROM student_grade_snapshots
            -- Match the strict filtering from the ingestion script
            WHERE max_points IS NOT NULL 
              AND max_points > 0
              AND assigned_score IS NOT NULL
              AND (is_excused IS FALSE OR is_excused IS NULL)
            GROUP BY snapshot_date, student_id, course_name, category
        ) AS calc
        WHERE target.snapshot_date = calc.snapshot_date
          AND target.student_id = calc.student_id
          AND target.course_name = calc.course_name
          AND target.category IS NOT DISTINCT FROM calc.category;
    """)

    start_time = datetime.now()
    with engine.begin() as conn:
        print("  Executing database update... (this might take a moment for 60k+ rows)")
        result = conn.execute(sql_update)
        print(f"  ✅ Updated {result.rowcount} rows.")

    duration = (datetime.now() - start_time).total_seconds()
    print(f"  Done in {duration:.2f} seconds.")

def populate_overall_grades(engine, table='student_grade_snapshots'):
    print("\n--- Calculating and Populating Overall Grades ---")
    q = lambda sql: text(sql.replace('student_grade_snapshots', table))

    # Snapshots taken before ingest recorded each course's Classroom grading settings
    # (grade_calculation IS NULL): weighted categories, divided by the SUM of active weights.
    # Unchanged from before so historical grades stay as they were.
    sql_legacy = q("""
        UPDATE student_grade_snapshots AS target
        SET overall_grade = calc.final_grade
        FROM (
            SELECT
                snapshot_date,
                student_id,
                course_name,
                ROUND(CAST(
                    SUM(student_grade_in_category * category_weight) / NULLIF(SUM(category_weight), 0)
                AS NUMERIC), 2) as final_grade
            FROM (
                SELECT DISTINCT snapshot_date, student_id, course_name, category, category_weight, student_grade_in_category
                FROM student_grade_snapshots
                WHERE student_grade_in_category IS NOT NULL
                  AND category_weight IS NOT NULL
                  AND category_weight > 0
                  AND grade_calculation IS NULL
            ) unique_categories
            GROUP BY snapshot_date, student_id, course_name
        ) AS calc
        WHERE target.snapshot_date = calc.snapshot_date
          AND target.student_id = calc.student_id
          AND target.course_name = calc.course_name
          AND target.grade_calculation IS NULL;
    """)

    # Older snapshots where the formula above gives nothing (no graded work in a category with a weight,
    # e.g. everything under "No Category"): clear the value. Without this, ingest's own estimate (0) or an
    # older calculation (100) stayed and showed up as a real grade (fixed 2026-10-12; the user agreed).
    sql_legacy_none = q("""
        UPDATE student_grade_snapshots
        SET overall_grade = NULL
        WHERE grade_calculation IS NULL
          AND overall_grade IS NOT NULL
          AND (snapshot_date, student_id, course_name) NOT IN (
              SELECT snapshot_date, student_id, course_name
              FROM student_grade_snapshots
              WHERE grade_calculation IS NULL
                AND student_grade_in_category IS NOT NULL
                AND category_weight IS NOT NULL
                AND category_weight > 0);
    """)

    # Newer snapshots: compute the overall grade the same way Classroom does for that course.
    #   HIDE_OVERALL_GRADE ("No overall grade")  -> NULL (reports show "This Class is Not Graded")
    #   TOTAL_POINTS                             -> sum of points earned / sum of points possible
    #   WEIGHTED_CATEGORIES                      -> weighted categories, as above
    # NULL also when nothing counted has been graded yet (Classroom shows no overall grade then).
    sql_classroom = q("""
        UPDATE student_grade_snapshots AS target
        SET overall_grade = calc.final_grade
        FROM (
            SELECT k.snapshot_date, k.student_id, k.course_name,
                CASE
                    WHEN k.grade_display = 'HIDE_OVERALL_GRADE' THEN NULL
                    WHEN k.grade_calculation = 'TOTAL_POINTS' THEN tp.pct
                    ELSE w.pct
                END AS final_grade
            FROM (
                SELECT snapshot_date, student_id, course_name,
                       MAX(grade_calculation) AS grade_calculation, MAX(grade_display) AS grade_display
                FROM student_grade_snapshots
                WHERE grade_calculation IS NOT NULL
                GROUP BY snapshot_date, student_id, course_name
            ) k
            LEFT JOIN (
                SELECT snapshot_date, student_id, course_name,
                       ROUND(SUM(assigned_score) * 100.0 / NULLIF(SUM(max_points), 0), 2) AS pct
                FROM student_grade_snapshots
                WHERE grade_calculation IS NOT NULL
                  AND max_points > 0
                  AND assigned_score IS NOT NULL
                  AND (is_excused IS FALSE OR is_excused IS NULL)
                GROUP BY snapshot_date, student_id, course_name
            ) tp USING (snapshot_date, student_id, course_name)
            LEFT JOIN (
                SELECT snapshot_date, student_id, course_name,
                       ROUND(SUM(student_grade_in_category * category_weight) / NULLIF(SUM(category_weight), 0), 2) AS pct
                FROM (
                    SELECT DISTINCT snapshot_date, student_id, course_name, category, category_weight, student_grade_in_category
                    FROM student_grade_snapshots
                    WHERE grade_calculation IS NOT NULL
                      AND student_grade_in_category IS NOT NULL
                      AND category_weight > 0
                ) unique_categories
                GROUP BY snapshot_date, student_id, course_name
            ) w USING (snapshot_date, student_id, course_name)
        ) AS calc
        WHERE target.snapshot_date = calc.snapshot_date
          AND target.student_id = calc.student_id
          AND target.course_name = calc.course_name;
    """)

    start_time = datetime.now()
    with engine.begin() as conn:
        print("  Executing database update... (this might take a moment for 60k+ rows)")
        legacy = conn.execute(sql_legacy).rowcount
        cleared = conn.execute(sql_legacy_none).rowcount
        current = conn.execute(sql_classroom).rowcount
        print(f"  ✅ Updated {legacy} rows (older snapshots, weighted categories) "
              f"and {current} rows (using each course's Classroom grading setting).")
        if cleared:
            print(f"  Cleared {cleared} rows in older snapshots with nothing graded in a weighted category "
                  f"(no overall grade, instead of a leftover 0 or 100).")

    duration = (datetime.now() - start_time).total_seconds()
    print(f"  Done in {duration:.2f} seconds.")

def update_teacher_dashboard():
    """Grades are final after this script, so push them to the teacher dashboard right away."""
    print("\n--- Updating Teacher Dashboard ---")
    try:
        import sync_dashboard
        sync_dashboard.sync()
    except Exception as e:
        # The grade calculation already succeeded; the nightly job will retry the dashboard.
        print(f"  ⚠️  Dashboard not updated: {e}")
        print("     Run 'python sync_dashboard.py' later, or wait for the nightly job.")

def main():
    parser = argparse.ArgumentParser(description="Calculate category and overall grades.")
    parser.add_argument('--no-dashboard', action='store_true',
                        help="Don't update the teacher dashboard afterwards (e.g. for test runs).")
    args = parser.parse_args()

    try:
        from backup_database import make_local_backup
        print("--- Backing up the database first ---")
        make_local_backup()

        engine = get_db_engine()
        ensure_column_exists(engine)
        populate_category_grades(engine)
        populate_overall_grades(engine)
        print("\nSuccess! The 'student_grade_in_category' column is now populated.")
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return

    if not args.no_dashboard:
        update_teacher_dashboard()

if __name__ == '__main__':
    main()
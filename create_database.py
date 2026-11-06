'''
Create an empty grades.db (or the file named by DB_PATH in .env) with the tables the scripts need.

Run once on a new install, before import_student_lists.py:
    .venv\\Scripts\\python.exe create_database.py

Safe to run again: existing tables and data are left alone (CREATE TABLE IF NOT EXISTS).
Other tables (live_grades, archived_*, raw student_list_* copies) are created by the scripts
that fill them.

Student name lists
------------------
import_student_lists.py fills the two name-list tables from CSV exports of your student lists.
For the High School list, any CSV column whose name matches a column below (ignoring case) is
copied; the Junior High list uses the fixed columns in import_student_lists.jh_rows().
You can add more columns to Student_Name_List_HS later (e.g. with a SQLite tool) and matching CSV
columns will then be imported too.
'''

from sqlalchemy import text

from db import get_db_engine, get_db_path

SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS "Student_Name_List_HS" (
        semester TEXT,
        "No." INTEGER,
        name TEXT,
        grade TEXT,
        "E Name" TEXT,
        "C Name" TEXT,
        "Home Room" TEXT,
        transferred TEXT,
        gender TEXT,
        "From JH" TEXT,
        "Student email" TEXT,
        "Parent email" TEXT,
        id TEXT,
        google_classroom_name TEXT,
        google_classroom_id TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS student_name_list_jh (
        semester TEXT,
        grade_level TEXT,
        chinese_name TEXT,
        first_name TEXT,
        last_name TEXT,
        gender TEXT,
        english TEXT,
        math TEXT,
        homeroom TEXT,
        passport_name TEXT,
        student_email TEXT,
        father_email TEXT,
        mother_email TEXT,
        google_classroom_name TEXT,
        google_classroom_id TEXT,
        extra_gaurdian_email TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS student_grade_snapshots (
        snapshot_date DATETIME,
        section TEXT,
        grade_level TEXT,
        student_name TEXT,
        course_name TEXT,
        assignment_title TEXT,
        category TEXT,
        category_weight DOUBLE,
        assigned_score DOUBLE,
        max_points DOUBLE,
        is_late BOOLEAN,
        is_excused BOOLEAN,
        state TEXT,
        assignment_id TEXT,
        student_id TEXT,
        overall_grade DOUBLE,
        semester TEXT,
        gender TEXT,
        attended_jh BOOLEAN,
        student_email TEXT,
        student_grade_in_category FLOAT,
        grade_calculation TEXT,
        grade_display TEXT,
        draft_score FLOAT
    )''',
    'CREATE INDEX IF NOT EXISTS idx_student_email ON student_grade_snapshots (student_email)',
    'CREATE INDEX IF NOT EXISTS idx_student_id ON student_grade_snapshots (student_id)',
    'CREATE INDEX IF NOT EXISTS idx_student_name ON student_grade_snapshots (student_name)',
    'CREATE INDEX IF NOT EXISTS idx_section_snapshot ON student_grade_snapshots (section, snapshot_date)',
    'CREATE INDEX IF NOT EXISTS idx_snapshot_date ON student_grade_snapshots (snapshot_date)',
]


def main():
    engine = get_db_engine(must_exist=False)
    with engine.begin() as conn:
        for statement in SCHEMA:
            conn.execute(text(statement))
    print(f"Database ready: {get_db_path()}")


if __name__ == '__main__':
    main()

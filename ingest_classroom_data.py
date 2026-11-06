'''

This script connects to multiple Google Classroom accounts (representing different schools or sections),
fetches active courses, and retrieves student submissions for each assignment.
It attempts to determine the grade level of each student by pre-scanning for "Homeroom" courses and
    mapping students to grades based on course names.
If a grade level cannot be determined during the initial fetch, it is marked as "Unknown".
After all data is collected and saved to the database, a post-processing step scans
    for any "Unknown" grade levels and tries to backfill them using course names that
    contain clues (like "Homeroom" or "PBL").
The script also calculates overall grades for each student in each course,
    ignoring excused assignments, and saves everything to a PostgreSQL database.
Detailed logging and progress bars are included to track the process.

'''

import datetime
import os
import re
import argparse
import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from sqlalchemy import inspect, text

from db import get_db_engine
from course_terms import current_term, split_courses, term_label, log_term_check
from grade_visibility_db import log_visibility_check
from student_selection import add_student_options, selected_students, norm, report_missing
from classroom_api import list_all, NUM_RETRIES
from tqdm import tqdm

# --- CONFIGURATION ---
# SAME credentials.json, DIFFERENT token files
SCHOOL_CONFIGS = [
    {'name': 'High School', 'token_file': 'token_hs.json'},
    {'name': 'Junior High', 'token_file': 'token_jh.json'}
]

SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.students.readonly',
    'https://www.googleapis.com/auth/classroom.rosters.readonly',
    'https://www.googleapis.com/auth/classroom.student-submissions.students.readonly'
]

os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

# --- HELPER FUNCTIONS ---

def get_credentials(token_filename):
    creds = None
    if os.path.exists(token_filename):
        try:
            creds = Credentials.from_authorized_user_file(token_filename, SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
    
        if not creds:
            if not os.path.exists('credentials.json'):
                print(f"Error: credentials.json not found.")
                return None
            
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES) 
            print(f"--- ACTION REQUIRED: Please log in for {token_filename} ---")
            creds = flow.run_local_server(port=0, prompt='consent')
        
        with open(token_filename, 'w') as token:
            token.write(creds.to_json())
        
    return creds

def format_duration(seconds):
    m, s = divmod(seconds, 60)
    return f"{int(m)}m {int(s)}s"

def build_student_grade_map(service, courses):
    """
    Pre-scans courses to find Homerooms and map students to grade levels.
    """
    print("    -> Pre-scanning for Homerooms to determine student grade levels...")
    student_grade_map = {}
    
    homeroom_count = 0
    for course in courses:
        name = course['name']
        if "homeroom" in name.lower():
            match = re.search(r'(?:G|Grade|Year)?\s?(\d{1,2})', name, re.IGNORECASE)
            if match:
                grade_num = int(match.group(1))
                if 5 <= grade_num <= 12:
                    grade_label = f"Grade {grade_num}"
                    homeroom_count += 1
                    try:
                        page_token = None
                        while True:
                            roster = service.courses().students().list(courseId=course['id'], pageToken=page_token).execute(num_retries=NUM_RETRIES)
                            for s in roster.get('students', []):
                                student_grade_map[s['userId']] = grade_label
                            page_token = roster.get('nextPageToken')
                            if not page_token:
                                break
                    except:
                        pass
    
    print(f"    -> Found {homeroom_count} Homeroom courses. Mapped {len(student_grade_map)} students to grades.")
    return student_grade_map

def count_pending_invitations(service):
    """Pending Classroom invitations = classes this account can't see yet. None if the check fails."""
    try:
        return len(list_all(service.invitations().list, 'invitations', userId='me'))
    except Exception:
        return None


def filter_current_term_courses(service, section_name, courses, include_old):
    """
    Drop ACTIVE courses whose names show a past semester (teachers don't always archive them),
    and log anything that suggests this semester's classes are missing.
    """
    term = current_term()
    groups = split_courses([c['name'] for c in courses], term)
    lines = [f"[{section_name}] Current semester: {term_label(term)} | {len(courses)} active courses: "
             f"{len(groups['current'])} this semester, {len(groups['unknown'])} no semester in name, "
             f"{len(groups['future'])} later semester, {len(groups['old'])} past semester"]

    if groups['old']:
        action = "KEPT (--include-old-courses)" if include_old else "EXCLUDED"
        lines.append(f"  {action} - past-semester courses still active in Classroom "
                     f"(ask the teacher to archive them):")
        lines += [f"    - {n}" for n in sorted(groups['old'])]
    if groups['unknown']:
        lines.append("  Kept, but no semester found in the name (check these are current):")
        lines += [f"    - {n}" for n in sorted(groups['unknown'])]
    if groups['future']:
        lines.append("  Kept - named for a later semester:")
        lines += [f"    - {n}" for n in sorted(groups['future'])]
    if not groups['current']:
        lines.append(f"  WARNING: no {term_label(term)} courses found for {section_name}. This semester's "
                     f"classes are probably missing - check for unaccepted invitations.")

    pending = count_pending_invitations(service)
    if pending:
        lines.append(f"  WARNING: {pending} pending Classroom invitation(s) for {section_name} - those classes "
                     f"are NOT included. Run: .venv\\Scripts\\python.exe accept_classroom_invitations.py")
    elif pending is None:
        lines.append("  (Could not check for pending invitations.)")

    log_term_check('ingest_classroom_data.py', lines)

    if include_old:
        return courses
    old = set(groups['old'])
    return [c for c in courses if c['name'] not in old]


# {section: {course name: (calculationType, displaySetting)}} for every current class, filled by
# process_school() from the course list
COURSE_SETTINGS = {}


def process_school(school_config, snapshot_time, target_students=None, target_course=None, course_keywords=None,
                   include_old=False):
    section_name = school_config['name']
    token_file = school_config['token_file']
    targets = {norm(n) for n in target_students} if target_students else None

    print(f"\n=== Processing Section: {section_name} ===")

    creds = get_credentials(token_file)
    if not creds:
        return []

    service = build('classroom', 'v1', credentials=creds)
    courses = list_all(service.courses().list, 'courses', courseStates=['ACTIVE'])
    # Before the homeroom scan, so an old unarchived homeroom can't set a student's grade level
    courses = filter_current_term_courses(service, section_name, courses, include_old)
    # Every current class's grading setting comes with the course list (no extra calls). A
    # student-filtered run applies it to the classes it doesn't fetch (carry_over_other_students).
    COURSE_SETTINGS[section_name] = {
        c['name']: (c.get('gradebookSettings', {}).get('calculationType'),
                    c.get('gradebookSettings', {}).get('displaySetting'))
        for c in courses}

    student_grade_map = build_student_grade_map(service, courses)

    # Apply course filters AFTER grade map is built (grade map needs all courses for homeroom scan)
    if target_course:
        courses = [c for c in courses if c['name'] == target_course]
        print(f"    -> Course filter (exact): '{target_course}' — {len(courses)} course(s) matched.")
    elif course_keywords:
        courses = [c for c in courses if any(kw.lower() in c['name'].lower() for kw in course_keywords)]
        print(f"    -> Course filter (keywords: {course_keywords}) — {len(courses)} course(s) matched.")

    school_records = []

    for i, course in enumerate(courses, 1):
        # MOVE THIS LINE TO THE TOP OF THE LOOP
        course_start = datetime.datetime.now()

        print(f"[{section_name}] [{i}/{len(courses)}] Checking: {course['name']}...")

        try:
            students = {}
            page_token = None
            while True:
                student_results = service.courses().students().list(courseId=course['id'], pageToken=page_token).execute(num_retries=NUM_RETRIES)
                for s in student_results.get('students', []):
                    students[s['userId']] = s['profile']['name']['fullName']
                page_token = student_results.get('nextPageToken')
                if not page_token:
                    break
        except:
            continue

        # Only classes that at least one selected student is in
        if targets and not any(norm(n) in targets for n in students.values()):
            continue

        course_work = list_all(service.courses().courseWork().list, 'courseWork', courseId=course['id'])

        gradebook = course.get('gradebookSettings', {})
        categories = gradebook.get('gradeCategories', [])
        # How Classroom computes this course's overall grade (used by calculate_category_grades.py):
        #   calculationType: WEIGHTED_CATEGORIES (category-based) or TOTAL_POINTS
        #   displaySetting:  SHOW_OVERALL_GRADE, SHOW_TEACHERS_ONLY, or HIDE_OVERALL_GRADE
        #                    ("No overall grade" in Classroom = TOTAL_POINTS + HIDE_OVERALL_GRADE)
        grade_calculation = gradebook.get('calculationType')
        grade_display = gradebook.get('displaySetting')
        cat_map = {c['id']: {'name': c['name'], 'weight': c.get('weight', 0)/10000} for c in categories}

        for work in tqdm(course_work, desc=f"    Fetching {len(course_work)} assignments", unit="asg", leave=False):
            # Now we only call this API for courses Chelsea is actually in
            submissions = list_all(service.courses().courseWork().studentSubmissions().list, 'studentSubmissions',
                                   courseId=course['id'], courseWorkId=work['id'])

            for sub in submissions:
                student_name = students.get(sub['userId'])

                # Double check to ensure we only record data for the target student
                if targets and norm(student_name) not in targets:
                    continue
                if not student_name:
                    continue

                cat_id = work.get('gradeCategory', {}).get('id')
                cat_info = cat_map.get(cat_id, {'name': 'No Category', 'weight': 0})
                cat_weight = cat_info['weight']

                raw_grade = sub.get('assignedGrade')

                # The Classroom API does not natively support 'is_excused'.
                # Defaulting to False to prevent false positives from "Marked as Complete".
                is_excused = False

                # Leave ungraded assignments as None (NULL in database) so they
                # don't falsely appear as zeroes on the report.
                final_score_for_db = raw_grade

                # Lookup Grade Level from pre-scan map
                grade_level = student_grade_map.get(sub['userId'], 'Unknown')

                record = {
                    'snapshot_date': snapshot_time,
                    'section': section_name,    
                    'grade_level': grade_level, 
                    'student_name': student_name,
                    'course_name': course['name'],
                    'assignment_title': work['title'],
                    'category': cat_info['name'],
                    'category_weight': cat_weight,
                    'assigned_score': final_score_for_db,
                    # Teacher's draft grade (not yet returned to the student). Only COUNTED on the staff
                    # dashboard ("graded, not returned yet"); never used in any grade or report.
                    'draft_score': sub.get('draftGrade'),
                    'max_points': work.get('maxPoints'),
                    'is_late': sub.get('late', False),
                    'is_excused': is_excused,
                    'state': sub.get('state'), 
                    'assignment_id': work['id'],
                    'student_id': sub['userId'],
                    'grade_calculation': grade_calculation,
                    'grade_display': grade_display,
                }
                school_records.append(record)

        course_end = datetime.datetime.now()
        duration_sec = (course_end - course_start).total_seconds()
        print(f"    -> Done in {format_duration(duration_sec)}.")

    return school_records

# --- NEW FUNCTION: POST-PROCESS GRADE LEVELS ---
def post_process_grade_levels(engine, snapshot_time):
    """
    Scans the data just inserted for 'Unknown' grades and fixes them 
    using 'PBL' or 'Homeroom' course names found in the database.
    """
    print("\n--- Running Post-Process: Fix Missing Grade Levels ---")
    
    # 1. Get all students and their courses for this specific snapshot
    query = text("""
        SELECT DISTINCT student_id, course_name 
        FROM student_grade_snapshots 
        WHERE snapshot_date = :snap
    """)
    
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params={"snap": snapshot_time})

    # 2. Build a map of Student ID -> Detected Grade based on course names
    student_grade_map = {}
    keywords = ['homeroom', 'pbl']
    
    for _, row in df.iterrows():
        c_name = row['course_name']
        # Check if course name looks interesting
        if any(k in c_name.lower() for k in keywords):
            # Regex for G9, G12, Grade 9, etc.
            match = re.search(r'(?:G|Grade|Year)\s?(\d{1,2})', c_name, re.IGNORECASE)
            if match:
                grade_num = int(match.group(1))
                if 5 <= grade_num <= 12:
                    student_grade_map[row['student_id']] = f"Grade {grade_num}"

    if not student_grade_map:
        print("  -> No additional grade level clues found.")
        return

    print(f"  -> Detected grade levels for {len(student_grade_map)} students based on course names.")

    # 3. Update the database
    update_count = 0
    with engine.begin() as conn: 
        for student_id, grade_label in tqdm(student_grade_map.items(), desc="  Updating DB", unit="student"):
            update_query = text("""
                UPDATE student_grade_snapshots
                SET grade_level = :grade
                WHERE student_id = :sid 
                  AND snapshot_date = :snap 
                  AND (grade_level = 'Unknown' OR grade_level IS NULL)
            """)
            result = conn.execute(update_query, {
                "grade": grade_label,
                "sid": student_id,
                "snap": snapshot_time
            })
            update_count += result.rowcount

    print(f"  -> Successfully backfilled {update_count} rows.")

def carry_over_other_students(engine, df, snapshot_time):
    """
    Student-filtered run (--student / --students-file): copy every OTHER student's rows from the
    section's previous latest snapshot into this new snapshot. Reports, the dashboard and the
    enrich scripts always use the latest snapshot, so without this it would contain only the
    selected students. Classes fetched in this run also get their current Classroom grading
    setting on the copied rows, so each class has one setting.
    """
    with engine.begin() as conn:
        cols = [c['name'] for c in inspect(conn).get_columns('student_grade_snapshots') if c['name'] != 'snapshot_date']
        col_sql = ', '.join(f'"{c}"' for c in cols)
        for section, sdf in df.groupby('section'):
            prev = conn.execute(text(
                "SELECT MAX(snapshot_date) FROM student_grade_snapshots WHERE section = :sec AND snapshot_date < :snap"
            ), {'sec': section, 'snap': snapshot_time}).scalar()
            if not prev:
                print(f"  [{section}] No earlier snapshot to copy other students from - this snapshot has only the selected students.")
                continue
            ids = sorted(set(sdf['student_id']))
            id_params = {f'id{i}': v for i, v in enumerate(ids)}
            not_in = ', '.join(f':id{i}' for i in range(len(ids)))
            copied = conn.execute(text(
                f'INSERT INTO student_grade_snapshots ("snapshot_date", {col_sql}) '
                f'SELECT :snap, {col_sql} FROM student_grade_snapshots '
                f'WHERE section = :sec AND snapshot_date = :prev AND student_id NOT IN ({not_in})'
            ), {'snap': snapshot_time, 'sec': section, 'prev': prev, **id_params}).rowcount
            others = conn.execute(text(
                f"SELECT COUNT(DISTINCT student_id) FROM student_grade_snapshots "
                f"WHERE section = :sec AND snapshot_date = :snap AND student_id NOT IN ({not_in})"
            ), {'snap': snapshot_time, 'sec': section, **id_params}).scalar()
            # Every class gets its CURRENT Classroom grading setting - also the classes this run
            # didn't fetch (no selected student in them), whose rows were copied with the old one.
            settings = dict(COURSE_SETTINGS.get(section, {}))
            for course, row in sdf.groupby('course_name')[['grade_calculation', 'grade_display']].first().iterrows():
                settings[course] = (row['grade_calculation'], row['grade_display'])
            for course, (calc, disp) in settings.items():
                conn.execute(text(
                    "UPDATE student_grade_snapshots SET grade_calculation = :calc, grade_display = :disp "
                    "WHERE section = :sec AND snapshot_date = :snap AND course_name = :course"
                ), {'calc': calc, 'disp': disp, 'sec': section, 'snap': snapshot_time, 'course': course})
            print(f"  [{section}] Copied the other {others} students' grades from the {str(prev)[:16]} snapshot "
                  f"({copied} rows) so this snapshot is complete. Only the selected students' grades are new.")


def main():
    # --- ADD ARGUMENT PARSING ---
    parser = argparse.ArgumentParser(description="Ingest Google Classroom Data")
    add_student_options(parser, 'Only fetch this student')
    parser.add_argument('--school', type=str, choices=['High School', 'Junior High'], help="Limit to one school section")
    parser.add_argument('--course', type=str, help="Exact course name to fetch (e.g. 'English 10 - Period 2')")
    parser.add_argument('--course-filter', type=str, nargs='+', metavar='KEYWORD',
                        help="One or more keywords to match against course names (case-insensitive, e.g. English literature)")
    parser.add_argument('--include-old-courses', action='store_true',
                        help="Also fetch active courses whose names show a past semester (excluded by default)")
    args = parser.parse_args()

    total_start_time = datetime.datetime.now()
    print(f"--- Starting Multi-School Snapshot: {total_start_time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    students = selected_students(args)
    if students:
        print(f"*** FILTER ACTIVE: {len(students)} student(s) = {', '.join(students)} ***")
        print("    (Other students' latest grades are copied into this snapshot so it stays complete.)")
    if args.course:
        print(f"*** FILTER ACTIVE: Course (exact) = '{args.course}' ***")
    if args.course_filter:
        print(f"*** FILTER ACTIVE: Course keywords = {args.course_filter} ***")

    snapshot_time = datetime.datetime.now()
    all_combined_records = []

    # --- FILTER SCHOOLS BASED ON ARGUMENT ---
    configs_to_run = SCHOOL_CONFIGS
    if args.school:
        configs_to_run = [c for c in SCHOOL_CONFIGS if c['name'] == args.school]

    for config in configs_to_run:
        # Pass the selected students (if any) here
        records = process_school(config, snapshot_time, target_students=students,
                                 target_course=args.course, course_keywords=args.course_filter,
                                 include_old=args.include_old_courses)
        all_combined_records.extend(records)

    if students:
        report_missing(students, {r['student_name'] for r in all_combined_records}, 'Google Classroom grades')

    if not all_combined_records:
        print("No grades found for any school (or for the specified student).")
        return

    # --- CALCULATE OVERALL GRADES ---
    print("Calculating final proportional grades...")
    df = pd.DataFrame(all_combined_records)

    # Ensure numeric columns
    df['max_points'] = pd.to_numeric(df['max_points'], errors='coerce').fillna(0)
    df['assigned_score'] = pd.to_numeric(df['assigned_score'], errors='coerce')
    df['draft_score'] = pd.to_numeric(df['draft_score'], errors='coerce')   # FLOAT column (added if missing)

    # 1. Filter out everything except valid, RETURNED, unexcused grades
    valid_df = df[
        (df['state'] == 'RETURNED') &
        (df['assigned_score'].notna()) &
        (df['is_excused'] != True) &
        (df['max_points'] > 0)
    ]

    if not valid_df.empty:
        # 2. Calculate Category Percentages
        cat_group = valid_df.groupby(['student_id', 'course_name', 'category', 'category_weight']).agg(
            earned=('assigned_score', 'sum'),
            max_pts=('max_points', 'sum')
        ).reset_index()

        cat_group['cat_pct'] = (cat_group['earned'] / cat_group['max_pts']) * 100

        # 3. Calculate Overall Grades using proportional weighting
        cat_group['weight_cont'] = cat_group['cat_pct'] * cat_group['category_weight']

        course_group = cat_group.groupby(['student_id', 'course_name']).agg(
            total_weight_cont=('weight_cont', 'sum'),
            active_weights=('category_weight', 'sum')
        ).reset_index()

        course_group['calc_overall_grade'] = (course_group['total_weight_cont'] / course_group['active_weights']).round(2)

        # 4. Map the newly calculated grades back to the main dataframe
        grade_map = course_group.set_index(['student_id', 'course_name'])['calc_overall_grade'].to_dict()

        # Apply the correct overall grade to all rows for that student+course combination
        df['overall_grade'] = df.set_index(['student_id', 'course_name']).index.map(grade_map)
        df['overall_grade'] = df['overall_grade'].fillna(0)
    else:
        df['overall_grade'] = 0.0

    # --- SAVE TO DB ---
    engine = get_db_engine()

    inspector = inspect(engine)
    if inspector.has_table('student_grade_snapshots'):
        existing_cols = [c['name'] for c in inspector.get_columns('student_grade_snapshots')]
        for col in df.columns:
            if col not in existing_cols:
                print(f"Adding missing column: {col}")
                dtype = 'TEXT'
                if pd.api.types.is_float_dtype(df[col]): dtype = 'FLOAT'
                elif pd.api.types.is_datetime64_any_dtype(df[col]): dtype = 'TIMESTAMP'
                elif pd.api.types.is_bool_dtype(df[col]): dtype = 'BOOLEAN'
                with engine.connect() as conn:
                    conn.execute(text(f'ALTER TABLE student_grade_snapshots ADD COLUMN "{col}" {dtype}'))
                    conn.commit()

    print(f"Writing {len(df)} records to database...")
    df.to_sql('student_grade_snapshots', engine, if_exists='append', index=False)
    if students:
        carry_over_other_students(engine, df, snapshot_time)
    
    # --- RUN POST-PROCESS ---
    # This runs immediately after saving, using the same timestamp
    post_process_grade_levels(engine, snapshot_time)

    # Courses whose overall grade won't appear on parent reports (teacher's Classroom setting)
    print()
    log_visibility_check('ingest_classroom_data.py', engine, sorted(df['section'].dropna().unique()))

    # --- FINAL STATS ---
    total_end_time = datetime.datetime.now()
    total_duration = (total_end_time - total_start_time).total_seconds()
    
    print("="*40)
    print("SNAPSHOT COMPLETE")
    print(f"Total Time:       {format_duration(total_duration)}")
    print(f"Total Records:    {len(df)}")
    print("="*40)

if __name__ == '__main__':
    main()
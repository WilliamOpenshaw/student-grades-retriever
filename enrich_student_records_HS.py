'''
Script to enrich student records for High School by syncing data from Google Classroom,
linking to the official student name list, and performing data audits and cleanups.

'''

import os
import json
import re
import pandas as pd
from sqlalchemy import text, inspect
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from difflib import get_close_matches
from datetime import datetime

from db import get_db_engine, parse_db_datetime, latest_snapshots_by_section, latest_snapshot_filter
from classroom_api import list_all
from tqdm import tqdm

# --- CONFIGURATION ---
SCHOOL_CONFIGS = [
    {'name': 'High School', 'token_file': 'token_hs.json'},
    {'name': 'Junior High', 'token_file': 'token_jh.json'}
]

SCOPES = [
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.rosters.readonly',
    'https://www.googleapis.com/auth/classroom.profile.emails',
]

MATCH_CUTOFF = 0.4  # For fuzzy name matching

# --- DATABASE & UTILS ---

def get_credentials(token_filename):
    creds = None
    if os.path.exists(token_filename):
        try:
            creds = Credentials.from_authorized_user_file(token_filename, SCOPES)
        except: creds = None

    # The token file is shared with ingest_classroom_data.py, which saves it with a narrower
    # access token (no classroom.profile.emails). Reusing that still-valid access token makes
    # Classroom return students WITHOUT emails and no error, so refresh to get one with our scopes.
    granted = set()
    try:
        with open(token_filename) as f:
            granted = set(json.load(f).get('scopes') or [])
    except Exception:
        pass
    needs_scopes = not set(SCOPES).issubset(granted)

    if creds and creds.refresh_token and (not creds.valid or needs_scopes):
        try:
            creds.refresh(Request())
        except Exception as e:
            print(f"  ⚠️  Could not refresh {token_filename} ({e}). Run authenticate.py to sign in again.")
            creds = None
    elif creds and not creds.valid:
        creds = None
    return creds

def get_semester_string(date_obj):
    date_obj = parse_db_datetime(date_obj)
    if date_obj.month == 1:
        return f"{date_obj.year - 1} Fall"
    elif date_obj.month >= 8:  # school starts late August (same rule as phase_populate_semesters)
        return f"{date_obj.year} Fall"
    else:
        return f"{date_obj.year} Spring"

def get_latest_snapshots(engine, section='High School'):
    """Latest snapshot per section, plus the semester of this school's latest snapshot."""
    with engine.connect() as conn:
        latest = latest_snapshots_by_section(conn)
    if not latest:
        return {}, None
    return latest, get_semester_string(latest.get(section) or max(latest.values()))

def get_grade_number(grade_str):
    if not grade_str: return 0
    match = re.search(r'\d+', str(grade_str))
    return int(match.group(0)) if match else 0

def ensure_columns_exist(engine):
    print("--- Checking Database Schema ---")
    with engine.connect() as conn:
        cols_to_check = [
            ('student_grade_snapshots', 'student_email', 'TEXT'),
            ('student_grade_snapshots', 'gender', 'TEXT'),
            ('student_grade_snapshots', 'attended_jh', 'BOOLEAN'),
            ('Student_Name_List_HS', 'google_classroom_name', 'TEXT'),
            ('Student_Name_List_HS', 'google_classroom_id', 'TEXT')
        ]
        inspector = inspect(engine)
        for table, col, dtype in cols_to_check:
            if col not in [c['name'] for c in inspector.get_columns(table)]:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {dtype}'))
        conn.commit()

# --- PHASE 1: FETCH EMAILS ---

def phase_1_sync_emails(engine):
    print("\n" + "="*50)
    print("PHASE 1: Syncing Emails from Google Classroom")
    print("="*50)

    student_email_map = {} # google_id -> email

    for config in SCHOOL_CONFIGS:
        creds = get_credentials(config['token_file'])
        if not creds:
            print(f"  ⚠️  Skipping {config['name']} (No credentials)")
            continue

        service = build('classroom', 'v1', credentials=creds)
        print(f"  Scanning {config['name']} courses...")

        try:
            courses = list_all(service.courses().list, 'courses', courseStates=['ACTIVE'])
            seen, with_email, errors = 0, 0, []
            for course in tqdm(courses, desc=f"    {config['name']}", unit="crs"):
                try:
                    students = list_all(service.courses().students().list, 'students', courseId=course['id'])
                    for s in students:
                        uid = s.get('userId')
                        email = s.get('profile', {}).get('emailAddress')
                        seen += 1
                        if uid and email:
                            student_email_map[uid] = email
                            with_email += 1
                except Exception as e:
                    errors.append(f"{course.get('name')}: {e}")
            print(f"    {seen} enrollments, {with_email} with an email address")
            if seen and not with_email:
                print(f"    ⚠️  {config['name']}: Classroom returned students but no emails. "
                      f"Run authenticate.py to re-grant the 'view student emails' permission.")
            for err in errors[:5]:
                print(f"    ⚠️  {err}")
            if len(errors) > 5:
                print(f"    ⚠️  ...and {len(errors) - 5} more course errors")
        except Exception as e:
            print(f"    Error: {e}")

    if not student_email_map:
        print("  ⚠️  No emails found (check authentication).")
        return

    print(f"  Writing {len(student_email_map)} emails to database...")
    with engine.begin() as conn:
        for sid, email in tqdm(student_email_map.items(), desc="    Updating DB", unit="rec"):
            conn.execute(text("""
                UPDATE student_grade_snapshots 
                SET student_email = :email 
                WHERE student_id = :sid AND (student_email IS NULL OR student_email != :email)
            """), {"email": email, "sid": sid})
    print("  ✅ Phase 1 Complete.")

# --- PHASE 2: LINK TABLES ---

def phase_2_link_tables(engine):
    print("\n" + "="*50)
    print("PHASE 2: Linking Snapshots to Name List (HS) via Email")
    print("="*50)

    latest, semester = get_latest_snapshots(engine)

    if not latest:
        print("  ❌ No snapshot data found.")
        return

    print(f"  Target Semester: {semester}")
    snap_cond, snap_params = latest_snapshot_filter(latest)

    # 1. Get Unmatched Students from Google Snapshots (latest snapshot of each section)
    query_google = text(f"""
        SELECT DISTINCT sgs.student_name, sgs.student_id, sgs.student_email
        FROM student_grade_snapshots sgs
        WHERE {snap_cond}
          AND (sgs.grade_level IN ('Grade 9', 'Grade 10', 'Grade 11', 'Grade 12')
               -- New students outside homeroom/PBL classes have no grade level until phase 3
               -- fills it in from this list, so include them (linking needs an exact email match).
               OR (sgs.section = 'High School'
                   AND (sgs.grade_level IS NULL OR sgs.grade_level IN ('', 'Unknown'))))
          AND NOT EXISTS (
              SELECT 1 FROM "Student_Name_List_HS" snl 
              WHERE snl."google_classroom_id" = sgs.student_id
          )
    """)

    with engine.connect() as conn:
        df_google = pd.read_sql(query_google, conn, params=snap_params)

    if df_google.empty:
        print("  🎉 All students are already linked!")
        return

    print(f"  Found {len(df_google)} unmatched students. Syncing via email...")

    # 2. Update Name List using Email as the Key
    # We loop through every unmatched student found in the snapshots
    success_count = 0
    with engine.begin() as conn:
        for _, row in df_google.iterrows():
            g_email = str(row['student_email']).strip().lower()
            g_id = row['student_id']
            g_name = row['student_name']

            if not g_email or g_email == 'nan':
                continue

            # This SQL finds the student in your list by email and saves the Google ID
            result = conn.execute(text("""
                UPDATE "Student_Name_List_HS" 
                SET "google_classroom_id" = :g_id, 
                    "google_classroom_name" = :g_name
                WHERE LOWER(TRIM("Student email")) = :email
                  AND semester = :sem
            """), {
                "g_id": g_id,
                "g_name": g_name,
                "email": g_email,
                "sem": semester
            })

            if result.rowcount > 0:
                success_count += result.rowcount

    print(f"  ✅ Phase 2 Complete. Linked {success_count} students to the Name List using emails.")

# --- PHASE 3: ENRICH SNAPSHOTS ---

def phase_3_enrich_snapshots(engine):
    print("\n" + "="*50)
    print("PHASE 3: Enriching Snapshot Data (Gender, JH, Grade)")
    print("="*50)

    # Need semester to correctly link via email if ID link is missing
    _, semester = get_latest_snapshots(engine)
    if semester:
        print(f"  Using semester '{semester}' for enrichment.")

    with engine.connect() as conn:
        print("  Updating Gender & JH Status...")
        result = conn.execute(text("""
            UPDATE student_grade_snapshots AS sgs
            SET 
                gender = snl.gender,
                attended_jh = (CASE WHEN snl."From JH" = 'V' THEN TRUE ELSE FALSE END)
            FROM "Student_Name_List_HS" snl
            WHERE sgs.student_id = snl.google_classroom_id
              AND (sgs.gender IS NULL OR sgs.attended_jh IS NULL)
        """))
        conn.commit()
        print(f"    -> Updated {result.rowcount} rows (ID Link).")

        print("  Fixing unknown grade levels via ID Link...")
        result = conn.execute(text(r"""
            UPDATE student_grade_snapshots AS sgs
            SET grade_level = CASE 
                    WHEN snl.grade REGEXP '^\d+$' THEN 'Grade ' || snl.grade 
                    ELSE snl.grade 
                END
            FROM "Student_Name_List_HS" snl
            WHERE sgs.student_id = snl.google_classroom_id
              AND (sgs.grade_level IS NULL OR sgs.grade_level = 'Unknown' OR sgs.grade_level = '')
        """))
        conn.commit()
        print(f"    -> Fixed {result.rowcount} rows (ID Link).")

        # --- NEW: Email-based fallback for Unknown Grades ---
        if semester:
            print("  Fixing unknown grade levels via Email Link (Fallback)...")
            result = conn.execute(text(r"""
                UPDATE student_grade_snapshots AS sgs
                SET grade_level = CASE 
                        WHEN snl.grade REGEXP '^\d+$' THEN 'Grade ' || snl.grade 
                        ELSE snl.grade 
                    END
                FROM "Student_Name_List_HS" snl
                WHERE LOWER(TRIM(sgs.student_email)) = LOWER(TRIM(snl."Student email"))
                  AND snl.semester = :sem
                  AND (sgs.grade_level IS NULL OR sgs.grade_level = 'Unknown' OR sgs.grade_level = '' OR sgs.grade_level = 'nan')
            """), {"sem": semester})
            conn.commit()
            print(f"    -> Fixed {result.rowcount} rows (Email Link).")

    print("  ✅ Phase 3 Complete.")

def phase_populate_semesters(engine):
    print("\n==================================================")
    print("PHASE: Populating Semester Column")
    print("==================================================")

    # First, make sure the column exists
    with engine.begin() as conn:
        inspector = inspect(engine)
        if 'semester' not in [c['name'] for c in inspector.get_columns('student_grade_snapshots')]:
            print("  Adding 'semester' column...")
            conn.execute(text('ALTER TABLE student_grade_snapshots ADD COLUMN semester TEXT'))

    # Run the SQL logic to calculate the semester based on snapshot_date
    query = text("""
        UPDATE student_grade_snapshots
        SET semester = 
            CASE 
                WHEN CAST(strftime('%m', snapshot_date) AS INTEGER) >= 8 THEN strftime('%Y', snapshot_date) || ' Fall'
                WHEN CAST(strftime('%m', snapshot_date) AS INTEGER) = 1 THEN (CAST(strftime('%Y', snapshot_date) AS INTEGER) - 1) || ' Fall'
                ELSE strftime('%Y', snapshot_date) || ' Spring'
            END
        WHERE semester IS NULL OR semester = '';
    """)

    with engine.begin() as conn:
        result = conn.execute(query)
        print(f"  ✅ Assigned semester to {result.rowcount} rows.")

# --- PHASE 4: GRADE LEVEL AUDIT & CLEANUP ---

def phase_4_audit_grades(engine):
    print("\n" + "="*50)
    print("PHASE 4: Grade Level Audit & Cleanup")
    print("="*50)

    with engine.begin() as conn:
        # 1. Standardize "G10", "10", "Year 10", "G09" -> "Grade 10"
        total_fixed = 0

        for g in range(5, 13):
            target_grade = f"Grade {g}"

            # This regex matches:
            # - Optional "g", "grade", "year" prefix
            # - Optional spaces
            # - Optional leading zeros (handling '09')
            # - The number
            regex_pattern = rf"(?i)^\s*(g|grade|year)?\s*0*{g}$"

            query = text(f"""
                UPDATE student_grade_snapshots
                SET grade_level = :target
                WHERE grade_level REGEXP :pattern
                  AND grade_level != :target
            """)

            result = conn.execute(query, {
                "target": target_grade,
                "pattern": regex_pattern
            })

            if result.rowcount > 0:
                print(f"  Fixed {result.rowcount} rows to '{target_grade}'")
                total_fixed += result.rowcount

        print(f"  Total rows standardized: {total_fixed}")

        # 2. Report remaining issues
        print("\n  [Audit Report]")

        # Check for NULLs
        null_count = conn.execute(text("SELECT COUNT(*) FROM student_grade_snapshots WHERE grade_level IS NULL")).scalar()

        # Check for invalid formats (not "Grade X")
        # Strict check on the final format
        invalid_query = text("""
            SELECT grade_level, COUNT(*) 
            FROM student_grade_snapshots 
            WHERE grade_level NOT REGEXP '^Grade (5|6|7|8|9|10|11|12)$'
              AND grade_level IS NOT NULL
            GROUP BY grade_level
        """)
        invalid_rows = conn.execute(invalid_query).fetchall()

        if null_count == 0 and not invalid_rows:
            print("  ✅ All grade levels are valid and formatted correctly!")
        else:
            if null_count > 0:
                print(f"  ⚠️  {null_count} rows have NULL grade_level.")

            if invalid_rows:
                print("  ⚠️  Found irregular grade formats:")
                for grade, count in invalid_rows:
                    print(f"     - '{grade}': {count} rows")

    print("  ✅ Phase 4 Complete.")

def main():
    engine = get_db_engine()
    ensure_columns_exist(engine)

    phase_1_sync_emails(engine)
    phase_2_link_tables(engine)
    phase_3_enrich_snapshots(engine)
    phase_populate_semesters(engine)
    phase_4_audit_grades(engine)

    print("\nAll sync operations finished successfully.")

if __name__ == '__main__':
    main()
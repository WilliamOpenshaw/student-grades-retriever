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
# Target Table Name for JH
JH_TABLE_NAME = "student_name_list_jh"

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

def get_latest_snapshots(engine, section='Junior High'):
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
            # Ensure the JH Name List has the linking columns
            (JH_TABLE_NAME, 'google_classroom_name', 'TEXT'),
            (JH_TABLE_NAME, 'google_classroom_id', 'TEXT')
        ]
        inspector = inspect(engine)
        for table, col, dtype in cols_to_check:
            if col not in [c['name'] for c in inspector.get_columns(table)]:
                conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {dtype}'))
        conn.commit()

# --- PHASE 1: FETCH EMAILS ---
# This matches the HS script logic (syncs all emails found in tokens)

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

# --- PHASE 2: LINK TABLES (JH) ---

def phase_2_link_tables(engine):
    print("\n" + "="*50)
    print("PHASE 2: Linking Snapshots to Name List (JH)")
    print("="*50)

    latest, semester = get_latest_snapshots(engine)

    if not latest: return
    print(f"  Target Semester: {semester}")
    snap_cond, snap_params = latest_snapshot_filter(latest)

    # Get Unmatched Google Students (Targeting Grades 5-8) from the latest snapshot of each section
    # Note: We filter roughly for Grade 5-8 strings or 'G5'-'G8'
    query_google = text(f"""
        SELECT DISTINCT sgs.student_name, sgs.student_id, sgs.grade_level, sgs.student_email
        FROM student_grade_snapshots sgs
        WHERE {snap_cond}
          AND (
               sgs.grade_level LIKE 'Grade 5%' OR sgs.grade_level LIKE 'Grade 6%' OR 
               sgs.grade_level LIKE 'Grade 7%' OR sgs.grade_level LIKE 'Grade 8%' OR
               sgs.grade_level = 'G5' OR sgs.grade_level = 'G6' OR 
               sgs.grade_level = 'G7' OR sgs.grade_level = 'G8'
          )
          AND NOT EXISTS (
              SELECT 1 FROM student_name_list_jh snl 
              WHERE snl."google_classroom_id" = sgs.student_id
          )
    """)

    with engine.connect() as conn:
        df_google = pd.read_sql(query_google, conn, params=snap_params)

    # Read JH Name List
    # We construct 'Full Name' for matching logic
    query_list = text(f"""
        SELECT student_email, first_name, last_name, grade_level
        FROM {JH_TABLE_NAME}
        WHERE semester = :sem
    """)
    with engine.connect() as conn:
        df_list = pd.read_sql(query_list, conn, params={"sem": semester})

    official_names_map = {}
    official_email_map = {}

    for idx, row in df_list.iterrows():
        # Construct full name: "First Last"
        fname = str(row['first_name']).strip()
        lname = str(row['last_name']).strip()
        full_name = f"{fname} {lname}".lower()

        if full_name: official_names_map[full_name] = row

        e = str(row['student_email']).strip().lower()
        if e and e not in ['nan', 'none', '']: official_email_map[e] = row

    if df_google.empty:
        print("  🎉 All JH students are already linked!")
        return

    print(f"  Found {len(df_google)} unmatched students. Attempting to link...")

    matches_to_save = []
    matched_indices = set()

    # 2A. Email Matching
    for idx, row in df_google.iterrows():
        g_email = str(row['student_email']).strip().lower()
        if g_email in official_email_map:
            target = official_email_map[g_email]
            matches_to_save.append({
                'email': target['student_email'], # Use email as key since JH list might not have a stable ID column in CSV
                'g_id': row['student_id'],
                'g_name': row['student_name']
            })
            matched_indices.add(idx)

    email_links = len(matches_to_save)
    print(f"    - Linked by Email: {email_links}")

    # 2B. Exact Name + Grade Matching
    for idx, row in df_google.iterrows():
        if idx in matched_indices: continue

        g_name = str(row['student_name']).strip().lower()
        g_grade = get_grade_number(row['grade_level'])

        if g_name in official_names_map:
            target = official_names_map[g_name]
            if get_grade_number(target['grade_level']) == g_grade:
                matches_to_save.append({
                    'email': target['student_email'],
                    'g_id': row['student_id'],
                    'g_name': row['student_name']
                })
                matched_indices.add(idx)

    print(f"    - Linked by Name+Grade: {len(matches_to_save) - email_links} (Total: {len(matches_to_save)})")

    # 2C. Manual Review
    manual_rows = df_google[~df_google.index.isin(matched_indices)]
    if not manual_rows.empty:
        print(f"\n  --- Manual Review Needed for {len(manual_rows)} Students ---")
        for idx, row in manual_rows.iterrows():
            g_name = row['student_name']
            g_grade_num = get_grade_number(row['grade_level'])

            print(f"  🔹 Analyzing: {g_name} ({row['grade_level']}) - Email: {row['student_email']}")

            # Filter candidates by grade proximity
            candidates = [k for k,v in official_names_map.items() if abs(get_grade_number(v['grade_level']) - g_grade_num) <= 1]
            matches = get_close_matches(g_name.lower(), candidates, n=3, cutoff=MATCH_CUTOFF)

            if not matches:
                print("     ❌ No likely matches found.")
                continue

            target = official_names_map[matches[0]]
            print(f"     👉 Potential Match: {target['first_name']} {target['last_name']} ({target['grade_level']})")

            ans = input("     Link this student? (y/n/skip): ").lower().strip()
            if ans == 'y':
                matches_to_save.append({
                    'email': target['student_email'],
                    'g_id': row['student_id'],
                    'g_name': row['student_name']
                })
                print("     ✅ Added.")
            else:
                print("     Ignored.")

    if matches_to_save:
        print(f"\n  Saving {len(matches_to_save)} links to DB...")
        with engine.begin() as conn:
            for m in matches_to_save:
                # Update using student_email as the unique key for the JH list
                conn.execute(text(f"""
                    UPDATE {JH_TABLE_NAME}
                    SET "google_classroom_id" = :g_id, "google_classroom_name" = :g_name
                    WHERE "student_email" = :email
                """), m)
    print("  ✅ Phase 2 Complete.")

# --- PHASE 3: ENRICH SNAPSHOTS ---

def phase_3_enrich_snapshots(engine):
    print("\n" + "="*50)
    print("PHASE 3: Enriching Snapshot Data (Gender, JH Status, Grade)")
    print("="*50)

    _, semester = get_latest_snapshots(engine)

    with engine.connect() as conn:
        print("  Updating Gender & JH Status...")
        # Logic: If they are in JH list, they attended JH (TRUE)
        result = conn.execute(text(f"""
            UPDATE student_grade_snapshots AS sgs
            SET 
                gender = snl.gender,
                attended_jh = TRUE
            FROM {JH_TABLE_NAME} snl
            WHERE sgs.student_id = snl.google_classroom_id
              AND (sgs.gender IS NULL OR sgs.attended_jh IS NULL OR sgs.attended_jh = FALSE)
        """))
        conn.commit()
        print(f"    -> Updated {result.rowcount} rows (ID Link).")

        print("  Fixing unknown grade levels via ID Link...")
        # Logic: JH list uses 'G5', 'G6'. We convert to 'Grade 5', 'Grade 6' if needed.
        result = conn.execute(text(f"""
            UPDATE student_grade_snapshots AS sgs
            SET grade_level = CASE 
                    WHEN snl.grade_level REGEXP '^G\\d+$' THEN 'Grade ' || SUBSTRING(snl.grade_level, 2)
                    WHEN snl.grade_level REGEXP '^\\d+$' THEN 'Grade ' || snl.grade_level
                    ELSE snl.grade_level 
                END
            FROM {JH_TABLE_NAME} snl
            WHERE sgs.student_id = snl.google_classroom_id
              AND (sgs.grade_level IS NULL OR sgs.grade_level = 'Unknown' OR sgs.grade_level = '')
        """))
        conn.commit()
        print(f"    -> Fixed {result.rowcount} rows (ID Link).")

        # Fallback via Email
        if semester:
            print("  Fixing unknown grade levels via Email Link (Fallback)...")
            result = conn.execute(text(f"""
                UPDATE student_grade_snapshots AS sgs
                SET grade_level = CASE 
                        WHEN snl.grade_level REGEXP '^G\\d+$' THEN 'Grade ' || SUBSTRING(snl.grade_level, 2)
                        WHEN snl.grade_level REGEXP '^\\d+$' THEN 'Grade ' || snl.grade_level
                        ELSE snl.grade_level 
                    END
                FROM {JH_TABLE_NAME} snl
                WHERE LOWER(TRIM(sgs.student_email)) = LOWER(TRIM(snl.student_email))
                  AND snl.semester = :sem
                  AND (sgs.grade_level IS NULL OR sgs.grade_level = 'Unknown' OR sgs.grade_level = '')
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
# This phase is general and cleans up both HS and JH formats in the snapshots table.

def phase_4_audit_grades(engine):
    print("\n" + "="*50)
    print("PHASE 4: Grade Level Audit & Cleanup")
    print("="*50)

    with engine.begin() as conn:
        # Standardize "G5", "5", "Year 5" -> "Grade 5" (Covers 5-12)
        total_fixed = 0

        for g in range(5, 13):
            target_grade = f"Grade {g}"
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

        # Report remaining issues
        print("\n  [Audit Report]")

        null_count = conn.execute(text("SELECT COUNT(*) FROM student_grade_snapshots WHERE grade_level IS NULL")).scalar()

        # Check for invalid formats
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
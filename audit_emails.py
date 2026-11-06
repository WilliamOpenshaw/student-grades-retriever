import os
import argparse
import re
import pandas as pd
from sqlalchemy import text
from db import get_db_engine as get_engine
from report_exclusions import load_exclusions
from report_folders import resolve_folder, report_files

# --- CONFIGURATION ---

def get_latest_semester(engine):
    """Fetches all semesters from the DB and determines the most recent one chronologically."""
    query = text('SELECT DISTINCT semester FROM "Student_Name_List_HS" WHERE semester IS NOT NULL')

    with engine.connect() as conn:
        semesters = [str(row[0]).strip() for row in conn.execute(query).fetchall()
                     if row[0] and str(row[0]).lower() != 'nan']

    if not semesters:
        return None

    def semester_sort_key(sem_str):
        # Extract the 4-digit year
        year_match = re.search(r'\d{4}', sem_str)
        year = int(year_match.group(0)) if year_match else 0

        # Fall happens later in the calendar year than Spring
        sem_lower = sem_str.lower()
        if 'fall' in sem_lower:
            season_val = 2
        elif 'spring' in sem_lower:
            season_val = 1
        else:
            season_val = 0

        return (year, season_val)

    return max(semesters, key=semester_sort_key)

def fetch_db_data(engine, semester):
    """Fetches students and their emails from both HS and JH tables for the target semester."""
    db_students = {}

    # 1. Fetch High School Data
    hs_query = text("""
        SELECT google_classroom_name, "Student email" as student_email, "Parent email" as parent_emails
        FROM "Student_Name_List_HS"
        WHERE semester = :sem
    """)

    # 2. Fetch Junior High Data
    jh_query = text("""
        SELECT google_classroom_name, student_email, father_email, mother_email, extra_gaurdian_email
        FROM student_name_list_jh
        WHERE semester = :sem
    """)

    with engine.connect() as conn:
        # Process HS
        hs_rows = conn.execute(hs_query, {"sem": semester}).fetchall()
        for row in hs_rows:
            name = str(row.google_classroom_name).strip()
            if not name or name.lower() == 'nan':
                continue

            s_email = str(row.student_email).strip() if pd.notna(row.student_email) else ''
            p_emails_raw = str(row.parent_emails) if pd.notna(row.parent_emails) else ''

            p_emails = {e.strip() for e in p_emails_raw.split(',') if e.strip() and e.lower() != 'nan'}

            # Two students can share a name (e.g. Alex Kim in HS and in JH): keep both records
            key = name if name not in db_students else f'{name} (HS)'
            db_students[key] = {
                'student_email': s_email if s_email.lower() != 'nan' else '',
                'guardian_emails': p_emails
            }

        # Process JH
        jh_rows = conn.execute(jh_query, {"sem": semester}).fetchall()
        for row in jh_rows:
            name = str(row.google_classroom_name).strip()
            if not name or name.lower() == 'nan':
                continue

            s_email = str(row.student_email).strip() if pd.notna(row.student_email) else ''

            p_emails = set()
            for col in [row.father_email, row.mother_email, row.extra_gaurdian_email]:
                if pd.notna(col):
                    val = str(col).strip()
                    if val and val.lower() != 'nan':
                        p_emails.add(val)

            key = name if name not in db_students else f'{name} (JH)'
            db_students[key] = {
                'student_email': s_email if s_email.lower() != 'nan' else '',
                'guardian_emails': p_emails
            }

    return db_students

def parse_html_emails(filepath):
    """Extracts the student name, student email, and guardian emails directly from the HTML code."""
    with open(filepath, 'r', encoding='utf-8') as f:
        html = f.read()

    student_email = ""
    guardian_emails = set()

    name_match = re.search(r'<title>Grade Report — (.*?)</title>', html)
    student_name = name_match.group(1).strip() if name_match else os.path.basename(filepath).replace('.html', '')

    email_blocks = re.findall(r'<span class="email-label">\s*(.*?)\s*</span>\s*(.*?)\s*</div>', html, re.IGNORECASE)

    for label, email in email_blocks:
        label = label.upper().strip()
        email = email.strip()

        if 'STUDENT' in label:
            student_email = email
        elif 'GUARDIAN' in label:
            guardian_emails.add(email)

    return student_name, student_email, guardian_emails

def main(folder=None):
    engine = get_engine()

    print("Connecting to database to determine the latest semester...")
    latest_semester = get_latest_semester(engine)

    if not latest_semester:
        print("Error: Could not find any semesters in the database.")
        return

    print(f"--> Automatically detected latest semester: '{latest_semester}'\n")

    print(f"Fetching student records for '{latest_semester}'...")
    db_students = fetch_db_data(engine, latest_semester)
    print(f"Loaded {len(db_students)} students from DB.")

    REPORTS_DIR = resolve_folder(folder)  # newest report run unless --folder is given
    print(f"Report folder: {os.path.relpath(REPORTS_DIR)}")

    html_files = report_files(REPORTS_DIR)
    print(f"Found {len(html_files)} HTML reports. Starting audit...\n")

    missing_in_db = []
    missing_guardians = []
    discrepancies = []
    excluded_present = []
    excluded = load_exclusions(engine, latest_semester)

    # Find each report's student by email first, then by name ignoring capitals: the list's
    # stored Classroom name can differ only in case (e.g. 'riley chen' vs 'Riley Chen').
    by_email = {rec['student_email'].strip().lower(): rec for rec in db_students.values() if rec['student_email']}
    by_name = {key.strip().lower(): rec for key, rec in db_students.items()}

    for filename in html_files:
        filepath = os.path.join(REPORTS_DIR, filename)
        name, html_s_email, html_g_emails = parse_html_emails(filepath)

        reason = excluded.reason(email=html_s_email, name=name)
        if reason:
            excluded_present.append((name, filename, reason))
            continue

        db_record = by_email.get((html_s_email or '').strip().lower()) or by_name.get((name or '').strip().lower())
        if not db_record:
            missing_in_db.append(name)
            continue

        db_s_email = db_record['student_email']
        db_g_emails = db_record['guardian_emails']

        if not html_g_emails and not db_g_emails:
            missing_guardians.append(name)
            continue

        issues = []
        if html_s_email.lower() != db_s_email.lower():
            issues.append(f"Student email mismatch: DB has '{db_s_email}', HTML has '{html_s_email}'")

        if html_g_emails != db_g_emails:
            missing_from_html = db_g_emails - html_g_emails
            extra_in_html = html_g_emails - db_g_emails

            if missing_from_html:
                issues.append(f"Guardians in DB but missing from HTML: {', '.join(missing_from_html)}")
            if extra_in_html:
                issues.append(f"Guardians in HTML but missing from DB: {', '.join(extra_in_html)}")

        if issues:
            discrepancies.append((name, issues))

    # --- PRINT FINAL REPORT ---
    print("=" * 60)
    print("AUDIT REPORT")
    print("=" * 60)

    if excluded_present:
        print(f"\n[!] REPORTS FOR STUDENTS WHO MUST NOT GET ONE ({len(excluded_present)}) - remove these files:")
        for s, fname, reason in excluded_present:
            print(f"  - {s} ({fname}): {reason}")

    if missing_guardians:
        print(f"\n[!] STUDENTS WITH ZERO GUARDIAN EMAILS ON FILE ({len(missing_guardians)}):")
        for s in missing_guardians:
            print(f"  - {s}")

    if missing_in_db:
        print(f"\n[!] STUDENTS IN HTML BUT NOT IN DB FOR '{latest_semester}' ({len(missing_in_db)}):")
        for s in missing_in_db:
            print(f"  - {s}")

    if discrepancies:
        print(f"\n[!] DATA DISCREPANCIES DETECTED ({len(discrepancies)}):")
        for name, issues in discrepancies:
            print(f"\n  {name}:")
            for issue in issues:
                print(f"    * {issue}")

    if not missing_guardians and not missing_in_db and not discrepancies and not excluded_present:
        print("\n✅ All HTML reports perfectly match the database! No missing emails detected.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Check the generated reports' emails against the database.")
    parser.add_argument('--folder', help="Report folder to audit (name inside grade_reports, or a path). "
                                         "Default: the newest report folder.")
    main(folder=parser.parse_args().folder)
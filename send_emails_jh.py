import os
import re
import smtplib
import argparse
import datetime
import time
import sys
from email.message import EmailMessage
from sqlalchemy import text

from db import get_db_engine as get_engine
from report_exclusions import load_exclusions
from report_folders import resolve_folder, report_files
from email_log import email_log
from student_selection import add_student_options, selected_students, norm

# --- FIX WINDOWS TEXT ENCODING ---
sys.stdout.reconfigure(encoding='utf-8')  # <-- ADD THIS

# --- EMAIL CONFIGURATION ---
# Sender address and Gmail app password come from .env (JH_SENDER_EMAIL / JH_SENDER_APP_PASSWORD).
from school_config import SCHOOL_SHORT_NAME, EMAIL_SIGNATURE, SMTP_SERVER, SMTP_PORT, sender_for, _get
SENDER_EMAIL = _get('JH_SENDER_EMAIL')

# --- DATABASE CONFIGURATION ---
# Reports are read from the newest grade_reports\<timestamp>_JH (or _ALL) folder; see report_folders.py

# --- CALCULATE PREVIOUS MONTH ---
today = datetime.date.today()
first_day_of_this_month = today.replace(day=1)
last_month_date = first_day_of_this_month - datetime.timedelta(days=1)
PREVIOUS_MONTH = last_month_date.strftime("%B")  # E.g., "April"


# --- DATABASE FUNCTIONS ---

def get_latest_semester(engine):
    query = text('SELECT DISTINCT semester FROM "Student_Name_List_HS" WHERE semester IS NOT NULL')
    with engine.connect() as conn:
        semesters = [str(row[0]).strip() for row in conn.execute(query).fetchall()
                     if row[0] and str(row[0]).lower() != 'nan']
    if not semesters:
        return "Current Semester"

    def semester_sort_key(sem_str):
        year_match = re.search(r'\d{4}', sem_str)
        year = int(year_match.group(0)) if year_match else 0
        sem_lower = sem_str.lower()
        season_val = 2 if 'fall' in sem_lower else (1 if 'spring' in sem_lower else 0)
        return (year, season_val)

    return max(semesters, key=semester_sort_key)


def parse_html_emails(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        html = f.read()

    guardian_emails = set()
    student_email = None

    name_match = re.search(r'<title>Grade Report — (.*?)</title>', html)
    student_name = name_match.group(1).strip() if name_match else os.path.basename(filepath).replace('.html', '')

    email_blocks = re.findall(r'<span class="email-label">\s*(.*?)\s*</span>\s*(.*?)\s*</div>', html, re.IGNORECASE)
    for label, email_html in email_blocks:
        clean_email = re.sub(r'<[^>]+>', '', email_html)
        clean_email = clean_email.replace('\xa0', '').replace(' ', '').strip()

        if not clean_email:
            continue

        if 'GUARDIAN' in label.upper():
            guardian_emails.add(clean_email)
        elif 'STUDENT' in label.upper():
            student_email = clean_email

    return student_name, student_email, list(guardian_emails)


def generate_email_body(student_name, is_student=False):
    en_greeting = f"Dear {student_name}," if is_student else f"Dear Parent of {student_name},"
    en_ownership = "Your" if is_student else "Your child's"

    tw_greeting = f"親愛的 {student_name}，" if is_student else f"親愛的 {student_name} 家長您好："
    tw_ownership = "您" if is_student else "貴子弟"

    return f"""{tw_greeting}

本信件附上{tw_ownership}的 {SCHOOL_SHORT_NAME} {PREVIOUS_MONTH} 成績單。

由於我們更新了動態排版，成績單將以 HTML 檔案格式（而非 PDF）作為附件發送。

如何查看成績單：

💻 在電腦上（Mac 或 Windows）：
請下載並連按兩下附件檔案。檔案將會自動且安全地在您的預設瀏覽器（Chrome、Safari、Edge 等）中開啟。如果您直接開啟檔案，可能只會看到類似純文字的原始碼。

📱 在智慧型手機或平板電腦上（iOS 或 Android）：
請點擊附件。如果您的電子郵件應用程式無法正確預覽（例如只顯示純文字），您可以選擇「以...開啟」，使用您的行動裝置瀏覽器來查看，或者先下載檔案後再透過瀏覽器開啟。

此檔案完全私密且支援離線查看。下載完成後，您無需網路連線即可開啟，且任何資料皆不會外流。

⚠️ 重要提醒：Google Classroom 上的成績會是最即時且準確的。本附件為系統當下輸出的成績單內容，可能偶有誤差或未完全同步的情況，實際成績請一律以 Google Classroom 為準。

{EMAIL_SIGNATURE}

========================================

{en_greeting}

{en_ownership} {SCHOOL_SHORT_NAME} grade report for {PREVIOUS_MONTH} is attached to this email.

Because we have updated to a dynamic layout, the report is attached as an HTML file rather than a PDF. 

HOW TO VIEW THE GRADE REPORT:

💻 On a Computer (Mac or Windows):
Download and then double-click the attached file. It will automatically open safely in your default web browser (Chrome, Safari, Edge, etc.). If you just open the file directly, you might only see text like a text file.

📱 On a Smartphone or Tablet (iOS or Android):
Tap the attachment. If your email app does not preview it correctly (or only shows plain text), choose "Open with..." and select your mobile web browser, or download the file first and then open it with your browser.

This file is completely private and offline. You do not need an internet connection to view it once downloaded, and no data is shared externally.

⚠️ IMPORTANT NOTE: Google Classroom will always reflect the most accurate and up-to-date grades. The attached HTML report is a generated snapshot and may occasionally contain slight discrepancies. For the official record, please always refer to Google Classroom.

Best regards,
{EMAIL_SIGNATURE}
"""



def connect_to_gmail():
    """Helper function to cleanly connect to Gmail."""
    server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
    server.starttls()
    server.login(*sender_for('JH'))
    return server


def test_note(real_recipients, test_mode):
    """In --test mode, a line at the top of the email saying who it would really go to."""
    if not test_mode:
        return ""
    return ("TEST MODE - in a real send this email would go to: " + ", ".join(real_recipients) + "\n"
            + "-" * 60 + "\n\n")


def resend_overrides(guardian_emails, student_email, to_address=None, parents_only=False):
    """--to replaces the parent addresses; with --to or --parents-only the student copy is not sent."""
    if to_address:
        guardian_emails = [to_address]
    skip_student = bool(to_address or parents_only)
    return guardian_emails, student_email, skip_student


def send_reports(test_mode=False, preview_mode=False, folder=None,
                 students=None, to_address=None, parents_only=False):
    REPORTS_DIR = resolve_folder(folder, school='JH')
    print(f"Report folder: {os.path.relpath(REPORTS_DIR)}")

    # Only this school's reports (an ALL folder also holds the other school's)
    files = report_files(REPORTS_DIR, school='JH')
    if not files:
        print("No HTML reports found to send.")
        return

    print("Connecting to database to determine the latest semester...")
    try:
        engine = get_engine()
        current_semester = get_latest_semester(engine)
    except Exception as e:
        print(f"Failed to connect to database: {e}")
        current_semester = "Current Semester"

    print(f"--> Automatically detected latest semester: '{current_semester}'\n")

    # Never email excluded students (honors "(H)" students, report_exclusions.txt), even if
    # their report somehow ended up in the folder. If the list can't be loaded, stop instead.
    try:
        excluded = load_exclusions(get_engine())
    except Exception as e:
        print(f"Could not load the report exclusions ({e}). Stopping so no excluded student is emailed.")
        return
    kept = []
    for filename in files:
        name, s_email, _ = parse_html_emails(os.path.join(REPORTS_DIR, filename))
        reason = excluded.reason(email=s_email, name=name)
        if reason:
            print(f"  Skipping {name}: {reason}")
        else:
            kept.append(filename)
    files = kept

    # --student: only these students (e.g. to resend a few reports)
    if students:
        wanted = {norm(s): s for s in students}
        chosen = []
        for filename in files:
            name, _, _ = parse_html_emails(os.path.join(REPORTS_DIR, filename))
            key = norm(name)
            if key in wanted:
                chosen.append(filename)
                wanted.pop(key)
        if wanted:
            print(f"No report in this folder for: {', '.join(wanted.values())}. Nothing was sent.")
            print("Check the spelling (it must match the report file name) or pass --folder.")
            return
        files = chosen
        print(f"Only these students: {', '.join(students)}")
    if to_address:
        print(f"Parent email goes ONLY to: {to_address} (instead of the addresses on file); no student copy.")
    elif parents_only:
        print("Parents only: the copy to the student is not sent.")
    print(f"Found {len(files)} reports to process.")
    print(f"Current Date: {today.strftime('%Y-%m-%d')} | Sending reports for: {PREVIOUS_MONTH}")

    # --- NEW: PREVIEW MODE LOGIC ---
    if preview_mode:
        print("\n" + "=" * 60)
        print("🔍 PREVIEW MODE ACTIVE: No emails will be sent.")
        print("=" * 60)

        for filename in files:
            filepath = os.path.join(REPORTS_DIR, filename)
            student_name, student_email, guardian_emails = parse_html_emails(filepath)
            guardian_emails, student_email, skip_student = resend_overrides(
                guardian_emails, student_email, to_address, parents_only)

            print(f"\nStudent: {student_name}")
            if guardian_emails:
                print(f"  -> Guardians : {', '.join(guardian_emails)}")
            else:
                print(f"  -> Guardians : [MISSING!]")

            if skip_student:
                print(f"  -> Student   : (not sent - parents only)")
            elif student_email:
                print(f"  -> Student   : {student_email}")
            else:
                print(f"  -> Student   : [MISSING!]")

        print("\n" + "=" * 60)
        print("Preview complete. Run script without --preview to send real emails.")
        return
    # -------------------------------

    # Per-email log lines are lined up in columns (status | student | -> | addresses) so a long
    # run is easy to scan and compare. Spaces, not tabs, so it lines up the same in the
    # terminal and in the saved log file.
    name_width = max((len(parse_html_emails(os.path.join(REPORTS_DIR, f))[0] or '') for f in files), default=0)

    def row(status, name, text):
        return f"  {'[' + status + ']':<19} {name:<{name_width}} -> {text}"

    if test_mode:
        print("🛑 RUNNING IN TEST MODE. All emails will be redirected to the sender.")

    try:
        print("Connecting to email server...")
        server = connect_to_gmail()
    except Exception as e:
        print(f"Failed to connect to email server: {e}")
        return

    success_count = 0
    skip_count = 0

    for index, filename in enumerate(files, 1):
        filepath = os.path.join(REPORTS_DIR, filename)
        student_name, student_email, guardian_emails = parse_html_emails(filepath)
        guardian_emails, student_email, skip_student = resend_overrides(
            guardian_emails, student_email, to_address, parents_only)

        with open(filepath, 'rb') as f:
            html_data = f.read()

        # Helper function to send email with auto-reconnect
        def safe_send(msg, recipient_type, intended=None):
            nonlocal server, success_count
            # In --test mode, also show who the email would really go to
            shown = (f"{', '.join(intended)}   (TEST - sent to {msg['To']} instead)"
                     if intended else msg['To'])
            try:
                server.send_message(msg)
                print(row(f"SENT {recipient_type.upper()}", student_name, shown))
                success_count += 1
            except Exception as send_err:
                print(f"  [CONNECTION DROPPED] Reconnecting to Gmail... ({send_err})")
                try:
                    server = connect_to_gmail()  # Auto-reconnect
                    server.send_message(msg)
                    print(row(f"SENT {recipient_type.upper()}", student_name, shown))
                    success_count += 1
                except Exception as retry_err:
                    print(row(f"FAILED {recipient_type.upper()}", student_name, f"NOT SENT: {retry_err}"))

        # 1. SEND TO GUARDIANS
        if guardian_emails:
            recipients = [SENDER_EMAIL] if test_mode else guardian_emails
            msg_guardians = EmailMessage()
            msg_guardians[
                'Subject'] = f"{student_name}'s {SCHOOL_SHORT_NAME} {PREVIOUS_MONTH} Grade Report (成績單) for {current_semester}"
            msg_guardians['From'] = SENDER_EMAIL
            msg_guardians['To'] = ", ".join(recipients)
            msg_guardians.set_content(test_note(guardian_emails, test_mode) + generate_email_body(student_name, is_student=False))
            msg_guardians.add_attachment(html_data, maintype='text', subtype='html', filename=filename)

            safe_send(msg_guardians, "Guardians", guardian_emails if test_mode else None)
            time.sleep(1.5)  # Pause for 1.5 seconds so Google doesn't block us
        else:
            print(row("SKIPPED GUARDIANS", student_name, "no guardian emails found"))

        # 2. SEND TO STUDENT
        if skip_student:
            print(row("NOT SENT STUDENT", student_name, "parents-only resend"))
        elif student_email:
            recipients = [SENDER_EMAIL] if test_mode else [student_email]
            msg_student = EmailMessage()
            msg_student['Subject'] = f"Your {SCHOOL_SHORT_NAME} {PREVIOUS_MONTH} Grade Report (成績單) for {current_semester}"
            msg_student['From'] = SENDER_EMAIL
            msg_student['To'] = ", ".join(recipients)
            msg_student.set_content(test_note([student_email], test_mode) + generate_email_body(student_name, is_student=True))
            msg_student.add_attachment(html_data, maintype='text', subtype='html', filename=filename)

            safe_send(msg_student, "Student", [student_email] if test_mode else None)
            time.sleep(1.5)  # Pause for 1.5 seconds so Google doesn't block us
        else:
            print(row("SKIPPED STUDENT", student_name, "no student email found"))

        # --- UPDATE YOUR BATCH PAUSE AT THE BOTTOM ---
        # 50 students processed = roughly 100 emails attempted
        if index % 50 == 0:
            print(f"  [PAUSE] Processed {index} students. Cooling down for 60 seconds...")
            time.sleep(60)

    try:
        server.quit()
    except:
        pass  # Ignore if server already closed

    print("-" * 40)
    print(f"Done! Successfully sent: {success_count} emails | Skipped: {skip_count}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Send Grade Reports via Email")
    parser.add_argument(
        '--test',
        action='store_true',
        help="Test mode: Send ALL emails to the sender address instead of the parents."
    )
    # --- NEW: Added Preview Argument ---
    parser.add_argument(
        '--preview',
        action='store_true',
        help="Preview mode: Print the list of intended recipients to the console without sending anything."
    )
    parser.add_argument(
        '--folder',
        help="Report folder to send (name inside grade_reports, or a path). Default: the newest JH or ALL folder."
    )
    add_student_options(parser, 'Only send for this student')
    parser.add_argument(
        '--to', metavar='EMAIL',
        help='Send the parent email to this address instead of the ones on file (exactly one --student; '
             'no student copy is sent).'
    )
    parser.add_argument(
        '--parents-only', action='store_true',
        help='With --student: only send the parent email, not the copy to the student.'
    )
    args = parser.parse_args()
    students = selected_students(args)
    # Safety: never send everyone's report to one address, or skip students for the whole school by accident
    if (args.to or args.parents_only) and not students:
        parser.error('--to and --parents-only can only be used together with --student')
    if args.to and len(students) != 1:
        parser.error('--to needs exactly one --student')
    if args.to and not re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", args.to.strip()):
        parser.error(f'--to does not look like a single email address: {args.to}')

    # Every run is also saved to the 'email logs' folder (see email_log.py)
    mode = 'PREVIEW' if args.preview else 'TEST' if args.test else 'SENT'
    with email_log('JH', mode):
        send_reports(test_mode=args.test, preview_mode=args.preview, folder=args.folder,
                     students=students, to_address=(args.to or '').strip() or None,
                     parents_only=args.parents_only)

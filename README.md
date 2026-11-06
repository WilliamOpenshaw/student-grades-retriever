# Student Grades Retriever

Pulls grades from **Google Classroom** for a school with two sections (High School and Junior High),
keeps dated snapshots in a local **SQLite** database, and produces:

- **Monthly HTML grade reports** for each student (category grades, every assignment, overall grade
  following each class's Classroom grading setting), emailed to parents and students
  (bilingual English / Traditional Chinese by default).
- A **staff dashboard** (Google Apps Script web app) for teachers, the dean and the well-being team:
  watch list, course and department averages, trends, past grades. Access is limited by an access
  Sheet plus a weekly rotating access code.
- Optional **archive export** of old Classroom classes (Apps Script, read-only) and an importer.

Windows-oriented (batch files, Task Scheduler), but the Python scripts run anywhere.

> **Student data stays on your computer.** The database, reports, logs and CSVs are all git-ignored.
> Never commit `.env`, `credentials.json` or `token_*.json`.

## Requirements

- Python 3.11+
- A Google Workspace (or Google) account per section that can see its Classroom classes
- A Google Cloud project with an OAuth client (Desktop app)

## Setup

1. **Install**
   ```
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. **Google Cloud project**: create one at https://console.cloud.google.com, enable the
   **Google Classroom API** (and the **Google Drive API** if you use the dashboard/backups), configure
   the OAuth consent screen (Internal, for a Workspace domain), create an **OAuth client ID** of type
   *Desktop app*, and download it as `credentials.json` into this folder.
3. **Configuration**: copy `.env.example` to `.env` and fill in your school's name, the sender
   accounts and their **App Passwords**. Optionally put a `report_logo.png` here, and copy
   `report_exclusions.example.txt` to `report_exclusions.txt`.
4. **Database**:
   ```
   .venv\Scripts\python.exe create_database.py
   ```
5. **Student lists**: export your lists as CSV into `student_list_csvs\` (columns described in
   [student_list_csvs/README.md](student_list_csvs/README.md)) and run `import_student_lists.py`
   (use `--dry-run` first).
6. **Sign in**: `.venv\Scripts\python.exe authenticate.py` opens a browser once per section
   (High School, then Junior High); sign in with the matching account. Tokens are saved as
   `token_hs.json` / `token_jh.json` (read-only Classroom scopes).

## Monthly run

The full run-book, with every option, is [INSTRUCTIONS.txt](INSTRUCTIONS.txt). In short:

```
.venv\Scripts\python.exe ingest_classroom_data.py
.venv\Scripts\python.exe enrich_student_records_HS.py
.venv\Scripts\python.exe enrich_student_records_JH.py
.venv\Scripts\python.exe calculate_category_grades.py
.venv\Scripts\python.exe verify_category_calculations.py --latest
.venv\Scripts\python.exe generate_grade_reports.py
.venv\Scripts\python.exe audit_emails.py
.venv\Scripts\python.exe send_emails_hs.py --preview
.venv\Scripts\python.exe send_emails_hs.py --test
```

`send_emails_hs.py` / `send_emails_jh.py` send **real email** without `--preview` or `--test`.
Always run `--preview` (lists who gets what, never connects) and `--test` (sends everything to the
sender's own inbox) first.

## Staff dashboard

See [dashboard_web_app/SETUP.md](dashboard_web_app/SETUP.md). `sync_dashboard.py` uploads JSON
files to a Drive folder that only you can see; the Apps Script web app reads them as you and shows
each staff member only their courses. `run_backup_and_sync.bat` (Task Scheduler) backs up the
database and refreshes current grades daily. If you use `dashboard_watcher_task.xml`, edit the
paths in it before importing it into Task Scheduler.

## Past classes

[archive_export/README.md](archive_export/README.md): an Apps Script that exports old (archived)
Classroom classes from any account that can see them, and `import_archive_export.py` to bring
them into the database.

## Customising for your school

- `.env`: school name, senders, logo.
- `send_emails_hs.py` / `send_emails_jh.py`: email wording (English + Traditional Chinese).
- `generate_grade_reports.py`: report layout and colours.
- `course_terms.py`: how the semester is read from course names (`26F`, `Spring '26`, ...).
- `course_departments.py`: keyword rules that suggest a department for each course, and optional
  course codes.
- `category_groups.py`: how grading-category names are grouped on the dashboard.
- `dashboard_web_app/Code.gs`: `RULES` for the watch list, roles, access-code settings.
- The two sections are configured as `SCHOOL_CONFIGS` in the scripts ("High School" →
  `token_hs.json`, "Junior High" → `token_jh.json`).

## Privacy

This project processes student names, emails and grades. Keep the database, `grade_reports\`,
`logs\`, `backups\` and any CSV exports on a trusted computer, follow your school's data-protection
rules, and don't paste student data into external services.

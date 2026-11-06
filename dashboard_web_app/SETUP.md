# Local Database, Backups & Teacher Dashboard: Setup

```
grades.db  (SQLite file in the project folder, used by every script and DataGrip)
   │
   ├── backup_database.py ──► Drive: Student Grades Retriever/Database Backups/grades_<date>.zip
   │
   └── sync_dashboard.py  ──► Drive: Student Grades Retriever/Dashboard/
                                  dashboard.json, history.json, courses.json,
                                  students_00..15.json                   (data)
                                  "Dashboard Access" Sheet + Apps Script (web app)
                                          │
                                          ▼
          Teachers, dean and well-being team open the web app link
          (school Google sign-in + rotating access code)
```

Teachers never get access to the Drive folder. The web app reads it as you and
only sends each teacher the courses listed for them in the **Dashboard Access** Sheet.

## One-time setup

### 1. The local database
Create `grades.db` once with `create_database.py` (see the main `README.md`).
The connection settings for every script are in `.env` (`DB_PATH=grades.db`).

**Open it in DataGrip:** **+ → Data Source → SQLite**, set **File** to `grades.db`
in the project folder, and click **Download missing driver files** if DataGrip asks.

### 2. Google Drive API
`credentials.json` belongs to a Google Cloud project. In that project, open
**APIs & Services → Library → Google Drive API** and make sure it is **Enabled**.

### 3. First backup and sync
```
.venv\Scripts\python.exe backup_database.py
.venv\Scripts\python.exe sync_dashboard.py
```
The first run opens a browser. Sign in with the **school Google account that will own
the dashboard**. It must be in the same school domain as the teachers. The sign-in is
saved in `token_drive.json`, and it can only access files these scripts created.
The scripts create the Drive folders, and their IDs are saved in `drive_config.json`.

### 4. Add the web app code
1. In Drive, open **Student Grades Retriever → Dashboard → Dashboard Access**.
2. **Extensions → Apps Script**.
3. Replace `Code.gs` with `dashboard_web_app/Code.gs`.
4. Click **+ → HTML**, name it `Index`, and paste in `dashboard_web_app/Index.html`
   (if `Index` already exists, replace its contents).
5. Save.
6. **Run `setup` once:** pick `setup` in the function list at the top of the editor and click
   **Run**. Approve the permissions it asks for (Sheets, Drive, sending email as you, triggers).
   It adds a `role` column to the first tab, creates the **Settings** and **Access Log** tabs,
   creates a secret for the access codes, schedules the daily code check and emails you the
   first access code. Running it again is safe.

### 5. Deploy
1. **Deploy → New deployment →** gear icon **→ Web app**.
2. Set **Execute as** to Me and **Who has access** to Anyone within *your school domain*.
3. Click **Deploy**, approve the permissions, and copy the **Web app URL** to send to staff.

If you already deployed the earlier version: **Deploy → Manage deployments → ✏️ → Version:
New version → Deploy**. The link stays the same.

Link in the access code email: add the Web app URL under **Project Settings → Script Properties** as
`DASHBOARD_URL`. Without it, the email uses the link Google reports, which can be another deployment's.
Update it if you ever create a new deployment.

As the owner, you always see every course (you still need the access code).

### 6. Give staff access (first tab of the Dashboard Access Sheet)
| email | courses | notes | role |
|---|---|---|---|
| jane.doe@school.edu | `Science 8, PE 6A` | sees courses containing those words | teacher |
| dean@school.edu | *(blank)* | blank = every course for dean / wellbeing / admin | dean |
| counsellor@school.edu | `G5, G6, G7, G8, Homeroom` | well-being for Junior High only | wellbeing |

- **role** is `teacher` (default if blank), `dean`, `wellbeing` or `admin`. Dean, well-being and
  admin see every course unless the **courses** column narrows it; teachers see only the courses
  listed. The role also picks the first page: teachers land on **Overview**, the others on the
  **Watch list**. The `wellbeing` role starts with assignment-level detail (the assignment lists
  on student and course pages) hidden; they can click **Show assignment details** at any time.
- Matching ignores capitals and starts at the beginning of a word: `PE 6` matches
  "25-26 Spring PE 6A" but not "Pebble". `Art 8` matches both "Adv Art 8A" and "Adv Art 8B".
  Note that `Science` also matches "Social Sciences"; write `G7-1 Science` etc. to be exact.
- Rows that start with `#` are ignored. Only the first tab is read.
- Changes take effect when the person reloads the page.
- **Leave the Sheet in the Dashboard folder.** The web app finds the data files next to it.
- Students are usually in the same Google domain, so this email list is what keeps them out.

### Access code (rotating password)
Everyone, including you, must enter the current **access code** after signing in.
- A new code starts every **7 days** (Monday 00:00, school time zone). Change the number in the
  **Settings** tab (`Rotate code every (days)`, 1–90).
- Shortly after midnight on the day it changes, the new code is emailed to **you and everyone with
  role `dean` or `wellbeing`** in the first tab (they are Bcc'd), and written into
  **Settings → Current code**. Teachers don't get it by email: you or the dean pass it on.
  To email it to anyone else as well, add them to **Settings → Email new code to** (one per row).
- **Settings tab layout** (since 2026-10-16): row 1 = the setting names across the top, row 2 = grey
  descriptions, row 3 = the values; `Email new code to` lists one address per row from row 3 down.
  To switch an older tab (one setting per row, emails comma-separated), paste the new `Code.gs`, save,
  and run **`convertSettingsTab`** once: it keeps every value and prints them in the Execution log.
  The script reads both layouts, so nothing breaks before you convert.
- These are the only emails the dashboard sends. They contain the code and the link, never grades,
  and have nothing to do with the grade-report emails (`send_emails_*.py`).
- The previous code keeps working for 12 hours after a change, so nobody is locked out mid-morning.
- **To email the current code again** (e.g. after adding a dean or well-being person): in Apps Script
  run `resendCurrentCode`. The code doesn't change; everyone on the list gets the email again.
- **Checking who got a code email:** every code email (setup, weekly, resend, change) prints a report
  in the **Execution log** at the bottom of the Apps Script editor: the code, its dates, each recipient
  and why they're on the list (owner / role dean or wellbeing with the row number / Settings), any
  address skipped as invalid, how many teachers were not emailed, the remaining daily email quota and
  SENT or FAILED with Google's error. The same report for the automatic Monday emails is under
  **Executions** (left sidebar) → the rotateAccessCode run. Each send or failure is also added to
  the **Access Log** tab (role `system`), and the email itself is in your Gmail **Sent** folder.
- After changing **Rotate code every (days)**, run `rotateAccessCode`: the change switches to a new
  code immediately, and this emails it now instead of after midnight.
- **If a code leaks:** in Apps Script run `changeCodeNow`. Old codes stop working immediately and the
  new one is emailed.
- 5 wrong codes lock that account out for 15 minutes.
- The page locks itself after 30 minutes without activity, when the tab is closed, or with the
  **Lock** button. The browser does not remember the code.
- Every unlock is recorded in the **Access Log** tab (time, email, role).

### Departments tab (kept by the dean)
Each course has a department, used by the dashboard's **Department** filter (e.g. English across HS
and JH, or HS + English). The **Departments** tab of the Sheet has one row per *course key*: the course
name without its term and section (`26F Math 6_A` and `26F Math 6_B` → `Math 6`), so one row covers
every section.
- Columns: **Course key** · **Department** (dropdown; a new name is allowed and just appears as a new
  department) · **Ontario code** · **Leave out of averages (Y/N)** (Homeroom and Other start as Y;
  used by the department report) · **Course names** (examples) · **Note**.
- The tab is created by `setup()` or by running `updateDepartmentsTab` once. After that, the daily
  code trigger adds any **new** course keys from the latest sync, with a *suggested* department
  (from `course_departments.py`), and notes it in the Access Log. Existing rows are never changed,
  so edits stay. A deleted row comes back as a suggestion the next night.
- Edits show on the next page load (no redeploy). On the dashboard, a department marked `*` is still
  only a suggestion (no row in the tab yet).
- Each new semester, check the newly added rows (sort or filter the Note column by date).

### 7. When the dashboard updates (already set up)
- **Right after your grade sync:** `calculate_category_grades.py` refreshes the dashboard
  as soon as the grades are calculated. Use `--no-dashboard` for test runs.
- **Every day at 3:40 PM:** the Windows Task Scheduler job **"Student Grades - Backup and
  Dashboard Sync"** runs `run_backup_and_sync.bat`: it backs up the database, reads current grades
  from Classroom (`refresh_dashboard.py`) and updates the dashboard, about 10 minutes in all. A
  window titled "Student Grades - Daily Dashboard Update" explains itself and closes on its own. If
  the computer is off at 3:40 PM, it runs the next time the computer is on. Results go to
  `nightly_log.txt`. To change the time: Task Scheduler → the job → **Properties → Triggers**.
- **Current grades are never saved as a snapshot.** They go in one table, `live_grades`, which each
  update overwrites. `sync_dashboard.py` uses it for a school when it's newer than that school's
  latest snapshot. Your own grade pulls for reports still make permanent snapshots as before, and
  grade reports and emails only ever use snapshots.
- **The Update grades button:** anyone with dashboard access can start the same update from the
  dashboard. The page leaves a request in `refresh_request.json` in the Drive folder;
  `dashboard_watcher.py` on the grades computer (no window, Task Scheduler job **"Student Grades -
  Dashboard Button Watcher"**) checks it every 30 seconds and runs `refresh_dashboard.py`. Works only
  while the computer is on and awake; one update at a time, 10 minutes apart. Each request is in the
  Access Log tab. Logs: `logs\dashboard_watcher.log`, `logs\dashboard_refresh.log`.
- **After an expired sign-in:** a school's previous grades stay on the dashboard and the update says
  so. Run `authenticate.py` by hand.

Teachers have no sync button. The dashboard is read-only, and the header shows when each
section's data is from.

## Backups
- **Local:** the `backups` folder keeps the newest 14 zips.
- **Google Drive:** `Database Backups` keeps the newest 30. Older ones go to Drive's trash,
  which empties itself after 30 days.
- **To restore:** close DataGrip, rename the current `grades.db` (and any `grades.db-wal` /
  `grades.db-shm`) to set it aside, then unzip a backup into the project folder.
  The zip contains `grades.db`.
- A backup can be made any time, even while DataGrip is open.

## Updating the web app later
After editing Code.gs or Index.html: **Deploy → Manage deployments → ✏️ → Version: New version → Deploy**.
The link stays the same.

Testing changes first (no Google involved): `local_test\build.py` builds a copy of the dashboard
with fake Apps Script services and the current data; its docstring has the steps. Its `out\` folder
contains student data: keep it on this computer.

## What staff see
All views include only the courses the person may see, and the filters (section, grade level,
homeroom, search) apply everywhere.
- **Overview:** number of students, average grade (and change vs last semester), how many need
  intervention / attention / are declining, missing assignments; a chart of the average grade over
  time (earlier semesters' final grades, then every grade update this semester, following the same
  students; High School and Junior High as two lines); the **grade distribution** (share of grades
  per letter, in %, as Bars or Proportions (ring chart), counting either *student grades*, meaning one
  per student per course, or *class averages*, meaning one per class; compared with last semester's
  final grades; "Show numbers" lists the counts); a table by grade level or homeroom; the courses
  with the lowest averages. Bars/Proportions and Student grades/Class averages are remembered per
  browser.
- **Watch list:** students flagged by the support rules, with the reasons in plain words
  ("Below 50: G8-2 Visual Arts 40 · 6 missing assignments · Down 12.3 vs 2026 Spring"). Filters for
  intervention, attention, declining and many missing assignments. **For heads of department:** pick
  a department (and section) in the filters, and choose **Flags from: <department> only** to list just
  the students flagged because of that subject (the rules run on that department's courses only);
  **All courses** shows each student's overall status instead.
- **Not returned (draft grades):** where a teacher has graded work but not returned it yet (e.g. a
  project still being graded for the whole class), the Watch list, Students list, course page and
  student page show "N not returned". These grades aren't in any average, flag or report until they're
  returned. The counts appear after the next grade sync (ingest) stores Classroom's draft grades.
- **Courses:** every course with its average, trend and missing work. A course page shows the grade
  distribution, the class average at each grade update, category averages, the assignments with
  the lowest class averages and most missing work, and the student list.
- **Departments:** replaces the monthly departmental spreadsheet. One row per department with the
  **average of class averages** (each class counts once, the spreadsheet's method), the **average
  of student grades**, grades below 60, the change since the last update, a trend line, the semester
  and school-year averages (average of the monthly values) and missing work. Semester buttons switch
  to earlier semesters (each class up to its last update). A department page shows the trend chart and
  every class **month by month** (or update by update) with its biggest change and semester average,
  plus **Download CSV** / **Copy for Sheets**, an **Each section / Sections combined** switch (A/B/C
  sections of a course as one row), and a **Grading categories** panel (this semester: tests, classwork,
  participation, portfolio … averaged across the department's classes, compared with the department;
  category names are grouped into families by `category_groups.py`). Classes marked "leave out" in the
  Departments tab are not included. One-student classes (e.g. independent studies) count as much as a full class in the
  average of class averages, so the dean may want to mark them "leave out".
- **Past grades:** type a name in the search box to find any student with grades from earlier
  semesters, including students who have left. Per semester: each class's final grade, the class
  average and how far above/below it the student was; a chart of the student's average by semester
  against the class averages (plus "now" for current students); transcript letter grades where the
  school has them. Sources: the last grade update of each earlier semester, and archived Classroom
  classes once imported (`import_archive_export.py`). Archived semesters also appear in the
  **Departments** tab (one "Final" column), and 2025-26 department pages show the old monthly
  Sheet's hand-typed averages. Every opening of a student's past grades is written to the Access Log.
  **Who can see it:** every role (decided 2026-10-06). To narrow it, add a row to the **Settings**
  tab: a column headed `Past grades visible to` with `dean, wellbeing` in row 3 (the owner always can). Teachers only see
  archived *class averages* for their own courses either way.
- **Students:** sortable list; a student page shows status and reasons, average over time, this
  semester's courses (grade, change, trend, missing), earlier semesters' final grades, and each
  course's categories and assignments with missing work listed first.
- **How it works:** where the data comes from, the support rules and the grade scale.

### Support rules (edit `RULES` at the top of Code.gs, then deploy a new version)
- **Needs intervention:** any course below 50, two or more courses below 60, average below 60, or
  10+ missing assignments.
- **Needs attention:** a course below 60, average below 70, 5+ missing assignments, or **declining**
  (average down 10+ points since the last grade update, or compared with last semester).
- A course counts only once it has **3 graded assignments** (`minGraded`); before that it is shown
  with a "few grades" tag, because one early zero can make a course grade 0–10%.
- **Missing** = past due, not turned in, no grade, not excused, worth points (Classroom's "Missing").
- Adapted from the Junior High Classroom Hub rules. "Average" is the plain mean of the student's
  counted course grades. Homeroom comes from the student's Homeroom course in Classroom.

Colors and letters match the emailed grade reports: green ≥ 70, orange 50–70, red ≤ 50;
A ≥ 85, AB ≥ 80, B ≥ 75, BC ≥ 70, C ≥ 60, D ≥ 50, F below that.

Trends need more than one grade update: the database keeps every snapshot, so each grade sync
during the semester adds a point to the course and student trend lines.

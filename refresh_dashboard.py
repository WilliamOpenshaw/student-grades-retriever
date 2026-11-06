r'''
Daily "current grades" update for the staff dashboard.

Pulls today's grades from Google Classroom into ONE table, live_grades, which is overwritten every
time, and sends them to the dashboard. It never adds a snapshot: student_grade_snapshots, the grade
reports and the emails are not touched. sync_dashboard.py uses live_grades for a school when it is
newer than that school's latest snapshot, and shows it as the newest point on the trend charts.

Run by:
    run_backup_and_sync.bat   every day at 3:40 PM (Task Scheduler)
    dashboard_watcher.py      when someone presses "Update grades" on the dashboard
    you, by hand:
        .venv\Scripts\python.exe refresh_dashboard.py               # pull and upload
        .venv\Scripts\python.exe refresh_dashboard.py --no-upload   # pull only, nothing sent to Drive
        .venv\Scripts\python.exe refresh_dashboard.py --compare     # also compare with the latest snapshot
        .venv\Scripts\python.exe refresh_dashboard.py --school "Junior High"

It only reads from Classroom (the same read-only tokens as the ingest) and never opens a sign-in
window; if a token has expired it says so and keeps that school's previous data. Only one run at a
time. Progress goes to the console, the log and refresh_status.json in the Drive folder, where the
dashboard reads it; those progress lines never contain student or class names.
'''

import argparse
import datetime
import json
import msvcrt
import os
import re
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'
os.environ['GRADES_NO_BROWSER'] = '1'

import pandas as pd
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import inspect, text

from calculate_category_grades import populate_category_grades, populate_overall_grades
from classroom_api import list_all
from course_terms import current_term, split_courses, term_label
from db import get_db_engine

SCHOOLS = [{'name': 'High School', 'token_file': 'token_hs.json'},
           {'name': 'Junior High', 'token_file': 'token_jh.json'}]
SCOPES = ['https://www.googleapis.com/auth/classroom.courses.readonly',
          'https://www.googleapis.com/auth/classroom.coursework.students.readonly',
          'https://www.googleapis.com/auth/classroom.rosters.readonly',
          'https://www.googleapis.com/auth/classroom.student-submissions.students.readonly']
LIVE_TABLE = 'live_grades'
STAGING_TABLE = 'live_grades_staging'
LOCK_FILE = os.path.join('logs', 'dashboard_refresh.lock')
STATUS_FILE = 'refresh_status.json'   # in the Drive Dashboard folder; read by Code.gs
MIN_KEEP_SHARE = 0.5                  # a pull with under half of the school's usual rows is not used
ALREADY_RUNNING = 3                   # exit code


class Tee:
    """Console + log file (a scheduled job's window should show the same text as the log)."""
    def __init__(self, path):
        self.console, self.file = sys.stdout, open(path, 'a', encoding='utf-8')

    def write(self, s):
        self.file.write(s)
        self.file.flush()
        if self.console:
            try:
                self.console.write(s)
                self.console.flush()
            except (OSError, ValueError, UnicodeError):
                pass

    def flush(self):
        self.file.flush()


class Progress:
    """Prints each stage, and writes the status file the dashboard's progress bar reads."""
    MIN_SECONDS = 4

    def __init__(self, request_id=None, requested_by=None, upload=True):
        self.status = {'state': 'running', 'stage': 'Starting', 'pct': 0, 'requestId': request_id,
                       'requestedBy': requested_by, 'startedAt': int(time.time() * 1000), 'log': [], 'updatedAt': 0}
        self.upload, self.service, self.config, self.file_id, self.last_write = upload, None, None, None, 0
        self.last_print = None

    def connect(self):
        """Control files must be created by this program: drive.file sign-in can only read those."""
        if not self.upload:
            return
        try:
            from google_drive import ensure_control_file, get_drive_service
            self.service = get_drive_service()
            self.file_id = ensure_control_file(self.service, STATUS_FILE)
        except Exception as e:
            print(f"  (progress for the dashboard is off: {e})")
            self.service = None

    def _write(self):
        self.status['updatedAt'] = int(time.time() * 1000)
        self.last_write = time.time()
        if not self.service:
            return
        try:
            from google_drive import write_control_file
            write_control_file(self.service, self.file_id, self.status)
        except Exception as e:
            print(f"  (could not update the dashboard's progress: {e})")

    def __call__(self, stage, pct=None, log=True):
        changed = stage != self.status['stage']
        self.status['stage'] = stage
        if pct is not None:
            self.status['pct'] = max(self.status['pct'], min(99, int(pct)))
        if log and changed:
            line = f"{datetime.datetime.now():%H:%M:%S}  {stage}"
            self.status['log'] = (self.status['log'] + [line])[-40:]
            print(f"  {line}")
        if (log and changed) or time.time() - self.last_write >= self.MIN_SECONDS:
            self._write()

    def finish(self, state, message):
        self.status.update(state=state, message=message, finishedAt=int(time.time() * 1000), pct=100 if state == 'done' else self.status['pct'])
        line = f"{datetime.datetime.now():%H:%M:%S}  {message}"
        self.status['log'] = (self.status['log'] + [line])[-40:]
        print(f"  {line}")
        self._write()


def get_credentials(token_file):
    """Always a fresh access token for exactly these scopes; never a browser window, never saved."""
    if not os.path.exists(token_file):
        raise RuntimeError(f"{token_file} not found. Run authenticate.py by hand.")
    creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    if not creds.refresh_token:
        raise RuntimeError(f"{token_file} has no refresh token. Run authenticate.py by hand.")
    creds.refresh(Request())
    return creds


def pull_school(school, stamp, known, progress, pct_from, pct_to):
    """All rows of one school's current classes. known = {student_id: {student fields from the database}}."""
    section = school['name']
    progress(f"{section}: connecting to Google Classroom", pct_from)
    service = build('classroom', 'v1', credentials=get_credentials(school['token_file']), cache_discovery=False)
    courses = list_all(service.courses().list, 'courses', courseStates=['ACTIVE'])
    term = current_term()
    old = set(split_courses([c['name'] for c in courses], term)['old'])
    courses = [c for c in courses if c['name'] not in old]
    if not courses:
        raise RuntimeError(f"{section}: Classroom returned no current classes.")

    rosters, failed = {}, []
    records = []
    for i, course in enumerate(courses, 1):
        progress(f"{section}: reading class {i} of {len(courses)}",
                 pct_from + (pct_to - pct_from) * (i - 1) / len(courses), log=(i == 1))
        try:
            students = {s['userId']: s['profile']['name']['fullName']
                        for s in list_all(service.courses().students().list, 'students', courseId=course['id'], pageSize=100)}
            works = {w['id']: w for w in list_all(service.courses().courseWork().list, 'courseWork', courseId=course['id'], pageSize=100)}
            # courseWorkId='-' = every assignment's submissions in one list, far fewer calls than the ingest.
            subs = list_all(service.courses().courseWork().studentSubmissions().list, 'studentSubmissions',
                            courseId=course['id'], courseWorkId='-', pageSize=500) if works else []
        except Exception as e:
            print(f"    class {i} failed, keeping its previous grades: {str(e)[:150]}")
            failed.append(course['name'])
            continue
        rosters[course['name']] = students
        book = course.get('gradebookSettings', {})
        cats = {c['id']: (c['name'], c.get('weight', 0) / 10000) for c in book.get('gradeCategories', [])}
        for sub in subs:
            name, work = students.get(sub['userId']), works.get(sub.get('courseWorkId'))
            if not name or not work:
                continue
            cat, weight = cats.get(work.get('gradeCategory', {}).get('id'), ('No Category', 0))
            records.append({
                'snapshot_date': stamp, 'section': section, 'student_name': name, 'course_name': course['name'],
                'assignment_title': work['title'], 'category': cat, 'category_weight': weight,
                'assigned_score': sub.get('assignedGrade'), 'draft_score': sub.get('draftGrade'),
                'max_points': work.get('maxPoints'), 'is_late': sub.get('late', False), 'is_excused': False,
                'state': sub.get('state'), 'assignment_id': work['id'], 'student_id': sub['userId'],
                'grade_calculation': book.get('calculationType'), 'grade_display': book.get('displaySetting'),
                'semester': term_label(term),
            })

    # Same grade-level sources as the ingest and enrich scripts: the list-checked value in the
    # database first, then the Homeroom or PBL class the student is in.
    from_classes = {}
    for keyword, pattern in (('pbl', r'(?:G|Grade|Year)\s?(\d{1,2})'), ('homeroom', r'(?:G|Grade|Year)?\s?(\d{1,2})')):
        for name, students in rosters.items():
            if keyword in name.lower():
                m = re.search(pattern, name, re.I)
                if m and 5 <= int(m.group(1)) <= 12:
                    from_classes.update({sid: f"Grade {int(m.group(1))}" for sid in students})
    for r in records:
        k = known.get(r['student_id'], {})
        level = k.get('grade_level')
        r['grade_level'] = level if level and level != 'Unknown' else from_classes.get(r['student_id'], 'Unknown')
        r['student_email'], r['gender'], r['attended_jh'] = k.get('student_email'), k.get('gender'), k.get('attended_jh')
    return records, failed, len(courses)


def known_students(engine):
    """{student_id: fields the ingest doesn't get from Classroom}, filled by the enrich scripts."""
    known = {}
    with engine.connect() as conn:
        for sid, email in conn.execute(text("SELECT student_id, MAX(student_email) FROM student_grade_snapshots "
                                            "WHERE student_email IS NOT NULL GROUP BY student_id")):
            known[sid] = {'student_email': email}
        for sid, level, gender, jh, email in conn.execute(text("""
                WITH latest AS (SELECT section, MAX(snapshot_date) AS snap FROM student_grade_snapshots GROUP BY section)
                SELECT DISTINCT s.student_id, s.grade_level, s.gender, s.attended_jh, s.student_email
                FROM student_grade_snapshots s JOIN latest l ON s.section = l.section AND s.snapshot_date = l.snap""")):
            k = known.setdefault(sid, {})
            k.update(grade_level=level, gender=gender, attended_jh=jh)
            if email:
                k['student_email'] = email
    return known


def previous_rows(engine, section):
    """(snapshot_date, table) the dashboard currently uses for this school, or (None, None)."""
    with engine.connect() as conn:
        best = conn.execute(text("SELECT MAX(snapshot_date) FROM student_grade_snapshots WHERE section = :s"), {'s': section}).scalar()
        table = 'student_grade_snapshots'
        if inspect(conn).has_table(LIVE_TABLE):
            live = conn.execute(text(f"SELECT MAX(snapshot_date) FROM {LIVE_TABLE} WHERE section = :s"), {'s': section}).scalar()
            if live and (not best or str(live) > str(best)):
                best, table = live, LIVE_TABLE
    return (best, table) if best else (None, None)


def build_staging(engine, df, stamp, failed_by_school, usable):
    """Staging table = the new rows, plus previous rows for classes that failed; grades calculated there."""
    with engine.begin() as conn:
        conn.execute(text(f'DROP TABLE IF EXISTS {STAGING_TABLE}'))
        conn.execute(text(f'CREATE TABLE {STAGING_TABLE} AS SELECT * FROM student_grade_snapshots WHERE 0'))
        cols = {c['name'] for c in inspect(conn).get_columns(STAGING_TABLE)}
        for extra in sorted(set(df.columns) - cols):   # e.g. draft_score before the first ingest that stores it
            conn.execute(text(f'ALTER TABLE {STAGING_TABLE} ADD COLUMN "{extra}" FLOAT'))
    df.to_sql(STAGING_TABLE, engine, if_exists='append', index=False)
    names = [c['name'] for c in inspect(engine).get_columns(STAGING_TABLE)]
    with engine.begin() as conn:
        for section, failed in failed_by_school.items():
            snap, table = previous_rows(engine, section) if section in usable else (None, None)
            if not snap or not failed:
                continue
            have = {c['name'] for c in inspect(conn).get_columns(table)}
            select = ', '.join(f'"{c}"' if c in have else f'NULL AS "{c}"' for c in names if c != 'snapshot_date')
            insert = ', '.join(f'"{c}"' for c in names if c != 'snapshot_date')
            for course in failed:
                conn.execute(text(f'INSERT INTO {STAGING_TABLE} ("snapshot_date", {insert}) SELECT :stamp, {select} '
                                  f'FROM {table} WHERE section = :s AND snapshot_date = :snap AND course_name = :c'),
                             {'stamp': stamp, 's': section, 'snap': snap, 'c': course})
    populate_category_grades(engine, table=STAGING_TABLE)
    populate_overall_grades(engine, table=STAGING_TABLE)


def publish_staging(engine, sections):
    """Replaces these schools' rows in live_grades in one transaction, then removes the staging table."""
    with engine.begin() as conn:
        names = [c['name'] for c in inspect(conn).get_columns(STAGING_TABLE)]
        if inspect(conn).has_table(LIVE_TABLE):
            if {c['name'] for c in inspect(conn).get_columns(LIVE_TABLE)} != set(names):
                conn.execute(text(f'DROP TABLE {LIVE_TABLE}'))   # disposable: the next sync falls back to snapshots
        if not inspect(conn).has_table(LIVE_TABLE):
            conn.execute(text(f'CREATE TABLE {LIVE_TABLE} AS SELECT * FROM {STAGING_TABLE} WHERE 0'))
            conn.execute(text(f'CREATE INDEX IF NOT EXISTS ix_{LIVE_TABLE}_section ON {LIVE_TABLE} (section)'))
        col_sql = ', '.join(f'"{c}"' for c in names)
        for section in sections:
            conn.execute(text(f'DELETE FROM {LIVE_TABLE} WHERE section = :s'), {'s': section})
            conn.execute(text(f'INSERT INTO {LIVE_TABLE} ({col_sql}) SELECT {col_sql} FROM {STAGING_TABLE} WHERE section = :s'), {'s': section})
        conn.execute(text(f'DROP TABLE {STAGING_TABLE}'))


def compare_with_snapshot(engine):
    """Read-only check: overall grades in live_grades vs each school's latest snapshot."""
    print("\n--- live_grades vs latest snapshot ---")
    with engine.connect() as conn:
        for section, in conn.execute(text(f"SELECT DISTINCT section FROM {LIVE_TABLE}")).fetchall():
            snap = conn.execute(text("SELECT MAX(snapshot_date) FROM student_grade_snapshots WHERE section = :s"), {'s': section}).scalar()
            rows = {}
            for name, table, cond in (('live', LIVE_TABLE, ''), ('snap', 'student_grade_snapshots', 'AND snapshot_date = :snap')):
                rows[name] = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(text(
                    f"SELECT student_id, course_name, MAX(overall_grade), COUNT(*) FROM {table} WHERE section = :s {cond} "
                    f"GROUP BY 1, 2"), {'s': section, 'snap': snap})}
            live, saved = rows['live'], rows['snap']
            both = set(live) & set(saved)
            same = sum(1 for k in both if (live[k][0] is None and saved[k][0] is None) or
                       (live[k][0] is not None and saved[k][0] is not None and abs(live[k][0] - saved[k][0]) < 0.011))
            sameasg = sum(1 for k in both if live[k][1] == saved[k][1])
            print(f"  {section}: snapshot {str(snap)[:16]} | student-course pairs: live {len(live)}, snapshot {len(saved)}, in both {len(both)}")
            print(f"    same overall grade: {same} of {len(both)}; same number of assignment rows: {sameasg}; "
                  f"only live: {len(set(live) - both)}; only snapshot: {len(set(saved) - both)}")


def refresh(args, progress):
    started = time.time()
    engine = get_db_engine()
    schools = [s for s in SCHOOLS if not args.school or s['name'] == args.school]
    stamp = datetime.datetime.now()
    known = known_students(engine)

    span = 68.0 / len(schools)
    records, failed_by_school, usable, problems, classes = [], {}, [], [], 0
    for i, school in enumerate(schools):
        section = school['name']
        try:
            rows, failed, n = pull_school(school, stamp, known, progress, 2 + span * i, 2 + span * (i + 1))
        except Exception as e:
            print(f"    {section} failed: {e}")
            problems.append(f"{section} not updated, its previous grades stay on the dashboard "
                            f"({'needs sign-in: run authenticate.py' if 'invalid_grant' in str(e) or 'authenticate.py' in str(e) else 'Classroom error'}).")
            continue
        snap, table = previous_rows(engine, section)
        before = 0
        if snap:
            with engine.connect() as conn:
                before = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE section = :s AND snapshot_date = :snap"),
                                      {'s': section, 'snap': snap}).scalar()
        if failed:
            problems.append(f"{section}: {len(failed)} of {n} classes could not be read and keep their previous grades.")
        kept_rows = 0
        if failed and snap:
            ph = ', '.join(f':c{j}' for j in range(len(failed)))
            with engine.connect() as conn:
                kept_rows = conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE section = :s AND snapshot_date = :snap AND course_name IN ({ph})"),
                                         {'s': section, 'snap': snap, **{f'c{j}': c for j, c in enumerate(failed)}}).scalar()
        # Guard: a much smaller pull (a class list problem, say) must not replace good data.
        if before and len(rows) + kept_rows < before * MIN_KEEP_SHARE:
            problems.append(f"{section} not updated: Classroom returned far fewer grades than usual, so its previous grades stay.")
            print(f"    {section}: only {len(rows) + kept_rows} rows, usually {before}. Not used.")
            continue
        records += rows
        failed_by_school[section] = failed
        usable.append(section)
        classes += n
        print(f"    {section}: {n} classes, {len(rows)} rows")

    if usable:
        progress("Calculating grades", 72)
        df = pd.DataFrame(records)
        for col in ('max_points', 'assigned_score', 'draft_score', 'category_weight'):
            df[col] = pd.to_numeric(df[col], errors='coerce')
        df['max_points'] = df['max_points'].fillna(0)   # as the ingest does
        build_staging(engine, df, stamp, failed_by_school, usable)
        publish_staging(engine, usable)
    if args.compare and usable:
        compare_with_snapshot(engine)

    message_time = f"{time.time() - started:.0f} seconds"
    if args.no_upload:
        return usable, problems, f"Pulled {classes} classes in {message_time}. Nothing sent to the dashboard (--no-upload)."

    def sync_progress(stage, pct):
        progress(stage, pct, log=not stage.startswith('Uploading file'))
    progress("Building the dashboard files", 78)
    import sync_dashboard
    sync_dashboard.sync(progress=sync_progress)
    return usable, problems, f"Done in {time.time() - started:.0f} seconds: {', '.join(usable) or 'no school'} updated."


def main():
    parser = argparse.ArgumentParser(description="Pull current grades from Classroom into live_grades and update the dashboard.")
    parser.add_argument('--school', choices=[s['name'] for s in SCHOOLS], help="Only this school.")
    parser.add_argument('--no-upload', action='store_true', help="Update live_grades only; nothing is sent to Google Drive.")
    parser.add_argument('--compare', action='store_true', help="Print a comparison with the latest snapshot.")
    parser.add_argument('--log', help="Also append all output to this file.")
    parser.add_argument('--request-id', help=argparse.SUPPRESS)
    parser.add_argument('--requested-by', help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.log:
        sys.stdout = sys.stderr = Tee(args.log)
    os.makedirs('logs', exist_ok=True)
    lock = open(LOCK_FILE, 'a+')
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print("Another dashboard update is already running; nothing to do.")
        sys.exit(ALREADY_RUNNING)

    print(f"--- Dashboard update from Google Classroom: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}"
          f"{' (requested by ' + args.requested_by + ')' if args.requested_by else ''} ---")
    progress = Progress(args.request_id, args.requested_by, upload=not args.no_upload)
    progress.connect()
    progress("Starting", 1)
    try:
        usable, problems, message = refresh(args, progress)
    except Exception as e:
        print(f"ERROR: {e}")
        progress.finish('error', "The update stopped because of an error, so the dashboard keeps its previous grades. "
                                 "The grades computer's log has the details.")
        sys.exit(1)
    if not usable:
        progress.finish('error', ' '.join(problems) or "Nothing was updated.")
        sys.exit(1)
    progress.finish('done', ' '.join([message] + problems))


if __name__ == '__main__':
    main()

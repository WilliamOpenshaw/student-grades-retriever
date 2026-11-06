"""
Grade Report Generator
Generates one self-contained HTML file per student from the latest snapshot
in student_grade_snapshots. Output saved to ./grade_reports/<StudentName>.html

Usage:
    python generate_grade_reports.py
"""

import os
import re
import math
import argparse
import base64
import pandas as pd
from sqlalchemy import text
from html import escape
from datetime import datetime, timedelta

from db import get_db_engine as get_engine, parse_db_datetime
from school_config import REPORT_LOGO_FILE, SCHOOL_SHORT_NAME
from report_exclusions import load_exclusions
from report_folders import new_run_folder, write_manifest
from grade_visibility_db import log_visibility_check
from student_selection import add_student_options, selected_students, norm, report_missing
from course_terms import current_term, split_courses, term_label, log_term_check

# --- CONFIGURATION ---
SECTION_NAMES = {'HS': 'High School', 'JH': 'Junior High'}


# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------

def get_logo_base64(filepath):
    try:
        with open(filepath, "rb") as img_file:
            encoded_string = base64.b64encode(img_file.read()).decode('utf-8')
            return f"data:image/png;base64,{encoded_string}"
    except FileNotFoundError:
        return ""  # Returns empty if the logo file is missing




def latest_snapshots(conn, school_filter):
    """
    Latest snapshot per section (HS and JH are often fetched on different days, so a single
    MAX(snapshot_date) would leave one school out). Returns [(section, snapshot_date)] and
    warning lines for snapshots that look stale or incomplete.
    """
    rows = conn.execute(text("""
        SELECT section, snapshot_date, COUNT(DISTINCT course_name)
        FROM student_grade_snapshots
        GROUP BY section, snapshot_date
        ORDER BY section, snapshot_date DESC
    """)).fetchall()

    history = {}
    for section, snap, n_courses in rows:
        history.setdefault(section, []).append((snap, n_courses))
    if school_filter in SECTION_NAMES:
        history = {s: h for s, h in history.items() if s == SECTION_NAMES[school_filter]}

    today_term = current_term()
    picked, warnings = [], []
    for section, snaps in history.items():
        snap, n_courses = snaps[0]
        picked.append((section, snap))
        snap_term = current_term(snap)
        print(f"  {section}: latest snapshot {str(snap)[:16]} ({term_label(snap_term)}, {n_courses} courses)")
        if snap_term != today_term:
            warnings.append(f"  WARNING: {section}'s latest snapshot is from {term_label(snap_term)}, but it is now "
                            f"{term_label(today_term)}. Run ingest_classroom_data.py first for current grades.")
        # A run with --course / --course-filter / -s saves a snapshot holding only part of the school
        if len(snaps) > 1 and n_courses < 0.75 * snaps[1][1]:
            warnings.append(f"  WARNING: {section}'s latest snapshot has {n_courses} courses vs {snaps[1][1]} in the "
                            f"one before - it may be a partial (filtered) run. Re-run ingest without filters.")
    return picked, warnings


def fetch_data(engine, target_students=None, school_filter='ALL'):
    with engine.connect() as conn:
        snapshots, warnings = latest_snapshots(conn, school_filter)

        # Fetch semesters to find the latest one chronologically
        semesters = conn.execute(
            text('SELECT DISTINCT semester FROM "Student_Name_List_HS" WHERE semester IS NOT NULL')
        ).fetchall()

    if not snapshots:
        raise RuntimeError("No snapshot data found in student_grade_snapshots.")
    latest = max(snap for _, snap in snapshots)

    # Find the latest semester mathematically (same logic as the audit script)
    latest_sem = None
    if semesters:
        sems = [str(r[0]).strip() for r in semesters if r[0] and str(r[0]).lower() != 'nan']
        def semester_sort_key(sem_str):
            import re
            year_match = re.search(r'\d{4}', sem_str)
            year = int(year_match.group(0)) if year_match else 0
            sem_lower = sem_str.lower()
            season_val = 2 if 'fall' in sem_lower else (1 if 'spring' in sem_lower else 0)
            return (year, season_val)
        if sems:
            latest_sem = max(sems, key=semester_sort_key)

    print(f"  Targeting student DB semester: {latest_sem}")
    if latest_sem and latest_sem != term_label(current_term(latest)):
        warnings.append(f"  WARNING: parent emails come from the {latest_sem} name list, but the grades are from "
                        f"{term_label(current_term(latest))}.")

    # Build the base query (Keep your existing CTEs and SELECT statements exactly as they are)
    base_query = """
                 WITH hs_parents AS (
                     SELECT google_classroom_id, "Parent email" AS parent_emails
                     FROM "Student_Name_List_HS"
                     WHERE semester = :sem AND google_classroom_id IS NOT NULL
                 ),
                 jh_parents AS (
                     SELECT google_classroom_id, 
                            CONCAT_WS(',', 
                                NULLIF(TRIM(father_email), ''), 
                                NULLIF(TRIM(mother_email), ''), 
                                NULLIF(TRIM(extra_gaurdian_email), '')
                            ) AS parent_emails
                     FROM student_name_list_jh
                     WHERE semester = :sem AND google_classroom_id IS NOT NULL
                 )
                 SELECT sgs.student_name,
                        sgs.student_id,
                        sgs.student_email,
                        sgs.grade_level,
                        sgs.section,
                        sgs.course_name,
                        sgs.semester,
                        sgs.assignment_title,
                        sgs.assignment_id,
                        sgs.category,
                        sgs.category_weight,
                        sgs.assigned_score,
                        sgs.max_points,
                        sgs.is_late,
                        sgs.is_excused,
                        sgs.state,
                        sgs.overall_grade,
                        sgs.grade_display,
                        sgs.student_grade_in_category,
                        COALESCE(hs.parent_emails, jh.parent_emails) AS parent_emails
                 FROM student_grade_snapshots sgs
                 LEFT JOIN hs_parents hs ON sgs.student_id = hs.google_classroom_id
                 LEFT JOIN jh_parents jh ON sgs.student_id = jh.google_classroom_id
                 WHERE (""" + " OR ".join(
                     f"(sgs.section = :sec{i} AND sgs.snapshot_date = :snap{i})" for i in range(len(snapshots))
                 ) + ")"
    params = {"sem": latest_sem}
    for i, (section, snap) in enumerate(snapshots):
        params[f"sec{i}"] = section
        params[f"snap{i}"] = snap

    # --- NEW: Apply the School Filter ---
    if school_filter == 'HS':
        base_query += " AND hs.google_classroom_id IS NOT NULL"
    elif school_filter == 'JH':
        base_query += " AND jh.google_classroom_id IS NOT NULL"

    # (Selected students are filtered after loading, so names match the same way everywhere -
    # see student_selection.py)

    base_query += " ORDER BY sgs.student_name, sgs.course_name, sgs.category, sgs.assignment_title"

    df = pd.read_sql(text(base_query), engine, params=params)

    df = drop_past_semester_courses(df, snapshots, warnings)
    if target_students:
        wanted = {norm(n) for n in target_students}
        df = df[df['student_name'].map(norm).isin(wanted)]
        report_missing(target_students, df['student_name'].unique(), 'the latest grades for this school')
    return df, parse_db_datetime(latest)


def drop_past_semester_courses(df, snapshots, warnings):
    """Safety net for snapshots saved before ingest filtered these out (see course_terms.py)."""
    lines = list(warnings)
    for section, snap in snapshots:
        term = current_term(snap)
        groups = split_courses(df.loc[df['section'] == section, 'course_name'].dropna().unique(), term)
        if groups['old']:
            lines.append(f"  [{section}] EXCLUDED past-semester courses (not {term_label(term)}):")
            lines += [f"    - {n}" for n in sorted(groups['old'])]
            df = df[~((df['section'] == section) & df['course_name'].isin(groups['old']))]
        if groups['unknown']:
            lines.append(f"  [{section}] Kept, but no semester found in the name:")
            lines += [f"    - {n}" for n in sorted(groups['unknown'])]
    if lines:
        log_term_check('generate_grade_reports.py', lines)
    return df

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------

def safe(val):
    if val is None:
        return ''
    if isinstance(val, float) and math.isnan(val):
        return ''
    return str(val).strip()


def letter_grade(pct):
    if pct is None:  # Use pct here
        return ""
    if pct >= 85:  # Use pct here
        return "A"
    elif pct >= 80:
        return "AB"
    elif pct >= 75:
        return "B"
    elif pct >= 70:
        return "BC"
    elif pct >= 60:
        return "C"
    elif pct >= 50:
        return "D"
    else:
        return "F"


def grade_color(score):
    if score is None:
        return '#8F8F8F'
    if score >= 70:
        return '#00BB00'
    if 50 < score < 70:
        return '#F28100'
    if score <= 50:
        return '#CF0101'
    return None


def grade_bg(score):
    if score is None:
        return '#E0E0E0'
    if score >= 70:
        return '#B9FFBB'
    if 50 < score < 70:
        return '#FFD58F'
    if score <= 50:
        return '#FF8F8D'
    return None


# ---------------------------------------------------------------------------
# Data aggregation
# ---------------------------------------------------------------------------

def build_student_data(df):
    students = {}

    for student_name, s_df in df.groupby('student_name', sort=True):
        row0 = s_df.iloc[0]

        # Extract and split parent emails by comma
        parent_emails_raw = str(row0.get('parent_emails', ''))
        if parent_emails_raw.lower() == 'nan':
            parent_emails_raw = ''

        # Split by comma and clean up whitespace, dropping literal 'nan' strings
        guardians = [email.strip() for email in parent_emails_raw.split(',')
                     if email.strip() and email.strip().lower() != 'nan']

        info = {
            'name': str(student_name),
            'email': str(row0.get('student_email', '')),
            'guardian_1_email': guardians[0] if len(guardians) > 0 else '',
            'guardian_2_email': guardians[1] if len(guardians) > 1 else '',
            'guardian_3_email': guardians[2] if len(guardians) > 2 else '',
            'grade_level': str(row0.get('grade_level', '')),
            'section': str(row0.get('section', '')),
        }

        courses = []

        for course_name, c_df in s_df.groupby('course_name', sort=False):
            # Overall grade (consistent across all rows of this course)
            og_series = c_df['overall_grade'].dropna()
            overall = float(og_series.iloc[0]) if not og_series.empty else None

            # "No overall grade" in Classroom -> no overall grade on the report ("This Class is Not Graded").
            # "Show overall grade to teachers only" IS shown on reports (user decision 2026-10-13,
            # as before 2026-10-05): the grade is calculated the usual way.
            display = c_df['grade_display'].dropna()
            not_graded = not display.empty and display.iloc[0] == 'HIDE_OVERALL_GRADE'
            if not_graded:
                overall = None

            sem_series = c_df['semester'].dropna()
            semester = safe(sem_series.iloc[0]) if not sem_series.empty else ''

            # Categories: (category_name, weight) → student_grade_in_category
            cat_dict = {}
            for _, row in c_df.iterrows():
                cat = safe(row['category']) or 'No Category'
                wt = float(row['category_weight']) if pd.notna(row['category_weight']) else 0.0
                key = (cat, wt)
                if key not in cat_dict:
                    cgv = row['student_grade_in_category']
                    cat_dict[key] = float(cgv) if pd.notna(cgv) else None
                elif cat_dict[key] is None:
                    cgv = row['student_grade_in_category']
                    if pd.notna(cgv):
                        cat_dict[key] = float(cgv)

            categories = sorted(
                [{'name': k[0], 'weight': k[1], 'grade': v}
                 for k, v in cat_dict.items()],
                key=lambda x: x['weight'],
                reverse=True
            )

            # Assignments: one row per Classroom assignment. Use the ID, not the title: a class can
            # have different assignments with the same title (e.g. three called "Exit Ticket").
            seen = set()
            assignments = []
            for _, row in c_df.iterrows():
                title = safe(row['assignment_title'])
                key = safe(row['assignment_id']) or title
                if not title or key in seen:
                    continue
                seen.add(key)

                cat = safe(row['category']) or 'No Category'
                # --- NEW: Rename category for the assignments table display ---
                if cat == 'No Category':
                    cat = 'No Category 沒有類別'
                # --------------------------------------------------------------

                assignments.append({
                    'title': title,
                    'category': cat,
                    'weight': float(row['category_weight']) if pd.notna(row['category_weight']) else 0,
                    'assigned': float(row['assigned_score']) if pd.notna(row['assigned_score']) else None,
                    'max': float(row['max_points']) if pd.notna(row['max_points']) else None,
                    'is_late': bool(row['is_late']) if pd.notna(row['is_late']) else False,
                    'is_excused': bool(row['is_excused']) if pd.notna(row['is_excused']) else False,
                })

            courses.append({
                'name': safe(course_name),
                'overall_grade': overall,
                'not_graded': not_graded,
                'semester': semester,
                'categories': categories,
                'assignments': assignments,
            })

        students[student_name] = {'info': info, 'courses': courses}

    return students


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------

CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto,
                 'Helvetica Neue', Arial, sans-serif;
    /* Changed to a soft, calming slate/blue-gray */
    background: #f0f4f8; 
    color: #1f2937;
    font-size: 14px;
    line-height: 1.55;
}

.page {
    max-width: 880px;
    margin: 0 auto;
    padding: 28px 16px 56px;
}

/* ── Header ─────────────────────────────── */
.report-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    background: #ffffff;
    padding: clamp(12px, 3.5vw, 22px) clamp(14px, 4vw, 28px);
    border-radius: 14px;
    box-shadow: 0 4px 12px rgba(0, 0, 0, 0.05);
    color: #1e3a8a;
    /* This creates the gap between the header and the student card */
    margin-bottom: 20px;
}

.header-left {
    flex-shrink: 0;
}

.header-right-group {
    display: flex;
    align-items: center; /* Vertical center alignment for the row */
    gap: clamp(8px, 2.5vw, 20px);
    flex-shrink: 0;
}

.title-block {
    display: flex;
    flex-direction: column;
    align-items: flex-end; /* Align Grade Report text to the right */
}

.separator-bar {
    /* Increased thickness to 3px for a bolder, cleaner look */
    width: 3px;
    height: clamp(32px, 7vw, 45px);
    background-color: #e5e7eb;
    /* Adding a slight curve to the bar ends makes it look more modern */
    border-radius: 999px;
    transform: translateY(1px);
    flex-shrink: 0;
}

.info-block {
    display: flex;
    flex-direction: column;
    align-items: flex-start; /* Align Semester/Date text to the left */
}

.header-main-text {
    font-size: clamp(12px, 3.5vw, 17px);
    font-weight: bold;
    line-height: 1.4;
}

.semester-label {
    font-size: clamp(12px, 3.5vw, 17px);
    font-weight: bold;
    line-height: 1.4;
    transform: translateY(1px);
}

.header-sub {
    font-size: clamp(12px, 3.5vw, 17px);
    font-weight: bold;
    color: #1e3a8a;
}

/* ── Student card ────────────────────────── */
.student-card {
    background: #fff;
    border-radius: 12px;
    padding: 18px 24px;
    margin-bottom: 20px; 
    box-shadow: 0 1px 5px rgba(0,0,0,0.07);
    display: flex;
    justify-content: space-between; /* Identity Left, Emails Right */
    align-items: center;
}

.student-identity {
    display: flex;
    align-items: center;
    gap: 15px;
}

.student-name {
    font-size: 22px;
    font-weight: 800;
    color: #1b3a6b;
}

.grade-pill {
    background: #f1f5f9;
    color: #1e3a8a;
    padding: 4px 10px;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.contact-info {
    display: flex;
    flex-direction: column;
    align-items: flex-end; /* Right-align the text */
    gap: 3px;
}

.email-item {
    font-size: 12px;
    color: #4b5563;
}

.email-label {
    font-weight: 700;
    color: #9ca3af;
    font-size: 10px;
    text-transform: uppercase;
    margin-right: 6px;
}

/* ── Course cards ────────────────────────── */
.course-card {
    background: #fff;
    border-radius: 12px;
    margin-bottom: 14px;
    box-shadow: 0 1px 5px rgba(0,0,0,0.07);
    overflow: hidden;
}
.course-header {
    padding: 14px 20px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    border-bottom: 2px solid #f0f2f5;
}
.course-name {
    font-size: 14.5px;
    font-weight: 700;
    color: #1f2937;
    flex: 1;
}
.overall-wrap {
    display: flex;
    align-items: center;
    gap: 9px;
    flex-shrink: 0;
}
.overall-pct {
    font-size: 26px;
    font-weight: 800;
    line-height: 1;
}
.overall-letter {
    font-size: 13px;
    font-weight: 700;
    padding: 4px 10px;
    border-radius: 7px;
}

/* ── Category bars ───────────────────────── */
.categories-section {
    padding: 14px 20px 10px;
    border-bottom: 1px solid #f0f2f5;
}
.section-label {
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.9px;
    color: #9ca3af;
    margin-bottom: 10px;
}
.cat-row { margin-bottom: 9px; }
.cat-meta {
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    margin-bottom: 4px;
    gap: 8px;
}
.cat-name {
    font-size: 13px;
    font-weight: 500;
    color: #374151;
    flex: 1;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
}
.cat-wt {
    font-size: 11px;
    color: #9ca3af;
    font-weight: 400;
    margin-left: 4px;
}
.cat-score { font-size: 12px; color: #6b7280; white-space: nowrap; }
.cat-score.none { font-style: italic; color: #9ca3af; }
.bar-bg {
    height: 7px;
    background: #e5e7eb;
    border-radius: 999px;
    overflow: hidden;
}
.bar-fill { height: 100%; border-radius: 999px; }

/* ── Assignments ─────────────────────────── */
details.asgmt-details { }
details.asgmt-details > summary {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 10px 20px;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: #9ca3af;
    cursor: pointer;
    list-style: none;
    user-select: none;
    transition: color 0.15s;
}
details.asgmt-details > summary::-webkit-details-marker { display: none; }
details.asgmt-details > summary::before {
    content: '▶';
    font-size: 8px;
    transition: transform 0.2s;
    display: inline-block;
}
details[open].asgmt-details > summary::before { transform: rotate(90deg); }
details.asgmt-details > summary:hover { color: #6b7280; background: #fafafa; }

table.asgmt-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12.5px;
}
table.asgmt-table th {
    padding: 7px 14px;
    text-align: left;
    background: #f9fafb;
    font-size: 10.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #9ca3af;
    border-bottom: 1px solid #f0f2f5;
}
table.asgmt-table td {
    padding: 7px 14px;
    border-bottom: 1px solid #f9fafb;
    color: #374151;
    vertical-align: middle;
}
table.asgmt-table tr:last-child td { border-bottom: none; }
table.asgmt-table tr:hover td { background: #fafafa; }

.tag {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 700;
    margin-left: 5px;
    vertical-align: middle;
}
.tag-late    { background: #fee2e2; color: #dc2626; }
.tag-excused { background: #fef9c3; color: #a16207; }

.score-right { text-align: right; font-weight: 600; white-space: nowrap; }
.score-na    { color: #9ca3af; font-style: italic; font-weight: 400; }

/* ── Mobile ──────────────────────────────── */
@media (max-width: 480px) {
    .student-card {
        flex-direction: column;
        align-items: flex-start;
        gap: 10px;
    }
    .contact-info {
        align-items: flex-start;
    }
    .student-name {
        font-size: clamp(16px, 5.5vw, 22px);
    }
}

/* ── Print ───────────────────────────────── */
@media print {
    body { background: #fff; font-size: 12px; }
    .page { max-width: 100%; padding: 0; }
    .report-header { border-radius: 0; box-shadow: none; }
    .course-card {
        break-inside: avoid;
        box-shadow: none;
        border: 1px solid #e5e7eb;
        margin-bottom: 10px;
    }
    details.asgmt-details > summary { display: none; }
    details.asgmt-details { display: block; }
}
"""


def _cat_bar_html(cat):
    name = escape(cat['name'])

    # --- NEW: Check for No Category ---
    if name == 'No Category':
        msg = "Assignments with No Category do not affect your Grade<br>未分類的作業不會影響您的成績"
        # Return just the text without the score or the background bar
        return f'<div class="cat-row"><div class="cat-meta" style="color:#6b7280; font-style:italic; font-size:12px; white-space:normal;">{msg}</div></div>'
    # ----------------------------------

    wt = cat['weight']
    grade = cat['grade']

    if grade is None:
        score_html = '<span class="cat-score none">No grades yet &nbsp;尚無成績</span>'
        bar_html = '<div class="bar-bg"><div class="bar-fill" style="width:0%;background:#e5e7eb"></div></div>'
    else:
        # 'grade' is already a percentage out of 100 from the database.
        pct = grade
        pct_c = min(max(pct, 0), 100)
        col = grade_color(pct)

        # Display the category score directly as a percentage
        score_html = f'<span class="cat-score">{pct:.2f}%</span>'
        bar_html = f'<div class="bar-bg"><div class="bar-fill" style="width:{pct_c:.1f}%;background:{col}"></div></div>'

    return f'<div class="cat-row"><div class="cat-meta"><span class="cat-name">{name}<span class="cat-wt">{wt:g}%</span></span>{score_html}</div>{bar_html}</div>'


def _score_html(assigned, max_pts):
    if assigned is None:
        return '<span class="score-na">—</span>'
    if max_pts:
        pct = (assigned / max_pts) * 100
        col = grade_color(pct)
        return f'<span style="color:{col}">{assigned:g}&thinsp;/&thinsp;{max_pts:g}</span>'
    return f'{assigned:g}'


def _course_card_html(course):
    name = escape(course['name'])
    overall = course['overall_grade']

    # --- NEW LOGIC: Override 0% if there are no graded assignments ---
    # Check if at least one assignment has a recorded score
    has_grades = any(a['assigned'] is not None for a in course['assignments'])

    if not has_grades:
        overall = None
    # -----------------------------------------------------------------

    # "No overall grade" class -> "Not Graded"; nothing graded yet -> "No Grade Yet";
    # otherwise render the percentage and letter grade
    if overall is None:
        # Two lines for the longer label, so it fits next to the class name on a phone
        no_grade_label = ('This Class is Not Graded<br>本課程不計成績' if course.get('not_graded')
                          else 'No Grade Yet &nbsp;尚無成績')
        overall_html = (f'<div class="overall-wrap">'
                        f'<span class="overall-pct" style="color:#9ca3af; font-size: 14px; font-weight: 600; font-style: italic; line-height: 1.35; text-align: right; white-space: nowrap;">{no_grade_label}</span>'
                        f'</div>')
    else:
        col = grade_color(overall)
        bg = grade_bg(overall)
        letter = letter_grade(overall)
        pct_str = f'{overall:.2f}%'

        overall_html = (f'<div class="overall-wrap">'
                        f'<span class="overall-pct" style="color:{col}">{pct_str}</span>'
                        f'<span class="overall-letter" style="color:{col};background:{bg}">{letter}</span>'
                        f'</div>')

    # A not-graded class doesn't need the "No Category ... do not affect your Grade" note; if nothing
    # else is left, the Grade Categories section is left out. The assignment table is unchanged.
    categories = [c for c in course['categories']
                  if not (course.get('not_graded') and c['name'] == 'No Category')]
    cats_html = ''.join(_cat_bar_html(c) for c in categories)
    categories_section = (f'<div class="categories-section">'
                          f'<div class="section-label">Grade Categories 成績類別</div>'
                          f'{cats_html}</div>') if categories else ''

    # Assignments table
    rows_html = []
    for a in course['assignments']:
        title = escape(a['title'])
        cat = escape(a['category'])
        tags = ''
        if a['is_late']:    tags += '<span class="tag tag-late">Late</span>'
        if a['is_excused']: tags += '<span class="tag tag-excused">Excused</span>'
        sh = _score_html(a['assigned'], a['max'])
        wt_str = f'{a["weight"]:g}%' if a['weight'] else ''
        rows_html.append(
            f'<tr><td>{title}{tags}</td>'
            f'<td style="color:#6b7280">{cat}</td>'
            f'<td style="text-align:right;color:#9ca3af">{wt_str}</td>'
            f'<td class="score-right">{sh}</td></tr>'
        )

    if rows_html:
        asgmt_section = (
            f'<details class="asgmt-details">'
            f'<summary>Assignments 作業 ({len(course["assignments"])})</summary>'
            f'<table class="asgmt-table">'
            f'<thead><tr>'
            f'<th>Assignment 作業</th>'
            f'<th>Category 類別</th>'
            f'<th style="text-align:right">Weight 比重</th>'
            f'<th style="text-align:right">Score 成績</th>'
            f'</tr></thead>'
            f'<tbody>{"".join(rows_html)}</tbody>'
            f'</table></details>'
        )
    else:
        asgmt_section = ''

    return (f'<div class="course-card">'
            f'<div class="course-header">'
            f'<span class="course-name">{name}</span>{overall_html}'
            f'</div>'
            f'{categories_section}'
            f'{asgmt_section}'
            f'</div>')


def render_html(student, snapshot_date):
    info = student['info']
    courses = student['courses']
    name = escape(info['name'])


    if hasattr(snapshot_date, 'strftime'):
        first_day_current = snapshot_date.replace(day=1)
        prev_month_date = first_day_current - timedelta(days=1)
        report_month_str = str(prev_month_date.month) + "月 " + str(prev_month_date.strftime('%B %Y'))
    else:
        date_str = str(snapshot_date)[:10]

    if 2 <= prev_month_date.month <= 7:
        semester = "Spring Semester"
    else:
        semester = "Fall Semester"

    #email_html = (f'&#9993;&ensp;{escape(info["email"])}&emsp;' if info['email'] else '')
    #grade_html = (f'<span class="grade-pill">{escape(info["grade_level"])}</span>' if info['grade_level'] else '')

    # --- Contact Info (Student & Guardian Emails) ---
    contact_html = ""

    # 1. Student Email
    student_email = info.get('email', '')
    if student_email and student_email.lower() != 'nan':
        contact_html += f'<div class="email-item"><span class="email-label">STUDENT</span> {escape(student_email)}</div>'

    # 2. Guardian Emails
    g1 = info.get('guardian_1_email', '')
    if g1:
        contact_html += f'<div class="email-item"><span class="email-label">GUARDIAN 1</span> {escape(g1)}</div>'

    g2 = info.get('guardian_2_email', '')
    if g2:
        contact_html += f'<div class="email-item"><span class="email-label">GUARDIAN 2</span> {escape(g2)}</div>'

    g3 = info.get('guardian_3_email', '')
    if g3:
        contact_html += f'<div class="email-item"><span class="email-label">GUARDIAN 3</span> {escape(g3)}</div>'

    # Fallback if there are no emails to display
    if not contact_html:
        contact_html = '<div class="email-item" style="color: #9ca3af; font-style: italic;">No contact info on file</div>'

    courses_html = ''.join(_course_card_html(c) for c in courses)

    # --- NEW: Get the embedded logo ---
    # Logo file: REPORT_LOGO_FILE in .env (default report_logo.png in this folder); optional
    logo_src = get_logo_base64(REPORT_LOGO_FILE)

    # Only render the image tag if the logo was successfully found
    logo_html = f'<img src="{logo_src}" alt="{escape(SCHOOL_SHORT_NAME)} Logo" style="height: clamp(50px, 14vw, 80px); border-radius: 4px; object-fit: contain;">' if logo_src else ''

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Grade Report — {name}</title>
<style>{CSS}</style>
</head>
<body>
<div class="page">

  <div class="report-header">
    <div class="header-left">
      {logo_html}
    </div>

    <div class="header-right-group">
      
      <div class="title-block">
        <div class="header-main-text">Grade Report</div>
        <div class="header-main-text" style="letter-spacing: 4px; margin-right: -4px;">成績報告</div>
</div>

      <div class="separator-bar"></div>

      <div class="info-block">
        <div class="semester-label">{escape(semester)}</div>
        <div class="header-sub">{report_month_str}</div>
      </div>

    </div>
  </div>

  <div class="student-card">
        <div class="student-identity">
            <div class="student-name">{name}</div>
            <div class="grade-pill">{escape(info["grade_level"])}</div>
        </div>
        
        <div class="contact-info">
            {contact_html}
        </div>
    </div>

  {courses_html}

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def safe_filename(name):
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()


def main():
    # Setup command line argument parsing
    parser = argparse.ArgumentParser(description="Generate Grade Reports from the local database")

    # EXAMPLES OF USE:
    # -------------------------------------------------------------------------
    # 1. Generate ALL reports:
    #    python generate_grade_reports.py
    #
    # 2. Generate for ONE student:
    #    python generate_grade_reports.py -s "Jane Smith"
    #
    # 3. Generate only for High School:
    #    python generate_grade_reports.py --school HS
    # -------------------------------------------------------------------------

    add_student_options(parser, 'Only make a report for this student')
    # NEW: Add the school filter argument
    parser.add_argument(
        '--school',
        type=str,
        choices=['HS', 'JH', 'ALL'],
        default='ALL',
        help='Filter by school: "HS" for High School, "JH" for Junior High, or "ALL" for everyone (default).'
    )
    args = parser.parse_args()


    engine = get_engine()

    print("Connecting to database...")

    # NEW: Pass the requested school filter to fetch_data
    selected_names = selected_students(args)
    if selected_names:
        print(f"Only these {len(selected_names)} students: {', '.join(selected_names)}")
    df, snapshot_date = fetch_data(engine, target_students=selected_names, school_filter=args.school)

    if df.empty:
        print("No records found for the specified criteria.")
        return

    # Students who must not get a report: honors "(H)" students and report_exclusions.txt
    excluded = load_exclusions(engine)
    skipped = {}
    for name, sid, email in df[['student_name', 'student_id', 'student_email']].drop_duplicates().itertuples(index=False):
        reason = excluded.reason(student_id=sid, email=email, name=name)
        if reason:
            skipped[name] = reason
    if skipped:
        print(f"  Skipping {len(skipped)} students who don't get reports:")
        for name, reason in sorted(skipped.items()):
            print(f"    - {name}: {reason}")
        df = df[~df['student_name'].isin(skipped)]

    # Courses whose overall grade won't appear on these reports (teacher's Classroom setting)
    print()
    log_visibility_check('generate_grade_reports.py', engine,
                         {'HS': ['High School'], 'JH': ['Junior High']}.get(args.school))
    print()

    total_students = df['student_name'].nunique()
    print(f"  {len(df):,} rows  |  {total_students} students")

    print("Building student data...")
    students = build_student_data(df)

    # Each run gets its own folder (grade_reports\<timestamp>_<HS|JH|ALL>), so earlier runs are
    # kept and never mixed in. A run for selected students is marked _SELECTED and is never picked
    # automatically for sending (pass --folder).
    out_dir = new_run_folder(args.school, selected=bool(selected_names))
    rel_dir = os.path.relpath(out_dir).replace(os.sep, '/')

    print(f"Writing {len(students)} HTML reports to ./{rel_dir}/")
    sections = {}
    for student_name, student in students.items():
        html = render_html(student, snapshot_date)
        fname = safe_filename(student_name) + '.html'
        with open(os.path.join(out_dir, fname), 'w', encoding='utf-8') as f:
            f.write(html)
        sections[fname] = student['info'].get('section', '')
    write_manifest(out_dir, args.school, snapshot_date, sections, excluded=skipped)

    print(f"\nDone! {len(students)} reports saved to ./{rel_dir}/")
    if selected_names:
        folder = os.path.basename(out_dir)
        senders = {'HS': ['hs'], 'JH': ['jh']}.get(args.school, ['hs', 'jh'])
        print("This folder is only used for sending when you name it. Next:")
        print(f"  .venv\\Scripts\\python.exe audit_emails.py --folder {folder}")
        for sch in senders:
            print(f"  .venv\\Scripts\\python.exe send_emails_{sch}.py --folder {folder} --preview")


if __name__ == '__main__':
    main()

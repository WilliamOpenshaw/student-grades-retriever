'''
Shared database connection (local SQLite file).

All scripts call get_db_engine() from here, so the database location is set
in one place: DB_PATH in the .env file (default: grades.db in this folder).

Usage:
    from db import get_db_engine
    engine = get_db_engine()

SQLite notes for writing queries:
  * Regex:  use  "col REGEXP :pattern"  (Python regex; prefix the pattern with (?i)
    for case-insensitive). Postgres "~" / "~*" do not exist in SQLite.
  * UPDATE with an alias needs AS:  UPDATE student_grade_snapshots AS sgs SET ... FROM ...
  * Dates are stored as text 'YYYY-MM-DD HH:MM:SS.ffffff', so MAX(snapshot_date)
    returns a string. Use parse_db_datetime() if you need a datetime object.
  * "Latest snapshot": HS and JH are often fetched in separate runs, so use
    latest_snapshots_by_section() / latest_snapshot_filter() rather than a single
    MAX(snapshot_date), which would only cover whichever school ran last.
'''

import os
import re
import sqlite3
from datetime import datetime

from sqlalchemy import create_engine, event, text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, '.env')
DB_DATETIME_FORMAT = '%Y-%m-%d %H:%M:%S.%f'  # same format SQLAlchemy uses for SQLite DateTime columns


def load_env(path=ENV_FILE):
    """Minimal .env reader: KEY=VALUE lines, '#' comments ignored."""
    values = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip()
    return values


def get_db_path():
    path = os.environ.get('DB_PATH') or load_env().get('DB_PATH') or 'grades.db'
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def parse_db_datetime(value):
    """Turn a stored date string (e.g. from MAX(snapshot_date)) back into a datetime."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def latest_snapshots_by_section(conn):
    """{section: latest snapshot_date} for each section in student_grade_snapshots."""
    rows = conn.execute(text(
        "SELECT section, MAX(snapshot_date) FROM student_grade_snapshots GROUP BY section"
    )).fetchall()
    return {section: snap for section, snap in rows if snap}


def latest_snapshot_filter(latest, alias='sgs'):
    """
    SQL condition + params matching only each section's latest snapshot, for use in a WHERE:
        cond, params = latest_snapshot_filter(latest_snapshots_by_section(conn))
        conn.execute(text(f"SELECT ... FROM student_grade_snapshots sgs WHERE {cond}"), params)
    """
    if not latest:
        return '1 = 0', {}
    prefix = f"{alias}." if alias else ''
    parts, params = [], {}
    for i, (section, snap) in enumerate(latest.items()):
        parts.append(f"({prefix}section = :_lsec{i} AND {prefix}snapshot_date = :_lsnap{i})")
        params[f"_lsec{i}"] = section
        params[f"_lsnap{i}"] = snap
    return '(' + ' OR '.join(parts) + ')', params


def _regexp(pattern, value):
    return value is not None and re.search(pattern, str(value)) is not None


# Store datetimes passed as query parameters in the same text format as the table data,
# so "WHERE snapshot_date = :snap" matches.
sqlite3.register_adapter(datetime, lambda d: d.strftime(DB_DATETIME_FORMAT))
try:
    import pandas as pd
    sqlite3.register_adapter(pd.Timestamp, lambda d: d.strftime(DB_DATETIME_FORMAT))
except ImportError:
    pass


def get_db_engine(must_exist=True):
    path = get_db_path()
    if must_exist and not os.path.exists(path):
        raise RuntimeError(f"Database file not found: {path}\n"
                           "Run create_database.py once (new install), or restore a backup (see INSTRUCTIONS.txt).")

    engine = create_engine(f'sqlite:///{path}', connect_args={'timeout': 30})

    @event.listens_for(engine, 'connect')
    def _on_connect(dbapi_conn, _record):
        dbapi_conn.create_function('REGEXP', 2, _regexp, deterministic=True)
        cur = dbapi_conn.cursor()
        cur.execute('PRAGMA journal_mode=WAL')    # lets DataGrip read while scripts write
        cur.execute('PRAGMA synchronous=NORMAL')
        cur.execute('PRAGMA busy_timeout=30000')
        cur.close()

    return engine

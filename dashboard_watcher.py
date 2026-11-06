r'''
Runs quietly on the grades computer (no window) so the dashboard's "Update grades" button can reach it.
The computer only makes outgoing calls to Google Drive; nothing can connect in.

Every 30 seconds it reads refresh_request.json from the Drive Dashboard folder (Code.gs writes it when
someone presses the button). A new request starts refresh_dashboard.py. The file is only ever used as a
signal: its text is never run, and the only thing a request can do is start that same update.
Every 3 minutes it writes refresh_heartbeat.json, which is how the dashboard knows the computer is on.

Task Scheduler ("Student Grades - Dashboard Button Watcher") starts it every 5 minutes if it isn't
running, so it comes back after a restart. It exits straight away if another copy is running.
Log: logs\dashboard_watcher.log. To test by hand: .venv\Scripts\python.exe dashboard_watcher.py --once
'''

import argparse
import datetime
import msvcrt
import os
import subprocess
import sys
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)
os.environ['GRADES_NO_BROWSER'] = '1'

from google_drive import ensure_control_file, get_drive_service, read_control_file, write_control_file

REQUEST_FILE = 'refresh_request.json'
HEARTBEAT_FILE = 'refresh_heartbeat.json'
LOCK_FILE = os.path.join('logs', 'dashboard_watcher.lock')
LOG_FILE = os.path.join('logs', 'dashboard_watcher.log')
POLL_SECONDS = 30
HEARTBEAT_SECONDS = 180
REQUEST_MAX_AGE_MS = 30 * 60 * 1000   # e.g. the computer slept right after the button was pressed
PYTHON = os.path.join(BASE_DIR, '.venv', 'Scripts', 'python.exe')


def log(message):
    os.makedirs('logs', exist_ok=True)
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")


def run_refresh(request):
    """One update, in its own process with no window. Waits so requests are handled one at a time."""
    by = str(request.get('by', ''))[:200]
    log(f"request {request.get('id')} from {by}: starting update")
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    code = subprocess.call([PYTHON, 'refresh_dashboard.py', '--log', os.path.join('logs', 'dashboard_refresh.log'),
                            '--request-id', str(request.get('id', ''))[:80], '--requested-by', by],
                           env=env, cwd=BASE_DIR, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    log(f"request {request.get('id')}: finished with exit code {code}")


def main():
    parser = argparse.ArgumentParser(description="Lets the dashboard's Update grades button reach this computer.")
    parser.add_argument('--once', action='store_true', help="Check once and exit (for testing).")
    args = parser.parse_args()

    os.makedirs('logs', exist_ok=True)
    lock = open(LOCK_FILE, 'a+')
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return  # already running

    log('watcher started')
    service, request_id, beat_id, handled, last_beat = None, None, None, set(), 0
    while True:
        try:
            if not service:
                service = get_drive_service()
                request_id = ensure_control_file(service, REQUEST_FILE)
                beat_id = ensure_control_file(service, HEARTBEAT_FILE)
                # A request from before this start is either old or still valid; the age check decides.
            if time.time() - last_beat >= HEARTBEAT_SECONDS:
                write_control_file(service, beat_id, {'at': int(time.time() * 1000)})
                last_beat = time.time()
            request = read_control_file(service, request_id) or {}
            rid = request.get('id')
            if rid and rid not in handled:
                handled.add(rid)
                age = time.time() * 1000 - float(request.get('at') or 0)
                if age > REQUEST_MAX_AGE_MS:
                    log(f"request {rid} ignored: too old")
                else:
                    run_refresh(request)
                    # Heartbeat straight away: the update can take longer than the 3-minute beat.
                    write_control_file(service, beat_id, {'at': int(time.time() * 1000)})
                    last_beat = time.time()
        except Exception as e:
            log(f"error: {str(e)[:300]}")
            service = None   # sign in again on the next round (e.g. no internet after waking from sleep)
        if args.once:
            return
        time.sleep(POLL_SECONDS)


if __name__ == '__main__':
    main()

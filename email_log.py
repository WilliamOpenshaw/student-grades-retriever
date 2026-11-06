'''
Saves a copy of everything an email script prints to a text file, as a record of what was
sent to whom. Used by send_emails_hs.py and send_emails_jh.py.

Files go to the "email logs" folder in the project, one per run:
    email logs\2026-10-05_1427_JH_SENT.txt      real send
    email logs\2026-10-05_1400_JH_TEST.txt      --test (everything went to the sender)
    email logs\2026-10-05_1350_JH_PREVIEW.txt   --preview (nothing sent)
The file is written even if the run stops with an error or Ctrl+C, so a partial send is
still recorded (the log then ends with what went wrong).
'''

import os
import sys
import traceback
from contextlib import contextmanager
from datetime import datetime

from db import BASE_DIR

LOG_DIR = os.path.join(BASE_DIR, 'email logs')


class _Tee:
    """Writes to the terminal and the log file at the same time."""

    def __init__(self, terminal, logfile):
        self.terminal, self.logfile = terminal, logfile

    def write(self, text):
        self.terminal.write(text)
        self.logfile.write(text)
        self.logfile.flush()  # keep the file current in case the run is interrupted

    def flush(self):
        self.terminal.flush()
        self.logfile.flush()


@contextmanager
def email_log(school, mode, argv=None):
    """
    with email_log('JH', 'SENT') as path:
        send_reports(...)
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    started = datetime.now()
    path = os.path.join(LOG_DIR, f"{started:%Y-%m-%d_%H%M%S}_{school}_{mode}.txt")
    with open(path, 'w', encoding='utf-8') as logfile:
        logfile.write(f"Email log - {school} - {mode}\n")
        logfile.write(f"Started: {started:%Y-%m-%d %H:%M:%S}\n")
        logfile.write(f"Command: {' '.join(argv or sys.argv)}\n")
        logfile.write("=" * 60 + "\n")
        old_stdout = sys.stdout
        sys.stdout = _Tee(old_stdout, logfile)
        try:
            yield path
        except BaseException as e:
            sys.stdout = old_stdout
            logfile.write("\n" + "=" * 60 + "\n")
            logfile.write(f"RUN STOPPED: {type(e).__name__}: {e}\n")
            logfile.write(traceback.format_exc())
            raise
        finally:
            sys.stdout = old_stdout
            logfile.write("\n" + "=" * 60 + "\n")
            logfile.write(f"Finished: {datetime.now():%Y-%m-%d %H:%M:%S}\n")
    print(f"\nLog saved to: {os.path.relpath(path)}")

'''

This script accepts all pending Google Classroom invitations for each configured school account,
so new-semester classes can be joined in one run without clicking through each one in the browser.

It needs the write-level 'classroom.rosters' scope (the other scripts only use the readonly one),
so it uses its own token files (token_*_invites.json) and leaves the existing tokens untouched.
The first run opens a browser login per school; sign in with that school's account.

'''

import os
import argparse
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# --- CONFIGURATION ---
SCHOOL_CONFIGS = [
    {'name': 'High School', 'token_file': 'token_hs_invites.json'},
    {'name': 'Junior High', 'token_file': 'token_jh_invites.json'}
]

SCOPES = [
    'https://www.googleapis.com/auth/classroom.rosters',
    'https://www.googleapis.com/auth/classroom.courses.readonly'
]

os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'


def get_credentials(token_filename):
    creds = None
    if os.path.exists(token_filename):
        try:
            creds = Credentials.from_authorized_user_file(token_filename, SCOPES)
        except Exception:
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None

        if not creds:
            if not os.path.exists('credentials.json'):
                print("Error: credentials.json not found.")
                return None

            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            print(f"--- ACTION REQUIRED: Please log in for {token_filename} ---")
            creds = flow.run_local_server(port=0, prompt='consent')

        with open(token_filename, 'w') as token:
            token.write(creds.to_json())

    return creds


def list_invitations(service):
    invitations = []
    page_token = None
    while True:
        res = service.invitations().list(userId='me', pageSize=100, pageToken=page_token).execute()
        invitations.extend(res.get('invitations', []))
        page_token = res.get('nextPageToken')
        if not page_token:
            return invitations


def accept_all(school_name, token_file, dry_run):
    print(f"\n=== {school_name} ===")
    creds = get_credentials(token_file)
    if not creds:
        return

    service = build('classroom', 'v1', credentials=creds)
    # Collect first: accepting an invitation removes it from the list being paged.
    invitations = list_invitations(service)
    print(f"  {len(invitations)} pending invitation(s)")

    accepted = failed = 0
    for inv in invitations:
        try:
            course = service.courses().get(id=inv['courseId']).execute()
            label = course.get('name', inv['courseId'])
        except HttpError:
            # Not viewable until the invitation is accepted
            label = inv['courseId']

        if dry_run:
            print(f"  [dry run] would accept {inv.get('role')} invite: {label}")
            continue

        try:
            service.invitations().accept(id=inv['id']).execute()
            accepted += 1
            print(f"  accepted {inv.get('role')} invite: {label}")
        except HttpError as e:
            failed += 1
            print(f"  FAILED {label}: {e}")

    if not dry_run:
        print(f"  Done: {accepted} accepted, {failed} failed")


def main():
    parser = argparse.ArgumentParser(description="Accept all pending Google Classroom invitations.")
    parser.add_argument('--dry-run', action='store_true', help="List invitations without accepting them.")
    args = parser.parse_args()

    for config in SCHOOL_CONFIGS:
        accept_all(config['name'], config['token_file'], args.dry_run)


if __name__ == '__main__':
    main()

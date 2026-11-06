'''
Google Classroom Re-Authentication Script
This script regenerates OAuth2 tokens for multiple school configurations,
ensuring they have all necessary permissions for both grade calculation and email syncing.

'''

import os
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

# --- CONFIGURATION ---

# We combine ALL scopes needed for both Grades and Emails.
# This ensures the generated tokens work for every script you have.
SCOPES = [
    # Grades Script Scopes
    'https://www.googleapis.com/auth/classroom.courses.readonly',
    'https://www.googleapis.com/auth/classroom.coursework.students.readonly', # <-- Fixed line
    'https://www.googleapis.com/auth/classroom.rosters.readonly',
    'https://www.googleapis.com/auth/classroom.student-submissions.students.readonly',

    # Email Script Scopes
    'https://www.googleapis.com/auth/classroom.profile.emails',
    'https://www.googleapis.com/auth/classroom.profile.photos'
]

SCHOOL_CONFIGS = [
    {'name': 'High School', 'token_file': 'token_hs.json'},
    {'name': 'Junior High', 'token_file': 'token_jh.json'}
]

# Allow relaxation for scope changes (useful when updating permissions)
os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

def authenticate_school(school_name, token_file):
    print(f"\n{'='*40}")
    print(f"AUTHENTICATING: {school_name}")
    print(f"Target File:    {token_file}")
    print(f"{'='*40}")

    creds = None

    # 1. Try to load existing token to see if it can be refreshed
    if os.path.exists(token_file):
        try:
            creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        except Exception:
            print("  -> Existing token file is corrupt or has mismatched scopes.")
            creds = None

    # 2. Check validity
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("  -> Token expired. Attempting refresh...")
            try:
                creds.refresh(Request())
                print("  -> Refresh successful!")
            except Exception as e:
                print(f"  -> Refresh failed ({e}). forcing new login.")
                creds = None

        # 3. If still no valid creds, force new login
        if not creds:
            print("  -> Launching browser for login...")
            if not os.path.exists('credentials.json'):
                print("  ❌ CRITICAL ERROR: 'credentials.json' not found in this folder.")
                print("     Please download it from Google Cloud Console and try again.")
                return

            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            # Run local server to catch the callback
            creds = flow.run_local_server(port=0, prompt='consent')

        # 4. Save the fresh token
        print(f"  -> Saving new token to {token_file}...")
        with open(token_file, 'w') as token:
            token.write(creds.to_json())

        print("  ✅ SUCCESS: Token saved.")
    else:
        print("  ✅ Token is already valid and has correct scopes.")

def main():
    print("--- Google Classroom Re-Authenticator ---")
    print("This script will regenerate tokens with ALL necessary permissions.")

    for config in SCHOOL_CONFIGS:
        authenticate_school(config['name'], config['token_file'])

    print("\nAll done! You can now run 'sync_emails_from_google.py'.")

if __name__ == '__main__':
    main()
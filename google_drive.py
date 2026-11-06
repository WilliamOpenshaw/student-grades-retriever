'''
Shared Google Drive helper for backup_database.py and sync_dashboard.py.

Sign-in uses the drive.file permission: these scripts can only see and change
files they created themselves, never the rest of your Drive.

Everything lives in one Drive folder (IDs are remembered in drive_config.json):
    Student Grades Retriever/
        Dashboard/          JSON data files + the "Dashboard Access" Sheet (Apps Script web app)
        Database Backups/   zipped copies of grades.db
'''

import io
import os
import json

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, 'credentials.json')
TOKEN_FILE = os.path.join(BASE_DIR, 'token_drive.json')
CONFIG_FILE = os.path.join(BASE_DIR, 'drive_config.json')

SCOPES = ['https://www.googleapis.com/auth/drive.file']
FOLDER_MIME = 'application/vnd.google-apps.folder'

ROOT_FOLDER = 'Student Grades Retriever'
FOLDERS = {  # config key -> folder name inside ROOT_FOLDER
    'dashboard_folder_id': 'Dashboard',
    'backups_folder_id': 'Database Backups',
}


def get_drive_service():
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if os.environ.get('GRADES_NO_BROWSER'):
                # Set by run_backup_and_sync.bat: a scheduled job can't complete a browser sign-in.
                raise RuntimeError("Not signed in to Google Drive yet. Run backup_database.py once by hand to sign in.")
            if not os.path.exists(CREDENTIALS_FILE):
                raise RuntimeError("credentials.json not found in the project folder.")
            print("  -> Opening browser: sign in with the school Google account that will own the backups and dashboard.")
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0, prompt='consent')
        with open(TOKEN_FILE, 'w') as f:
            f.write(creds.to_json())
    return build('drive', 'v3', credentials=creds, cache_discovery=False)


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_config(config):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2)


def file_exists(service, file_id):
    if not file_id:
        return False
    try:
        meta = service.files().get(fileId=file_id, fields='id, trashed').execute()
        return not meta.get('trashed')
    except Exception:
        return False


def _ensure_folder(service, config, key, name, parent_id=None):
    if file_exists(service, config.get(key)):
        return config[key]
    body = {'name': name, 'mimeType': FOLDER_MIME}
    if parent_id:
        body['parents'] = [parent_id]
    config[key] = service.files().create(body=body, fields='id').execute()['id']
    save_config(config)
    print(f"  Created Drive folder: {name}")
    return config[key]


def ensure_folders(service):
    """Create the folder structure if needed; returns the config dict with folder IDs."""
    config = load_config()
    root = _ensure_folder(service, config, 'root_folder_id', ROOT_FOLDER)
    for key, name in FOLDERS.items():
        _ensure_folder(service, config, key, name, root)
    return config


def upload(service, folder_id, name, *, path=None, data=None, mimetype='application/octet-stream',
           file_id=None, convert_to=None):
    """Create a file in folder_id, or replace the contents of file_id (keeping its ID and link)."""
    if path:
        media = MediaFileUpload(path, mimetype=mimetype, resumable=True)
    else:
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype, resumable=True)

    if file_exists(service, file_id):
        return service.files().update(fileId=file_id, media_body=media, fields='id').execute()['id']

    body = {'name': name, 'parents': [folder_id]}
    if convert_to:
        body['mimeType'] = convert_to
    return service.files().create(body=body, media_body=media, fields='id').execute()['id']


def list_folder(service, folder_id, name_prefix=''):
    """Files in a folder (not trashed), newest name first."""
    q = f"'{folder_id}' in parents and trashed = false"
    if name_prefix:
        q += f" and name contains '{name_prefix}'"
    files, token = [], None
    while True:
        resp = service.files().list(q=q, fields='nextPageToken, files(id, name, size)',
                                    pageSize=1000, pageToken=token).execute()
        files += resp.get('files', [])
        token = resp.get('nextPageToken')
        if not token:
            break
    return sorted(files, key=lambda f: f['name'], reverse=True)


def move_to_trash(service, file_id):
    """Trash (not permanently delete); Drive empties its trash after 30 days."""
    service.files().update(fileId=file_id, body={'trashed': True}).execute()


def ensure_control_file(service, name):
    """
    Small JSON file for the dashboard's Update button, created here because the drive.file sign-in
    can only read files this program made. Code.gs may overwrite its contents. Returns its ID.
    """
    config = ensure_folders(service)
    ids = config.setdefault('control_files', {})
    if not file_exists(service, ids.get(name)):
        ids[name] = upload(service, config['dashboard_folder_id'], name, data=b'{}', mimetype='application/json')
        save_config(config)
    return ids[name]


def read_control_file(service, file_id):
    """Contents of a control file as a dict ({} if empty or not valid JSON)."""
    try:
        return json.loads(service.files().get_media(fileId=file_id).execute().decode('utf-8')) or {}
    except ValueError:
        return {}


def write_control_file(service, file_id, data):
    media = MediaIoBaseUpload(io.BytesIO(json.dumps(data, ensure_ascii=False).encode('utf-8')), mimetype='application/json')
    service.files().update(fileId=file_id, media_body=media, fields='id').execute()

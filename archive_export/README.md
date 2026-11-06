# Classroom grade export

Saves the grades of old Google Classroom classes that a Google account can see, so the school can
compare current grades with earlier years. It runs inside that person's Google account, from a web
browser. Nothing is installed and nothing in Classroom is changed: the script only has **read-only**
Classroom access. It creates one folder in the person's Google Drive.

**When someone else runs a script you set up, it runs as them:** their Classroom access, their Drive,
their own saved progress. You never see their password, and their run doesn't mix with yours.

---

## Part A: for the IT office, before the visit (about 15 minutes)

1. Go to **https://script.google.com** with your school account → **New project** → name it
   "Classroom grade export".
2. **Project Settings** (gear icon) → tick **Show "appsscript.json" manifest file in editor**.
3. **Editor** (`< >`) → open `appsscript.json`, select all and paste this folder's `appsscript.json`.
   Open `Code.gs`, select all and paste `ArchiveExport.gs`. Save (Ctrl+S).
4. **Test it on your own account first** (catches permission problems before the visit):
   - Choose **previewClasses** in the function list → **Run** → approve the permissions. The
     Execution log shows how many classes your account sees, by school year.
   - Optional full test on a few classes: at the top of the script set
     `NAME_FILTER: /Physics/,` (any word from a class name), run **startExport**, and wait for
     "EXPORT FINISHED". Download the folder from Drive and check it with
     `.venv\Scripts\python.exe import_archive_export.py "<zip>" --dry-run`.
     Then set `NAME_FILTER: null,` again, save, run **stopExport**, and delete the test folder from Drive.
   - If you get "You do not have permission to call DriveApp…", see "If something goes wrong" below.
5. **Share the project with your coworker:** **Share** button (top right) → their email → **Editor**.
   Editor access is needed to run it. They can see the code, which is fine (there are no passwords
   in it). Copy the project's link (the browser address while it's open).
6. Optional: if the coworker's account is a Workspace **admin**, it may see **every** class in the
   school (thousands). That's fine (it runs by itself), but `previewClasses` on their account will
   show the count first. To limit it to classes where they teach, set `ONLY_MY_TEACHING: true`.

## Part B: for the coworker (about 2 minutes)

1. Open the link from the IT office, signed in to Chrome with **your** school account.
2. Choose **previewClasses** in the function list at the top → **Run**. Google asks for permission:
   **Review permissions** → your account → **Allow**.
   - If you see "Google hasn't verified this app", click **Advanced → Go to Classroom grade export**.
   - The permissions are: see your Classroom classes, rosters (with emails) and grades (read-only);
     see and create files in your Google Drive (it only creates its own export folder); run itself
     again later. If it asks again later (e.g. after the script was updated), approve again.
   The log at the bottom lists how many classes will be saved and roughly how long it takes.
3. Choose **startExport** → **Run**. After about 5 minutes the log says "… classes left. Continuing
   automatically". **You can close the browser now**: it keeps going by itself.
4. Later, open the link again and run **exportStatus** to see how far it is. When it says
   **EXPORT FINISHED**, a folder **"Classroom grade export <date>"** in your Google Drive has
   `EXPORT_FINISHED.txt` in it.
5. In Google Drive, right-click that folder → **Share** → the IT office's school email → **Viewer**.
   (Or right-click → **Download** and give them the zip.) Please don't share it outside the school.
   After the IT office has copied it, delete the folder and empty the Drive trash: it contains
   student names and grades.

## Part C: for the IT office, afterwards

1. Open the shared folder in Drive → right-click → **Download** (big folders may come as several zips).
2. On the grades computer:
   ```
   .venv\Scripts\python.exe import_archive_export.py "<zip>" --dry-run
   .venv\Scripts\python.exe import_archive_export.py "<zip>"
   .venv\Scripts\python.exe compare_archived_grades.py
   .venv\Scripts\python.exe sync_dashboard.py
   ```
3. Ask the coworker to delete the Drive folder; delete the zip(s) here too.
4. Optional: remove the coworker's access to the script project (Share → remove), or delete the project.

---

## If something goes wrong
- **"Exception: Service error: Drive" (or "Internal error encountered") in the log:** a temporary
  Google error. Since 2026-10-14 the script retries these by itself and tries an affected class again
  in a later run. If you see it with an older copy of the script, paste in the current `ArchiveExport.gs`,
  save, and run **continueExport** (not startExport): it carries on from where it stopped.
- **"You do not have permission to call DriveApp.createFolder…":** the project was set up with an
  older `appsscript.json` (it had `drive.file`). Open `appsscript.json` in the project, replace
  `"https://www.googleapis.com/auth/drive.file"` with `"https://www.googleapis.com/auth/drive"`,
  save, and run startExport again: Google asks for permission once more → Allow. (Fixed in this
  folder's `appsscript.json` on 2026-10-13.)
- **"Classroom API has not been used in project … / is disabled":** make sure `appsscript.json` was
  pasted completely (the `enabledAdvancedServices` part turns the Classroom API on), then save and run.
- **To stop it:** run **stopExport**. The files saved so far stay. **continueExport** carries on from
  where it stopped; **startExport** starts again from scratch in a new folder.
- Classes the account may see but not read grades from are marked "saved with errors" or
  "failed" in `export_index.csv` in the folder. That's normal; the rest still export.

## Settings (top of the script)
- `COURSE_STATES`: `['ARCHIVED', 'ACTIVE']` = archived classes plus old classes that were never
  archived. Current classes are skipped later, on the IT computer.
- `ONLY_MY_TEACHING`: `true` = only classes where the account is a teacher.
- `NAME_FILTER`: e.g. `/2022|2023/` to export only matching class names; `null` = all.
- `RUN_SECONDS`: length of each run, `1500` (25 minutes) for Workspace domains that allow 30-minute
  runs. If Google stops a run earlier (a 6-minute limit), the export notices on its next run and
  uses `SAFE_RUN_SECONDS` (270) from then on, so it always finishes by itself. Only the first restart
  after such a cut-off waits longer (about 28 minutes).

## How it knows where to continue
`startExport` writes the list of all classes to `_export_progress.json` in the export folder (its
location is remembered in the script's private settings for the person running it). Every run reads
that file, skips the classes already done, records each class as soon as it's saved, and schedules
the next run a minute after it stops. Each run also schedules a backup restart when it begins, in
case Google cuts it off. A class saved twice simply overwrites its file. `stopExport` stops after the
class being saved; `continueExport` (run by hand) resumes.

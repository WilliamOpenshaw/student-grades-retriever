/**
 * Classroom grade export (read-only).
 *
 * Saves the grade data of every Google Classroom class this account can see (archived classes
 * by default, as far back as Classroom has them) as one JSON file per class in a new Drive folder
 * "Classroom grade export <date>". The files are then imported on the grades computer with
 * import_archive_export.py, which calculates final grades the same way as the grade reports.
 *
 * Nothing in Classroom is changed: the script only has read-only Classroom permissions
 * (see appsscript.json). The files contain student names, emails and grades: keep the folder
 * private and delete it once it has been copied to the grades computer.
 *
 * How to run (README.md has the steps, including setting it up for someone else and sharing it):
 *   0. Optional: previewClasses counts the classes by school year and saves nothing.
 *   1. Run startExport once. Approve the permissions.
 *   2. It works for about 5 minutes at a time and then schedules itself to continue a minute later,
 *      until every class is saved. Run exportStatus any time to see how far it is.
 *   3. When the log says "EXPORT FINISHED", download the folder from Drive (right click > Download).
 * stopExport cancels the automatic continuation; startExport later starts a NEW export.
 * continueExport can also be run by hand to resume after a stop.
 */

var CONFIG = {
  // Which classes: 'ARCHIVED' = archived classes; add 'ACTIVE' to include classes that were never
  // archived (old classes teachers forgot to archive are kept apart by the importer by their term).
  COURSE_STATES: ['ARCHIVED', 'ACTIVE'],
  // true = only classes where this account is a teacher; false = every class the account can see
  // (for a Workspace admin account: every class in the school).
  ONLY_MY_TEACHING: false,
  // Optional: only classes whose name matches this regular expression, e.g. /2023|2024/ ; null = all.
  NAME_FILTER: null,
  // Classes whose name matches this are skipped.
  SKIP_NAMES: /TEST CASE|DO NOT USE/i,
  // Each run stops after this long and continues a minute later. Google Workspace domains that allow
  // 30-minute runs can use 1500 (25 min). If a run is cut off earlier (e.g. the domain's limit is
  // 6 minutes), the next run notices and switches to SAFE_RUN_SECONDS for the rest of the export.
  RUN_SECONDS: 1500,
  SAFE_RUN_SECONDS: 270
};

var EXPORT_VERSION = 1;

/**
 * Saves nothing: counts the classes this account can see (with the settings above), by school year
 * as read from the class name or creation date, and estimates how long the export will take.
 */
function previewClasses() {
  var courses = listCourses_();
  var byYear = {};
  courses.forEach(function (c) {
    var y = schoolYear_(c), k = y + (c.courseState === 'ARCHIVED' ? ' (archived)' : ' (active)');
    byYear[k] = (byYear[k] || 0) + 1;
  });
  var lines = Object.keys(byYear).sort().map(function (k) { return '  ' + k + ': ' + byYear[k]; });
  var minutes = Math.ceil(courses.length * 2.5 / 60) + Math.ceil(courses.length * 2.5 / CONFIG.RUN_SECONDS);
  console.log('Account: ' + accountEmail_() + '\n' + courses.length + ' classes would be exported:\n' + lines.join('\n') +
              '\nRough time: ' + minutes + ' minutes (it runs by itself; you can close the browser).' +
              '\nNothing was saved. Run startExport to export.');
}

function listCourses_() {
  var all = listAll_(function (token) {
    var args = { courseStates: CONFIG.COURSE_STATES, pageSize: 100, pageToken: token };
    if (CONFIG.ONLY_MY_TEACHING) args.teacherId = 'me';
    return Classroom.Courses.list(args);
  }, 'courses');
  return all.filter(function (c) {
    return !(CONFIG.SKIP_NAMES && CONFIG.SKIP_NAMES.test(c.name || '')) &&
           !(CONFIG.NAME_FILTER && !CONFIG.NAME_FILTER.test(c.name || ''));
  });
}

/** '2024-25' from the class name (26S, 2025F, 25-26, Spring '24 ...) or else its creation date. */
function schoolYear_(c) {
  var n = c.name || '', m, y = null, fall = null;
  if ((m = /(20\d{2}|\d{2})\s*[-–\/]\s*(20\d{2}|\d{2})/.exec(n)) && +m[2].slice(-2) === (+m[1].slice(-2) + 1) % 100) {
    y = 2000 + (+m[1].slice(-2)); fall = true;
  } else if ((m = /(?:^|[^A-Za-z0-9])(?:20)?(\d{2})\s?([FS])(?![A-Za-z])/i.exec(n))) {
    y = 2000 + (+m[1]); fall = /f/i.test(m[2]);
  } else if ((m = /(spring|fall|autumn)[\s_]*'?\s*(20\d{2}|\d{2})/i.exec(n)) || (m = /(20\d{2})\s*(spring|fall|autumn)/i.exec(n))) {
    var yr = /\d/.test(m[1]) ? m[1] : m[2], season = /\d/.test(m[1]) ? m[2] : m[1];
    y = 2000 + (+yr.slice(-2)); fall = !/spring/i.test(season);
  } else if ((m = /^(\d{4})-(\d{2})/.exec(c.creationTime || ''))) {
    y = +m[1]; fall = +m[2] >= 7;
  }
  if (y === null) return 'unknown year';
  var start = fall ? y : y - 1;
  return start + '-' + String(start + 1).slice(2);
}

function startExport() {
  deleteTriggers_();
  PropertiesService.getUserProperties().deleteProperty('EXPORT_STOPPED');
  var stamp = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd HHmm');
  var folder = DriveApp.createFolder('Classroom grade export ' + stamp);
  console.log('Listing classes...');
  var todo = listCourses_().map(function (c) { return { id: c.id, name: c.name || '', state: c.courseState, created: c.creationTime }; });
  todo.sort(function (a, b) { return (a.created || '') < (b.created || '') ? 1 : -1; });   // newest first

  var progress = { version: EXPORT_VERSION, folderId: folder.getId(), startedAt: new Date().toISOString(),
                   account: accountEmail_(), courses: todo, done: {} };
  saveProgress_(progress, true);
  console.log('Export folder: "' + folder.getName() + '". ' + todo.length + ' classes to save.');
  continueExport();
}

/**
 * How resuming works: startExport writes the list of every class to export into _export_progress.json
 * in the export folder (its file ID is kept in this user's script properties). Each run reads that
 * file, skips the classes already marked done, saves the progress every ~20 seconds (and at the end),
 * and before it stops schedules the next run. Temporary Google errors are retried; a run that still
 * stops on one continues a minute later. At the start of each run a backup run is also scheduled, so the
 * export carries on even if Google cuts a run off (then later runs use the shorter safe length).
 */
function continueExport(e) {
  if (!e) PropertiesService.getUserProperties().deleteProperty('EXPORT_STOPPED');   // run by hand = resume
  else if (stopped_()) { deleteTriggers_(); return; }                               // automatic run after a stop
  var lock = LockService.getUserLock();
  if (!lock.tryLock(1000)) { console.log('Another run is still working; this one stops.'); return; }
  try {
    runExport_();
  } catch (err) {
    // A Google hiccup that survived the retries: carry on a minute later (no slower safe mode).
    PropertiesService.getUserProperties().setProperty('EXPORT_ERROR_RESTART', '1');
    if (!stopped_()) scheduleContinue_(60);
    console.log('This run stopped on an error (' + (err.message || err) + '). Nothing is lost: it continues ' +
                'automatically in about a minute.');
  } finally { lock.releaseLock(); }
}

function runExport_() {
  var started = Date.now();
  var progress = loadProgress_();
  if (!progress) { console.log('No export in progress. Run startExport.'); return; }
  var folder = DriveApp.getFolderById(progress.folderId);
  var left = progress.courses.filter(function (c) { return !progress.done[c.id]; });

  // Was the previous run cut off by Google's time limit before it could finish? Then use the safe run
  // length. (A run that stopped on an error says so in EXPORT_ERROR_RESTART; that's not a time limit.)
  var props = PropertiesService.getUserProperties(), afterError = props.getProperty('EXPORT_ERROR_RESTART') === '1';
  props.deleteProperty('EXPORT_ERROR_RESTART');
  if (progress.runStartedAt && !progress.runEndedAt && !progress.safeMode && !afterError) {
    progress.safeMode = true;
    console.log('The previous run was stopped by Google before it finished: using ' + CONFIG.SAFE_RUN_SECONDS +
                '-second runs from now on.');
  }
  var runSeconds = progress.safeMode ? CONFIG.SAFE_RUN_SECONDS : CONFIG.RUN_SECONDS;
  progress.runStartedAt = new Date().toISOString();
  progress.runEndedAt = null;
  saveProgress_(progress, true);
  if (left.length) scheduleContinue_(runSeconds + 180);    // backup in case this run is cut off

  for (var i = 0; i < left.length; i++) {
    if ((Date.now() - started) / 1000 > runSeconds || stopped_()) break;
    var c = left[i], result;
    try {
      var data = retry_(function () { return exportCourse_(c.id); }), name = 'course_' + c.id + '.json', text = JSON.stringify(data);
      retry_(function () {
        var existing = folder.getFilesByName(name);   // a cut-off run may have saved it already
        if (existing.hasNext()) existing.next().setContent(text);
        else folder.createFile(name, text, 'application/json');
      });
      result = { status: data.errors.length ? 'saved with errors' : 'saved', students: data.students.length,
                 work: data.courseWork.length, submissions: data.submissions.length, errors: data.errors.join(' | ') };
    } catch (e) {
      result = { status: 'failed', error: String(e.message || e) };
      // A temporary Google error: try this class again in a later run (up to 3 times) instead of giving up.
      progress.tries = progress.tries || {};
      progress.tries[c.id] = (progress.tries[c.id] || 0) + 1;
      if (isTemporary_(e) && progress.tries[c.id] < 3) {
        console.log('   (temporary Google error on ' + c.name + '; it will be tried again in a later run)');
        continue;
      }
    }
    progress.done[c.id] = result;
    console.log((Object.keys(progress.done).length) + '/' + progress.courses.length + '  ' + result.status + '  ' + c.name +
                (result.error ? '  (' + result.error + ')' : ''));
    saveProgress_(progress);   // at most every 20 s
  }
  try { retry_(function () { writeIndex_(folder, progress); }); } catch (err) { console.log('export_index.csv not updated: ' + err.message); }
  progress.runEndedAt = new Date().toISOString();
  saveProgress_(progress, true);

  var remaining = progress.courses.length - Object.keys(progress.done).length;
  if (remaining > 0 && stopped_()) {
    deleteTriggers_();
    console.log('Stopped with ' + remaining + ' classes left. Run continueExport to carry on.');
  } else if (remaining > 0) {
    scheduleContinue_(60);
    console.log(remaining + ' classes left. Continuing automatically in about a minute ' +
                '(or run continueExport now). Run exportStatus to check.');
  } else {
    deleteTriggers_();
    folder.createFile('EXPORT_FINISHED.txt', 'Finished ' + new Date().toISOString() + ' by ' + progress.account +
                      '\n' + summary_(progress), 'text/plain');
    console.log('EXPORT FINISHED. ' + summary_(progress) + '\nDownload the folder "' + folder.getName() +
                '" from Google Drive (right click > Download) and give the zip file to the IT office.');
  }
}

function exportStatus() {
  var progress = loadProgress_();
  if (!progress) { console.log('No export has been started.'); return; }
  var folder = DriveApp.getFolderById(progress.folderId);
  console.log('Folder "' + folder.getName() + '": ' + summary_(progress) +
              (ScriptApp.getProjectTriggers().length ? '\nStill running (continues automatically).' : '\nNot running.'));
}

/**
 * Cancels the automatic continuation (the files saved so far stay in Drive). A run that is working
 * right now stops after its current class. Run continueExport by hand to resume.
 */
function stopExport() {
  PropertiesService.getUserProperties().setProperty('EXPORT_STOPPED', '1');
  deleteTriggers_();
  console.log('Stopped. The files saved so far stay in Drive; run continueExport to carry on.');
}

/* ------------------------------------------------------------------ */

function exportCourse_(courseId) {
  var errors = [];
  var course = Classroom.Courses.get(courseId);
  var tryList = function (what, fn, key) {
    try { return listAll_(fn, key); } catch (e) { errors.push(what + ': ' + (e.message || e)); return []; }
  };
  var person = function (p) {
    var prof = p.profile || {};
    return { userId: p.userId, name: (prof.name && prof.name.fullName) || '', email: prof.emailAddress || '' };
  };
  var teachers = tryList('teachers', function (t) { return Classroom.Courses.Teachers.list(courseId, { pageSize: 100, pageToken: t }); }, 'teachers').map(person);
  var students = tryList('students', function (t) { return Classroom.Courses.Students.list(courseId, { pageSize: 100, pageToken: t }); }, 'students').map(person);
  var work = tryList('courseWork', function (t) { return Classroom.Courses.CourseWork.list(courseId, { pageSize: 100, pageToken: t }); }, 'courseWork')
    .map(function (w) {
      return { id: w.id, title: w.title || '', maxPoints: w.maxPoints == null ? null : w.maxPoints, workType: w.workType,
               state: w.state, gradeCategory: w.gradeCategory || null, dueDate: w.dueDate || null,
               creationTime: w.creationTime, gradingPeriodId: w.gradingPeriodId || null };
    });
  var subs = work.length ? tryList('submissions', function (t) {
    return Classroom.Courses.CourseWork.StudentSubmissions.list(courseId, '-', { pageSize: 1000, pageToken: t });
  }, 'studentSubmissions').map(function (s) {
    return { courseWorkId: s.courseWorkId, userId: s.userId, state: s.state,
             assignedGrade: s.assignedGrade == null ? null : s.assignedGrade,
             draftGrade: s.draftGrade == null ? null : s.draftGrade, late: !!s.late };
  }) : [];
  return {
    exportVersion: EXPORT_VERSION, exportedAt: new Date().toISOString(), exportedBy: accountEmail_(),
    course: { id: course.id, name: course.name || '', section: course.section || '', descriptionHeading: course.descriptionHeading || '',
              room: course.room || '', courseState: course.courseState, creationTime: course.creationTime,
              updateTime: course.updateTime, ownerId: course.ownerId, gradebookSettings: course.gradebookSettings || null },
    teachers: teachers, students: students, courseWork: work, submissions: subs, errors: errors
  };
}

function listAll_(fetch, key) {
  var out = [], token = undefined;
  do {
    var resp = fetch(token) || {};
    out = out.concat(resp[key] || []);
    token = resp.nextPageToken;
  } while (token);
  return out;
}

function accountEmail_() { try { return Session.getActiveUser().getEmail(); } catch (e) { return ''; } }

/** Runs fn again (up to 5 times, waiting 1, 2, 4, 8 s) when Google reports a temporary error. */
function retry_(fn) {
  for (var attempt = 0; ; attempt++) {
    try { return fn(); } catch (err) {
      if (attempt >= 4 || !isTemporary_(err)) throw err;
      Utilities.sleep(1000 * Math.pow(2, attempt));
    }
  }
}
function isTemporary_(err) {
  return /service error|service unavailable|internal error|backend error|rate limit|too many|timed out|try again|unavailable|\b50[0-4]\b/i
    .test(String(err && err.message || err));
}

function scheduleContinue_(seconds) {
  deleteTriggers_();
  ScriptApp.newTrigger('continueExport').timeBased().after(seconds * 1000).create();
}
function deleteTriggers_() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'continueExport') ScriptApp.deleteTrigger(t);
  });
}
function stopped_() { return PropertiesService.getUserProperties().getProperty('EXPORT_STOPPED') === '1'; }

/**
 * Progress lives in a file in the export folder (too big for script properties); its ID in a property.
 * Saved at most every 20 seconds during a run (Drive refuses very frequent updates of one file) and
 * always at the start and end of a run (force). Drive errors are retried; if a save still fails, the
 * run goes on and the next save catches up (at worst a few classes are saved again later).
 */
var lastProgressSave_ = 0;
function saveProgress_(progress, force) {
  if (!force && Date.now() - lastProgressSave_ < 20000) return;
  try {
    retry_(function () { writeProgress_(progress); });
    lastProgressSave_ = Date.now();
  } catch (err) {
    if (!PropertiesService.getUserProperties().getProperty('EXPORT_PROGRESS_FILE')) throw err;   // no file yet: can't go on
    console.log('Progress not saved this time (' + (err.message || err) + '); it is saved again shortly.');
  }
}
function writeProgress_(progress) {
  var props = PropertiesService.getUserProperties();
  var text = JSON.stringify(progress), id = props.getProperty('EXPORT_PROGRESS_FILE');
  var file = null;
  if (id) { try { file = DriveApp.getFileById(id); } catch (e) { file = null; } }
  if (file && props.getProperty('EXPORT_FOLDER') === progress.folderId) file.setContent(text);
  else {
    file = DriveApp.getFolderById(progress.folderId).createFile('_export_progress.json', text, 'application/json');
    props.setProperty('EXPORT_PROGRESS_FILE', file.getId());
    props.setProperty('EXPORT_FOLDER', progress.folderId);
  }
}
function loadProgress_() {
  var id = PropertiesService.getUserProperties().getProperty('EXPORT_PROGRESS_FILE');
  if (!id) return null;
  try { return JSON.parse(DriveApp.getFileById(id).getBlob().getDataAsString()); } catch (e) { return null; }
}

/** A readable list of every class and what happened to it (export_index.csv in the folder). */
function writeIndex_(folder, progress) {
  var q = function (v) { v = String(v == null ? '' : v); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; };
  var lines = [['class_id', 'class_name', 'state', 'created', 'status', 'students', 'assignments', 'submissions', 'errors'].join(',')];
  progress.courses.forEach(function (c) {
    var d = progress.done[c.id] || { status: 'not yet' };
    lines.push([c.id, c.name, c.state, c.created, d.status, d.students, d.work, d.submissions, d.errors || d.error].map(q).join(','));
  });
  var files = folder.getFilesByName('export_index.csv');
  if (files.hasNext()) files.next().setContent(lines.join('\n'));
  else folder.createFile('export_index.csv', lines.join('\n'), 'text/csv');
}

function summary_(progress) {
  var n = { saved: 0, 'saved with errors': 0, failed: 0 };
  Object.keys(progress.done).forEach(function (k) { n[progress.done[k].status] = (n[progress.done[k].status] || 0) + 1; });
  return Object.keys(progress.done).length + ' of ' + progress.courses.length + ' classes done: ' + n.saved + ' saved, ' +
         n['saved with errors'] + ' saved with errors, ' + n.failed + ' failed (see export_index.csv).';
}

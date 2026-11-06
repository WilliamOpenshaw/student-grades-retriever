/**
 * Student Grades Dashboard - Apps Script web app (read-only).
 * For teachers, the dean and the well-being team.
 *
 * Paste into the Apps Script project attached to the "Dashboard Access" Sheet
 * (open the Sheet, then Extensions > Apps Script). That Sheet lives in the Drive
 * folder "Student Grades Retriever/Dashboard", next to the JSON data files that
 * sync_dashboard.py uploads from the grades computer.
 *
 * Deploy as a web app with:
 *   Execute as:      Me
 *   Who has access:  Anyone within <your school domain>
 * Then run setup() once from the editor (see SETUP.md).
 *
 * Two locks protect the data:
 *   1. The viewer's school Google account must be listed in the first tab of the Sheet.
 *   2. They must enter the current access code. It changes automatically every
 *      CODE_DAYS days (Settings tab) and is emailed to the people listed there.
 * Teachers never get access to the Drive folder or the Sheet; this script reads
 * them as you and only returns the courses listed for each person in the Sheet.
 */

var SHARDS = 16;  // must match SHARDS in sync_dashboard.py

var SETTINGS_SHEET = 'Settings';
var LOG_SHEET = 'Access Log';
var DEPT_SHEET = 'Departments';
var DEPT_HEADER = ['Course key', 'Department', 'Ontario code', 'Leave out of averages (Y/N)', 'Course names', 'Note'];
// Same names as DEPARTMENTS in course_departments.py. Offered as a dropdown in the Departments tab;
// other names are allowed (a new department just appears in the dashboard's filter).
var DEPARTMENTS = ['English / EAL', 'Mathematics', 'Natural Sciences', 'Social Sciences', 'Modern Languages',
                   'Art & Design', 'PHE', 'PBL', 'Homeroom', 'Other'];
var ROLES = ['teacher', 'dean', 'wellbeing', 'admin'];
var CODE_ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789';  // no 0/O, 1/I/L
var CODE_LENGTH = 6;
var GRACE_HOURS = 12;        // the previous code still works this long after a change
var MAX_WRONG_CODES = 5;     // wrong codes per person before a 15-minute lockout
var LOCKOUT_SECONDS = 900;

/**
 * Support rules for the Watch list (percent grades). Adapted from the JH Classroom Hub rules.
 * Shown on the page, so changing a number here changes both the flags and the explanation.
 */
var RULES = {
  // A course only counts towards averages and flags once it has this many graded assignments
  // (early in a semester one zero can make a course grade 0-10%).
  minGraded: 3,
  intervention: {
    courseBelow: 50,        // any course below this (F)
    twoCoursesBelow: 60,    // two or more courses below this
    averageBelow: 60,       // average of all graded courses below this
    missingAtLeast: 10      // missing assignments (all courses) at least this
  },
  attention: {
    courseBelow: 60,
    averageBelow: 70,
    missingAtLeast: 5,
    dropAtLeast: 10         // average fell this many points since the last update or last semester
  }
};

function doGet() {
  return HtmlService.createHtmlOutputFromFile('Index')
    .setTitle('Student Grades Dashboard')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

/* ------------------------------------------------------------------ */
/* Who may open the dashboard (first tab of the "Dashboard Access" Sheet)
 *   email | courses | notes | role
 * ------------------------------------------------------------------ */

function viewerEmail_() {
  var email = (Session.getActiveUser().getEmail() || '').trim().toLowerCase();
  if (!email) {
    throw new Error('Could not identify your Google account. Please sign in with your school account.');
  }
  return email;
}

function ownerEmail_() {
  return (Session.getEffectiveUser().getEmail() || '').trim().toLowerCase();
}

/** Rows of the first tab: [{ email, courses, role }], skipping blank and '#' rows. */
function accessRows_() {
  var values = SpreadsheetApp.getActiveSpreadsheet().getSheets()[0].getDataRange().getValues();
  var header = values[0].map(function (h) { return String(h).trim().toLowerCase(); });
  var col = function (name, fallback) { var i = header.indexOf(name); return i === -1 ? fallback : i; };
  var cEmail = col('email', 0), cCourses = col('courses', 1), cRole = col('role', 3);
  var rows = [];
  for (var i = 1; i < values.length; i++) {
    var email = String(values[i][cEmail] || '').trim().toLowerCase();
    if (!email || email.charAt(0) === '#') continue;
    var role = String(values[i][cRole] || '').trim().toLowerCase().replace(/[^a-z]/g, '');
    if (ROLES.indexOf(role) === -1) role = 'teacher';
    rows.push({ email: email, courses: String(values[i][cCourses] || '').trim(), role: role, row: i + 1 });
  }
  return rows;
}

function getAccess_() {
  var email = viewerEmail_();
  if (email === ownerEmail_()) return { email: email, role: 'admin', all: true, patterns: [] };

  var rows = accessRows_();
  for (var i = 0; i < rows.length; i++) {
    if (rows[i].email !== email) continue;
    var role = rows[i].role;
    var courses = rows[i].courses;
    // Dean, well-being and admin see every course unless the Sheet narrows it.
    if (courses === '*' || (!courses && role !== 'teacher')) {
      return { email: email, role: role, all: true, patterns: [] };
    }
    var patterns = courses.split(',')
      .map(function (k) { return k.trim(); })
      .filter(function (k) { return k; })
      .map(function (k) {
        // Match at the start of a word: "PE 6" matches "Spring PE 6A" but not "Pebble".
        return new RegExp('(^|[^A-Za-z0-9])' + k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i');
      });
    return { email: email, role: role, all: false, patterns: patterns };
  }
  throw new Error('Your account (' + email + ') has not been given access to this dashboard. ' +
                  'Please ask the dashboard owner to add you.');
}

function canSee_(access, courseName) {
  if (access.all) return true;
  return access.patterns.some(function (re) { return re.test(courseName); });
}

/* ------------------------------------------------------------------ */
/* Rotating access code                                                */
/* ------------------------------------------------------------------ */

function props_() { return PropertiesService.getScriptProperties(); }

/*
 * Settings tab layout (since 2026-10-16, made by setup() / convertSettingsTab()):
 *   row 1  headers:      Rotate code every (days) | Email new code to | Current code | Valid until | ... | Notes
 *   row 2  descriptions  (grey; never read as values)
 *   row 3  values        ("Email new code to": one address per row, from row 3 down)
 * The older layout (one setting per row: name | value | note) is still read, so nothing breaks before
 * the tab is converted.
 */
var SETTINGS_HEADERS = [
  ['Rotate code every (days)', 'New code every N days, counted from Mondays. Changes at midnight.'],
  ['Email new code to', 'One email per row. Each new code is emailed to them (Bcc), to you and to every dean / wellbeing person in the first tab.'],
  ['Current code', 'Written automatically. To change it early, run changeCodeNow() in Apps Script.'],
  ['Valid until', 'Written automatically.'],
  ['Notes', 'Editing this tab does not give anyone access; the first tab does.']
];
var SETTINGS_VALUE_ROW = 3;
var EMAIL_SETTING = 'Email new code to';
var KNOWN_SETTINGS = ['Rotate code every (days)', 'Email new code to', 'Also email new code to', 'Current code',
                      'Valid until', 'Past grades visible to'];

/** True when row 1 holds the setting names as column headers (the new layout). */
function settingsAcross_(values) {
  var row = (values[0] || []).map(function (h) { return String(h).trim(); });
  return row.filter(function (h) { return KNOWN_SETTINGS.indexOf(h) !== -1; }).length >= 2;
}

/** Settings tab: { 'Rotate code every (days)': 7, 'Email new code to': 'a@x, b@x', ... } (either layout) */
function readSettings_() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SETTINGS_SHEET);
  var out = {};
  if (!sheet) return out;
  var values = sheet.getDataRange().getValues();
  if (!settingsAcross_(values)) {
    values.forEach(function (r) { if (r[0]) out[String(r[0]).trim()] = r[1]; });
    return out;
  }
  values[0].forEach(function (h, c) {
    var name = String(h).trim();
    if (!name) return;
    if (name === EMAIL_SETTING || name === 'Also email new code to') {
      // Every cell below the header that contains an address (a cell may also hold several, comma-separated).
      out[name] = values.slice(1).map(function (r) { return String(r[c] || '').trim(); })
                        .filter(function (v) { return v.indexOf('@') !== -1; }).join(', ');
    } else {
      out[name] = values.length >= SETTINGS_VALUE_ROW ? values[SETTINGS_VALUE_ROW - 1][c] : '';
    }
  });
  return out;
}

function codeDays_() {
  var days = parseInt(readSettings_()['Rotate code every (days)'], 10);
  return days >= 1 && days <= 90 ? days : 7;
}

/** Periods are counted from Monday 2024-01-01 00:00 in the script's time zone. */
function periodInfo_(when) {
  var days = codeDays_();
  var tz = Session.getScriptTimeZone();
  var off = Utilities.formatDate(when, tz, 'Z');  // e.g. +0800
  var offMs = (off.charAt(0) === '-' ? -1 : 1) *
              (parseInt(off.substr(1, 2), 10) * 60 + parseInt(off.substr(3, 2), 10)) * 60000;
  var epoch = Date.UTC(2024, 0, 1) - offMs;
  var length = days * 86400000;
  var index = Math.floor((when.getTime() - epoch) / length);
  return { index: index, start: new Date(epoch + index * length), end: new Date(epoch + (index + 1) * length) };
}

function secret_() {
  var secret = props_().getProperty('CODE_SECRET');
  if (!secret) throw new Error('The dashboard is not set up yet: the owner must run setup() once.');
  return secret;
}

function codeFor_(periodIndex) {
  var bytes = Utilities.computeHmacSha256Signature('access-code:' + periodIndex, secret_());
  var code = '';
  for (var i = 0; i < CODE_LENGTH; i++) code += CODE_ALPHABET.charAt((bytes[i] & 0xff) % CODE_ALPHABET.length);
  return code;
}

function normalizeCode_(code) {
  return String(code || '').toUpperCase().replace(/[^A-Z0-9]/g, '');
}

function codeIsValid_(code) {
  code = normalizeCode_(code);
  if (!code) return false;
  var now = new Date(), p = periodInfo_(now);
  if (code === codeFor_(p.index)) return true;
  return now.getTime() - p.start.getTime() < GRACE_HOURS * 3600000 && code === codeFor_(p.index - 1);
}

/** Checks the viewer's account and code on every call. Wrong codes count towards a lockout. */
function authorize_(code) {
  var access = getAccess_();
  var cache = CacheService.getScriptCache();
  var key = 'wrong:' + access.email;
  var wrong = parseInt(cache.get(key) || '0', 10);
  if (wrong >= MAX_WRONG_CODES) {
    throw new Error('ACCESS_CODE: Too many wrong codes. Please wait 15 minutes and try again.');
  }
  if (!codeIsValid_(code)) {
    if (normalizeCode_(code)) cache.put(key, String(wrong + 1), LOCKOUT_SECONDS);
    throw new Error('ACCESS_CODE: ' + (normalizeCode_(code)
      ? 'That access code is not correct, or it has changed. Please enter the current code.'
      : 'Please enter the access code.'));
  }
  return access;
}

/**
 * Daily trigger (installed by setup()). When a new code period has started, writes the
 * code into the Settings tab and emails it to the people listed there.
 */
function rotateAccessCode() {
  try { updateDepartmentsTab(); } catch (e) { console.log('Departments tab not updated: ' + e.message); }
  var p = periodInfo_(new Date());
  if (props_().getProperty('LAST_SENT_PERIOD') === String(p.index)) {
    console.log('rotateAccessCode: the code has not changed since it was last emailed (period ' + p.index +
                ', valid until ' + Utilities.formatDate(p.end, Session.getScriptTimeZone(), 'EEE yyyy-MM-dd HH:mm') +
                '). Nothing sent.');
    return;
  }
  sendCurrentCode_('rotateAccessCode (new code period)');
}

/** Run from the editor if a code may have leaked: makes a new code right away and emails it. */
function changeCodeNow() {
  props_().setProperty('CODE_SECRET', Utilities.getUuid() + Utilities.getUuid());
  sendCurrentCode_('changeCodeNow (new secret, old codes no longer work)');
}

/**
 * Run from the editor to email the CURRENT code again without changing it, e.g. after adding a
 * dean or well-being person to the first tab. Everyone on the list gets it again.
 * What was sent, and to whom, is printed in the Execution log and added to the Access Log tab.
 */
function resendCurrentCode() {
  sendCurrentCode_('resendCurrentCode (same code)');
}

/**
 * Emails the current code and writes it into the Settings tab. Prints a report to the
 * Execution log (editor runs) / Executions page (trigger runs) and adds a row to the Access Log
 * tab, whether the email was sent or failed.
 */
function sendCurrentCode_(reason) {
  reason = reason || 'setup';
  var lines = [];
  var log = function (s) { lines.push(s); console.log(s); };
  var now = new Date();
  var tz = Session.getScriptTimeZone();
  var p = periodInfo_(now);
  var code = codeFor_(p.index);
  var fmt = function (d) { return Utilities.formatDate(d, tz, 'EEE yyyy-MM-dd HH:mm'); };
  var until = fmt(p.end);
  var owner = ownerEmail_();

  log('=== Access code email: ' + reason + ' ===');
  log('Run at:        ' + fmt(now) + ' (' + tz + '), as ' + owner);
  log('Code:          ' + code + '  (period #' + p.index + ', valid ' + fmt(p.start) + ' to ' + until +
      '; previous code also accepted until ' + fmt(new Date(p.start.getTime() + GRACE_HOURS * 3600000)) + ')');
  log('Rotation:      every ' + codeDays_() + ' day(s)');

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SETTINGS_SHEET);
  if (sheet) {
    setSetting_(sheet, 'Current code', code);
    setSetting_(sheet, 'Valid until', until);
    log('Settings tab:  "Current code" and "Valid until" updated');
  } else {
    log('Settings tab:  NOT FOUND - run setup() to create it');
  }
  // getUrl() can return another deployment's link, so the Script Property DASHBOARD_URL comes first.
  var savedUrl = String(props_().getProperty('DASHBOARD_URL') || '').trim();
  var url = savedUrl || ScriptApp.getService().getUrl();
  log('Web app link:  ' + (url || 'none yet (the web app has not been deployed)') +
      (savedUrl ? '  (from Script Property DASHBOARD_URL)' : '  (from getUrl(); DASHBOARD_URL is not set)'));

  var info = codeRecipientDetails_();
  log('Recipients (' + info.list.length + '):');
  info.list.forEach(function (r) {
    log('  ' + (r.email === owner ? 'To   ' : 'Bcc  ') + r.email + '  - ' + r.source);
  });
  info.skipped.forEach(function (s) { log('  SKIPPED ' + s); });
  log('Not emailed:   ' + info.teachers + ' teacher row(s) in the first tab (they get the code from you)');

  var quota = null;
  try { quota = MailApp.getRemainingDailyQuota(); } catch (e) { /* not available in every context */ }
  if (quota != null) log('Email quota:   ' + quota + ' recipient(s) left today before sending');

  var bcc = info.list.map(function (r) { return r.email; }).filter(function (e) { return e !== owner; });
  try {
    MailApp.sendEmail({
      to: owner,
      bcc: bcc.join(','),
      subject: 'Student Grades Dashboard - access code until ' + until,
      body: 'The access code for the Student Grades Dashboard is:\n\n    ' + code + '\n\n' +
            'It works until ' + until + ' (' + tz + '). The previous code keeps working for ' +
            GRACE_HOURS + ' hours after it changes.\n\nDashboard: ' + (url || '(link not available yet)') + '\n\n' +
            'Please do not forward this code. The dashboard shows student grades.\n' +
            '儀表板存取碼：' + code + '（請勿轉寄）'
    });
  } catch (err) {
    log('RESULT:        FAILED - ' + err.message);
    logEvent_('access code email FAILED (' + reason + '): ' + err.message);
    throw err;  // shows the run as failed; the nightly trigger tries again the next day
  }
  props_().setProperty('LAST_SENT_PERIOD', String(p.index));
  log('RESULT:        SENT to ' + info.list.length + ' address(es). Check your Sent folder for the message.');
  logEvent_('access code emailed (' + reason + ') to: ' + info.list.map(function (r) { return r.email; }).join(', '));
  return lines.join('\n');
}

/**
 * Who gets each new code, and why: you (the owner), everyone with role dean or wellbeing in the
 * first tab, plus any extra addresses in Settings > "Email new code to". Teachers get it from you.
 * Returns { list: [{ email, source }], skipped: [text], teachers: count }.
 */
function codeRecipientDetails_() {
  var list = [], seen = {}, skipped = [], teachers = 0;
  var add = function (email, source) {
    if (seen[email]) { seen[email].source += '; also ' + source; return; }
    seen[email] = { email: email, source: source };
    list.push(seen[email]);
  };
  var valid = function (e) { return /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(e); };
  add(ownerEmail_(), 'owner of the dashboard');
  accessRows_().forEach(function (r) {
    if (r.role === 'dean' || r.role === 'wellbeing') {
      if (valid(r.email)) add(r.email, 'role ' + r.role + ' (first tab, row ' + r.row + ')');
      else skipped.push('"' + r.email + '" (first tab, row ' + r.row + '): not a valid email address');
    } else if (r.role === 'teacher') {
      teachers++;
    }
  });
  var settings = readSettings_();
  String(settings['Also email new code to'] || settings['Email new code to'] || '')
    .split(/[,;\s]+/).forEach(function (e) {
      e = e.trim().toLowerCase();
      if (!e) return;
      if (valid(e)) add(e, 'Settings > Email new code to');
      else skipped.push('"' + e + '" (Settings > Email new code to): not a valid email address');
    });
  return { list: list, skipped: skipped, teachers: teachers };
}

function codeRecipients_() {
  return codeRecipientDetails_().list.map(function (r) { return r.email; });
}

/** A row in the Access Log tab for things the script does by itself (code emails). */
function logEvent_(event) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(LOG_SHEET);
    if (sheet) sheet.appendRow([new Date(), ownerEmail_(), 'system', event]);
  } catch (e) { console.log('Could not write to the Access Log tab: ' + e.message); }
}

function setSetting_(sheet, name, value) {
  var values = sheet.getDataRange().getValues();
  if (settingsAcross_(values)) {
    var c = values[0].map(function (h) { return String(h).trim(); }).indexOf(name);
    if (c === -1) {   // a new column just before "Notes" (or at the end)
      c = values[0].map(function (h) { return String(h).trim(); }).indexOf('Notes');
      if (c === -1) c = values[0].length;
      else sheet.insertColumnBefore(c + 1);
      sheet.getRange(1, c + 1).setValue(name);
    }
    sheet.getRange(SETTINGS_VALUE_ROW, c + 1).setValue(value);
    return;
  }
  for (var i = 0; i < values.length; i++) {
    if (String(values[i][0]).trim() === name) { sheet.getRange(i + 1, 2).setValue(value); return; }
  }
  sheet.appendRow([name, value]);
}

/**
 * Run once from the Apps Script editor to switch the Settings tab to the layout with the settings
 * across the top and one email per row (see SETTINGS_HEADERS). Keeps every value, including emails
 * typed comma-separated in one cell and any extra settings (e.g. "Past grades visible to").
 * Safe to run again: it just tidies the tab. Prints what it kept to the Execution log.
 */
function convertSettingsTab() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SETTINGS_SHEET);
  if (!sheet) throw new Error('No "' + SETTINGS_SHEET + '" tab: run setup() instead.');
  var s = readSettings_();
  var before = JSON.stringify(s);
  if (s['Also email new code to'] && !s[EMAIL_SETTING]) s[EMAIL_SETTING] = s['Also email new code to'];
  delete s['Also email new code to'];
  writeSettingsTab_(sheet, s);
  var after = readSettings_();
  console.log('Settings before: ' + before);
  console.log('Settings now:    ' + JSON.stringify(after));
  ['Rotate code every (days)', EMAIL_SETTING, 'Past grades visible to'].forEach(function (k) {
    var norm = function (v) { return String(v == null ? '' : v).toLowerCase().split(/[,;\s]+/).filter(String).sort().join(','); };
    if (k in s && norm(s[k]) !== norm(after[k])) throw new Error('Check the Settings tab: "' + k + '" changed while converting.');
  });
  console.log('Done. Emails get the new code: ' + codeRecipients_().join(', '));
}

/** Rewrites the Settings tab in the across layout from a settings object (values kept as given). */
function writeSettingsTab_(sheet, s) {
  var cols = SETTINGS_HEADERS.slice(0, -1);
  Object.keys(s).forEach(function (k) {   // extra settings someone added (e.g. "Past grades visible to")
    if (k && k !== 'Notes' && !cols.some(function (c) { return c[0] === k; }) && k !== 'Also email new code to') {
      cols.push([k, k === 'Past grades visible to' ? '"all" (default) or roles, e.g. "dean, wellbeing". The owner always can.' : '']);
    }
  });
  cols.push(SETTINGS_HEADERS[SETTINGS_HEADERS.length - 1]);
  var emails = String(s[EMAIL_SETTING] || '').split(/[,;\s]+/).map(function (e) { return e.trim(); }).filter(String);
  var n = Math.max(1, emails.length);
  var grid = [cols.map(function (c) { return c[0]; }), cols.map(function (c) { return c[1]; })];
  for (var i = 0; i < n; i++) {
    grid.push(cols.map(function (c) {
      if (c[0] === EMAIL_SETTING) return emails[i] || '';
      if (i > 0 || c[0] === 'Notes') return '';
      var v = s[c[0]];
      return v == null ? '' : v;
    }));
  }
  sheet.clear();
  sheet.getRange(1, 1, grid.length, cols.length).setValues(grid);
  sheet.getRange(1, 1, 1, cols.length).setFontWeight('bold').setBackground('#e8eaed');
  sheet.getRange(2, 1, 1, cols.length).setFontColor('#5f6368').setFontStyle('italic').setWrap(true).setVerticalAlignment('top');
  sheet.setFrozenRows(2);
  cols.forEach(function (c, i) { sheet.setColumnWidth(i + 1, c[0] === EMAIL_SETTING ? 300 : c[0] === 'Notes' ? 320 : 190); });
}

/**
 * Run once from the Apps Script editor (choose "setup" and click Run). Safe to run again.
 * Adds the "role" column, the Settings and Access Log tabs, the code secret and the
 * daily code-rotation trigger, then emails the first code.
 */
function setup() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var access = ss.getSheets()[0];
  var header = access.getRange(1, 1, 1, Math.max(access.getLastColumn(), 1)).getValues()[0]
    .map(function (h) { return String(h).trim().toLowerCase(); });
  if (header.indexOf('role') === -1) {
    access.getRange(1, header.filter(String).length + 1).setValue('role');
  }

  var settings = ss.getSheetByName(SETTINGS_SHEET) || ss.insertSheet(SETTINGS_SHEET);
  if (!settings.getLastRow()) writeSettingsTab_(settings, { 'Rotate code every (days)': 7 });
  var log = ss.getSheetByName(LOG_SHEET) || ss.insertSheet(LOG_SHEET);
  if (!log.getLastRow()) log.appendRow(['time', 'email', 'role', 'event']);

  if (!props_().getProperty('CODE_SECRET')) {
    props_().setProperty('CODE_SECRET', Utilities.getUuid() + Utilities.getUuid());
  }
  var hasTrigger = ScriptApp.getProjectTriggers().some(function (t) {
    return t.getHandlerFunction() === 'rotateAccessCode';
  });
  if (!hasTrigger) ScriptApp.newTrigger('rotateAccessCode').timeBased().everyDays(1).atHour(0).nearMinute(5).create();
  try { updateDepartmentsTab(); } catch (e) { console.log('Departments tab not updated: ' + e.message); }
  props_().deleteProperty('LAST_SENT_PERIOD');
  sendCurrentCode_();
}

/* ------------------------------------------------------------------ */
/* Departments tab: course key -> department, kept by the dean          */
/* ------------------------------------------------------------------ */

/**
 * Run from the editor (or automatically every night by rotateAccessCode, and by setup): creates the
 * Departments tab if needed and adds a row for every course key in the latest data that the tab
 * doesn't have yet, filled in with the suggested department. Existing rows are never changed, so the
 * dean's edits stay. Returns the number of rows added.
 */
function updateDepartmentsTab() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(DEPT_SHEET);
  if (!sheet) {
    sheet = ss.insertSheet(DEPT_SHEET);
    sheet.appendRow(DEPT_HEADER);
    try {
      sheet.setFrozenRows(1);
      sheet.getRange(1, 1, 1, DEPT_HEADER.length).setFontWeight('bold');
      sheet.getRange('B2:B').setDataValidation(SpreadsheetApp.newDataValidation()
        .requireValueInList(DEPARTMENTS, true).setAllowInvalid(true).build());
      sheet.setColumnWidth(1, 300);
      sheet.setColumnWidth(5, 420);
    } catch (e) { /* formatting only */ }
  }
  var have = {};
  sheet.getDataRange().getValues().slice(1).forEach(function (r) {
    var k = String(r[0] || '').trim().toLowerCase();
    if (k) have[k] = true;
  });

  var info = (readJson_('dashboard.json').courseInfo) || {};
  var add = {};   // key -> row
  Object.keys(info).sort().forEach(function (name) {
    var s = info[name], k = String(s[0]).trim().toLowerCase();
    if (!k || have[k]) return;
    if (s.length > 4 && !s[4]) return;   // past / archived course: no row (it uses the suggestion)
    var row = add[k] || (add[k] = [s[0], s[1], s[2] || '', s[3] ? 'Y' : 'N', [], 'added ' +
      Utilities.formatDate(new Date(), Session.getScriptTimeZone(), 'yyyy-MM-dd') + ' (suggested department)']);
    if (row[4].length < 4) row[4].push(name);
  });
  var rows = Object.keys(add).map(function (k) { var r = add[k]; r[4] = r[4].join(' · '); return r; })
    .sort(function (a, b) { return a[1] === b[1] ? (a[0] < b[0] ? -1 : 1) : (a[1] < b[1] ? -1 : 1); });
  if (rows.length) sheet.getRange(sheet.getLastRow() + 1, 1, rows.length, DEPT_HEADER.length).setValues(rows);
  console.log('Departments tab: ' + rows.length + ' new course key(s) added' +
              (rows.length ? ': ' + rows.map(function (r) { return r[0] + ' -> ' + r[1]; }).join(', ') : '.'));
  if (rows.length) logEvent_('Departments tab: ' + rows.length + ' new course key(s) added with suggested departments');
  return rows.length;
}

/** The tab as { 'course key (lower case)': { dept, code, leaveOut (true/false/null) } }. */
function departments_() {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(DEPT_SHEET);
  var out = {};
  if (!sheet) return out;
  sheet.getDataRange().getValues().slice(1).forEach(function (r) {
    var k = String(r[0] || '').trim().toLowerCase();
    if (!k) return;
    var lo = String(r[3] || '').trim().toLowerCase();
    out[k] = { dept: String(r[1] || '').trim(), code: String(r[2] || '').trim(),
               leaveOut: /^(y|yes|true|x|1)$/.test(lo) ? true : /^(n|no|false|0)$/.test(lo) ? false : null };
  });
  return out;
}

/**
 * Department of each course the viewer may see: the Departments tab first, else the suggestion
 * from sync_dashboard.py. { name: { key, dept, code, leaveOut, suggested } }
 */
function courseDepartments_(info, names) {
  var tab = departments_(), out = {};
  names.forEach(function (name) {
    if (!name || out[name]) return;
    var s = info[name] || [name, '', '', false];
    var t = tab[String(s[0]).toLowerCase()] || {};
    out[name] = { key: s[0], dept: t.dept || s[1] || 'Unassigned', code: t.code || s[2] || '',
                  leaveOut: t.leaveOut != null ? t.leaveOut : !!s[3], suggested: !t.dept };
  });
  return out;
}

function logAccess_(access, event) {
  try {
    var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(LOG_SHEET);
    if (sheet) sheet.appendRow([new Date(), access.email, access.role, event]);
  } catch (e) { /* logging must never block the dashboard */ }
}

/* ------------------------------------------------------------------ */
/* Reading the JSON data files from Drive                              */
/* ------------------------------------------------------------------ */

/** Map of file name -> Drive file ID for the folder this Sheet is in (cached for 6 hours). */
function fileIds_(refresh) {
  var cache = CacheService.getScriptCache();
  var cached = !refresh && cache.get('fileIds');
  if (cached) return JSON.parse(cached);

  var sheetFile = DriveApp.getFileById(SpreadsheetApp.getActiveSpreadsheet().getId());
  var parents = sheetFile.getParents();
  if (!parents.hasNext()) throw new Error('The "Dashboard Access" Sheet must stay in the Dashboard folder.');
  var files = parents.next().getFiles(), ids = {};
  while (files.hasNext()) {
    var f = files.next();
    if (/\.json$/.test(f.getName())) ids[f.getName()] = f.getId();
  }
  cache.put('fileIds', JSON.stringify(ids), 21600);
  return ids;
}

function readJson_(name, optional) {
  var ids = fileIds_(false);
  if (!ids[name]) ids = fileIds_(true);  // a new file appeared since the cache was filled
  if (!ids[name]) {
    if (optional) return null;
    throw new Error('Missing data file "' + name + '". Run sync_dashboard.py on the grades computer.');
  }
  return JSON.parse(DriveApp.getFileById(ids[name]).getBlob().getDataAsString('UTF-8'));
}

/* ------------------------------------------------------------------ */
/* "Update grades" button: asks the grades computer to pull from Classroom.
 * The computer can't be reached directly, so this leaves a request in a small file in the Drive
 * folder; dashboard_watcher.py checks that file every 30 seconds. refresh_heartbeat.json says when
 * the computer was last on, refresh_status.json is the progress of the current update. All three
 * are created by the Python scripts (their Drive sign-in can only read files they made).
 * ------------------------------------------------------------------ */

var REFRESH_COOLDOWN_MINUTES = 10;   // time between updates, so repeated clicks don't queue up
var COMPUTER_OFF_AFTER_MINUTES = 7;  // the heartbeat is written every 3 minutes
var UPDATE_STALLED_MINUTES = 5;      // a running update writes its status every few seconds

function refreshState_() {
  var now = new Date().getTime();
  var beat = readJson_('refresh_heartbeat.json', true);
  var s = readJson_('refresh_status.json', true) || {};
  var running = s.state === 'running';
  if (running && now - (s.updatedAt || 0) > UPDATE_STALLED_MINUTES * 60000) {
    running = false;
    s.state = 'error';
    s.message = 'The update stopped without finishing (the grades computer may have been turned off or gone to sleep). ' +
                'The dashboard keeps its previous grades.';
  }
  var lastSeen = Math.max((beat && beat.at) || 0, s.updatedAt || 0);
  var waitMs = !running && s.finishedAt ? REFRESH_COOLDOWN_MINUTES * 60000 - (now - s.finishedAt) : 0;
  return {
    available: !!beat,     // false: the watcher has never run on the grades computer
    computerOn: now - lastSeen < COMPUTER_OFF_AFTER_MINUTES * 60000,
    lastSeen: lastSeen ? Utilities.formatDate(new Date(lastSeen), Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm') : '',
    cooldownMinutes: waitMs > 0 ? Math.ceil(waitMs / 60000) : 0,
    state: s.state || 'idle', stage: s.stage || '', pct: s.pct || 0, log: s.log || [], message: s.message || '',
    requestId: s.requestId || '', startedAt: s.startedAt || 0, finishedAt: s.finishedAt || 0, now: now
  };
}

/** Progress of the current or last update, for the page's progress bar. */
function getRefreshStatus(code) {
  authorize_(code);
  return refreshState_();
}

/** The button. Returns { ok, id } or { ok: false, reason }; a running update is simply joined. */
function requestRefresh(code) {
  var access = authorize_(code);
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(10000)) return { ok: false, reason: 'Someone else is starting an update. Please try again in a moment.' };
  try {
    var st = refreshState_();
    if (st.state === 'running') return { ok: true, id: st.requestId, joined: true };
    if (!st.available) return { ok: false, reason: 'The update service has not been set up on the grades computer yet.' };
    if (!st.computerOn) {
      return { ok: false, reason: 'The grades computer appears to be off or asleep' +
        (st.lastSeen ? ' (last seen ' + st.lastSeen + ')' : '') + ', so it can\'t update now.' };
    }
    if (st.cooldownMinutes) {
      return { ok: false, reason: 'The grades were just updated. You can update again in about ' + st.cooldownMinutes + ' minute(s).' };
    }
    var pending = readJson_('refresh_request.json', true);
    if (!pending) return { ok: false, reason: 'The update service has not been set up on the grades computer yet.' };
    if (pending.id && pending.id !== st.requestId && new Date().getTime() - (pending.at || 0) < 180000) {
      return { ok: true, id: pending.id, joined: true };   // asked moments ago, not picked up yet
    }
    var id = Utilities.getUuid();
    DriveApp.getFileById(fileIds_(false)['refresh_request.json'])
      .setContent(JSON.stringify({ id: id, by: access.email, at: new Date().getTime() }));
    logAccess_(access, 'asked for a dashboard update');
    return { ok: true, id: id };
  } finally {
    lock.releaseLock();
  }
}

function shardOf_(studentId) {
  var sum = 0;
  for (var i = 0; i < studentId.length; i++) sum += studentId.charCodeAt(i);
  return sum % SHARDS;
}

/* ------------------------------------------------------------------ */
/* Functions called from the page (google.script.run)                  */
/* ------------------------------------------------------------------ */

/** Checks the code once when the page is unlocked, and records the visit in the Access Log. */
function unlock(code) {
  var access = authorize_(code);
  logAccess_(access, 'opened dashboard');
  var p = periodInfo_(new Date());
  return {
    user: access.email, role: access.role, isAll: access.all,
    codeValidUntil: Utilities.formatDate(p.end, Session.getScriptTimeZone(), 'yyyy-MM-dd HH:mm')
  };
}

/**
 * Everything the overview, watch list, course and student lists need.
 *   rows:  [section, grade_level, semester, student_id, student_name, student_email,
 *           course_name, overall_grade, late_count, assignment_count, missing_count, grade_display,
 *           graded_count]
 *   history: { courses: [name], dates: [day], trend: [sid, ci, di, grade],
 *              past: [sid, semester, ci, grade], semesters: [..],
 *              timeline: [section, semester, ci, day, average, students, below_60],
 *              semesterClasses: [section, semester, ci, average, graded students] (earlier semesters,
 *              all students incl. former ones) }
 */
function getDashboardData(code) {
  var access = authorize_(code);
  var data = readJson_('dashboard.json');
  var rows = data.overall.filter(function (r) { return canSee_(access, r[6]); });

  var visibleStudents = {};
  rows.forEach(function (r) { visibleStudents[r[3]] = true; });
  var homerooms = {};
  Object.keys(data.homerooms || {}).forEach(function (sid) {
    if (visibleStudents[sid]) homerooms[sid] = data.homerooms[sid];
  });

  var history = readJson_('history.json', true) ||
                { courses: [], dates: [], trend: [], past: [], semesters: [], timeline: [] };
  var ok = history.courses.map(function (name) { return canSee_(access, name); });
  history.trend = history.trend.filter(function (r) { return ok[r[1]] && visibleStudents[r[0]]; });
  history.past = history.past.filter(function (r) { return ok[r[2]] && visibleStudents[r[0]]; });
  history.timeline = history.timeline.filter(function (r) { return ok[r[2]]; });
  history.semesterClasses = (history.semesterClasses || []).filter(function (r) { return ok[r[2]]; });
  history.courses = history.courses.map(function (name, i) { return ok[i] ? name : ''; });
  // Current, history and past/archived course names the viewer may see (archived-only names come in
  // pastCourseInfo, kept apart so the Departments tab never gets rows for them).
  var allInfo = {};
  [data.pastCourseInfo || {}, data.courseInfo || {}].forEach(function (m) { Object.keys(m).forEach(function (n) { allInfo[n] = m[n]; }); });
  var courseInfo = courseDepartments_(allInfo, rows.map(function (r) { return r[6]; }).concat(history.courses,
    Object.keys(allInfo).filter(function (n) { return canSee_(access, n); })));

  // Grading-category families per course (department "Categories" panel), visible courses only.
  var catSummary = {};
  Object.keys(data.catSummary || {}).forEach(function (n) { if (canSee_(access, n)) catSummary[n] = data.catSummary[n]; });

  // Draft grades (graded, not returned yet) of visible students in visible courses: [sid, course, n].
  var drafts = (data.drafts || []).filter(function (r) { return visibleStudents[r[0]] && canSee_(access, r[1]); });

  return {
    user: access.email, role: access.role, isAll: access.all, rules: RULES,
    meta: data.meta, sections: data.sections || {}, rows: rows, homerooms: homerooms, history: history,
    courseInfo: courseInfo, catSummary: catSummary, drafts: drafts, hasDrafts: !!data.hasDrafts
  };
}

/**
 * Past grades (earlier semesters, archived Classroom classes, transcripts). Visible to every role
 * unless Settings > "Past grades visible to" lists roles, e.g. "dean, wellbeing" (the owner always can).
 */
function pastAllowed_(access) {
  var v = String(readSettings_()['Past grades visible to'] || 'all').trim().toLowerCase();
  if (!v || v === 'all' || access.role === 'admin') return true;
  return v.split(/[,;\s]+/).indexOf(access.role) !== -1;
}

/** Student list for the Past grades search, past class averages and the old Sheet's department averages. */
function getPastIndex(code) {
  var access = authorize_(code);
  if (!pastAllowed_(access)) return { allowed: false, students: [], classes: [], sheet: [] };
  var idx = readJson_('past_index.json', true) || { students: [], classes: [], sheet: [] };
  // Class averages (department history) follow the usual course access; student past grades don't.
  idx.classes = idx.classes.filter(function (c) { return canSee_(access, c[0]); });
  if (!access.all) idx.sheet = [];
  idx.allowed = true;
  return idx;
}

/** One student's past grades: { n: name, g: [[semester, course, final, calc, class avg, source]], t: [[semester, subject, letter, credit]] } */
function getPastGrades(code, studentId) {
  var access = authorize_(code);
  if (!pastAllowed_(access)) throw new Error('Past grades are not available for your role.');
  studentId = String(studentId);
  var n = shardOf_(studentId);
  var shard = readJson_('past_' + (n < 10 ? '0' : '') + n + '.json', true) || {};
  logAccess_(access, 'viewed past grades');
  return shard[studentId] || { n: '', g: [], t: [] };
}

/** Category averages and per-assignment results for one course. */
function getCourseDetail(code, courseName) {
  var access = authorize_(code);
  if (!canSee_(access, courseName)) throw new Error('You do not have access to this course.');
  var all = readJson_('courses.json', true) || {};
  return all[courseName] || { cats: [], asg: [] };
}

/** Category grades and assignments for one student, limited to courses the viewer may see. */
function getStudentDetail(code, studentId) {
  var access = authorize_(code);
  studentId = String(studentId);
  var n = shardOf_(studentId);
  var shard = readJson_('students_' + (n < 10 ? '0' : '') + n + '.json');
  var entry = shard[studentId] || { c: [], a: [] };
  return {
    categories: entry.c.filter(function (r) { return canSee_(access, r[0]); }),   // [course, category, weight, grade]
    assignments: entry.a.filter(function (r) { return canSee_(access, r[0]); })   // [course, category, title, score, max, late, excused, state]
  };
}

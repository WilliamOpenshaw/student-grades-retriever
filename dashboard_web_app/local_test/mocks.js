// Fake Apps Script services for testing Code.gs + Index.html locally. Never touches Google.
(function () {
  var params = new URLSearchParams(location.search);
  var who = params.get('as') || 'owner';
  var USERS = { owner: 'owner@school.test', dean: 'dean@school.test', wellbeing: 'wb@school.test',
                teacher: 'teacher@school.test', hsteacher: 'hs@school.test' };
  var ACCESS = [['email', 'courses', 'notes', 'role'],
                ['dean@school.test', '', '', 'dean'],
                ['wb@school.test', '', '', 'wellbeing'],
                ['teacher@school.test', 'English, World Literature, Math 6', '', 'teacher'],
                ['hs@school.test', 'Intro. to Physics', '', 'teacher']];
  var sheet = function (values) {
    var noop = function () { return range; };
    var range = { setValue: noop, setFontWeight: noop, setDataValidation: noop };
    return {
      values: values,
      getDataRange: function () { return { getValues: function () { return values.map(function (r) { return r.slice(); }); } }; },
      appendRow: function (r) { values.push(r.slice()); },
      getLastRow: function () { return values.length; },
      getRange: function (row, col, nRows, nCols) {
        if (typeof row !== 'number' || !nRows) return range;
        return { setValues: function (rows) { rows.forEach(function (r, i) { values[row - 1 + i] = r.slice(); }); },
                 setValue: noop, setFontWeight: noop, setDataValidation: noop };
      },
      setFrozenRows: noop, setColumnWidth: noop
    };
  };
  // ?depts=none starts without a Departments tab; otherwise a small tab with two of the dean's edits.
  var DEPTS = [['Course key', 'Department', 'Ontario code', 'Leave out of averages (Y/N)', 'Course names', 'Note'],
               ['Intro. to Entrepreneurship', 'Business', 'BDI3C', 'N', '', 'test: a new department'],
               ['honors philosophy', 'Social Sciences', '', 'Y', '', 'test: lower-case key, left out']];
  var sheets = { 0: sheet(ACCESS), Settings: sheet([['Rotate code every (days)', 7]]), 'Access Log': sheet([['time', 'email', 'role', 'event']]) };
  if (params.get('depts') !== 'none') sheets.Departments = sheet(DEPTS);
  window.FAKE_SHEETS = sheets;   // for checks in the browser console
  window.SpreadsheetApp = {
    newDataValidation: function () { var b = { requireValueInList: function () { return b; }, setAllowInvalid: function () { return b; }, build: function () { return {}; } }; return b; }
  };
  var iter = function (list) { var i = 0; return { hasNext: function () { return i < list.length; }, next: function () { return list[i++]; } }; };
  window.Session = {
    getActiveUser: function () { return { getEmail: function () { return USERS[who] || who; } }; },
    getEffectiveUser: function () { return { getEmail: function () { return USERS.owner; } }; },
    getScriptTimeZone: function () { return 'Etc/UTC'; }
  };
  window.SpreadsheetApp.getActiveSpreadsheet = function () {
    return { getSheets: function () { return [sheets[0]]; }, getSheetByName: function (n) { return sheets[n] || null; }, getId: function () { return 'SHEET'; },
             insertSheet: function (n) { return (sheets[n] = sheet([])); } };
  };
  // "Update grades" button: ?computer=off = no recent heartbeat.
  window.FILES['refresh_heartbeat.json'] = JSON.stringify({ at: params.get('computer') === 'off' ? Date.now() - 36e5 : Date.now() });
  window.FILES['refresh_request.json'] = '{}';
  window.FILES['refresh_status.json'] = '{}';
  window.LockService = { getScriptLock: function () { return { tryLock: function () { return true; }, releaseLock: function () {} }; } };
  window.DriveApp = { getFileById: function (id) {
    if (id === 'SHEET') return { getParents: function () { return iter([{ getFiles: function () {
      return iter(Object.keys(window.FILES).map(function (n) { return { getName: function () { return n; }, getId: function () { return n; } }; }));
    } }]); } };
    return { getBlob: function () { return { getDataAsString: function () { return window.FILES[id]; } }; },
             setContent: function (s) { window.FILES[id] = s; } };
  } };
  var cache = {};
  window.CacheService = { getScriptCache: function () { return { get: function (k) { return cache[k] || null; }, put: function (k, v) { cache[k] = v; } }; } };
  window.PropertiesService = { getScriptProperties: function () { return { getProperty: function (k) { return k === 'CODE_SECRET' ? 'local-test' : null; } }; } };
  window.Utilities = {
    formatDate: function (d, tz, f) { return f === 'Z' ? '+0800' : d.toISOString().slice(0, 16).replace('T', ' '); },
    computeHmacSha256Signature: function (msg, key) {
      var s = msg + key, out = [], h = 2166136261;
      for (var i = 0; i < 32; i++) { for (var j = 0; j < s.length; j++) { h ^= s.charCodeAt(j) + i; h = Math.imul(h, 16777619); } out.push((h >>> 0) & 0xff); }
      return out;
    },
    getUuid: function () { return 'uuid'; }
  };
  window.google = { script: {} };
  Object.defineProperty(window.google.script, 'run', { get: function () {
    var ok, fail, r = {
      withSuccessHandler: function (f) { ok = f; return p; },
      withFailureHandler: function (f) { fail = f; return p; }
    };
    var p = new Proxy(r, { get: function (t, name) {
      if (name in t) return t[name];
      return function () {
        var args = arguments;
        setTimeout(function () {
          try { var res = JSON.parse(JSON.stringify(window[name].apply(null, args))); if (ok) ok(res); }
          catch (e) { console.error(e); if (fail) fail(e); }
        }, 5);
      };
    } });
    return p;
  } });
})();

// Records the README demo video in a hidden Chrome window, using only made-up demo data.
//   node tools/demo/record-demo.js            -> docs/demo.mp4 and docs/demo-poster.jpg
//   node tools/demo/record-demo.js headed     (watch it happen)
//   node tools/demo/record-demo.js noprep     (reuse demo_output/ from the last run)
// Needs Node 22+, Chrome or Edge, ffmpeg, and the project's Python packages (.venv, or set PYTHON).
//
// Preparation runs the real scripts on demo_output/demo_grades.db (made by make_demo_data.py):
// grade calculation, the check, report generation, the email script in --preview mode (never
// connects to email) and the dashboard build in --dry-run mode (never touches Google). The terminal
// scene replays the output those commands actually printed.
const { execFileSync } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { launch, sleep } = require('./cdp');

const ROOT = path.resolve(__dirname, '..', '..');
const OUT = path.join(ROOT, 'demo_output');
const PORT = 5177, W = 1280, H = 720;
const REPO = 'github.com/WilliamOpenshaw/student-grades-retriever';
const args = process.argv.slice(2);

// ---------- 1. preparation: real scripts on the made-up database ----------
function python() {
  if (process.env.PYTHON) return process.env.PYTHON;
  const venv = path.join(ROOT, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  return fs.existsSync(venv) ? venv : 'python';
}
function run(script, ...a) {
  const env = { ...process.env, DB_PATH: path.join(OUT, 'demo_grades.db'), PYTHONIOENCODING: 'utf-8', GRADES_NO_BROWSER: '1', PYTHONDONTWRITEBYTECODE: '1' };
  return execFileSync(python(), [script, ...a], { cwd: ROOT, env, encoding: 'utf-8' });
}
const lines = s => s.replace(/\r/g, '').split('\n').filter(l => l.trim() !== '');
function prepare() {
  console.log('preparing demo data...');
  run('tools/demo/make_demo_data.py');
  const calc = lines(run('calculate_category_grades.py', '--no-dashboard'));
  const verify = lines(run('verify_category_calculations.py', '--latest'));
  const reports = lines(run('generate_grade_reports.py'));
  const preview = lines(run('send_emails_hs.py', '--preview'));
  fs.rmSync(path.join(ROOT, 'dashboard_web_app/local_test/out'), { recursive: true, force: true });
  const sync = lines(run('sync_dashboard.py', '--dry-run', '--out-dir', 'dashboard_web_app/local_test/out/data'));
  run('dashboard_web_app/local_test/build.py');
  const pick = (arr, ...res) => arr.filter(l => res.some(r => r.test(l)));
  const folder = (reports.find(l => /reports saved to/.test(l)) || '').match(/grade_reports\/([^/]+)\//)[1];
  const steps = [
    { cmd: '.venv\\Scripts\\python.exe calculate_category_grades.py', pause: 700,
      out: pick(calc, /Calculating Category|Updated \d+ rows\.|Calculating and Populating|using each course/) },
    { cmd: '.venv\\Scripts\\python.exe verify_category_calculations.py --latest', pause: 900,
      out: pick(verify, /Overall grades|All match|Everything matches/) },
    { cmd: '.venv\\Scripts\\python.exe generate_grade_reports.py', pause: 900,
      out: pick(reports, /latest snapshot/, /rows +\| +\d+ students/, /reports saved/) },
    { cmd: '.venv\\Scripts\\python.exe send_emails_hs.py --preview', pause: 1600,
      out: [...pick(preview, /Found \d+ reports/, /Sending reports for/, /PREVIEW MODE/),
            ...preview.slice(preview.findIndex(l => l.startsWith('Student:')), preview.findIndex(l => l.startsWith('Student:')) + 9), '...'] },
  ];
  // a made-up student whose first class has late work, so the "Late" tag is on screen
  const dir = path.join(ROOT, 'grade_reports', folder);
  const student = fs.readdirSync(dir).filter(f => f.endsWith('.html')).sort().find(f => {
    const first = fs.readFileSync(path.join(dir, f), 'utf8').split('class="course-card"')[1] || '';
    return (first.match(/tag-late/g) || []).length >= 2;
  }).replace(/\.html$/, '');
  fs.writeFileSync(path.join(OUT, 'terminal.json'), JSON.stringify({ steps, folder, student, sync }, null, 1));
}

// ---------- 2. a small web server for the pages, plus PUT /out/ for the recording ----------
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.json': 'application/json', '.css': 'text/css', '.png': 'image/png' };
const server = http.createServer((req, res) => {
  const p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  if (req.method === 'PUT' && p.startsWith('/out/')) {
    const f = fs.createWriteStream(path.join(OUT, path.basename(p)));
    req.pipe(f); f.on('finish', () => { res.writeHead(200); res.end(); });
    return;
  }
  const full = path.join(ROOT, p);
  if (!full.startsWith(ROOT) || !fs.existsSync(full) || fs.statSync(full).isDirectory()) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { 'Content-Type': TYPES[path.extname(full)] || 'application/octet-stream' });
  fs.createReadStream(full).pipe(res);
});

(async () => {
  if (!args.includes('noprep')) prepare();
  const term = JSON.parse(fs.readFileSync(path.join(OUT, 'terminal.json'), 'utf8'));
  await new Promise(r => server.listen(PORT, '127.0.0.1', r));
  const b = await launch({ width: W, height: H, port: 9336, headless: !args.includes('headed') });
  const { send, evaluate } = b;
  const js = (fn, ...a) => evaluate(`(${fn})(${a.map(x => JSON.stringify(x)).join(',')})`);
  const marks = {};
  try {
    await send('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-color-scheme', value: 'light' }] });
    await send('Page.navigate', { url: `http://127.0.0.1:${PORT}/tools/demo/stage.html` });
    await sleep(1000);
    await evaluate(`window.__errs = []; addEventListener('error', e => __errs.push(e.message));`);

    // ---------- input helpers: real mouse events through DevTools, cursor drawn by the stage ----------
    let cur = { x: W / 2, y: H / 2 };
    const mouse = (type, x, y, buttons = 0) => send('Input.dispatchMouseEvent', { type, x, y, buttons, button: type === 'mouseMoved' ? 'none' : 'left', clickCount: type === 'mouseMoved' ? 0 : 1 });
    const moveTo = async (p, ms = 500) => {
      const s0 = { ...cur }, t0 = Date.now();
      for (;;) {
        const t = Math.min(1, (Date.now() - t0) / ms), e = t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
        cur = { x: Math.round(s0.x + (p.x - s0.x) * e), y: Math.round(s0.y + (p.y - s0.y) * e) };
        await js((x, y) => __demo.place(x, y), cur.x, cur.y);
        await mouse('mouseMoved', cur.x, cur.y);
        if (t >= 1) break;
        await sleep(14);
      }
    };
    const click = async (p, ms) => {
      await moveTo(p, ms); await sleep(120);
      await js((x, y) => __demo.ping(x, y), p.x, p.y);
      await mouse('mousePressed', p.x, p.y, 1); await sleep(70); await mouse('mouseReleased', p.x, p.y); await sleep(150);
    };
    const at = (sel, text) => js((s, t) => __demo.center(s, t), sel, text || '');
    const wheel = async (dy, steps = 6) => { for (let i = 0; i < steps; i++) { await send('Input.dispatchMouseEvent', { type: 'mouseWheel', x: cur.x, y: cur.y, deltaX: 0, deltaY: dy / steps }); await sleep(60); } };
    const T0 = Date.now();
    const stamp = () => ((Date.now() - T0) / 1000).toFixed(1);
    const caption = (html, where) => { console.log(stamp() + 's', html.replace(/<.*/, '')); return js((h, w) => __demo.caption(h, w), html, where || ''); };
    const hide = () => js(() => __demo.hide());
    const card = html => js(h => __demo.card(h), html);
    const load = url => js(u => __demo.load(u), url);

    // ---------- 3. scenes ----------
    await card(`<h1>Student Grades Retriever</h1>
      <p>Copies Google Classroom grades into a local SQLite database, makes a monthly HTML grade report
      for each student, emails the reports to families, and shows the grades on a staff dashboard.</p>
      <p class="note">All names, emails and grades in this video are made-up demo data.</p>`);
    await load('/tools/demo/terminal.html');
    const mime = await evaluate('__demo.startRec(4000000)', true);
    console.log('recording as', mime);
    await sleep(5500);

    // A. the monthly run in a terminal
    await js(() => __demo.uncard());
    marks.terminal = stamp();
    await caption('The monthly run is a series of Python scripts<small>after ingest_classroom_data.py and the enrich scripts have read the grades from Classroom</small>');
    await evaluate(`__demo.win().play(${JSON.stringify(term.steps)})`);
    await hide();
    await sleep(300);

    // B. a grade report
    await load(`/grade_reports/${encodeURIComponent(term.folder)}/${encodeURIComponent(term.student)}.html`);
    marks.report = stamp();
    await moveTo({ x: 1040, y: 380 }, 400);
    await caption('One HTML report per student: overall grade for each class, category grades and weights');
    await sleep(2600);
    await wheel(330, 8);
    await sleep(2200);
    await wheel(320, 8);
    await sleep(800);
    await caption('Every assignment with its category weight and score; late work is tagged');
    const det = await at('.asgmt-details summary');
    await click(det, 600);
    await sleep(1800);
    await wheel(300, 8);
    await sleep(2400);
    await caption('The report is one file, attached to an email to the student and to the parents<small>email text in English and Traditional Chinese (editable)</small>');
    await sleep(3600);
    await hide();

    // C. the staff dashboard (local test copy with the same Code.gs and Index.html, fake Google services)
    await load('/dashboard_web_app/local_test/out/index.html?as=owner');
    await sleep(900);
    marks.dashboard = stamp();
    await caption('Staff dashboard: a Google Apps Script web app that reads the synced grade files from Google Drive');
    await click(await at('#tabs button', 'Overview'), 600);
    await moveTo({ x: 700, y: 400 }, 500);
    await sleep(3200);
    await wheel(420, 8);
    await sleep(1800);
    await wheel(-420, 6);
    await caption('Watch list: students flagged by adjustable rules<small>low course grades, low average, missing work, falling average</small>');
    await click(await at('#tabs button', 'Watch list'), 600);
    marks.watch = stamp();
    await sleep(3400);
    await caption('Student page: current grades, change over time, categories and assignments');
    await click(await at('[data-student]'), 600);
    await sleep(2600);
    await wheel(450, 8);
    await sleep(2200);
    await caption('Courses: average, trend and missing work for each class');
    await click(await at('#tabs button', 'Courses'), 600);
    await sleep(2200);
    await click(await at('[data-course]'), 600);
    await sleep(2400);
    await wheel(380, 8);
    await sleep(1600);
    await caption('Departments: average of class averages for each department, at each grade pull');
    await click(await at('#tabs button', 'Departments'), 600);
    marks.depts = stamp();
    await sleep(3600);
    await caption('Each staff member sees only the courses listed for them in an access Sheet<small>and must enter an access code that changes every week (by default)</small>');
    await moveTo({ x: 900, y: 330 }, 600);
    await sleep(3800);
    await hide();

    // D. end card
    await js(() => __demo.place(-100, -100));
    await card(`<h1>Student Grades Retriever</h1>
      <ul><li>Python 3.11+ and a Google Cloud OAuth client (Classroom API, read-only)</li>
      <li>One Google account per school section (High School and Junior High)</li>
      <li>Written for Windows; the daily dashboard update uses Task Scheduler</li></ul>
      <p class="url">${REPO}</p>`);
    await sleep(5500);

    const size = await evaluate(`__demo.stopRec('/out/demo-raw.${mime.includes('webm') ? 'webm' : 'mp4'}')`);
    console.log(`saved raw recording (${(size / 1048576).toFixed(2)} MB)`, JSON.stringify(marks));
    console.log('page errors:', JSON.stringify(await evaluate('(window.__errs||[])')));
    fs.writeFileSync(path.join(OUT, 'marks.json'), JSON.stringify({ marks, mime }));
  } finally {
    b.close();
    server.close();
  }

  // ---------- 4. web encode and a poster frame ----------
  const { marks: mk, mime: m } = JSON.parse(fs.readFileSync(path.join(OUT, 'marks.json'), 'utf8'));
  const raw = path.join(OUT, `demo-raw.${m.includes('webm') ? 'webm' : 'mp4'}`);
  const docs = path.join(ROOT, 'docs');
  fs.mkdirSync(docs, { recursive: true });
  const ff = a => execFileSync('ffmpeg', ['-hide_banner', '-loglevel', 'error', '-y', ...a], { stdio: 'inherit' });
  ff(['-i', raw, '-an', '-vf', 'fps=30,format=yuv420p', '-c:v', 'libx264', '-preset', 'slow', '-crf', '26', '-movflags', '+faststart', path.join(docs, 'demo.mp4')]);
  ff(['-ss', String(+mk.dashboard + 2.5), '-i', path.join(docs, 'demo.mp4'), '-frames:v', '1', '-q:v', '3', path.join(docs, 'demo-poster.jpg')]);
  console.log(`docs/demo.mp4: ${(fs.statSync(path.join(docs, 'demo.mp4')).size / 1048576).toFixed(2)} MB`);
})().catch(e => { console.error(e); process.exit(1); });

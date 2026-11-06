r'''
Local test page for the staff dashboard (Code.gs + Index.html) with fake Apps Script services.
Never touches the real Sheet, Drive or email. Not part of the Apps Script project.

    .venv\Scripts\python.exe sync_dashboard.py --dry-run --out-dir dashboard_web_app\local_test\out\data
    .venv\Scripts\python.exe dashboard_web_app\local_test\build.py

Then serve local_test\out on 127.0.0.1 (Claude: preview "dashboard-harness" in .claude\launch.json)
and open index.html?as=owner|dean|wellbeing|teacher|hsteacher (accounts are defined in mocks.js).
The page unlocks itself with the current fake access code.

out\ holds real student data (a copy of the dashboard JSON): keep it on this computer, and
delete it when you no longer need it.
'''
import json
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
OUT = os.path.join(HERE, 'out')
DATA = os.path.join(OUT, 'data')

files = {n: open(os.path.join(DATA, n), encoding='utf-8').read() for n in os.listdir(DATA) if n.endswith('.json')}
with open(os.path.join(OUT, 'data.js'), 'w', encoding='utf-8') as f:
    f.write('window.FILES = ' + json.dumps(files) + ';')
shutil.copyfile(os.path.join(APP, 'Code.gs'), os.path.join(OUT, 'code.js'))
shutil.copyfile(os.path.join(HERE, 'mocks.js'), os.path.join(OUT, 'mocks.js'))

page = open(os.path.join(APP, 'Index.html'), encoding='utf-8').read()
# Apps Script adds the viewport tag in doGet(); add it here so phone widths test correctly.
page = page.replace('<meta charset="utf-8">', '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">', 1)
inject = ('<script src="data.js"></script><script src="mocks.js"></script><script src="code.js"></script>'
          '<script>try { sessionStorage.setItem("dash_code", codeFor_(periodInfo_(new Date()).index)); } '
          'catch (e) { console.error(e); }</script>\n')
i = page.index('<script>')
with open(os.path.join(OUT, 'index.html'), 'w', encoding='utf-8') as f:
    f.write(page[:i] + inject + page[i:])
print(f'Built {os.path.join(OUT, "index.html")} with {len(files)} data files.')

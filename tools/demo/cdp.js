// Minimal Chrome DevTools Protocol helper for the demo recorder (no npm packages needed).
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const sleep = ms => new Promise(r => setTimeout(r, ms));

function findChrome() {
  const list = process.platform === 'win32'
    ? ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe']
    : ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/usr/bin/google-chrome', '/usr/bin/chromium'];
  const found = list.find(p => fs.existsSync(p));
  if (!found) throw new Error('Chrome or Edge not found');
  return found;
}

async function launch({ width, height, port = 9333, headless = true }) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'grades-demo-'));
  const args = [
    `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, `--window-size=${width},${height}`,
    '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--hide-scrollbars', '--mute-audio',
    '--auto-accept-this-tab-capture', '--autoplay-policy=no-user-gesture-required', '--force-device-scale-factor=1',
    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
  ];
  if (headless) args.push('--headless=new');
  args.push('about:blank');
  const proc = spawn(findChrome(), args, { stdio: 'ignore' });
  let targets;
  for (let i = 0; i < 50; i++) {
    try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (targets.some(t => t.type === 'page')) break; } catch (e) { /* not up yet */ }
    await sleep(200);
  }
  const page = targets.find(t => t.type === 'page');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pending = new Map();
  const listeners = [];
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result); }
    else if (m.method) listeners.forEach(l => l(m));
  };
  const send = (method, params = {}) => new Promise((res, rej) => { const i = ++id; pending.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
  const evaluate = async (expression, userGesture = false) => {
    const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true, userGesture });
    if (r.exceptionDetails) throw new Error('page error: ' + (r.exceptionDetails.exception && r.exceptionDetails.exception.description || r.exceptionDetails.text));
    return r.result.value;
  };
  const close = () => { try { ws.close(); } catch (e) { /* ignore */ } proc.kill(); setTimeout(() => fs.rmSync(profile, { recursive: true, force: true }), 1500); };
  await send('Page.enable');
  await send('Runtime.enable');
  // Grow the window until the page area (what tab capture records) is exactly width x height.
  const { windowId } = await send('Browser.getWindowForTarget');
  for (let i = 0; i < 3; i++) {
    const inner = await evaluate('[innerWidth, innerHeight]');
    const { bounds } = await send('Browser.getWindowBounds', { windowId });
    if (inner[0] === width && inner[1] === height) break;
    await send('Browser.setWindowBounds', { windowId, bounds: { width: bounds.width + width - inner[0], height: bounds.height + height - inner[1] } });
    await sleep(300);
  }
  return { send, evaluate, close, on: f => listeners.push(f) };
}

module.exports = { launch, sleep };

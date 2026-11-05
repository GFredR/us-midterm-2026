#!/usr/bin/env node
// verify.mjs — 加载本地页面，检查渲染结果与控制台错误，并截图
import fs from 'node:fs';
const PORT = process.env.CDP_PORT || 9333;
const URL_ = process.argv[2];
const SHOT = process.argv[3];

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.listeners = []; }
  static async connect(wsUrl) {
    const ws = new WebSocket(wsUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    const c = new CDP(ws);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && c.pending.has(msg.id)) {
        const { resolve, reject } = c.pending.get(msg.id);
        c.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      } else if (msg.method) c.listeners.forEach((fn) => fn(msg));
    };
    return c;
  }
  send(method, params = {}, sessionId) {
    const id = ++this.id;
    const payload = { id, method, params };
    if (sessionId) payload.sessionId = sessionId;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify(payload));
      setTimeout(() => { if (this.pending.has(id)) { this.pending.delete(id); reject(new Error('timeout ' + method)); } }, 60000);
    });
  }
}

const browser = await CDP.connect((await (await fetch(`http://127.0.0.1:${PORT}/json/version`)).json()).webSocketDebuggerUrl);
const { targetId } = await browser.send('Target.createTarget', { url: 'about:blank' });
const { sessionId } = await browser.send('Target.attachToTarget', { targetId, flatten: true });
await browser.send('Page.enable', {}, sessionId);
await browser.send('Runtime.enable', {}, sessionId);
await browser.send('Log.enable', {}, sessionId);
await browser.send('Emulation.setDeviceMetricsOverride', { width: 1440, height: 2200, deviceScaleFactor: 1, mobile: false }, sessionId);

const logs = [];
browser.listeners.push((m) => {
  if (m.sessionId !== sessionId) return;
  if (m.method === 'Runtime.consoleAPICalled') {
    logs.push(`[${m.params.type}] ` + (m.params.args || []).map(a => a.value ?? a.description ?? '').join(' '));
  }
  if (m.method === 'Runtime.exceptionThrown') {
    logs.push('[exception] ' + (m.params.exceptionDetails?.exception?.description || m.params.exceptionDetails?.text));
  }
  if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
    logs.push('[log.error] ' + m.params.entry.text);
  }
});

await browser.send('Page.navigate', { url: URL_ }, sessionId);
await new Promise(r => setTimeout(r, 6000));

const r = await browser.send('Runtime.evaluate', {
  expression: `JSON.stringify({
    title: document.title,
    badge: document.querySelector('#statusBadge')?.innerText,
    statusNote: document.querySelector('#statusNote')?.innerText,
    updated: document.querySelector('#updatedAt')?.innerText,
    kpiCount: document.querySelectorAll('#kpiGrid .kpi').length,
    kpiText: [...document.querySelectorAll('#kpiGrid .kpi')].map(k=>k.innerText.replace(/\\n+/g,' | ')),
    houseSeats: document.querySelectorAll('#houseGrid .house-seat').length,
    houseSub: document.querySelector('#houseSub')?.innerText,
    voteLegend: document.querySelector('#voteLegend')?.innerText,
    coverageNote: document.querySelector('#coverageNote')?.innerText,
    battleRows: document.querySelectorAll('#battleBody tr').length,
    battleFirst: [...document.querySelectorAll('#battleBody tr')].slice(0,4).map(t=>t.innerText.replace(/\\n+/g,' | ')),
    raceRows: document.querySelectorAll('#raceBody tr').length,
    sourceRows: document.querySelectorAll('#sourceBody tr').length,
    chartCanvas: !!document.querySelector('#voteChart canvas'),
    bodyH: document.body.scrollHeight
  }, null, 1)`, returnByValue: true, awaitPromise: true
}, sessionId);

console.log(r.result?.value || 'EVAL FAILED');
console.log('--- console ---');
console.log(logs.length ? logs.join('\n') : '(无错误)');

if (SHOT) {
  // 可选第 4 个参数：截图前先滚动到指定选择器，便于核对局部版式
  const anchor = process.argv[4];
  if (anchor) {
    await browser.send('Runtime.evaluate', {
      expression: `document.querySelector(${JSON.stringify(anchor)})?.scrollIntoView({block:'start'}); window.scrollBy(0,-12);`,
      returnByValue: true
    }, sessionId);
    await new Promise(r => setTimeout(r, 1200));
  }
  // 页面很长，整页截图会超时；只截当前视口作为渲染校验
  const shot = await browser.send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false }, sessionId);
  fs.writeFileSync(SHOT, Buffer.from(shot.data, 'base64'));
  console.log('--- screenshot ->', SHOT);
}
await browser.send('Target.closeTarget', { targetId });
browser.ws.close();
process.exit(0);

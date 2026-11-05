#!/usr/bin/env node
/**
 * browser.mjs — 用本机 Chrome（CDP）抓取官方选举结果页面
 *
 *   node browser.mjs html <url> <waitMs> <outFile>
 *       渲染页面后输出 outerHTML（含同源 iframe 内容）；自动处理 Cloudflare 挑战页
 *
 *   node browser.mjs enr <electionUrl> <outFile>
 *       打开 SOE ENR / Clarity 选举页，捕获其调用的州级汇总 JSON 并原样输出
 *
 *   node browser.mjs links <url> <waitMs> <outFile>
 *       输出页面所有链接（用于定位 CSV / PDF / 子结果页）
 *
 * 依赖：系统已安装 Google Chrome；需以 --remote-debugging-port=9333 启动。
 */
import fs from 'node:fs';

const PORT = process.env.CDP_PORT || 9333;
const mode = process.argv[2];
const url = process.argv[3];
const arg4 = process.argv[4];
const arg5 = process.argv[5];

// Node 22+ 默认提供全局 WebSocket；Node 20 需要 --experimental-websocket。
// 这里提前给出明确报错，否则会抛一个看不出原因的 "WebSocket is not defined"。
if (typeof WebSocket === 'undefined') {
  console.error(
    '当前 Node 缺少全局 WebSocket。请使用 Node 22 及以上版本，' +
    '或用 node --experimental-websocket 运行。当前版本：' + process.version
  );
  process.exit(4);
}

const IS_MAC = process.platform === 'darwin';
const UA = IS_MAC
  ? 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36'
  : 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';
const UA_PLATFORM = IS_MAC ? 'MacIntel' : 'Linux x86_64';
const CHALLENGE = /just a moment|performing security verification|attention required|checking your browser|one more step|ddos protection|request unsuccessful|incapsula incident|access denied|enable javascript and cookies/i;

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
      } else if (msg.method) {
        c.listeners.forEach((fn) => fn(msg));
      }
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
await browser.send('Network.enable', {}, sessionId);
await browser.send('Network.setUserAgentOverride', { userAgent: UA, acceptLanguage: 'en-US,en;q=0.9', platform: UA_PLATFORM }, sessionId);
await browser.send('Emulation.setUserAgentOverride', { userAgent: UA, acceptLanguage: 'en-US,en;q=0.9', platform: UA_PLATFORM }, sessionId);

const seen = new Map();
browser.listeners.push((m) => {
  if (m.method === 'Network.responseReceived' && m.sessionId === sessionId) {
    seen.set(m.params.response.url, m.params.response.mimeType || '');
  }
});

const evalJs = async (expression) => {
  const r = await browser.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
  return r.result?.value;
};

await browser.send('Page.navigate', { url }, sessionId);

if (mode === 'raw') {
  // 抓任意 URL 的纯文本内容（用于直接取官方 JSON / txt 接口的原文）
  // 参数顺序与 html 模式一致：<url> <waitMs> <outFile>
  const waitMs = Number(arg4 || 6000);
  const outFile = arg5;
  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1000));
    const probe = await evalJs('document.body ? document.body.innerText.length : 0');
    if (Number(probe) > 0) break;
  }
  const text = await evalJs(`(() => {
    const t = document.body ? document.body.innerText : '';
    const pre = document.querySelector('pre');
    return (pre ? pre.innerText : t) || '';
  })()`);
  fs.writeFileSync(outFile, text || '');
  console.error(`raw 已写入 ${outFile}（${(text || '').length} 字节）`);
} else if (mode === 'enr') {
  const outFile = arg4;
  const deadline = Date.now() + 40000;
  let target = null;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 900));
    for (const [u, ct] of seen) {
      if (/json/i.test(ct) && /\/json\/(sum|en\/summary)\.json(\?|$)/.test(u)) { target = u; break; }
    }
    if (target) break;
  }
  if (!target) {
    console.error('未捕获到 ENR 汇总 JSON。已捕获的 JSON 接口：\n' +
      [...seen.keys()].filter((u) => /json/i.test(seen.get(u))).join('\n'));
    await browser.send('Target.closeTarget', { targetId });
    browser.ws.close();
    process.exit(2);
  }
  const body = await evalJs(`fetch(${JSON.stringify(target)}).then(r => r.text())`);
  fs.writeFileSync(outFile, body || '');
  console.error(`ENR 汇总已写入 ${outFile}（源：${target}，${(body || '').length} 字节）`);
} else {
  const waitMs = Number(arg4 || 9000);
  const outFile = arg5;
  const deadline = Date.now() + waitMs;
  let attempts = 0;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 1200));
    const probe = await evalJs('document.title + "||" + (document.body ? document.body.innerText.slice(0,400) : "")');
    if (!CHALLENGE.test(probe || '') && attempts >= 1) break;
    if (CHALLENGE.test(probe || '') && attempts < 3) {
      attempts++;
      await browser.send('Page.reload', { ignoreCache: false }, sessionId);
      await new Promise((r) => setTimeout(r, 3500));
    }
  }
  await new Promise((r) => setTimeout(r, 1500));

  // 导航失败检测（DNS 被屏蔽 / 连接失败时 Chrome 显示错误页）
  const navCheck = await evalJs(`JSON.stringify({ href: location.href, title: document.title, body: (document.body ? document.body.innerText.slice(0,160) : '') })`);
  let nav = {};
  try { nav = JSON.parse(navCheck || '{}'); } catch { nav = {}; }
  const navFailed = /^https?:\/\//.test(nav.title || '') || /ERR_|not valid|无法访问|非法的|DNS_PROBE/i.test(nav.body || '');

  if (mode === 'net') {
    const all = [...seen.keys()].filter((u) => !/^(data:|blob:|chrome-)/.test(u));
    const urls = all.filter((u) => /json|api|result|candidate|contest|race|election|summary|vote|ajax|svc/i.test(u));
    console.log(JSON.stringify({ url, title: nav.title, navFailed, urls, all: all.slice(0, 80) }, null, 1));
  } else if (mode === 'probe') {
    const probe = await evalJs(`JSON.stringify({
      href: location.href, title: document.title,
      len: (document.body ? document.body.innerText.length : 0),
      head: (document.body ? document.body.innerText.slice(0,260) : '')
    })`);
    console.log(JSON.stringify({ ...JSON.parse(probe || '{}'), navFailed }));
  } else if (navFailed) {
    console.error(`NAV_FAILED: ${nav.title || nav.href} | ${(nav.body || '').replace(/\\s+/g, ' ').slice(0, 120)}`);
    await browser.send('Target.closeTarget', { targetId });
    browser.ws.close();
    process.exit(3);
  }

  if (mode === 'links') {
    const links = await evalJs(`JSON.stringify({
      url: location.href, title: document.title,
      links: [...document.querySelectorAll('a[href]')].map(a => ((a.innerText||'').trim().replace(/\\s+/g,' ')) + ' => ' + a.href).slice(0,200),
      text: document.body.innerText.slice(0,3000)
    }, null, 1)`);
    fs.writeFileSync(outFile, links || '{}');
    console.error(`链接已写入 ${outFile}`);
  } else {
    // 主体 + 所有 iframe（含跨域）的内容
    // 同源 iframe 用 f.contentDocument 能拿，跨域拿不到（会抛安全异常）→ 只能靠 CDP
    // 在每个 frame 自己的执行上下文里取 outerHTML。
    const html = await evalJs(`(() => {
      const parts = [];
      parts.push('<!-- MAIN FRAME -->\\n' + document.documentElement.outerHTML);
      const frames = [...document.querySelectorAll('iframe,frame')];
      frames.forEach((f, i) => {
        try {
          const d = f.contentDocument;
          if (d) parts.push('\\n<!-- FRAME ' + i + ' ' + (f.src||'') + ' -->\\n' + d.documentElement.outerHTML);
          else parts.push('\\n<!-- FRAME ' + i + ' ' + (f.src||'') + ' cross-origin -->');
        } catch (e) { parts.push('\\n<!-- FRAME ' + i + ' ' + (f.src||'') + ' inaccessible -->'); }
      });
      return parts.join('\\n');
    })()`);

    // 跨域 iframe：用 Page.getFrameTree + 隔离世界逐个取
    const frameHtml = [];
    try {
      const tree = await browser.send('Page.getFrameTree', {}, sessionId);
      const frameIds = [];
      (function walk(node) {
        if (!node) return;
        if (node.frame && node.frame.id) frameIds.push(node.frame.id);
        (node.childFrames || []).forEach(walk);
      })(tree.frameTree);
      for (const fid of frameIds) {
        try {
          const world = await browser.send('Page.createIsolatedWorld', {
            frameId: fid, worldName: 'elscrape-probe', grantUniveralAccess: true,
          }, sessionId);
          const r = await browser.send('Runtime.evaluate', {
            expression: 'document.documentElement ? document.documentElement.outerHTML : ""',
            contextId: world.executionContextId, returnByValue: true,
          }, sessionId);
          const h = r && r.result && r.result.value;
          if (h && h.length > 200) frameHtml.push(`<!-- FRAME ${fid} -->\n${h}`);
        } catch (e) { /* 该 frame 不可读，跳过 */ }
      }
    } catch (e) { /* getFrameTree 失败不影响主流程 */ }

    const out = (html || '') + (frameHtml.length ? '\n\n' + frameHtml.join('\n\n') : '');
    fs.writeFileSync(outFile, out || '');
    console.error(`页面已写入 ${outFile}（${(out || '').length} 字节，${frameHtml.length} 个 iframe 内容）`);
  }
}

await browser.send('Target.closeTarget', { targetId });
browser.ws.close();
process.exit(0);

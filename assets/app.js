/* ==========================================================================
   2026 US Midterm Election Tracker — frontend
   数据文件：data/election-2026.json（由 scripts/collect.py 从各州官方系统抓取生成）

   席位模型（total / up / unchanged / pending）：
     total    该院/该职位总席位数
     up       本轮改选席位数
     unchanged 本轮未改选、仍由原持有人占有的席位（来自 baseline）
     called   本轮改选且官方结果已产生领先方的席位
     pending  本轮改选但尚无官方结果的席位
   ========================================================================== */
'use strict';

const CFG = window.SITE_CONFIG || {};
const DATA_URL = CFG.local || 'data/election-2026.json';
const REMOTE_DATA_URL = CFG.remote || '';
const AUTO_REFRESH_MS = CFG.autoRefreshMs || 5 * 60 * 1000;
const SERVER_STATUS_URL = CFG.serverStatusUrl || '/api/status';
const SERVER_REFRESH_URL = CFG.serverRefreshUrl || '/api/refresh';

const PARTY = {
  R: { label: '共和党', cls: 'R', color: '#e0484b' },
  D: { label: '民主党', cls: 'D', color: '#2f83f7' },
  O: { label: '其他', cls: 'O', color: '#8b97a8' }
};

const CHAMBER = {
  senate: '参议院',
  house: '众议院',
  governor: '州长',
  other: '其他州级职位'
};

const state = {
  raw: null,
  races: [],
  states: [],
  seats: null,
  baseline: {},
  serverMode: false,       // 服务端部署（可真的重抓）
  serverChecked: false,
  serverStatus: null,
  dataSource: 'local',     // 当前展示的数据来自 'remote'(CI 快照) 还是 'local'(同源快照)
  remoteFailed: false,
  sortBattle: { key: 'marginPct', dir: 'asc' },
  sortRace: { key: null, dir: 'asc' },
  filters: { q: '', party: '', chamber: '' },
  charts: []
};

/* ---------------- utils ---------------- */
const $ = (s) => document.querySelector(s);
const fmt = (n) => (n === null || n === undefined || isNaN(n)) ? '—' : Number(n).toLocaleString('en-US');
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (n, t) => (!t ? 0 : (n / t * 100).toFixed(2));

function fmtTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return String(iso);
  const pad = (n) => String(n).padStart(2, '0');
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  return `${sameDay ? '今天 ' : ''}${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function relTime(iso) {
  if (!iso) return '';
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (isNaN(diff)) return '';
  if (diff < 90) return '刚刚';
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`;
  return `${Math.floor(diff / 86400)} 天前`;
}

/* ---------------- aggregation ---------------- */
const CHAMBER_DEF = {
  house: { key: 'house', label: '美国众议院', total: 435, threshold: 218, up: 435 },
  senate: { key: 'senate', label: '美国参议院', total: 100, threshold: 50, up: 35 },
  governor: { key: 'governor', label: '州长', total: 50, threshold: 26, up: 36 }
};

/**
 * 哥伦比亚特区在众议院只有 1 名无表决权代表（delegate），
 * 不占 435 个投票席位、也不影响 218 席过半门槛。
 * 依据 National Archives 官方选举人团表：50 州 EV 合计 535 − 100 参院席 = 435 席。
 */
function isDelegate(st) {
  return st.id === 'DC';
}

/** 该州在本层级本轮有几个席位改选 */
function seatsUpIn(st, chamber) {
  if (chamber === 'house') return isDelegate(st) ? 0 : (st.house_seats || 0);
  if (chamber === 'senate') return st.senate_seats_up || 0;
  if (chamber === 'governor') return st.gov_up ? 1 : 0;
  return 0;
}

/** 该州该层级席位的原持有人党派（仅参院 / 州长有） */
function holderOf(st, chamber) {
  if (chamber === 'senate') return st.senate_holder || '';
  if (chamber === 'governor') return st.gov_holder || '';
  return '';
}

/**
 * 依据各州官方结果重算席位分布。
 * 返回每个层级的 total / up / called / pending / unchanged / 翻转席位。
 */
function computeSeats(states, races, baseline) {
  const out = {};
  for (const key of ['house', 'senate', 'governor']) {
    const def = CHAMBER_DEF[key];
    const b = (baseline && baseline[key]) || {};
    const hold = b.unchanged || { R: 0, D: 0, O: 0 };
    const called = { R: 0, D: 0, O: 0 };
    const flips = [];
    let pending = 0;
    let resolved = 0;

    for (const st of states) {
      const n = seatsUpIn(st, key);
      if (!n) continue;
      const stRaces = races.filter((r) => r.state === st.id && r.chamber === key);
      const seen = new Set();
      for (const r of stRaces) {
        if (seen.size >= n) break;      // 一个州该层级只认 n 席
        if (seen.has(r.id)) continue;
        const lead = leadParty(r);
        if (!lead) continue;
        seen.add(r.id);
        called[lead]++;
        resolved++;
        const h = holderOf(st, key);
        if (h && lead !== h) {
          flips.push({ state: st.id, state_zh: st.state_zh || st.zh, office: r.office,
                       from: h, to: lead, race: r });
        }
      }
      pending += Math.max(0, n - seen.size);
    }

    const R = hold.R + called.R;
    const D = hold.D + called.D;
    const O = hold.O + called.O;
    out[key] = {
      ...def,
      up: def.up,
      R, D, O, pending,
      holdR: hold.R, holdD: hold.D, holdO: hold.O,
      called: { ...called },
      resolved,
      flips,
      netD: flips.filter((f) => f.to === 'D').length - flips.filter((f) => f.to === 'R').length,
      upHolder: b.up_holder || {},
      note: b.note || ''
    };
  }
  return out;
}

function leadParty(race) {
  const cs = (race.candidates || []).filter((c) => c.votes !== null && c.votes !== undefined);
  if (!cs.length) return null;
  const top = [...cs].sort((a, b) => (b.votes || 0) - (a.votes || 0))[0];
  const p = (top.party || '').trim().toUpperCase();
  if (!p) return null;                       // 党派未标注 → 不计入席位
  if (p.startsWith('R') || p.includes('REPUBLICAN') || p.includes('GOP')) return 'R';
  if (p.startsWith('D') || p.includes('DEMOCRATIC')) return 'D';
  return 'O';
}

function marginOf(race) {
  const c = (race.candidates || []).filter((x) => x.votes !== null && x.votes !== undefined);
  if (c.length < 2) return null;
  const s = [...c].sort((a, b) => b.votes - a.votes);
  return s[0].votes - s[1].votes;
}

function totalVotesOf(race) {
  return (race.candidates || []).reduce((s, c) => s + (c.votes || 0), 0);
}

/* ---------------- render: KPI ---------------- */
function renderKPIs(seats) {
  const grid = $('#kpiGrid');
  const order = ['house', 'senate', 'governor'];
  grid.innerHTML = order.map((k) => {
    const s = seats[k];
    const controller = s.R > s.D ? 'R' : s.D > s.R ? 'D' : null;
    const ctrlHtml = controller
      ? `<div class="kpi-ctrl"><span class="dot" style="background:${PARTY[controller].color}"></span>当前领先：<b style="color:${PARTY[controller].color}">${PARTY[controller].label}</b>　·　需 ${s.threshold} 席过半</div>`
      : `<div class="kpi-ctrl"><span class="dot" style="background:var(--text-3)"></span>暂无过半一方　·　需 ${s.threshold} 席过半</div>`;

    const upLine = k === 'house'
      ? `全部 ${s.up} 席改选 · 已出结果 ${s.resolved} 席`
      : `本轮改选 ${s.up} 席 · 已出结果 ${s.resolved} 席 · 待定 ${s.pending} 席`;

    let netHtml = '';
    {
      const upR = s.upHolder.R || 0, upD = s.upHolder.D || 0;
      const flipR = s.flips.filter((f) => f.to === 'R').length;
      const flipD = s.flips.filter((f) => f.to === 'D').length;
      const needR = Math.max(0, s.threshold - s.holdR);   // 红方需在本轮改选席中拿下的席数
      const needD = Math.max(0, s.threshold - s.holdD);
      const holder = k === 'house'
        ? '本轮 435 席全部改选'
        : `改选席位原持有人 R${upR}/D${upD}`;
      netHtml = `<div class="kpi-net">${holder} · 已出结果席位 <b style="color:${PARTY.R.color}">红 ${s.called.R}</b> / <b style="color:${PARTY.D.color}">蓝 ${s.called.D}</b>
        ${s.flips.length ? `· <span class="flip-tag">翻转 红 ${flipR} / 蓝 ${flipD}</span>` : ''}</div>
        <div class="kpi-net">过半所需：在本轮 ${s.up} 席中 <b style="color:${PARTY.D.color}">蓝需 ≥${needD} 席</b> · <b style="color:${PARTY.R.color}">红需 ≥${needR} 席</b></div>`;
    }

    // 席位条：红左蓝右双向分布。
    // 红从最左端长出、蓝从最右端长出，中间斜纹区为尚未确定归属的席位（待数据 + 其他）。
    // 过半线画在"过半席位"那一格的中心：席位编号从 1 计，第 N 席占据 [N-1, N]，故线在 N-0.5 处。
    const overLine = pct(s.threshold - 0.5, s.total);
    const bar = `
      <div class="seatbar">
        <div class="seg rep" style="width:${pct(s.R, s.total)}%" title="共和党 ${s.R} 席"></div>
        <div class="seg oth" style="width:${pct(s.O, s.total)}%" title="其他 ${s.O} 席"></div>
        <div class="seg pend" style="width:${pct(s.pending, s.total)}%" title="暂无官方数据 ${s.pending} 席"></div>
        <div class="seg dem" style="width:${pct(s.D, s.total)}%" title="民主党 ${s.D} 席"></div>
      </div>
      <div class="threshold"><i style="left:${overLine}%" data-label="过半 ${s.threshold}"></i></div>
      <div class="seatbar-labels">
        <span class="end-cap rep">← 共和党 0</span>
        <span class="end-cap dem">${s.total} 民主党 →</span>
      </div>`;

    return `
      <div class="kpi" style="--kpi-accent:${PARTY[controller || 'O'].color}">
        <div class="kpi-head">
          <span class="kpi-title">${s.label}</span>
          <span class="kpi-total">${s.total} 席</span>
        </div>
        <div class="kpi-nums">
          <span class="kpi-num rep"><b>${s.R}</b><span>红</span></span>
          <span class="kpi-num dem"><b>${s.D}</b><span>蓝</span></span>
          <span class="kpi-num oth"><b>${s.O}</b><span>其他</span></span>
          <span class="kpi-num pend"><b>${s.pending}</b><span>待数据</span></span>
        </div>
        ${bar}
        <div class="kpi-up">${upLine}</div>
        ${netHtml}
        ${ctrlHtml}
      </div>`;
  }).join('');
}

/* ---------------- render: house grid ---------------- */
function renderHouseGrid(seats) {
  const grid = $('#houseGrid');
  const s = seats.house;
  const cells = [];
  for (let i = 0; i < s.R; i++) cells.push(['rep', `共和党 ${i + 1}`]);
  for (let i = 0; i < s.D; i++) cells.push(['dem', `民主党 ${s.R + i + 1}`]);
  for (let i = 0; i < s.O; i++) cells.push(['oth', `其他 ${s.R + s.D + i + 1}`]);
  for (let i = 0; i < s.pending; i++) cells.push(['pend', '暂无官方数据']);
  grid.innerHTML = cells.map(([c, t]) => `<div class="house-seat ${c}" data-tip="${t}" title="${t}"></div>`).join('');
  $('#houseSub').textContent = `435 席中：红 ${s.R} · 蓝 ${s.D} · 其他 ${s.O} · 暂无数据 ${s.pending}（全部 435 席本轮改选）`;
  $('#houseNote').innerHTML = '众院 435 个投票席位全部由 50 州占据，各州席数 = 该州选举人票 − 2（依据 National Archives 官方选举人团表，50 州 EV 合计 535 − 100 参院席 = 435）。<b>哥伦比亚特区</b>另有 1 名<b>无表决权众议院代表</b>，不计入 435 席与过半门槛；特区 3 张选举人票依宪法第 23 修正案另行产生。';
  $('#houseLegend').innerHTML = legendHTML([
    ['rep', `共和党 ${s.R}`], ['dem', `民主党 ${s.D}`], ['oth', `其他 ${s.O}`], ['pend', `暂无数据 ${s.pending}`]
  ]);
}
function legendHTML(items) {
  const map = { rep: '#e0484b', dem: '#2f83f7', oth: '#8b97a8', pend: '#1b2430', R: '#e0484b', D: '#2f83f7', O: '#8b97a8' };
  return items.map(([k, label]) => `<span><i style="background:${map[k]}"></i>${label}</span>`).join('');
}

/* ---------------- render: charts ---------------- */
/** 票数图只统计联邦与州长竞选（参院 / 众院 / 州长），且排除党派未标注的候选人 */
function renderVoteChart(races) {
  const el = $('#voteChart');
  if (state.charts.length) { state.charts.forEach((c) => c.dispose()); state.charts = []; }
  if (!window.echarts) { el.innerHTML = '<div class="empty">图表库加载失败</div>'; return; }

  const SCOPE = new Set(['senate', 'house', 'governor']);
  const groups = [['参议院', 'senate'], ['众议院', 'house'], ['州长', 'governor'], ['合计', '*']];
  const rows = groups.map(([label, key]) => {
    let R = 0, D = 0, O = 0;
    for (const r of races) {
      if (!SCOPE.has(r.chamber)) continue;
      if (key !== '*' && r.chamber !== key) continue;
      for (const c of r.candidates || []) {
        const v = c.votes || 0;
        const p = (c.party || '').trim().toUpperCase();
        if (!p) continue;                       // 党派未标注不计入
        if (p.startsWith('R') || p.includes('REPUBLICAN')) R += v;
        else if (p.startsWith('D') || p.includes('DEMOCRATIC')) D += v;
        else O += v;
      }
    }
    return { label, R, D, O };
  });

  const chart = echarts.init(el, null, { renderer: 'canvas' });
  chart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 58, right: 26, top: 12, bottom: 26 },
    tooltip: {
      trigger: 'axis', axisPointer: { type: 'shadow' },
      backgroundColor: '#1b2432', borderColor: '#2c3a50', textStyle: { color: '#e7eef8', fontSize: 12 },
      valueFormatter: (v) => fmt(v) + ' 票'
    },
    legend: {
      data: ['红', '蓝', '其他'], top: 0, right: 0, itemWidth: 10, itemHeight: 10,
      textStyle: { color: '#a9b7cb', fontSize: 11.5 }
    },
    xAxis: {
      type: 'value',
      axisLabel: { color: '#71829a', fontSize: 11, formatter: (v) => v >= 1000000 ? (v / 1000000).toFixed(1) + 'M' : v >= 1000 ? (v / 1000).toFixed(0) + 'K' : v },
      splitLine: { lineStyle: { color: '#1b2432' } }
    },
    yAxis: { type: 'category', data: rows.map((r) => r.label).reverse(), axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: '#a9b7cb', fontSize: 12 } },
    series: [
      { name: '红', type: 'bar', stack: 'v', data: rows.map((r) => r.R).reverse(), itemStyle: { color: '#e0484b' }, barWidth: '52%' },
      { name: '蓝', type: 'bar', stack: 'v', data: rows.map((r) => r.D).reverse(), itemStyle: { color: '#2f83f7' }, barWidth: '52%' },
      { name: '其他', type: 'bar', stack: 'v', data: rows.map((r) => r.O).reverse(), itemStyle: { color: '#8b97a8' }, barWidth: '52%' }
    ]
  });
  state.charts.push(chart);

  const tot = rows[rows.length - 1];
  const sum = tot.R + tot.D + tot.O;
  $('#voteLegend').innerHTML = legendHTML([
    ['R', `红 ${fmt(tot.R)}（${sum ? (tot.R / sum * 100).toFixed(1) : 0}%）`],
    ['D', `蓝 ${fmt(tot.D)}（${sum ? (tot.D / sum * 100).toFixed(1) : 0}%）`],
    ['O', `其他 ${fmt(tot.O)}（${sum ? (tot.O / sum * 100).toFixed(1) : 0}%）`]
  ]);
  const nOk = state.states.filter((s) => s.status === 'ok').length;
  $('#coverageNote').textContent = `覆盖 ${nOk} / ${state.states.length} 个辖区官方结果系统，${state.races.length} 场竞选`;
}

/* ---------------- render: battle table ---------------- */
function battleRows() {
  const KEY = new Set(['senate', 'governor']);
  return state.races
    .filter((r) => KEY.has(r.chamber) || (r.chamber === 'house' && r.key_race))
    .map((r) => {
      const cands = [...(r.candidates || [])].sort((a, b) => (b.votes || 0) - (a.votes || 0));
      const rep = cands.find((c) => isParty(c, 'R'));
      const dem = cands.find((c) => isParty(c, 'D'));
      const m = marginOf(r);
      const total = totalVotesOf(r);
      const st = state.states.find((x) => x.id === r.state);
      const holder = st ? holderOf(st, r.chamber) : '';
      const lead = leadParty(r);
      return {
        state: r.state, state_zh: r.state_zh, chamber: r.chamber, office: r.office,
        rep, dem, margin: m, marginPct: total ? (m / total * 100) : null,
        reporting: r.reporting, source_url: r.source_url, source_id: r.source_id,
        flip: holder && lead && holder !== lead ? { from: holder, to: lead } : null,
        race: r
      };
    })
    .filter((x) => x.rep || x.dem)
    .sort((a, b) => (a.marginPct ?? 999) - (b.marginPct ?? 999));
}
const isParty = (c, p) => {
  const s = (c.party || '').toUpperCase();
  return p === 'R' ? (s.startsWith('R') || s.includes('REPUBLICAN')) : (s.startsWith('D') || s.includes('DEMOCRATIC'));
};

function renderBattle() {
  const rows = battleRows();
  const key = state.sortBattle.key, dir = state.sortBattle.dir === 'asc' ? 1 : -1;
  if (key) {
    rows.sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      return (typeof av === 'string' ? av.localeCompare(bv) : av - bv) * dir;
    });
  }
  const tbody = $('#battleBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8"><div class="empty">暂无参议院 / 州长官方计票数据</div></td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((x) => {
    const race = x.race;
    const margin = marginOf(race);
    const repVotes = x.rep ? x.rep.votes : 0;
    const demVotes = x.dem ? x.dem.votes : 0;
    const tot = repVotes + demVotes;
    const redPct = tot ? repVotes / tot * 100 : 50;
    const barW = Math.min(100, Math.abs(redPct - 50) * 2 * 10 + 2);
    const status = race.called ? '<span class="status-pill ok"><span class="dotm"></span>已确定</span>'
      : (race.reporting !== null && race.reporting !== undefined && race.reporting < 100)
        ? '<span class="status-pill pend"><span class="dotm"></span>计票中</span>'
        : '<span class="status-pill pend"><span class="dotm"></span>待认证</span>';
    const flipTag = x.flip
      ? `<span class="flip-mark" title="原持有人 ${PARTY[x.flip.from].label} → 当前领先 ${PARTY[x.flip.to].label}">翻转</span>`
      : '';
    return `<tr>
      <td><button class="state-link" data-state="${esc(x.state)}">${esc(x.state_zh)}</button></td>
      <td>${esc(x.office)} ${flipTag}</td>
      <td>${x.rep ? `${esc(x.rep.name)} <span class="muted">${fmt(x.rep.votes)}</span>` : '<span class="muted">—</span>'}</td>
      <td>${x.dem ? `${esc(x.dem.name)} <span class="muted">${fmt(x.dem.votes)}</span>` : '<span class="muted">—</span>'}</td>
      <td class="num">${fmt(margin)}</td>
      <td class="num">
        <span class="margin-track"><span class="margin-bar" style="width:${barW}%;background:${margin >= 0 ? '#e0484b' : '#2f83f7'}"></span></span>
        ${x.marginPct === null ? '—' : x.marginPct.toFixed(2) + '%'}
      </td>
      <td class="num">${race.reporting === null || race.reporting === undefined ? '<span class="muted">—</span>' : (race.reporting).toFixed(0) + '%'}</td>
      <td>${race.source_url ? `<a class="src-link" href="${esc(race.source_url)}" target="_blank" rel="noopener">官方 ↗</a>` : '<span class="muted">—</span>'}</td>
    </tr>`;
  }).join('');
}

/* ---------------- render: race table ---------------- */
function renderRaceTable() {
  const { q, party, chamber } = state.filters;
  const qy = q.trim().toLowerCase();
  let rows = state.races.filter((r) => {
    if (chamber && r.chamber !== chamber) return false;
    if (party) {
      const hit = (r.candidates || []).some((c) => isParty(c, party));
      if (!hit) return false;
    }
    if (qy) {
      const hay = `${r.state_zh} ${r.state} ${r.office} ${(r.candidates || []).map((c) => c.name + ' ' + (c.party || '')).join(' ')}`.toLowerCase();
      if (!hay.includes(qy)) return false;
    }
    return true;
  });

  const key = state.sortRace.key, dir = state.sortRace.dir === 'asc' ? 1 : -1;
  if (key) {
    rows.sort((a, b) => {
      let av, bv;
      if (key === 'votes' || key === 'pct') {
        av = Math.max(0, ...(a.candidates || []).map((c) => c.votes || 0));
        bv = Math.max(0, ...(b.candidates || []).map((c) => c.votes || 0));
      } else { av = a[key]; bv = b[key]; }
      if (av === null || av === undefined) return 1;
      if (bv === null || bv === undefined) return -1;
      return (typeof av === 'string' ? av.localeCompare(bv) : av - bv) * dir;
    });
  }

  const tbody = $('#raceBody');
  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7"><div class="empty">没有符合条件的记录</div></td></tr>';
    return;
  }
  tbody.innerHTML = rows.map((r) => {
    const cands = [...(r.candidates || [])].sort((a, b) => (b.votes || 0) - (a.votes || 0));
    const total = totalVotesOf(r) || 1;
    const lead = leadParty(r);
    const rowsHtml = cands.map((c, i) => {
      const p = (c.party || '').toUpperCase();
      const tag = p.startsWith('R') || p.includes('REPUBLICAN') ? 'R' : p.startsWith('D') || p.includes('DEMOCRATIC') ? 'D' : 'O';
      const win = c.winner ? '<span class="winner-mark">✔ 领先</span>' : '';
      return `<td>${esc(c.name)}${win}</td>
        <td><span class="tag tag-${tag}">${esc(c.party || '—')}</span></td>
        <td class="num">${fmt(c.votes)}</td>
        <td class="num">${c.pct !== null && c.pct !== undefined ? c.pct.toFixed(2) + '%' : ((c.votes / total * 100).toFixed(2) + '%')}</td>
        <td>${i === 0 ? `<span class="status-pill ${r.called ? 'ok' : 'pend'}"><span class="dotm"></span>${r.called ? '已确定' : '计票中/待认证'}</span>` : ''}</td>`;
    }).join('');
    return `<tr data-state="${esc(r.state)}">
      <td rowspan="${cands.length}"><button class="state-link" data-state="${esc(r.state)}">${esc(r.state_zh)}</button></td>
      <td rowspan="${cands.length}">${esc(r.office)}</td>
      ${rowsHtml}
    </tr>`;
  }).join('');
}

/* ---------------- render: sources ---------------- */
function renderSources() {
  const tbody = $('#sourceBody');
  const order = { ok: 0, pending: 1, blocked: 2, error: 3 };
  const sts = [...state.states].sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9) || a.zh.localeCompare(b.zh, 'zh'));
  tbody.innerHTML = sts.map((s) => {
    const map = {
      ok: '<span class="status-pill ok src-status"><span class="dotm"></span>已接入</span>',
      pending: '<span class="status-pill pend src-status"><span class="dotm"></span>未接入</span>',
      blocked: '<span class="status-pill err src-status"><span class="dotm"></span>访问受限</span>',
      error: '<span class="status-pill err src-status"><span class="dotm"></span>抓取失败</span>'
    };
    return `<tr>
      <td><b>${esc(s.zh)}</b> <span class="muted">${esc(s.id)}</span></td>
      <td class="muted">${esc(s.agency || '—')}</td>
      <td>${s.url ? `<a class="src-link" href="${esc(s.url)}" target="_blank" rel="noopener">官方结果页 ↗</a>` : '<span class="muted">—</span>'}</td>
      <td>${map[s.status] || '<span class="muted">未知</span>'}</td>
      <td class="num muted">${s.updated_at ? fmtTime(s.updated_at) : '—'}</td>
    </tr>`;
  }).join('');
}

/* ---------------- data load ---------------- */

/**
 * 服务端能力探测。
 * 站点部署成 Python 服务（server.py）时，/api/refresh 能在服务端真的重新抓一遍各州官方结果；
 * 部署成纯静态时没有这个接口，刷新按钮只能重读同一份快照 —— 两种模式必须在界面上说清楚。
 */
async function detectServer() {
  try {
    const res = await fetch(SERVER_STATUS_URL, { cache: 'no-store' });
    if (!res.ok) return null;
    const st = await res.json();
    state.serverMode = true;
    state.serverStatus = st;
    return st;
  } catch (e) {
    state.serverMode = false;
    return null;
  }
}

/** 触发服务端重新采集，并轮询到完成；静态部署下直接返回 false */
async function triggerServerRefresh() {
  if (!state.serverMode) return false;
  setRefreshBtn('采集中');
  try {
    await fetch(SERVER_REFRESH_URL, { cache: 'no-store' });
  } catch (e) {
    setRefreshBtn('');
    return false;
  }
  // 采集一轮全量通常要几分钟，轮询服务端状态直到这一轮结束
  const deadline = Date.now() + 25 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 5000));
    let st = null;
    try {
      const res = await fetch(SERVER_STATUS_URL, { cache: 'no-store' });
      if (res.ok) st = await res.json();
    } catch (e) { /* 忽略单次失败，继续轮询 */ }
    if (!st) continue;
    state.serverStatus = st;
    if (!st.busy && st.runs > 0) {
      setRefreshBtn('');
      return true;
    }
  }
  setRefreshBtn('');
  return false;
}

function setRefreshBtn(mode) {
  const btn = $('#refreshBtn');
  if (!btn) return;
  btn.classList.toggle('loading', mode === '采集中');
  const span = btn.querySelector('span');
  if (span) span.textContent = mode === '采集中' ? '采集中' : '刷新';
}

async function loadData(manual) {
  const btn = $('#refreshBtn');
  if (!state.serverMode && !state.serverChecked) {
    state.serverChecked = true;
    await detectServer();
  }

  // 服务端模式：手动刷新时，先让服务端真的去重抓一轮官方结果，再拉最新快照。
  if (manual && state.serverMode) {
    const ok = await triggerServerRefresh();
    if (!ok) {
      const st = state.serverStatus || {};
      if (st.last_error) {
        $('#statusNote').innerHTML =
          `本轮服务端采集失败，已回退到上一份完好快照：<span style="color:var(--rep)">${esc(String(st.last_error).slice(0, 160))}</span>`;
      }
    }
  }

  btn.classList.add('loading');
  try {
    const best = await loadLatest(manual);
    state.dataSource = best.src;
    applyData(best.d);
  } catch (err) {
    const badge = $('#statusBadge');
    badge.className = 'badge badge-err';
    badge.textContent = '数据加载失败';
    $('#statusNote').textContent = `无法读取 ${DATA_URL}：${err.message}（请先运行 scripts/collect.py 生成数据）`;
  } finally {
    btn.classList.remove('loading');
  }
}

/** 拉一份 JSON；bust=true 时加时间戳绕过 CDN / 代理缓存 */
async function fetchJson(url, bust) {
  const u = bust ? `${url}${url.includes('?') ? '&' : '?'}t=${Date.now()}` : url;
  const res = await fetch(u, { cache: bust ? 'no-store' : 'default' });
  if (!res.ok) throw new Error('HTTP ' + res.status);
  return res.json();
}

const genTs = (d) => Date.parse((d && d.meta && d.meta.generated_at) || '') || 0;

/**
 * 远程（GitHub Actions 采集产物）与本地快照各拉一份，取 generated_at 较新的一份。
 * 远程是静态部署下唯一能持续更新的来源；本地是离线兜底。
 */
async function loadLatest(manual) {
  const jobs = [];
  if (REMOTE_DATA_URL) {
    jobs.push(
      fetchJson(REMOTE_DATA_URL, true)
        .then((d) => ({ src: 'remote', d }))
        .catch((e) => ({ src: 'remote', err: e }))
    );
  }
  jobs.push(
    fetchJson(DATA_URL, manual)
      .then((d) => ({ src: 'local', d }))
      .catch((e) => ({ src: 'local', err: e }))
  );
  const res = await Promise.all(jobs);
  state.remoteFailed = res.some((r) => r.src === 'remote' && r.err);
  const ok = res.filter((r) => r.d);
  if (!ok.length) {
    const firstErr = res.find((r) => r.err);
    throw (firstErr && firstErr.err) || new Error('无可用数据');
  }
  ok.sort((a, b) => genTs(b.d) - genTs(a.d));
  return ok[0];
}

/** 把一份快照灌进页面 */
function applyData(data) {
  state.raw = data;
  state.races = data.races || [];
  state.states = data.states || [];
  state.baseline = data.meta?.baseline || {};
  state.seats = computeSeats(state.states, state.races, state.baseline);
  $('#updatedAt').textContent = fmtTime(data.meta?.generated_at);
  $('#updatedAt').title = data.meta?.generated_at || '';
  renderAll();
  const nOk = state.states.filter((s) => s.status === 'ok').length;
  const badge = $('#statusBadge');
  badge.className = 'badge badge-live';
  badge.textContent = '官方数据已加载';
  $('#statusNote').innerHTML = statusNoteHtml(nOk, data);
}

/** 状态条文案：区分「服务端实时」「CI 远程快照」「本地静态快照」三种部署形态 */
function statusNoteHtml(nOk, data) {
  const base = `已接入 ${nOk}/${state.states.length} 个辖区官方结果系统 · ${state.races.length} 场竞选 · ${relTime(data.meta?.generated_at)}更新 · 认证前均为非官方结果`;
  const src = state.dataSource === 'remote'
    ? '当前展示 <b>CI 远程快照</b>'
    : '当前展示 <b>同源本地快照</b>';
  if (state.serverMode) {
    const st = state.serverStatus || {};
    const every = Math.round((st.interval_sec || 1800) / 60);
    return `${base} · 服务端每 <b>${every}</b> 分钟自动重抓一次，点「刷新」可立即触发 · ${src}`;
  }
  if (state.remoteFailed) {
    return `${base} · <b>远程快照暂时取不到</b>，${src}（可能是网络原因）；可用 <code>python3 scripts/collect.py</code> 本地重抓后重新部署`;
  }
  return `${base} · ${src} · GitHub Actions 每 <b>30</b> 分钟在美国 IP 重抓一轮，点「刷新」立即取最新`;
}

function escapeHTML(s) {
  return esc(s);
}

function renderAll() {
  renderKPIs(state.seats);
  renderHouseGrid(state.seats);
  renderVoteChart(state.races);
  renderBattle();
  renderRaceTable();
  renderSources();
}

/* ---------------- events ---------------- */
function bindSort(tableSel, sortObj, rerender) {
  const table = $(tableSel);
  if (!table) return;
  table.querySelectorAll('th[data-sort]').forEach((th) => {
    th.addEventListener('click', () => {
      const k = th.dataset.sort;
      if (sortObj.key === k) sortObj.dir = sortObj.dir === 'asc' ? 'desc' : 'asc';
      else { sortObj.key = k; sortObj.dir = 'asc'; }
      table.querySelectorAll('th[data-sort]').forEach((t) => t.classList.remove('sorted-asc', 'sorted-desc'));
      th.classList.add(sortObj.dir === 'asc' ? 'sorted-asc' : 'sorted-desc');
      rerender();
    });
  });
}

function bind() {
  $('#refreshBtn').addEventListener('click', () => loadData(true));
  $('#stateSearch').addEventListener('input', (e) => { state.filters.q = e.target.value; renderRaceTable(); });
  $('#partyFilter').addEventListener('change', (e) => { state.filters.party = e.target.value; renderRaceTable(); });
  $('#chamberFilter').addEventListener('change', (e) => { state.filters.chamber = e.target.value; renderRaceTable(); });
  bindSort('#battleTable', state.sortBattle, renderBattle);
  bindSort('#raceTable', state.sortRace, renderRaceTable);
  document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-state]');
    if (!b) return;
    const id = b.dataset.state;
    const st = state.states.find((s) => s.id === id);
    if (st && st.url) window.open(st.url, '_blank', 'noopener');
  });
  window.addEventListener('resize', () => state.charts.forEach((c) => c.resize()));
}

bind();
loadData(false);
setInterval(() => loadData(false), AUTO_REFRESH_MS);

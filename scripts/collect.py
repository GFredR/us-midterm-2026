#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect.py — 2026 美国中期选举官方计票采集器

从各州州务卿 / 选举委员会的官方结果系统抓取 2026-11-03 大选结果，
归一化为统一 JSON：data/election-2026.json

设计原则
  1. 只用 Python 标准库；抓取走系统 curl（自动继承代理与环境），必要时回落到 urllib。
  2. 只认官方域名结果页；解析失败宁可标记 pending / blocked，绝不臆造数字。
  3. 每个州都记录官方机构、原始 URL、抓取时间与状态，前端可逐条溯源。
  4. 支持三种抓取通道：
       mode=direct   → curl 直连（官方 JSON API / 静态表格页）
       mode=browser  → 本机 Chrome 无头渲染（Cloudflare JS 挑战、SPA、iframe）
       adapter=enr_json → 官方 ENR/SOE 选举夜报告系统的州级汇总 JSON

用法
  python3 scripts/collect.py                 # 全量刷新
  python3 scripts/collect.py --only WA,CO    # 只刷指定州
  python3 scripts/collect.py --no-browser    # 禁用浏览器通道（只用直连）
  python3 scripts/collect.py --list          # 打印内置州表与官方链接
"""

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
RAW_DIR = os.path.join(DATA_DIR, "raw")
CONFIG_PATH = os.path.join(HERE, "state_sources.json")
OUT_PATH = os.path.join(DATA_DIR, "election-2026.json")
BROWSER_MJS = os.path.join(HERE, "browser.mjs")

CDP_PORT = 9333

CHROME_CANDIDATES = [
    os.environ.get("CHROME_BIN", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/snap/bin/chromium",
]


def find_chrome():
    """找一个可用的 Chrome。macOS / Linux 容器 / CI 通用。"""
    for p in CHROME_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return None


def proxy_from_env():
    """
    从环境变量取本机代理，交给 Chrome 用。
    Chrome 不认 http_proxy 这类环境变量，只认系统代理设置；
    而本机（尤其是受限网络环境）只把代理写在环境变量里，
    不显式告诉 Chrome 的话，Chrome 会直连 → 很多州官方站点打不开。
    """
    for k in ("ALL_PROXY", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        v = os.environ.get(k)
        if not v:
            continue
        v = v.strip()
        if "://" in v:
            v = v.split("://", 1)[1]
        v = v.split("/", 1)[0]
        if v:
            return v
    return None


# 客户端指纹头：macOS 本机与 Linux CI 上跑时平台必须自洽，
# 否则 sec-ch-ua-platform 与 UA 对不上，部分州官方站的 WAF 会起疑。
IS_MAC = sys.platform == "darwin"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36") if IS_MAC else (
      "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
UA_PLATFORM = "macOS" if IS_MAC else "Linux"
UA_PLATFORM_VERSION = "10_15_7" if IS_MAC else "6_5_0"


# ==========================================================================
# 抓取层
# ==========================================================================
def curl_get(url, timeout=35):
    """curl 直连。返回 (status, text)；status 为 int 或 'error'。"""
    if not shutil.which("curl"):
        return urllib_get(url, timeout)
    cmd = [
        "curl", "-sSL", "--compressed", "--max-time", str(timeout),
        "-A", UA,
        "-H", "Accept: text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "sec-ch-ua: \"Chromium\";v=\"140\", \"Not=A?Brand\";v=\"24\"",
        "-H", "sec-ch-ua-mobile: ?0", "-H", f"sec-ch-ua-platform: \"{UA_PLATFORM}\"",
        "-H", "Sec-Fetch-Dest: document", "-H", "Sec-Fetch-Mode: navigate",
        "-H", "Sec-Fetch-Site: none", "-H", "Sec-Fetch-User: ?1",
        "-w", "\n__HTTP_STATUS__:%{http_code}",
        url,
    ]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 12)
    except subprocess.TimeoutExpired:
        return "error", "curl timeout"
    out = p.stdout.decode("utf-8", errors="replace")
    m = re.search(r"__HTTP_STATUS__:(\d{3})\s*$", out)
    if not m:
        return "error", (p.stderr.decode("utf-8", errors="replace")[:160] or "no status")
    body = out[:m.start()]
    return int(m.group(1)), body


def urllib_get(url, timeout=35):
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return "error", f"{type(e).__name__}: {str(e)[:120]}"


class Chrome:
    """按需拉起带调试端口的 Chrome。"""

    def __init__(self):
        self.proc = None
        self.profile = None

    @staticmethod
    def _port_open():
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=3) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def ensure(self):
        if self._port_open():
            return True
        chrome = find_chrome()
        if not chrome:
            return False
        self.profile = tempfile.mkdtemp(prefix="elscrape-chrome-")
        base = [chrome, f"--remote-debugging-port={CDP_PORT}", f"--user-data-dir={self.profile}",
                "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
                "--ignore-certificate-errors", "about:blank"]
        proxy = proxy_from_env()
        if proxy:
            base[1:1] = [f"--proxy-server={proxy}"]
        # CI（GitHub Actions 等）里没有 X server，Chrome 不加 --headless 会直接退出，
        # 所以按运行环境决定用无头还是带界面。
        if os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("CI") == "true":
            base[1:1] = ["--headless=new", "--disable-gpu"]
        # 两段式启动：先按常规参数起；某些受限环境（容器 / CI / 沙箱）里 Chrome 自带沙箱会直接
        # 崩掉（"sandbox initialization failed"），这时退回 --no-sandbox --disable-gpu 再试一次。
        variants = [base]
        if os.environ.get("CHROME_NO_SANDBOX", "") not in ("0", "false", "no"):
            relaxed = list(base)
            relaxed[1:1] = ["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"]
            variants.append(relaxed)
        for cmd in variants:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(40):
                if self._port_open():
                    return True
                time.sleep(0.5)
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass
            self.proc = None
        return False

    def close(self):
        if self.proc:
            try:
                self.proc.send_signal(signal.SIGTERM)
                self.proc.wait(timeout=8)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if self.profile:
            shutil.rmtree(self.profile, ignore_errors=True)


def browser_fetch(mode, url, out_path, wait_ms=9000):
    """通过 Chrome CDP 抓取。mode: html / enr。返回 (status, path_or_text)。"""
    if not os.path.exists(BROWSER_MJS):
        return "error", "缺少 browser.mjs"
    cmd = ["node", BROWSER_MJS, mode, url]
    cmd += [str(wait_ms), out_path] if mode == "html" else [out_path]
    env = dict(os.environ, CDP_PORT=str(CDP_PORT))
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=90, env=env)
    except subprocess.TimeoutExpired:
        return "error", "browser timeout"
    if p.returncode != 0:
        return "error", p.stderr.decode("utf-8", errors="replace")[:300]
    return 200, out_path


# ==========================================================================
# 文本工具
# ==========================================================================
def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


NUM_RE = re.compile(r"^[\s$]*([0-9][0-9,]*)\s*$")
PCT_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*%?$")


def to_int(s):
    m = NUM_RE.match((s or "").strip())
    return int(m.group(1).replace(",", "")) if m else None


def to_pct(s):
    m = PCT_RE.match((s or "").strip())
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


PARTY_EXACT = {
    "REP": "R", "R": "R", "REPUBLICAN": "R", "GOP": "R",
    "DEM": "D", "D": "D", "DEMOCRATIC": "D",
    "IND": "O", "LIB": "O", "LBR": "O", "GRN": "O", "GR": "O", "UNI": "O",
    "ACN": "O", "FWD": "O", "APV": "O", "NON": "O", "CON": "O", "OTH": "O",
    "PF": "O", "REP ": "R", "W": "O", "WRITE-IN": "O", "WRITE IN": "O",
}

PARTY_MAP = [
    (r"^(rep|republican|r|gop)$|republican|共和党|共和", "R"),
    (r"^(dem|democratic|d)$|democratic|民主党|民主", "D"),
    (r"libertarian|independen|green|nonpartisan|no labels|other|write|自由意志|绿党|独立|无党|其他|write-?in", "O"),
]


def party_code(text):
    t = (text or "").strip()
    if not t:
        return ""
    up = t.upper()
    if up in PARTY_EXACT:
        return PARTY_EXACT[up]
    tl = t.lower()
    for pat, code in PARTY_MAP:
        if re.search(pat, tl):
            return code
    return ""


PARENS_PARTY = re.compile(r"\(([A-Za-z]{1,4})\)")


def party_from_name(name):
    """从 '张三 (R)' 这类写法里提取党派。"""
    m = PARENS_PARTY.search(name or "")
    if m:
        c = party_code(m.group(1))
        if c:
            return c
    return ""


OFFICE_RULES = [
    ("governor", r"governor|州长"),
    ("senate", r"united states senator|united states senate|u\.?\s*s\.?\s*senator|u\.?\s*s\.?\s*senate\b"
               r"|参议员|联邦参议员|参议院"),
    ("house", r"united states representative|u\.?\s*s\.?\s*representative|representative in congress"
             r"|representative to the|congressional district|member,\s*house of representatives"
             r"|house of representatives\s*\(\s*\d|众议员|联邦众议员|国会议员|选区"),
    ("other", r"state senator|state representative|state senate|state house|legislative|judge|justice"
             r"|attorney general|secretary of state|treasurer|auditor|comptroller|lieutenant governor"
             r"|ballot measure|initiative|referendum|commissioner|superintendent|board of"),
]

# 州级 / 地方职位特征：命中则一律归入 other，避免被 federal 关键词误伤
STATE_LEVEL_GUARD = re.compile(
    r"state board of education|board of education|regent of the university|university of|board of regents"
    r"|state senator|state representative|state senate|state house|house of delegates|state assembly"
    r"|county|municipal|township|parish|borough|judicial|judge|justice|magistrate|district attorney"
    r"|sheriff|prosecut|public service commission|public utilities|education service|"
    r"school board|water district|park district|fire district|soil and water|reclamation district"
    r"|natural resources district|learning community|metropolitan utilities|power district",
    re.I)


def dedupe_races(races):
    """
    按「层级 + 职位标题 + 候选人名单及票数」去重。

    为什么要去重：browser.mjs 会把主框架和 iframe 里的内容都拼进存档文件，
    而有些州的官方结果表在 iframe 里又完整出现了一遍（DE 就是），
    直接解析会把每个竞选算两次 —— 席位多算、票数翻倍。
    """
    seen = set()
    out = []
    for r in races:
        cands = r.get("candidates") or []
        key = (
            r.get("chamber", ""),
            clean(r.get("office") or "").lower(),
            tuple(sorted((c.get("name", ""), c.get("votes")) for c in cands)),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def classify_office(title):
    t = clean(title)
    tl = t.lower()
    # 副州长 / 州务卿等不是州长本席，先排除
    if re.search(r"lieutenant governor|副州长", tl):
        return "other", t
    # 州级 / 地方职位优先归 other
    federal = re.search(r"united states|u\.?\s*s\.?\s*(?:senator|senate|representative|congress)", tl)
    if STATE_LEVEL_GUARD.search(tl) and not federal:
        return "other", t
    for chamber, pat in OFFICE_RULES:
        if re.search(pat, tl):
            return chamber, t
    return "other", t


# ==========================================================================
# 各州解析器
# ==========================================================================
TX_POLL_RE = re.compile(r"Polling Locations Reporting:\s*([0-9.]+)\s*%", re.I)
TX_CELL = {
    "party": re.compile(r'<td[^>]*class="party"[^>]*>(.*?)</td>', re.S | re.I),
    "name": re.compile(r'<td[^>]*class="candidate"[^>]*>(.*?)</td>', re.S | re.I),
    "votes": re.compile(r'<td[^>]*class="ballot"[^>]*>(.*?)</td>', re.S | re.I),
}
TX_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)


def _strip_tags(s):
    return clean(re.sub(r"<[^>]+>", " ", s or "").replace("&nbsp;", " ").replace("&amp;", "&"))


def parse_tx_html(html, cfg):
    """德克萨斯州官方结果页（electionresults.sos.state.tx.us）。

    结构：<div class="election">职位名</div><div class="polling">...</div>
    <table class="election"><tr><td class="party">..</td><td class="candidate">..</td>
    <td class="ballot">..</td></tr></table>
    """
    out = []
    chunks = re.split(r'<div\s+class="election"\s*>', html, flags=re.I)
    for ch in chunks[1:]:
        m = re.match(r"\s*(.*?)</div>", ch, re.S)
        if not m:
            continue
        office = _strip_tags(m.group(1))
        if not office:
            continue
        reporting = None
        pm = TX_POLL_RE.search(ch)
        if pm:
            try:
                reporting = min(100.0, float(pm.group(1)))   # 统一为百分数
            except ValueError:
                reporting = None
        cands = []
        for rm in TX_ROW_RE.finditer(ch):
            row = rm.group(1)
            if 'class="header"' in row.lower():
                continue
            pm2 = TX_CELL["party"].search(row)
            nm = TX_CELL["name"].search(row)
            vt = TX_CELL["votes"].search(row)
            if not nm or not vt:
                continue
            name = _strip_tags(nm.group(1))
            votes = to_int(_strip_tags(vt.group(1)))
            if not name or votes is None:
                continue
            raw_party = _strip_tags(pm2.group(1)) if pm2 else ""
            cands.append({"name": name,
                          "party": party_code(raw_party) or party_from_name(name),
                          "votes": votes, "pct": None, "winner": False})
        if len(cands) < 2:
            continue
        chamber, office_title = classify_office(office)
        total = sum(c["votes"] for c in cands)
        for c in cands:
            if total:
                c["pct"] = round(c["votes"] / total * 100, 2)
        out.append({"id": f"{cfg['id']}-TX{len(out)}", "chamber": chamber, "office": office_title,
                    "candidates": cands, "total_votes": total, "reporting": reporting, "called": False})
    return out


def i18n_text(v):
    """把 EnhancedVoting 的多语言结构 [{languageId,text}] 取成字符串。"""
    if v is None:
        return ""
    if isinstance(v, str):
        return clean(v)
    if isinstance(v, dict):
        return clean(v.get("text") or "")
    if isinstance(v, list):
        en = next((x for x in v if isinstance(x, dict) and x.get("languageId") == "en"), None)
        if en:
            return clean(en.get("text") or "")
        first = next((x for x in v if isinstance(x, dict) and x.get("text")), None)
        if first:
            return clean(first.get("text") or "")
        return " ".join(clean(str(x)) for x in v if isinstance(x, str))
    return clean(str(v))


def parse_wa_api(doc, cfg):
    """EnhancedVoting 州级官方 JSON（WA / VA 等：results/public/api/.../data）。"""
    out = []
    for item in doc.get("ballotItems") or []:
        name = i18n_text(item.get("name"))
        if not name:
            continue
        ctype = (item.get("contestType") or "").lower()
        chamber, office = classify_office(name)
        if ctype in ("statemeasure", "ballotmeasure"):
            chamber, office = "other", name
        opts = ((item.get("summaryResults") or {}).get("ballotOptions")) or []
        cands = []
        for o in opts:
            p = o.get("party") or {}
            if not isinstance(p, dict):
                p = {}
            raw_party = (p.get("standardName") or p.get("name") or p.get("abbreviation") or "")
            raw_party = i18n_text(raw_party)
            oname = o.get("name")
            oname = i18n_text(oname)
            cands.append({
                "name": clean(oname),
                "party": party_code(raw_party) or party_from_name(oname),
                "votes": o.get("voteCount"),
                "pct": None,
                "winner": bool(o.get("isWinner")),
            })
        cands = [c for c in cands if c["name"]]
        if not cands:
            continue
        total = sum(c["votes"] or 0 for c in cands) or item.get("voteTotal") or 0
        for c in cands:
            c["pct"] = round((c["votes"] or 0) / total * 100, 2) if total else None
        rs = item.get("reportingStatus") or {}
        rep = None
        if rs.get("totalUnits"):
            rep = round((rs.get("reportingUnits", 0) / rs["totalUnits"]) * 100, 4)
        out.append({
            "id": item.get("id"), "chamber": chamber, "office": office,
            "candidates": cands, "total_votes": total, "reporting": rep,
            "called": any(c["winner"] for c in cands),
        })
    return out


def parse_enr(doc, cfg):
    """SOE Software ENR / Clarity 州级汇总（sum.json）。"""
    contests = doc.get("Contests") if isinstance(doc, dict) else doc
    if not isinstance(contests, list):
        return []
    out = []
    for c in contests:
        name = clean(c.get("C") or "")
        if not name:
            continue
        ch = c.get("CH") or []
        ps = c.get("P") or []
        vs = c.get("V") or []
        pcts = c.get("PCT") or []
        if not ch or not vs:
            continue
        cands = []
        for i, nm in enumerate(ch):
            v = vs[i] if i < len(vs) else None
            if v is None:
                continue
            party = ps[i] if i < len(ps) else ""
            pc = party_code(party) or party_from_name(nm)
            cands.append({
                "name": clean(nm),
                "party": pc,
                "votes": int(v) if isinstance(v, (int, float)) else None,
                "pct": round(pcts[i], 2) if i < len(pcts) and isinstance(pcts[i], (int, float)) else None,
                "winner": bool((c.get("W") or [])[i]) if c.get("W") and i < len(c["W"]) else False,
            })
        cands = [x for x in cands if x["name"] and x["votes"] is not None]
        if len(cands) < 2:
            continue
        chamber, office = classify_office(name)
        total = c.get("TV") or sum(x["votes"] for x in cands)
        tp, pr = c.get("TP"), c.get("PR")
        rep = round(pr / tp * 100, 2) if (tp and pr is not None) else None
        out.append({
            "id": f"{cfg['id']}-{c.get('K')}", "chamber": chamber, "office": office,
            "candidates": cands, "total_votes": total, "reporting": rep,
            "called": any(x["winner"] for x in cands),
        })
    return out


class TableExtractor(HTMLParser):
    """把 HTML 拆成 (caption, rows) 列表。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables = []
        self._stack = []
        self._rows = []
        self._cells = []
        self._cell = None
        self._caption = None
        self._in_caption = False

    def handle_starttag(self, tag, attrs):
        if tag == "caption":
            self._in_caption = True
            self._cap = []
        if tag == "table":
            self._stack.append((self._caption, self._rows))
            self._rows, self._caption = [], None
        elif tag == "tr":
            self._cells = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "caption":
            self._in_caption = False
            self._caption = clean(" ".join(self._cap))
        elif tag == "table":
            cap, rows = self._caption, self._rows
            if self._stack:
                self._caption, self._rows = self._stack.pop()
            else:
                self._caption, self._rows = None, []
            if rows:
                self.tables.append((cap, rows))
        elif tag == "tr":
            if self._cells:
                self._rows.append(self._cells)
            self._cells = []
        elif tag in ("td", "th"):
            if self._cell is not None:
                self._cells.append(clean(" ".join(self._cell)))
            self._cell = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        if self._in_caption:
            self._cap.append(data)


SKIP_CELL = re.compile(r"precinct|registered|voter|turnout|date|county|election date|reporting", re.I)


def parse_generic_html(html, cfg):
    """通用 HTML 表格抽取：按表头识别姓名列/党派列/票数列/百分比列。"""
    p = TableExtractor()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for cap, rows in p.tables:
        if not rows:
            continue
        head = rows[0]
        body = rows[1:] if len(rows) > 1 else []
        headtxt = " ".join(head).lower()
        if not re.search(r"vote|total|candidate|percent|%|票", headtxt):
            continue
        vote_idx = pct_idx = party_idx = name_idx = None
        for i, h in enumerate(head):
            hl = h.lower()
            if vote_idx is None and re.search(r"^votes?$|votes cast|total votes|^票", hl):
                vote_idx = i
            if pct_idx is None and re.search(r"percent|%|得票率", hl):
                pct_idx = i
            if party_idx is None and re.search(r"party|affiliation|党派", hl):
                party_idx = i
            if name_idx is None and re.search(r"candidate|contest|office|name|候选人|职位", hl):
                name_idx = i
        if vote_idx is None:
            for r in body:
                nums = [i for i, c in enumerate(r) if to_int(c) is not None]
                if nums:
                    vote_idx = nums[-1]
                    break
        if vote_idx is None:
            continue
        if name_idx is None:
            name_idx = next((i for i in range(min(4, len(head))) if i != vote_idx), None)
        if name_idx is None or name_idx == vote_idx:
            continue

        cands = []
        for r in body:
            if len(r) <= max(name_idx, vote_idx):
                continue
            nm = clean(r[name_idx])
            if not nm or SKIP_CELL.search(nm) or to_int(nm) is not None:
                continue
            v = to_int(r[vote_idx])
            if v is None:
                continue
            party = ""
            if party_idx is not None and party_idx < len(r):
                party = r[party_idx]
            cands.append({"name": nm, "party": party_code(party) or party_from_name(nm) or party_code(nm),
                          "votes": v,
                          "pct": to_pct(r[pct_idx]) if (pct_idx is not None and pct_idx < len(r)) else None,
                          "winner": False})
        if len(cands) < 2:
            continue
        title = cap or (clean(rows[0][0]) if rows[0] and to_int(rows[0][0]) is None else "")
        if not title:
            continue
        chamber, office = classify_office(title)
        total = sum(c["votes"] for c in cands)
        for c in cands:
            if c["pct"] is None and total:
                c["pct"] = round(c["votes"] / total * 100, 2)
        out.append({"id": f"{cfg['id']}-T{len(out)}", "chamber": chamber, "office": office,
                    "candidates": cands, "total_votes": total, "reporting": None, "called": False})
    return out


# ==========================================================================
# 主流程
# ==========================================================================
def collect_state(cfg, chrome):
    url = cfg.get("url")
    mode = cfg.get("mode", "direct")
    rec = {
        "id": cfg["id"], "zh": cfg["zh"], "en": cfg["en"],
        "ev": cfg.get("ev"), "house_seats": cfg.get("house_seats", 0),
        "senate_seats_up": cfg.get("senate_seats_up", 0), "gov_up": bool(cfg.get("gov_up")),
        "agency": cfg.get("agency", ""), "url": url,
        "status": "pending", "updated_at": None, "races": 0, "note": ""
    }
    if not url or cfg.get("disabled"):
        rec["status"] = "blocked"
        rec["note"] = cfg.get("note", "未配置")
        return rec, []

    os.makedirs(RAW_DIR, exist_ok=True)
    adapter_name = cfg.get("adapter", "generic_html")
    body = None

    if adapter_name == "enr_json":
        tmp = os.path.join(RAW_DIR, f"{cfg['id']}-enr.json")
        st, info = browser_fetch("enr", url, tmp)
        if st != 200:
            rec["status"] = "blocked"
            rec["note"] = "ENR 通道不可用：" + str(info)[:140]
            return rec, []
        try:
            body = open(tmp, encoding="utf-8").read()
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["note"] = f"read error: {e}"
            return rec, []
    elif mode == "browser":
        tmp = os.path.join(RAW_DIR, f"{cfg['id']}.html")
        st, info = browser_fetch("html", url, tmp, cfg.get("wait", 9000))
        if st != 200:
            rec["status"] = "blocked"
            rec["note"] = "浏览器通道不可用：" + str(info)[:140]
            return rec, []
        try:
            body = open(tmp, encoding="utf-8").read()
        except Exception as e:  # noqa: BLE001
            rec["status"] = "error"
            rec["note"] = f"read error: {e}"
            return rec, []
    else:
        st, body = curl_get(url, cfg.get("timeout", 35))
        if st == "error":
            rec["status"] = "error"
            rec["note"] = str(body)[:160]
            return rec, []
        if st != 200:
            rec["status"] = "blocked"
            rec["note"] = f"HTTP {st}"
            return rec, []

    races = []
    try:
        if adapter_name in ("wa_api_json", "enr_json"):
            races = {"wa_api_json": parse_wa_api, "enr_json": parse_enr}[adapter_name](json.loads(body), cfg)
        elif adapter_name == "tx_html":
            races = parse_tx_html(body, cfg)
        else:
            races = parse_generic_html(body, cfg)
    except Exception as e:  # noqa: BLE001
        rec["status"] = "error"
        rec["note"] = f"parse error: {type(e).__name__}: {str(e)[:140]}"
        return rec, []

    races = dedupe_races(races)

    if not races:
        rec["status"] = "pending"
        rec["note"] = "页面可访问但未解析出票数（结果未公布或结构需适配）"
        # 解析失败也要把原始页面存下来：这是后续写适配器唯一的依据
        try:
            with open(os.path.join(RAW_DIR, f"{cfg['id']}.html"), "w", encoding="utf-8") as f:
                f.write(body[:3_000_000])
        except OSError:
            pass
        return rec, []

    rec["status"] = "ok"
    rec["updated_at"] = datetime.now(timezone.utc).isoformat()
    rec["races"] = len(races)
    rec["note"] = f"{len(races)} 场竞选"
    return rec, races


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔的州缩写，如 WA,CO")
    ap.add_argument("--no-browser", action="store_true", help="禁用浏览器通道")
    ap.add_argument("--list", action="store_true", help="打印内置州表")
    args = ap.parse_args()

    with open(CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)
    states = config["states"]

    if args.list:
        for s in states:
            print(f"{s['id']:<4} {s['zh']:<9} ev={s.get('ev'):<3} house={s.get('house_seats',0):<2} "
                  f"sen={s.get('senate_seats_up',0):<2} gov={str(s.get('gov_up')):<5} "
                  f"{s.get('adapter','generic_html'):<12} {s.get('mode','direct'):<8} {s.get('url','')}")
        return

    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    if only:
        states = [s for s in states if s["id"].upper() in only]

    chrome = Chrome()
    need_browser = any(s.get("mode") == "browser" or s.get("adapter") == "enr_json" for s in states)
    browser_ok = False
    if need_browser and not args.no_browser:
        print("启动 Chrome 渲染通道…")
        browser_ok = chrome.ensure()
        print("  Chrome:", "就绪" if browser_ok else "启动失败（相关州将标记为受限）")

    print(f"\n开始采集 {len(states)} 个辖区（{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}）\n")
    out_states, all_races = [], []
    for cfg in states:
        if (cfg.get("mode") == "browser" or cfg.get("adapter") == "enr_json") and not browser_ok:
            rec = {"id": cfg["id"], "zh": cfg["zh"], "en": cfg["en"], "ev": cfg.get("ev"),
                   "house_seats": cfg.get("house_seats", 0), "senate_seats_up": cfg.get("senate_seats_up", 0),
                   "gov_up": bool(cfg.get("gov_up")), "agency": cfg.get("agency", ""), "url": cfg.get("url"),
                   "status": "blocked", "updated_at": None, "races": 0, "note": "浏览器通道不可用"}
            out_states.append(rec)
            print(f"  {cfg['id']:<4} {cfg['zh']:<9} blocked   0 场  浏览器通道不可用")
            continue
        rec, races = collect_state(cfg, chrome)
        for r in races:
            r["state"] = cfg["id"]
            r["state_zh"] = cfg["zh"]
            r["source_id"] = cfg["id"]
            r["source_url"] = cfg.get("url")
            r["source_agency"] = cfg.get("agency", "")
            r["source_channel"] = cfg.get("mode", "direct")
        all_races.extend(races)
        out_states.append(rec)
        print(f"  {cfg['id']:<4} {cfg['zh']:<9} {rec['status']:<8} {rec['races']:>3} 场  {rec['note'][:64]}")
        time.sleep(0.4)

    chrome.close()

    # 合并历史快照中本次未覆盖的州（增量刷新不丢数据），并保证 51 个辖区全部在册
    prev = {"states": [], "races": []}
    if os.path.exists(OUT_PATH):
        with open(OUT_PATH, encoding="utf-8") as f:
            try:
                prev = json.load(f)
            except (OSError, ValueError):
                prev = {"states": [], "races": []}

    collected = {s["id"]: s for s in out_states}
    prev_states = {s["id"]: s for s in prev.get("states", [])}
    final_states = []
    for cfg in config["states"]:                       # 始终按官方 51 个辖区输出
        cid = cfg["id"]
        new_rec = collected.get(cid)
        if new_rec and new_rec.get("status") == "ok" and new_rec.get("races"):
            final_states.append(new_rec)               # 本轮成功拿到新数据
        elif cid in prev_states:
            old = dict(prev_states[cid])               # 本轮失败 → 沿用上一轮快照
            old["stale"] = True
            final_states.append(old)
        elif new_rec:
            final_states.append(new_rec)               # 本轮失败且无历史
        else:
            final_states.append({
                "id": cid, "zh": cfg["zh"], "en": cfg["en"], "ev": cfg.get("ev"),
                "house_seats": cfg.get("house_seats", 0),
                "senate_seats_up": cfg.get("senate_seats_up", 0),
                "gov_up": bool(cfg.get("gov_up")), "agency": cfg.get("agency", ""),
                "url": cfg.get("url"), "status": "pending", "updated_at": None,
                "races": 0, "note": "尚未接入该辖区官方结果系统",
            })
    out_states = final_states

    # 只有本次真正拿到新数据的州才替换旧记录；抓取失败的州沿用上一轮快照
    touched = {s["id"] for s in collected.values() if s.get("status") == "ok" and s.get("races")}
    kept = [r for r in prev.get("races", []) if r.get("state") not in touched]
    all_races = kept + all_races

    doc = {
        "meta": {
            "election": "2026 United States General Election",
            "election_date": "2026-11-03",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generator": "scripts/collect.py",
            "source_policy": "数据仅来自各州州务卿 / 选举委员会官方结果系统；FEC 在认证后汇编发布。",
            "races_count": len(all_races),
            "states_ok": sum(1 for s in out_states if s["status"] == "ok"),
            "states_total": len(out_states),
            "baseline": config.get("_meta", {}).get("baseline", {}),
            "structure_notes": config.get("_meta", {}).get("notes", []),
        },
        "races": all_races,
        "states": out_states,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    # 原子写：先写临时文件再 replace，避免服务端读到写了一半的 JSON
    tmp_path = OUT_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    os.replace(tmp_path, OUT_PATH)
    print(f"\n已写入 {OUT_PATH}")
    print(f"  官方接入 {doc['meta']['states_ok']}/{doc['meta']['states_total']} 个辖区，{len(all_races)} 场竞选")


if __name__ == "__main__":
    main()

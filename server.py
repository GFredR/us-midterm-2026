#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py — 站点服务端（让「刷新」按钮真的刷新）

问题：纯静态站点里，刷新按钮只是重新拉同一个 election-2026.json，
      文件不变 → 数字永远不变，看起来像"刷新坏了"。

做法：本进程既发静态资源，也发计票快照。后台线程周期性调用
      scripts/collect.py 重新抓各州官方结果，写回 data/election-2026.json。
      前端请求 /data/election-2026.json 时拿到的就是最近一次采集结果。

      采集失败时自动回滚到上一份完好快照，绝不返回坏 JSON。

环境变量
  PORT              监听端口（部署平台注入），默认 3000
  REFRESH_INTERVAL  自动刷新间隔（秒），默认 1800（30 分钟）
  REFRESH_ON_START  启动后是否立刻采一次，默认 1
  COLLECT_TIMEOUT   单次采集超时（秒），默认 2400
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(ROOT, "data", "election-2026.json")
COLLECT = os.path.join(ROOT, "scripts", "collect.py")
STATE_FILE = os.path.join(ROOT, "data", "server-state.json")

PORT = int(os.environ.get("PORT", "3000"))
INTERVAL = max(60, int(os.environ.get("REFRESH_INTERVAL", "1800")))
ON_START = os.environ.get("REFRESH_ON_START", "1") not in ("0", "false", "no")
TIMEOUT = int(os.environ.get("COLLECT_TIMEOUT", "2400"))
COLLECTOR_OK = False       # 启动时探测决定

LOCK = threading.Lock()
STATE = {
    "snapshot": None,       # 内存中的快照字节
    "mtime": 0,            # 快照文件 mtime
    "last_attempt": None,  # 上次尝试采集时间
    "last_ok": None,       # 上次成功采集时间
    "last_error": None,    # 上次失败原因
    "runs": 0,
    "busy": False,
    "collector": None,     # 采集器探测结果
    "history": [],         # 最近若干次采集结果摘要
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def probe_collector():
    """
    探测本机能不能真的采集。

    部署沙箱里通常**没有 Chrome**，而且出口 IP 仍被各州官方站的国家级封禁拦着。
    与其让采集线程每 30 分钟失败一次（还把状态条搞成报错），不如先探明：
      可用 → 正常周期采集
      不可用 → 只发静态快照，状态里如实说明，由前端改从 CI 远程快照取数
    """
    try:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
        import collect as collector_mod            # noqa: PLC0415
    except Exception as e:                        # noqa: BLE001
        return {"available": False, "reason": f"采集器模块不可用: {e}"}
    chrome = collector_mod.find_chrome()
    if not chrome:
        return {"available": False, "reason": "本机没有可用的 Chrome，无法渲染抓取"}
    return {"available": True, "reason": "", "chrome": chrome}


def load_snapshot():
    """把磁盘上的快照读进内存。读不到就保持原样。"""
    try:
        with open(DATA_FILE, "rb") as f:
            blob = f.read()
        if not blob:
            return False
        json.loads(blob.decode("utf-8"))     # 校验是完整 JSON 再采用
        with LOCK:
            STATE["snapshot"] = blob
            STATE["mtime"] = os.path.getmtime(DATA_FILE)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[server] 读取快照失败: {e}", flush=True)
        return False


def run_collection(reason="定时"):
    """跑一次采集。失败自动回滚。"""
    with LOCK:
        if STATE["busy"]:
            return False
        STATE["busy"] = True
        STATE["last_attempt"] = now_iso()

    backup = DATA_FILE + ".bak"
    had_backup = False
    try:
        if os.path.exists(DATA_FILE):
            shutil.copy2(DATA_FILE, backup)
            had_backup = True
    except OSError:
        had_backup = False

    started = time.time()
    try:
        print(f"[server] 开始采集（{reason}）…", flush=True)
        proc = subprocess.run(
            [sys.executable, COLLECT],
            cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT,
        )
        tail = (proc.stdout or "").strip().splitlines()[-6:]
        ok = proc.returncode == 0 and os.path.exists(DATA_FILE)
        if ok:
            load_snapshot()
            with LOCK:
                STATE["last_ok"] = now_iso()
                STATE["last_error"] = None
        else:
            raise RuntimeError(
                "collect.py 退出码 %s：%s" % (proc.returncode, " / ".join(tail)[:400]
                                              or (proc.stderr or "")[-400:])
            )
        with LOCK:
            STATE["runs"] += 1
            STATE["history"].insert(0, {
                "at": now_iso(), "reason": reason, "ok": True,
                "seconds": round(time.time() - started, 1),
                "summary": " / ".join(tail)[-300:],
            })
            del STATE["history"][8:]
        print(f"[server] 采集完成，用时 {time.time() - started:.0f}s", flush=True)
        save_state()
        return True

    except Exception as e:  # noqa: BLE001
        # 采集失败：把上一份完好快照恢复回去
        if had_backup:
            try:
                shutil.copy2(backup, DATA_FILE)
                load_snapshot()
            except OSError:
                pass
        with LOCK:
            STATE["last_error"] = str(e)[:600]
            STATE["history"].insert(0, {
                "at": now_iso(), "reason": reason, "ok": False,
                "seconds": round(time.time() - started, 1),
                "summary": str(e)[:300],
            })
            del STATE["history"][8:]
        print(f"[server] 采集失败: {e}", flush=True)
        save_state()
        return False

    finally:
        with LOCK:
            STATE["busy"] = False


def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in STATE.items() if k != "snapshot"}, f,
                      ensure_ascii=False, indent=1)
    except OSError:
        pass


def worker():
    """后台循环：启动后立刻采一次，之后按 INTERVAL 周期刷新。"""
    if not COLLECTOR_OK:
        print(f"[server] 采集能力不可用（{STATE['collector'].get('reason')}）"
              "，本进程只发静态快照；前端会改从 CI 远程快照取数。", flush=True)
        return
    if ON_START:
        run_collection("启动")
    while True:
        time.sleep(INTERVAL)
        run_collection("定时")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    def log_message(self, fmt, *args):
        pass                                   # 静默常规访问日志

    def _send(self, code, body, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = self.path.split("?", 1)[0]

        if path == "/api/status":
            with LOCK:
                snap = STATE
                body = {
                    "generated_at": (json.loads(snap["snapshot"])["meta"].get("generated_at")
                                     if snap["snapshot"] else None),
                    "last_attempt": snap["last_attempt"],
                    "last_ok": snap["last_ok"],
                    "last_error": snap["last_error"],
                    "runs": snap["runs"],
                    "busy": snap["busy"],
                    "interval_sec": INTERVAL,
                    "collector": snap.get("collector"),
                    "history": snap["history"],
                    "server_time": now_iso(),
                }
            return self._send(200, json.dumps(body, ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")

        if path == "/api/refresh":
            if not COLLECTOR_OK:
                return self._send(503, json.dumps({
                    "accepted": False,
                    "error": "本部署环境没有可用的 Chrome，无法在服务端采集",
                    "detail": (STATE.get("collector") or {}).get("reason", ""),
                }, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            # 异步触发：立刻返回，真正的采集在后台线程跑
            threading.Thread(target=run_collection, args=("手动",), daemon=True).start()
            return self._send(202, json.dumps({"accepted": True, "note": "采集已在后台启动"},
                                              ensure_ascii=False).encode("utf-8"),
                              "application/json; charset=utf-8")

        if path.endswith("/election-2026.json"):
            with LOCK:
                blob = STATE["snapshot"]
            if blob is None:
                load_snapshot()
                with LOCK:
                    blob = STATE["snapshot"]
            if blob is None:
                return self._send(503, b'{"error":"snapshot unavailable"}',
                                  "application/json; charset=utf-8")
            return self._send(200, blob, "application/json; charset=utf-8")

        return super().do_GET()


def main():
    global COLLECTOR_OK
    print(f"[server] 启动，端口 {PORT}，自动刷新间隔 {INTERVAL}s", flush=True)
    load_snapshot()
    STATE["collector"] = probe_collector()
    COLLECTOR_OK = bool(STATE["collector"].get("available"))
    print(f"[server] 采集能力：{'可用' if COLLECTOR_OK else '不可用'} "
          f"{STATE['collector'].get('reason') or STATE['collector'].get('chrome', '')}", flush=True)
    threading.Thread(target=worker, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()

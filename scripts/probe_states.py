#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe_states.py — 批量嗅探各州官方结果页实际调用的机读接口

逐州打开官方结果页，记录它加载的 JSON / API / 结果子页地址，
输出 probe_results.json。这些线索是给 state_sources.json 配 URL 和选适配器的依据。

单独跑：
  python3 scripts/probe_states.py            # 只探尚未接入的州
  python3 scripts/probe_states.py AZ,CA      # 只探指定州
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import collect  # noqa: E402

CONFIG = os.path.join(HERE, "state_sources.json")
OUT = os.path.join(ROOT, "probe_results.json")
BROWSER = os.path.join(HERE, "browser.mjs")


def main():
    only = {x.strip().upper() for x in (sys.argv[1] if len(sys.argv) > 1 else "").split(",") if x.strip()}
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)

    # 已经能解析出结果的州不用再探
    done = set()
    data_path = os.path.join(ROOT, "data", "election-2026.json")
    if os.path.exists(data_path):
        try:
            with open(data_path, encoding="utf-8") as f:
                for st in json.load(f).get("states", []):
                    if st.get("status") == "ok" and st.get("races"):
                        done.add(st["id"])
        except (OSError, ValueError):
            pass

    targets = [s for s in cfg["states"]
               if s["id"] not in done and s.get("url") and not s.get("disabled")
               and (not only or s["id"] in only)]
    print(f"待探测 {len(targets)} 个辖区：{' '.join(s['id'] for s in targets)}\n", flush=True)

    chrome = collect.Chrome()
    if not chrome.ensure():
        print("Chrome 启动失败，探测中止", flush=True)
        return 1
    print("Chrome 就绪\n", flush=True)

    results = {}
    for i, s in enumerate(targets, 1):
        sid, url = s["id"], s["url"]
        print(f"[{i}/{len(targets)}] {sid} …", end=" ", flush=True)
        rec = {"id": sid, "configured_url": url, "agency": s.get("agency", "")}
        try:
            p = subprocess.run(
                ["node", BROWSER, "net", url, "11000", os.path.join("/tmp", f"probe_{sid}.json")],
                capture_output=True, timeout=150,
                env=dict(os.environ, CDP_PORT=str(collect.CDP_PORT)),
            )
            try:
                d = json.loads(p.stdout.decode("utf-8", "replace"))
                rec["title"] = d.get("title")
                rec["navFailed"] = d.get("navFailed")
                rec["api_urls"] = [u for u in d.get("urls", []) if u not in (url,)]
                rec["all_urls"] = d.get("all", [])[:60]
            except Exception:                      # noqa: BLE001
                rec["error"] = (p.stderr.decode("utf-8", "replace") or p.stdout.decode("utf-8", "replace"))[:300]
        except subprocess.TimeoutExpired:
            rec["error"] = "timeout"
        results[sid] = rec
        print(f"{len(rec.get('api_urls', []))} 个接口线索"
              + (f" · {rec['error'][:60]}" if rec.get("error") else ""), flush=True)
        time.sleep(0.5)

    chrome.close()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

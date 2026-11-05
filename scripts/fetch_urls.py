#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_urls.py — 在能直连美国官方站的环境里（CI runner）批量抓任意 URL

本机（中国出口 + 代理）被 Imperva / Cloudflare 按 IP 拦的州，只能在 CI 里抓。
这个脚本用 Chrome 通道把指定 URL 的原文抓下来存成文件，便于离线分析结构。

URL 列表在 scripts/fetch_targets.json 里：
  [{"name": "标识", "url": "...", "mode": "raw|html"}]
    raw  → 只存文本内容（用于 JSON / txt 接口）
    html → 存整页 HTML（用于分析页面结构）

用法：
  python3 scripts/fetch_urls.py            # 抓 fetch_targets.json 里全部
  python3 scripts/fetch_urls.py pa_all     # 只抓名字里含 pa_all 的
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

TARGETS = os.path.join(HERE, "fetch_targets.json")
OUTDIR = os.path.join(ROOT, "fetch_out")
BROWSER = os.path.join(HERE, "browser.mjs")


def main():
    with open(TARGETS, encoding="utf-8") as f:
        targets = json.load(f)
    if len(sys.argv) > 1:
        kw = sys.argv[1].lower()
        targets = [t for t in targets if kw in t["name"].lower()]
    if not targets:
        print("没有匹配的目标")
        return 1

    os.makedirs(OUTDIR, exist_ok=True)
    chrome = collect.Chrome()
    if not chrome.ensure():
        print("Chrome 启动失败", flush=True)
        return 1
    print(f"Chrome 就绪，待抓 {len(targets)} 个 URL\n", flush=True)

    index = []
    for i, t in enumerate(targets, 1):
        name, url, mode = t["name"], t["url"], t.get("mode", "raw")
        out = os.path.join(OUTDIR, f"{name}.txt")
        print(f"[{i}/{len(targets)}] {name} ({mode}) …", end=" ", flush=True)
        rec = {"name": name, "url": url, "mode": mode}
        try:
            args = ["node", BROWSER, mode, url, "9000" if mode == "raw" else "9000", out]
            p = subprocess.run(
                args, capture_output=True, timeout=180,
                env=dict(os.environ, CDP_PORT=str(collect.CDP_PORT)),
            )
            size = os.path.getsize(out) if os.path.exists(out) else 0
            rec["bytes"] = size
            rec["ok"] = size > 0
            if size == 0:
                rec["error"] = (p.stderr.decode("utf-8", "replace") or "")[:200]
            print(f"{size} 字节" + ("" if rec["ok"] else f" · {rec.get('error','')[:70]}"))
        except subprocess.TimeoutExpired:
            rec["ok"] = False
            rec["error"] = "timeout"
            print("超时")
        index.append(rec)
        time.sleep(0.4)

    chrome.close()
    with open(os.path.join(OUTDIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    print(f"\n已写入 {OUTDIR}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

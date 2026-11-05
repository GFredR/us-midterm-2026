#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
push_via_api.py — 代理能通 api.github.com 但不通 github.com 时，
用 GitHub Git Data API 把本地改动推上去（一次 commit，不产生多余历史）。

用法：
  python3 scripts/push_via_api.py <文件路径> [更多文件...]
"""
import base64
import json
import os
import subprocess
import sys
import urllib.request

REPO = os.environ.get("GH_REPO", "GFredR/us-midterm-2026")
BRANCH = os.environ.get("GH_BRANCH", "main")
API = "https://api.github.com"


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if t:
        return t.strip()
    return subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip()


def api(path, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={
            "Authorization": f"Bearer {token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "push-via-api",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def main():
    files = sys.argv[1:]
    if not files:
        print("用法: push_via_api.py <file> [file...]")
        return 1

    # 远端分支当前 HEAD
    ref = api(f"/repos/{REPO}/git/ref/heads/{BRANCH}")
    head_sha = ref["object"]["sha"]
    commit = api(f"/repos/{REPO}/git/commits/{head_sha}")
    base_tree = commit["tree"]["sha"]
    print(f"远端 HEAD: {head_sha[:12]}  tree: {base_tree[:12]}")

    tree_entries = []
    for rel in files:
        with open(rel, "rb") as f:
            content = f.read()
        blob = api(f"/repos/{REPO}/git/blobs",
                   "POST", {"content": base64.b64encode(content).decode(), "encoding": "base64"})
        tree_entries.append({
            "path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"],
        })
        print(f"  blob {rel:<40} {blob['sha'][:10]}  {len(content)}B")

    new_tree = api(f"/repos/{REPO}/git/trees", "POST",
                   {"base_tree": base_tree, "tree": tree_entries})
    print(f"新 tree: {new_tree['sha'][:12]}")

    msg = os.environ.get("GH_COMMIT_MSG", "chore: 同步本地改动")
    new_commit = api(f"/repos/{REPO}/git/commits", "POST", {
        "message": msg, "tree": new_tree["sha"], "parents": [head_sha],
    })
    print(f"新 commit: {new_commit['sha'][:12]}")

    api(f"/repos/{REPO}/git/refs/heads/{BRANCH}", "PATCH",
        {"sha": new_commit["sha"], "force": False})
    print("✅ 已推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())

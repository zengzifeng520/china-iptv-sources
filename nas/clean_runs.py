#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每周清理 GitHub Actions 运行记录：每个工作流只保留最新一条。

由 NAS cron 每周执行。删除未结束的运行会失败，跳过即可。
"""
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
REPOS = ["IPTV", "IPTVZB", "china-iptv-sources"]
API = "https://api.github.com"
KEEP = 1
TOKEN_FILE = os.path.join(BASE, ".github_token_dispatch")


def load_token():
    for name in (TOKEN_FILE, os.path.join(BASE, ".github_token")):
        if os.path.exists(name):
            t = open(name, encoding="utf-8").read().strip()
            if t:
                return t
    raise SystemExit("找不到 GitHub token")


HDR = {
    "Authorization": "Bearer " + load_token(),
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "nas-clean-runs",
}


def call(url, method="GET"):
    req = urllib.request.Request(url, method=method, headers=HDR)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return r.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception:
        return 0, None


def drop_one(repo, run_id):
    for attempt in range(3):
        st, _ = call(f"{API}/repos/zengzifeng520/{repo}/actions/runs/{run_id}", "DELETE")
        if st in (204, 200):
            return True
        if st in (403, 429):
            time.sleep(3 * (attempt + 1))
            continue
        return False
    return False


def main():
    total_deleted = 0
    for repo in REPOS:
        runs, page = [], 1
        while True:
            st, data = call(f"{API}/repos/zengzifeng520/{repo}/actions/runs?per_page=100&page={page}")
            if st != 200 or not data:
                print(f"{repo}: 第 {page} 页读取失败（{st}）", flush=True)
                break
            batch = data.get("workflow_runs", [])
            runs.extend(batch)
            if len(batch) < 100:
                break
            page += 1

        by_wf = {}
        for r in runs:
            by_wf.setdefault(r["name"], []).append(r)

        print(f"=== {repo}: {len(runs)} 条 ===", flush=True)
        targets = []
        for name, items in by_wf.items():
            items.sort(key=lambda x: x["created_at"], reverse=True)
            drop = [r["id"] for r in items[KEEP:] if r["status"] == "completed"]
            print(f"  {name}: 保留 1 条，删除 {len(drop)} 条", flush=True)
            targets.extend(drop)

        if not targets:
            continue
        deleted = 0
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            for ok in ex.map(lambda rid: drop_one(repo, rid), targets):
                deleted += 1 if ok else 0
        total_deleted += deleted
        print(f"  已删除 {deleted}/{len(targets)} 条", flush=True)

    print(f"全部完成，共删除 {total_deleted} 条", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

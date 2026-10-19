# -*- coding: utf-8 -*-
"""一键部署：本地运行 run.py 生成完整订阅，再推送 output/ 到 GitHub 仓库。

为什么需要它：
  GitHub Actions 的 runner 在海外，抓不到国内运营商（电信/移动）提供的
  直播服务器，而央视部分频道（如 CCTV-2/7/10/11/15/16/17）的稳定源恰好在
  国内公网上。因此在本机运行本脚本得到的订阅最完整，推送到仓库后
  jsDelivr/raw 订阅地址即为最新版。

用法:
    python deploy.py                 # 读取 config.json，运行并推送
    python deploy.py --push-only     # 只推送本地 output/（跳过重新采集）
    python deploy.py --no-push       # 只采集生成，不推送

环境变量:
    GITHUB_TOKEN  访问令牌（必填，需仓库 Contents 读写权限）
    GITHUB_REPO   "owner/repo"（默认 zengzifeng520/china-iptv-sources）
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = "zengzifeng520/china-iptv-sources"
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
FILES_TO_PUSH = ["live.txt", "live.m3u", "stats.json"]


def get_sha(repo, path, token):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "deploy"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")).get("sha")
    except Exception:  # noqa: BLE001
        return None


def push_file(repo, path, content_bytes, token):
    sha = get_sha(repo, path, token)
    payload = {
        "message": f"chore: 更新直播源 {path}",
        "content": base64.b64encode(content_bytes).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "deploy",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.loads(r.read().decode("utf-8"))
        return resp.get("content", {}).get("html_url") or url


def load_token():
    """读取推送令牌：优先环境变量 GITHUB_TOKEN，其次本地 .github_token 文件。"""
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    tf = os.path.join(BASE_DIR, ".github_token")
    if os.path.exists(tf):
        tok = open(tf, encoding="utf-8").read().strip()
        if tok:
            return tok
    print("未找到推送令牌：请设置环境变量 GITHUB_TOKEN，"
          "或把令牌写入本目录 .github_token 文件（该文件已在 .gitignore 中，不会上传）。")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push-only", action="store_true", help="只推送本地 output/")
    ap.add_argument("--no-push", action="store_true", help="只采集生成，不推送")
    args = ap.parse_args()

    token = load_token()
    repo = os.environ.get("GITHUB_REPO", DEFAULT_REPO)

    if not args.push_only:
        print("== 运行 run.py 采集并生成订阅 ==")
        rc = subprocess.call([sys.executable, os.path.join(BASE_DIR, "run.py")])
        if rc != 0:
            print("run.py 未生成有效订阅（退出码 %d），终止" % rc)
            sys.exit(1)

    if args.no_push:
        print("已跳过推送。")
        return

    print(f"== 推送 output/ 到 {repo} ==")
    ok = 0
    for name in FILES_TO_PUSH:
        path = os.path.join(OUTPUT_DIR, name)
        if not os.path.exists(path):
            print(f"  跳过（不存在）: {name}")
            continue
        with open(path, "rb") as f:
            data = f.read()
        try:
            url = push_file(repo, f"output/{name}", data, token)
            print(f"  已推送 {name} ({len(data)} bytes) -> {url}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  推送失败 {name}: {e}")
    print(f"完成：成功 {ok}/{len(FILES_TO_PUSH)} 个文件。")
    print("订阅地址（约 1 分钟后刷新）：")
    print(f"  https://raw.githubusercontent.com/{repo}/main/output/live.m3u")
    print(f"  https://raw.githubusercontent.com/{repo}/main/output/live.txt")
    print(f"  https://cdn.jsdelivr.net/gh/{repo}@main/output/live.m3u")


if __name__ == "__main__":
    main()

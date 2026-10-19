# -*- coding: utf-8 -*-
"""一键部署：采集/导入直播源并推送到 GitHub 仓库。

三种用法：
  1) 自动采集（默认）：本地运行 run.py 生成完整订阅，再推送 output/ 到 GitHub。
     在**国内网络**下运行，可获得最全的央视源（GitHub Actions 海外服务器
     访问不到国内电信源）。
  2) 导入本地/NAS 源文件：把 NAS 或本机维护的直播源文件（m3u/txt）直接
     生成订阅并推送，适合"NAS 定时推送"场景。
        python deploy.py --import "D:/nas/iptv/live.m3u"
        python deploy.py --import "D:/nas/iptv/live.txt" --validate   # 可选：推送前校验
  3) 只推送本地已有 output/：
        python deploy.py --push-only

令牌获取（按顺序）：
  1. 环境变量 GITHUB_TOKEN
  2. 本目录 .github_token 文件（已被 .gitignore 排除，不会上传）

定时推送（NAS 示例）：
  群晖/威联通等 Linux NAS 上配置 cron：
     30 2 * * * cd /volume1/iptv/china-iptv-sources && python3 deploy.py --import "/volume1/iptv/live.m3u" >> push.log 2>&1
  Windows 可用"任务计划程序"设置相同命令。
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

sys.path.insert(0, BASE_DIR)
from iptv_sources.generator import now_str, write_outputs  # noqa: E402
from iptv_sources.parser import parse_auto  # noqa: E402
from iptv_sources.validator import check_stream_many  # noqa: E402


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


def read_source_file(path):
    """读取本地直播源文件，自动尝试 utf-8 / gbk 编码。"""
    for enc in ("utf-8", "gbk", "utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


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


def push_outputs(repo, token):
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
            print(f"  已推送 {name} ({len(data)} bytes)")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  推送失败 {name}: {e}")
    print(f"完成：成功 {ok}/{len(FILES_TO_PUSH)} 个文件。")
    if ok:
        print("订阅地址（约 1 分钟后刷新）：")
        print(f"  https://raw.githubusercontent.com/{repo}/main/output/live.m3u")
        print(f"  https://raw.githubusercontent.com/{repo}/main/output/live.txt")
        print(f"  https://cdn.jsdelivr.net/gh/{repo}@main/output/live.m3u")


def run_auto():
    print("== 运行 run.py 采集并生成订阅 ==")
    rc = subprocess.call([sys.executable, os.path.join(BASE_DIR, "run.py")])
    if rc != 0:
        print("run.py 未生成有效订阅（退出码 %d），终止" % rc)
        sys.exit(1)


def run_import(src_file, validate, category):
    """导入本地/NAS 源文件：解析 → 可选校验 → 生成 output/。"""
    print(f"== 导入本地源文件: {src_file} ==")
    text = read_source_file(src_file)
    entries = parse_auto(text)
    print(f"解析出 {len(entries)} 条")

    if category:
        entries = [(category, n, u) for _c, n, u in entries]
    elif not entries:
        print("文件为空或无法解析，终止。")
        sys.exit(1)

    if validate:
        print(f"== 校验 {len(entries)} 条（可选步骤）==")
        urls = [u for _, _, u in entries]
        ok_map = check_stream_many(urls, timeout=8, workers=40, retries=1)
        entries = [(c, n, u) for c, n, u in entries if ok_map.get(u)]
        print(f"校验后剩 {len(entries)} 条")

    updated = now_str()
    stats = {
        "updated_at": updated,
        "source": os.path.basename(src_file),
        "import": True,
        "total": len(entries),
        "categories": {},
    }
    for c, _n, _u in entries:
        stats["categories"][c] = stats["categories"].get(c, 0) + 1
    write_outputs(entries, OUTPUT_DIR, updated=updated, stats=stats)
    print(f"已生成 output/live.txt、live.m3u、stats.json（共 {len(entries)} 条）")


def main():
    ap = argparse.ArgumentParser(description="采集/导入直播源并推送 GitHub")
    ap.add_argument("--push-only", action="store_true", help="只推送本地 output/（跳过采集/导入）")
    ap.add_argument("--no-push", action="store_true", help="只生成，不推送")
    ap.add_argument("--import", dest="src_file", metavar="FILE", help="导入本地/NAS 源文件（m3u/txt）")
    ap.add_argument("--validate", action="store_true", help="导入时执行三级校验（默认原样保留）")
    ap.add_argument("--category", default="", help="统一覆盖分类名（默认保留文件内分类）")
    args = ap.parse_args()

    token = load_token()
    repo = os.environ.get("GITHUB_REPO", DEFAULT_REPO)

    if args.src_file:
        run_import(args.src_file, args.validate, args.category)
    elif not args.push_only:
        run_auto()

    if args.no_push:
        print("已跳过推送。")
        return
    push_outputs(repo, token)


if __name__ == "__main__":
    main()

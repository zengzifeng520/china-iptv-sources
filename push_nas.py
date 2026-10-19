# -*- coding: utf-8 -*-
"""飞牛 NAS 直播源自动推送 GitHub 的入口脚本。

把 NAS 上维护的直播源文件（m3u / txt）定时推送到 GitHub 仓库，
订阅地址（output/live.m3u、output/live.txt）始终指向最新内容。

用法（在 NAS 上执行）:
    python3 push_nas.py --file "/vol1/IPTV/live.m3u"             # 导入并推送
    python3 push_nas.py --file "/vol1/IPTV/live.m3u" --validate  # 推送前先校验有效性
    python3 push_nas.py --file "/vol1/IPTV/live.txt" --category "我的源"  # 覆盖分类
    python3 push_nas.py --push-only                              # 只推送本地 output/
    python3 push_nas.py --file ... --no-push                     # 只生成，不推送

定时推送（NAS 上 crontab -e 添加）:
    30 2 * * * cd /vol1/iptv/china-iptv-sources && python3 push_nas.py --file "/vol1/IPTV/live.m3u" >> push.log 2>&1
（飞牛 fnOS 基于 Debian，自带 python3；若提示缺命令，先执行 sudo apt install python3）

令牌读取顺序：
    1) 环境变量 GITHUB_TOKEN
    2) 本目录 .github_token 文件（已加入 .gitignore，不会上传）
"""
import argparse
import base64
import json
import os
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
    """读取直播源文件，自动尝试 utf-8 / gbk 等编码（NAS 文件常见 GBK）。"""
    for enc in ("utf-8", "gbk", "utf-8-sig", "latin-1"):
        try:
            with open(path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    with open(path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def pick_latest(directory):
    """从目录中自动选择最新的 .m3u / .txt 源文件。

    适用于 iptv-manager 等工具每次生成带随机文件名新文件的情况：
    定时任务只需指向目录，脚本自动取最新的源。
    """
    if not os.path.isdir(directory):
        print(f"目录不存在: {directory}")
        sys.exit(1)
    files = []
    for name in os.listdir(directory):
        low = name.lower()
        if low.endswith(".m3u") or low.endswith(".m3u8") or low.endswith(".txt"):
            files.append(os.path.join(directory, name))
    if not files:
        print(f"目录中没有找到 .m3u / .m3u8 / .txt 源文件: {directory}")
        sys.exit(1)
    latest = max(files, key=os.path.getmtime)
    print(f"自动选择最新源文件: {latest}")
    return latest


def get_sha(repo, path, token):
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "push-nas"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8")).get("sha")
    except Exception:  # noqa: BLE001
        return None


def push_file(repo, path, content_bytes, token):
    sha = get_sha(repo, path, token)
    payload = {
        "message": f"chore: NAS 自动更新直播源 {path}",
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
            "User-Agent": "push-nas",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        json.loads(r.read().decode("utf-8"))
    return True


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
            push_file(repo, f"output/{name}", data, token)
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
    return ok


def run_import(src_file, validate, category):
    print(f"== 导入源文件: {src_file} ==")
    text = read_source_file(src_file)
    entries = parse_auto(text)
    print(f"解析出 {len(entries)} 条")

    if not entries:
        print("文件为空或无法解析，终止（请检查文件路径与格式：m3u 或 txt）。")
        sys.exit(1)
    if category:
        entries = [(category, n, u) for _c, n, u in entries]

    if validate:
        print(f"== 校验 {len(entries)} 条（--validate）==")
        urls = [u for _, _, u in entries]
        ok_map = check_stream_many(urls, timeout=8, workers=40, retries=1)
        entries = [(c, n, u) for c, n, u in entries if ok_map.get(u)]
        print(f"校验后剩 {len(entries)} 条")

    updated = now_str()
    stats = {
        "updated_at": updated,
        "source": os.path.basename(src_file),
        "via": "NAS push",
        "total": len(entries),
        "categories": {},
    }
    for c, _n, _u in entries:
        stats["categories"][c] = stats["categories"].get(c, 0) + 1
    write_outputs(entries, OUTPUT_DIR, updated=updated, stats=stats)
    print(f"已生成 output/live.txt、live.m3u、stats.json（共 {len(entries)} 条）")


def main():
    ap = argparse.ArgumentParser(description="飞牛 NAS 直播源自动推送 GitHub")
    ap.add_argument("--file", dest="src_file", metavar="FILE", help="NAS 上直播源文件路径（m3u/txt）")
    ap.add_argument("--latest-dir", dest="src_dir", metavar="DIR",
                    help="自动取目录中最新的 m3u/txt 源文件（文件名字会变时用这个）")
    ap.add_argument("--validate", action="store_true", help="推送前做三级有效性校验（默认原样保留）")
    ap.add_argument("--category", default="", help="统一覆盖分类名（默认保留文件内分组）")
    ap.add_argument("--push-only", action="store_true", help="只推送本地 output/（不导入）")
    ap.add_argument("--no-push", action="store_true", help="只生成，不推送")
    args = ap.parse_args()

    token = load_token()
    repo = os.environ.get("GITHUB_REPO", DEFAULT_REPO)

    src = args.src_file
    if args.src_dir:
        src = pick_latest(args.src_dir)

    if src:
        run_import(src, args.validate, args.category)
    elif not args.push_only:
        print("请指定 --file <源文件路径> 或 --latest-dir <源文件目录>，或使用 --push-only。")
        sys.exit(1)

    if args.no_push:
        print("已跳过推送。")
        return
    push_outputs(repo, token)


if __name__ == "__main__":
    main()

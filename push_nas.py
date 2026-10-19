# -*- coding: utf-8 -*-
"""飞牛 NAS 直播源自动推送 GitHub 的入口脚本。

把 NAS 上维护的直播源文件（m3u / txt）定时推送到 GitHub 仓库，
订阅地址（output/live.m3u、output/live.txt）始终指向最新内容。

用法（在 NAS 上执行）:
    python3 push_nas.py --file "/vol1/IPTV/live.m3u"             # 导入并推送（原样全量）
    python3 push_nas.py --file "/vol1/IPTV/live.m3u" --validate  # 检测有效性：生成有效版 live.* + 全量版 all.* 再推送
    python3 push_nas.py --file "/vol1/IPTV/live.txt" --category "我的源"  # 覆盖分类
    python3 push_nas.py --push-only                              # 只推送本地 output/
    python3 push_nas.py --file ... --no-push                     # 只生成，不推送

定时推送（NAS 上 crontab -e 添加，推荐加 --validate 自动检测）:
    0 7 * * * cd /vol1/1000/docker/china-iptv-sources && python3 push_nas.py --latest-dir /vol1/1000/docker/iptv-manager/data/uploads --validate >> push.log 2>&1

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
FILES_TO_PUSH = ["live.txt", "live.m3u", "all.txt", "all.m3u", "stats.json"]

# 组播 / rtsp 等无法用 HTTP 探测的协议：校验时自动保留，不误删
NON_HTTP_PREFIXES = ("rtp://", "udp://", "igmp://", "rtsp://", "rtmp://")

sys.path.insert(0, BASE_DIR)
from iptv_sources.generator import now_str, build_txt, build_m3u, write_outputs  # noqa: E402
from iptv_sources.parser import parse_auto  # noqa: E402
from iptv_sources.validator import check_stream_best_many  # noqa: E402


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
        print(f"  有效版订阅（live.m3u，校验后生成）：https://raw.githubusercontent.com/{repo}/main/output/live.m3u")
        print(f"  jsDelivr 镜像：https://cdn.jsdelivr.net/gh/{repo}@main/output/live.m3u")
        if os.path.exists(os.path.join(OUTPUT_DIR, "all.m3u")):
            print(f"  全量版订阅（all.m3u，原样兜底）：https://raw.githubusercontent.com/{repo}/main/output/all.m3u")
        print(f"  TXT（TVBox 用）：https://raw.githubusercontent.com/{repo}/main/output/live.txt")
    return ok


def is_non_http(url):
    """组播 / rtsp / rtmp 等无法用 HTTP 探测的协议。"""
    low = url.lower()
    return low.startswith(NON_HTTP_PREFIXES)


def write_all_outputs(all_entries, updated):
    """把全量原样版本写入 output/all.txt、output/all.m3u（校验时的兜底订阅）。"""
    txt = build_txt(all_entries)
    m3u = build_m3u(all_entries, updated=updated)
    with open(os.path.join(OUTPUT_DIR, "all.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    with open(os.path.join(OUTPUT_DIR, "all.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write(m3u)
    print(f"已生成 output/all.txt、all.m3u（全量原样 {len(all_entries)} 条，校验时兜底订阅）")


def run_import(src_file, validate, category):
    print(f"== 导入源文件: {src_file} ==")
    text = read_source_file(src_file)
    entries = parse_auto(text)
    total = len(entries)
    print(f"解析出 {total} 条")

    if not entries:
        print("文件为空或无法解析，终止（请检查文件路径与格式：m3u 或 txt）。")
        sys.exit(1)
    if category:
        entries = [(category, n, u) for _c, n, u in entries]
    all_entries = list(entries)  # 全量原样（校验时写 all.* 兜底）

    dropped = 0
    if validate:
        print(f"== 有效性检测（--validate，20 并发，每条超时 8 秒，约需 30-120 分钟）==")
        http_items = [(i, u) for i, (c, n, u) in enumerate(entries)
                      if u.lower().startswith(("http://", "https://"))]
        urls = [u for _, u in http_items]
        non_http_count = len(entries) - len(urls)
        print(f"待检测 http(s) 源 {len(urls)} 条；组播/rtsp 等 {non_http_count} 条自动保留（不误删）")
        best_map = check_stream_best_many(urls, timeout=8, workers=20, retries=1)
        kept = set()
        replace = {}
        for i, u in http_items:
            b = best_map.get(u)
            if b:
                kept.add(i)
                if b != u:
                    replace[i] = b  # 把 master 地址替换为可直接播的分片清单地址
        valid_entries = []
        for idx, (c, n, u) in enumerate(entries):
            if idx in kept:
                valid_entries.append((c, n, replace.get(idx, u)))
            elif is_non_http(u):
                valid_entries.append((c, n, u))
        dropped = len(entries) - len(valid_entries)
        print(f"检测结果：保留 {len(valid_entries)} 条"
              f"（http(s) 通过 {len(kept)} + 组播保留 {len(valid_entries) - len(kept)}），"
              f"删除失效 {dropped} 条")
        entries = valid_entries

    updated = now_str()
    stats = {
        "updated_at": updated,
        "source": os.path.basename(src_file),
        "via": "NAS push",
        "total": total,
        "valid": len(entries),
        "dropped": dropped,
        "categories": {},
    }
    for c, _n, _u in entries:
        stats["categories"][c] = stats["categories"].get(c, 0) + 1
    write_outputs(entries, OUTPUT_DIR, updated=updated, stats=stats)
    print(f"已生成 output/live.txt、live.m3u、stats.json（订阅版 {len(entries)} 条）")
    if validate:
        write_all_outputs(all_entries, updated)


def main():
    ap = argparse.ArgumentParser(description="飞牛 NAS 直播源自动推送 GitHub")
    ap.add_argument("--file", dest="src_file", metavar="FILE", help="NAS 上直播源文件路径（m3u/txt）")
    ap.add_argument("--latest-dir", dest="src_dir", metavar="DIR",
                    help="自动取目录中最新的 m3u/txt 源文件（文件名字会变时用这个）")
    ap.add_argument("--validate", action="store_true",
                    help="推送前做有效性检测：http(s) 源 20 并发逐个探测，组播/rtsp 自动保留，"
                         "生成有效版 live.* + 全量版 all.*（检测耗时约 30-120 分钟）")
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

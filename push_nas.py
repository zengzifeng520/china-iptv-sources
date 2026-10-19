# -*- coding: utf-8 -*-
"""飞牛 NAS 直播源自动推送 GitHub 的入口脚本。

把 NAS 上维护的直播源文件（m3u / txt）定时推送到 GitHub 仓库，
订阅地址（output/live.m3u、output/live.txt）始终指向最新内容。

可选功能：
  --sync-epg   同步最新节目单（EPG）到 output/epg.xml 并推送（订阅头部自动挂载）
  --sync-logo  同步最新频道图标到 output/logo/ 并推送（M3U 自动带 tvg-logo）

用法（在 NAS 上执行）:
    python3 push_nas.py --file "/vol1/IPTV/live.m3u"             # 导入并推送（原样全量）
    python3 push_nas.py --file "/vol1/IPTV/live.m3u" --validate  # 检测有效性：生成有效版 live.* + 全量版 all.* 再推送
    python3 push_nas.py --file "/vol1/IPTV/live.m3u" --validate --sync-epg --sync-logo  # 检测+节目单+图标 全自动
    python3 push_nas.py --file "/vol1/IPTV/live.txt" --category "我的源"  # 覆盖分类
    python3 push_nas.py --push-only                              # 只推送本地 output/
    python3 push_nas.py --file ... --no-push                     # 只生成，不推送

定时推送（NAS 上 crontab -e 添加，推荐加 --validate --sync-epg --sync-logo 全自动）:
    0 7 * * * cd /vol1/1000/docker/china-iptv-sources && python3 push_nas.py --latest-dir /vol1/1000/docker/iptv-manager/data/uploads --validate --sync-epg --sync-logo >> push.log 2>&1

令牌读取顺序：
    1) 环境变量 GITHUB_TOKEN
    2) 本目录 .github_token 文件（已加入 .gitignore，不会上传）
"""
import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sys
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = "zengzifeng520/china-iptv-sources"
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
LOGO_DIR = os.path.join(OUTPUT_DIR, "logo")
LOGO_STATE = os.path.join(BASE_DIR, ".logo_state.json")  # 已推送图标记录（本地，gitignore）
FILES_TO_PUSH = ["live.txt", "live.m3u", "all.txt", "all.m3u", "stats.json", "epg.xml"]

# 组播 / rtsp 等无法用 HTTP 探测的协议：校验时自动保留，不误删
NON_HTTP_PREFIXES = ("rtp://", "udp://", "igmp://", "rtsp://", "rtmp://")

# ===== 数据源（均为持续维护的活跃仓库）=====
# EPG 节目单：alantang1977/iptv_api（活跃），经 ghproxy 镜像国内可下载
EPG_SOURCE = "https://ghproxy.net/https://raw.githubusercontent.com/alantang1977/iptv_api/main/output/epg.xml"
# 图标索引：拉取活跃仓库文件清单，按频道名精确匹配（不靠猜路径）
LOGO_INDEX_DIRS = [
    ("Meroser/Media_Coll", "TVlogo"),
    ("alantang1977/iptv_api", "pic"),
]
SKIP_LOGO_DIRS = {"Adult"}  # 跳过不适内容目录
LOGO_RAW_MIRROR = "https://ghproxy.net/https://raw.githubusercontent.com/{repo}/main/{path}"
DEFAULT_BASE_URL = "https://tingbao.kdns.fr"  # 订阅/图标/EPG 的对外域名（你自己的 GitHub Pages）

# 频道名常见后缀（清洗时去掉）
_NAME_SUFFIXES = [
    "高清", "超清", "4K", "1080P", "1080", "720P", "标清", "普清", "蓝光",
    "H265", "H264", "HEVC", "AVC", "SDR", "HDR", "HD", "测试", "源",
    "线路", "回看", "轮播", "频道", "直播", "FHD", "UHD", "IPV4", "IPV6",
]

sys.path.insert(0, BASE_DIR)
from iptv_sources.generator import now_str, build_txt, build_m3u, write_outputs  # noqa: E402
from iptv_sources.parser import parse_auto  # noqa: E402
from iptv_sources.validator import check_stream_best_many  # noqa: E402


def load_config():
    cfg = {}
    p = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(p):
        try:
            cfg = json.load(open(p, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            cfg = {}
    return cfg


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
        "message": f"chore: NAS 自动更新 {path}",
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
        print(f"  有效版订阅（live.m3u，含图标与节目单）：https://raw.githubusercontent.com/{repo}/main/output/live.m3u")
        print(f"  你的域名：https://tingbao.kdns.fr/output/live.m3u")
        if os.path.exists(os.path.join(OUTPUT_DIR, "all.m3u")):
            print(f"  全量版订阅（all.m3u，原样兜底）：https://raw.githubusercontent.com/{repo}/main/output/all.m3u")
        print(f"  TXT（TVBox 用）：https://raw.githubusercontent.com/{repo}/main/output/live.txt")
    return ok


def is_non_http(url):
    """组播 / rtsp / rtmp 等无法用 HTTP 探测的协议。"""
    low = url.lower()
    return low.startswith(NON_HTTP_PREFIXES)


# ==================== EPG 与图标 ====================

def download_http(url, timeout=30, want=None):
    """下载 URL，返回 bytes 或 None（失败/非 200）。中文路径自动做 URL 编码。"""
    try:
        # 只编码路径中的非 ASCII（保留 / : ? & = % # 等）
        from urllib.parse import quote
        scheme, rest = url.split("://", 1)
        host, path = rest.split("/", 1)
        path = quote(path, safe="/:?&=%#~+")
        url = f"{scheme}://{host}/{path}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                     "Accept": "*/*"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            data = r.read(want) if want else r.read()
            return data
    except Exception:  # noqa: BLE001
        return None


def fetch_github_dir(repo, path, token=None):
    """列 GitHub 仓库某目录，返回 items 列表（公开仓库；带 token 防限流）。"""
    try:
        from urllib.parse import quote
        url = f"https://api.github.com/repos/{repo}/contents/{quote(path)}"
        headers = {"User-Agent": "push-nas"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []


def build_logo_index(token=None):
    """从活跃仓库构建图标索引 {文件名: 镜像下载URL}（只含 .png，跳过不适目录）。"""
    idx = {}
    for repo, prefix in LOGO_INDEX_DIRS:
        items = fetch_github_dir(repo, prefix, token)
        for it in items:
            if not isinstance(it, dict):
                continue
            if it.get("type") == "file" and it["name"].lower().endswith(".png"):
                idx.setdefault(it["name"], LOGO_RAW_MIRROR.format(repo=repo, path=it["path"]))
            elif it.get("type") == "dir" and it["name"] not in SKIP_LOGO_DIRS:
                for f in fetch_github_dir(repo, it["path"], token):
                    if isinstance(f, dict) and f.get("type") == "file" and f["name"].lower().endswith(".png"):
                        idx.setdefault(f["name"], LOGO_RAW_MIRROR.format(repo=repo, path=f["path"]))
    print(f"图标索引：{len(idx)} 个文件名（来源：Meroser/Media_Coll + alantang1977/iptv_api）")
    return idx


def download_epg(url):
    """下载 EPG 节目单到 output/epg.xml。

    支持两种 URL：
      - GitHub API contents（返回 JSON base64）
      - 普通 XML / gz 直链
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    data = download_http(url, timeout=90)
    if not data:
        print("EPG 下载失败，跳过（不影响订阅）。")
        return None
    if "api.github.com/repos" in url:
        try:
            js = json.loads(data.decode("utf-8", "ignore"))
            if isinstance(js, dict) and js.get("content"):
                data = base64.b64decode(js["content"].replace("\n", ""))
        except Exception:  # noqa: BLE001
            pass
    out = os.path.join(OUTPUT_DIR, "epg.xml")
    with open(out, "wb") as f:
        f.write(data)
    print(f"已下载 EPG 节目单 -> output/epg.xml（{len(data)} bytes）")
    return out


def norm_channel(name):
    """频道名清洗：去空格/常见后缀，统一央视写法（CCTV-1 -> CCTV1）。"""
    n = name.strip().replace(" ", "").replace("　", "")
    for s in _NAME_SUFFIXES:
        if n.endswith(s):
            n = n[: -len(s)]
            break
    n = re.sub(r"CCTV[-－](\d+)", r"CCTV\1", n)
    n = re.sub(r"央视(\d+)", r"CCTV\1", n)
    m2 = re.match(r"CCTV\d+\+?", n)
    if m2:
        n = m2.group(0)  # CCTV2财经 -> CCTV2；CCTV5+体育 -> CCTV5+
    return n


def safe_logo_name(name):
    """文件名安全化：只保留中英文、数字、点、下划线、加号。"""
    s = re.sub(r"[^\w\u4e00-\u9fa5.\-+]", "_", name)
    return s


def channel_logo_candidates(name):
    """生成频道图标的候选文件名（按优先级）。"""
    norm = norm_channel(name)
    cands = []
    cands.append(f"{norm}.png")            # Meroser 图标库（活跃，中文/英文名直配）
    cands.append(f"logos{norm}.png")       # alantang1977 图标库（活跃）
    m = re.match(r"CCTV(\d+)(\+?)", norm)
    if m:
        cands.append(f"CCTV{m.group(1)}{m.group(2)}.png")  # Meroser CCTV 目录（活跃，CCTV5+ 等）
    cands.append(f"logos{name.strip()}.png")   # 原样兜底
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def sync_logos(entries, workers=20, token=None):
    """下载频道图标到 output/logo/，返回 {频道名: 图标文件名}。

    先拉取活跃仓库图标索引，再按频道名候选精确匹配下载（不靠试错路径）。
    """
    os.makedirs(LOGO_DIR, exist_ok=True)
    names = list(dict.fromkeys(n for _c, n, _u in entries))
    print(f"== 同步频道图标（{len(names)} 个唯一频道，并发 {workers}）==")
    idx = build_logo_index(token)
    result = {}

    def work(name):
        for cand in channel_logo_candidates(name):
            url = idx.get(cand)
            if not url:
                continue
            fname = safe_logo_name(cand)
            local = os.path.join(LOGO_DIR, fname)
            if os.path.exists(local) and os.path.getsize(local) > 300:
                return fname  # 已下载过
            data = download_http(url, timeout=15)
            if data and len(data) > 300 and (
                    data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8"
                    or data[:4] == b"GIF8"):
                with open(local, "wb") as f:
                    f.write(data)
                return fname
        return None

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, n): n for n in names}
        for f in cf.as_completed(futs):
            n = futs[f]
            try:
                r = f.result()
                if r:
                    result[n] = r
            except Exception:  # noqa: BLE001
                pass
    print(f"图标同步完成：{len(result)}/{len(names)} 个频道有图标（存于 output/logo/）")
    return result


def parse_epg_channels(xml_path):
    """解析 EPG 的 channel 列表，返回 {清洗后的频道名: channel id}。"""
    try:
        text = open(xml_path, encoding="utf-8", errors="ignore").read()
    except Exception:  # noqa: BLE001
        return {}
    ch = {}
    for m in re.finditer(r'<channel\s+id="([^"]+)"[^>]*>(.*?)</channel>', text, re.S):
        cid = m.group(1)
        dm = re.search(r"<display-name[^>]*>([^<]+)</display-name>", m.group(2))
        if dm:
            key = norm_channel(dm.group(1))
            ch.setdefault(key, cid)
    print(f"EPG 解析出 {len(ch)} 个频道的节目单标识")
    return ch


def push_logos(repo, token):
    """增量推送图标：只推本地新增/变化的文件（记录 sha256）。"""
    if not os.path.isdir(LOGO_DIR):
        print("output/logo/ 不存在，跳过图标推送。")
        return 0
    state = {}
    if os.path.exists(LOGO_STATE):
        try:
            state = json.load(open(LOGO_STATE, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    files = [f for f in os.listdir(LOGO_DIR) if f.lower().endswith(".png")]
    changed = 0
    for f in sorted(files):
        p = os.path.join(LOGO_DIR, f)
        data = open(p, "rb").read()
        h = hashlib.sha256(data).hexdigest()
        if state.get(f) == h:
            continue
        try:
            push_file(repo, f"output/logo/{f}", data, token)
            state[f] = h
            changed += 1
            print(f"  图标 {f} ({len(data)} bytes)")
        except Exception as e:  # noqa: BLE001
            print(f"  图标推送失败 {f}: {e}")
    try:
        with open(LOGO_STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
    except Exception:  # noqa: BLE001
        pass
    print(f"图标推送完成：本次变更 {changed} 个。")
    return changed


def write_all_outputs(all_entries, updated, logo_map=None, tvgid_map=None, epg_url=None):
    """把全量原样版本写入 output/all.txt、output/all.m3u（校验时的兜底订阅）。"""
    txt = build_txt(all_entries)
    m3u = build_m3u(all_entries, logo_map=logo_map, tvgid_map=tvgid_map, updated=updated, epg_url=epg_url)
    with open(os.path.join(OUTPUT_DIR, "all.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    with open(os.path.join(OUTPUT_DIR, "all.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write(m3u)
    print(f"已生成 output/all.txt、all.m3u（全量原样 {len(all_entries)} 条，校验时兜底订阅）")


def run_import(src_file, validate, category, logo_map=None, tvgid_map=None, epg_url=None):
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

    logo_map = logo_map or {}
    tvgid_map = tvgid_map or {}
    write_outputs(entries, OUTPUT_DIR, logo_map=logo_map, tvgid_map=tvgid_map,
                  updated=updated, stats=stats, epg_url=epg_url)
    print(f"已生成 output/live.txt、live.m3u、stats.json（订阅版 {len(entries)} 条）")
    if validate:
        write_all_outputs(all_entries, updated, logo_map=logo_map, tvgid_map=tvgid_map, epg_url=epg_url)
    return entries


def main():
    ap = argparse.ArgumentParser(description="飞牛 NAS 直播源自动推送 GitHub")
    ap.add_argument("--file", dest="src_file", metavar="FILE", help="NAS 上直播源文件路径（m3u/txt）")
    ap.add_argument("--latest-dir", dest="src_dir", metavar="DIR",
                    help="自动取目录中最新的 m3u/txt 源文件（文件名字会变时用这个）")
    ap.add_argument("--validate", action="store_true",
                    help="推送前做有效性检测：http(s) 源 20 并发逐个探测，组播/rtsp 自动保留，"
                         "生成有效版 live.* + 全量版 all.*（检测耗时约 30-120 分钟）")
    ap.add_argument("--sync-epg", action="store_true",
                    help="同步最新节目单 EPG 到 output/epg.xml 并推送（订阅头部自动挂载节目单）")
    ap.add_argument("--sync-logo", action="store_true",
                    help="同步最新频道图标到 output/logo/ 并推送（M3U 自动带台标）")
    ap.add_argument("--category", default="", help="统一覆盖分类名（默认保留文件内分组）")
    ap.add_argument("--push-only", action="store_true", help="只推送本地 output/（不导入）")
    ap.add_argument("--no-push", action="store_true", help="只生成，不推送")
    args = ap.parse_args()

    cfg = load_config()
    token = load_token()
    repo = os.environ.get("GITHUB_REPO", DEFAULT_REPO)
    base_url = cfg.get("base_url", DEFAULT_BASE_URL).rstrip("/")
    epg_source = cfg.get("epg_source", EPG_SOURCE)

    src = args.src_file
    if args.src_dir:
        src = pick_latest(args.src_dir)

    # 1) 同步 EPG（生成 m3u 需要节目单频道映射）
    epg_map = None
    if args.sync_epg:
        ep = download_epg(epg_source)
        if ep:
            epg_map = parse_epg_channels(ep)
    elif os.path.exists(os.path.join(OUTPUT_DIR, "epg.xml")):
        epg_map = parse_epg_channels(os.path.join(OUTPUT_DIR, "epg.xml"))

    # 2) 同步图标（需要频道名集合）
    logo_map = {}
    if args.sync_logo:
        if src:
            text = read_source_file(src)
            raw = parse_auto(text)
            if raw:
                logo_map = sync_logos(raw, token=token)
        elif os.path.isdir(LOGO_DIR):
            # push-only 模式：从本地已下载图标建立映射（有图标文件的频道）
            logo_map = {f[:-4].replace("logos", "", 1): f for f in os.listdir(LOGO_DIR) if f.lower().endswith(".png")}
            print(f"使用本地已有图标 {len(logo_map)} 个")
        # 转成完整 URL（台标地址指向你自己的域名）
        logo_map = {k: f"{base_url}/output/logo/{v}" for k, v in logo_map.items()}

    # 3) 生成（用 EPG 映射构建 tvg-id）
    tvgid_map = {}
    if epg_map and src:
        text = read_source_file(src)
        for _c, n, _u in parse_auto(text):
            k = norm_channel(n)
            if k in epg_map:
                tvgid_map[n] = epg_map[k]

    entries = None
    if src:
        entries = run_import(src, args.validate, args.category,
                             logo_map=logo_map, tvgid_map=tvgid_map,
                             epg_url=f"{base_url}/output/epg.xml" if (epg_map or os.path.exists(os.path.join(OUTPUT_DIR, "epg.xml"))) else None)
    elif not args.push_only:
        print("请指定 --file <源文件路径> 或 --latest-dir <源文件目录>，或使用 --push-only。")
        sys.exit(1)

    if args.no_push:
        print("已跳过推送。")
        return

    push_outputs(repo, token)
    if args.sync_logo:
        push_logos(repo, token)


if __name__ == "__main__":
    main()

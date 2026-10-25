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
import time
import json
import os
import re
import sys
import urllib.request
import urllib.parse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPO = "zengzifeng520/china-iptv-sources"
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
LOGO_DIR = os.path.join(OUTPUT_DIR, "logo")
LOGO_STATE = os.path.join(BASE_DIR, ".logo_state.json")  # 已推送图标记录（本地，gitignore）
FILES_TO_PUSH = ["live.txt", "live.m3u", "all.txt", "all.m3u", "stats.json", "epg.xml", "uptime.txt"]

# 组播 / rtsp 等无法用 HTTP 探测的协议：校验时自动保留，不误删
NON_HTTP_PREFIXES = ("rtp://", "udp://", "igmp://", "rtsp://", "rtmp://")

# ===== 数据源 =====
# EPG 节目单（多源，播放器按顺序尝试）：
#   alantang1977/iptv_api（活跃，每天更新，频道 100+，51zmt 免费镜像同源）
#   CCSH/IPTV（活跃聚合 EPG，505 频道，含央视/卫视/地方台/港澳台，输出 e.xml 6.7MB）
#   epg.51zmt.top（国内老牌节目单，完整版需 token，免费版已退化为 alantang 同源）
EPG_SOURCE = "https://ghproxy.net/https://raw.githubusercontent.com/alantang1977/iptv_api/main/output/epg.xml"
# alantang 的 GitHub API 兜底通道（ghproxy 不通时走 contents→blob，需 token）
EPG_ALANTANG_API = "https://api.github.com/repos/alantang1977/iptv_api/contents/output/epg.xml"
# CCSH 大覆盖 EPG 的多条下载通道（jsdelivr gz 优先，体积小；GitHub blob API 兜底）
EPG_CCSH_CHANNELS = [
    "https://cdn.jsdelivr.net/gh/CCSH/IPTV@main/e.xml.gz",
    "https://fastly.jsdelivr.net/gh/CCSH/IPTV@main/e.xml.gz",
    "https://ghproxy.net/https://raw.githubusercontent.com/CCSH/IPTV/main/e.xml.gz",
    "https://api.github.com/repos/CCSH/IPTV/contents/e.xml",  # 最后走 API（需 token，见 download_http_multi）
]
# EPG 频道英文/别名 -> 规范中文（提升匹配率，只在规范名匹配失败时使用）
_EPG_ALIAS = {
    "CCTVNEWS": "CGTN", "CGTNNEWS": "CGTN", "CGTV": "CGTN",
    "BTV国际频道": "BTV国际", "BTV国际": "BTV国际",
    "BRTV新闻": "BTV新闻", "BRTV文艺": "BTV文艺", "BRTV影视": "BTV影视",
    "BRTV体育休闲": "BTV体育", "BRTV生活": "BTV生活", "BRTV财经": "BTV财经",
    "CETV1": "中国教育1台", "CETV2": "中国教育2台", "CETV3": "中国教育3台", "CETV4": "中国教育4台",
    "CHC家庭影院": "CHC家庭影院", "CHC动作电影": "CHC动作电影", "CHC高清电影": "CHC高清电影",
}
# 台标索引（按优先级拉取文件清单，按频道名精确+模糊匹配）：
#   Meroser/Media_Coll TVlogo（活跃，央视+卫视+地方台）
#   fanmingming/live tv（929 个中文台标，最全；台标为静态图片，不受仓库停更影响）
#   Meroser/IPTV TVlogo（活跃，少量补充）
#   alantang1977/iptv_api pic（保留，当前基本为空）
LOGO_INDEX_DIRS = [
    ("Meroser/Media_Coll", "TVlogo"),
    ("fanmingming/live", "tv"),
    ("Meroser/IPTV", "TVlogo"),
    ("alantang1977/iptv_api", "pic"),
]
SKIP_LOGO_DIRS = {"Adult"}  # 跳过不适内容目录
LOGO_RAW_MIRROR = "https://ghproxy.net/https://raw.githubusercontent.com/{repo}/main/{path}"
LOGO_INDEX_CACHE = os.path.join(BASE_DIR, ".logo_index.json")  # 台标索引本地缓存（24h）
LOGO_INDEX_TTL = 24 * 3600
DEFAULT_BASE_URL = "https://zengzifeng.ccwu.cc"  # 订阅/图标/EPG 的对外域名（你自己的 GitHub Pages）

# 频道名常见后缀（清洗时去掉）
_NAME_SUFFIXES = [
    "高清", "超清", "4K", "1080P", "1080", "720P", "标清", "普清", "蓝光",
    "H265", "H264", "HEVC", "AVC", "SDR", "HDR", "HD", "测试", "源",
    "线路", "回看", "轮播", "频道", "直播", "FHD", "UHD", "IPV4", "IPV6",
    "咪咕", "央视",
]
# 频道名常见前缀（清洗时去掉）
_NAME_PREFIXES = ["4K", "8K", "1080P", "1080", "720P", "HD", "UHD", "FHD",
                  "高清", "超清", "标清", "普清", "蓝光", "数字", "数码"]
# 繁转简（频道名常见字，用字典避免长度错位）
_TRAD_MAP = {ord(k): v for k, v in {
    "頻": "频", "劇": "剧", "臺": "台", "體": "体", "國": "国", "視": "视",
    "電": "电", "欄": "栏", "綜": "综", "藝": "艺", "動": "动", "畫": "画",
    "數": "数", "無": "无", "衛": "卫", "經": "经", "濟": "济", "訊": "讯",
    "聞": "闻", "銀": "银", "樂": "乐", "華": "华", "亞": "亚", "東": "东",
    "灣": "湾", "島": "岛", "網": "网", "際": "际", "廣": "广", "價": "价",
    "術": "术", "區": "区", "龍": "龙", "鳳": "凤", "飛": "飞", "蘭": "兰",
    "極": "极", "貴": "贵", "產": "产", "億": "亿", "萬": "万",
    "線": "线", "韓": "韩", "還": "还", "這": "这", "個": "个", "與": "与",
    "於": "于", "時": "时", "間": "间", "開": "开", "發": "发", "節": "节",
    "舊": "旧", "標": "标", "樣": "样", "點": "点", "單": "单", "雙": "双",
    "會": "会", "員": "员", "務": "务", "處": "处", "戰": "战", "爭": "争",
    "號": "号", "軍": "军", "師": "师", "聯": "联", "聲": "声", "愛": "爱",
    "見": "见", "觀": "观", "親": "亲", "夢": "梦", "機": "机", "權": "权",
    "驗": "验", "驚": "惊", "張": "张", "陳": "陈", "劉": "刘", "鳳": "凤",
    "說": "说", "話": "话", "讀": "读", "寫": "写", "車": "车", "馬": "马",
    "鳥": "鸟", "魚": "鱼", "風": "风", "雲": "云", "紅": "红", "綠": "绿",
    "藍": "蓝", "黃": "黄", "雜": "杂", "劇": "剧",
}.items()}

sys.path.insert(0, BASE_DIR)
from iptv_sources.generator import now_str, build_txt, build_m3u, write_outputs  # noqa: E402
from iptv_sources.parser import parse_auto  # noqa: E402
from iptv_sources.validator import check_stream_best_many  # noqa: E402


# ---------- 批量提交（每次 push 都会触发一次 Pages 构建，这里合并成一次） ----------
def _git_blob_sha(data):
    """按 git 规则算 blob sha：sha1("blob <len>\0" + data)"""
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def _gh_api(method, url, token, payload=None, timeout=120):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "nas-batch-push",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


def push_files_batch(repo, files, token, message="chore: 批量更新用户订阅", branch="main"):
    """files: [(path, bytes)]，全部合并进一次 commit。

    只提交内容有变化的文件；都没变则不产生提交（不触发 Pages 构建）。
    返回真正提交的文件数。
    """
    api = "https://api.github.com/repos/%s" % repo
    # 并发时 publish_sub 可能抢先提交，我们的 PATCH 就不是快进 -> 422，整轮重试
    for attempt in range(3):
        try:
            return _push_batch_once(api, repo, files, token, message, branch)
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)
    return 0


def _push_batch_once(api, repo, files, token, message, branch):
    ref = _gh_api("GET", "%s/git/ref/heads/%s" % (api, branch), token)
    base_sha = ref["object"]["sha"]
    base_commit = _gh_api("GET", "%s/git/commits/%s" % (api, base_sha), token)
    base_tree = base_commit["tree"]["sha"]

    # 现有文件 sha（判重，内容没变就不提交）
    existing = {}
    if files:
        d = os.path.dirname(files[0][0])
        try:
            listing = _gh_api("GET", "%s/contents/%s" % (api, urllib.parse.quote(d, safe="/")), token)
            for it in listing:
                existing[it.get("name", "")] = it.get("sha", "")
        except Exception:  # noqa: BLE001
            pass

    entries = []
    for path, data in files:
        name = os.path.basename(path)
        if existing.get(name) == _git_blob_sha(data):
            continue
        blob = _gh_api("POST", "%s/git/blobs" % api, token, {
            "content": base64.b64encode(data).decode("ascii"),
            "encoding": "base64",
        })
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    if not entries:
        return 0

    tree = _gh_api("POST", "%s/git/trees" % api, token,
                   {"base_tree": base_tree, "tree": entries})
    commit = _gh_api("POST", "%s/git/commits" % api, token, {
        "message": message, "tree": tree["sha"], "parents": [base_sha],
    })
    _gh_api("PATCH", "%s/git/refs/heads/%s" % (api, branch), token, {"sha": commit["sha"]})
    return len(entries)


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


def api_url(repo, path):
    """构造 Contents API URL（路径含中文文件名，需要 URL 编码）。"""
    from urllib.parse import quote
    return f"https://api.github.com/repos/{repo}/contents/{quote(path, safe='/')}"


def get_sha(repo, path, token):
    url = api_url(repo, path)
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
    url = api_url(repo, path)
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


def delete_file(repo, path, token):
    """删除 GitHub 上的文件（Contents API DELETE）。"""
    sha = get_sha(repo, path, token)
    if not sha:
        return False
    payload = {"message": f"chore: NAS 到期删除 {path}", "sha": sha}
    url = api_url(repo, path)
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="DELETE",
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "push-nas",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        json.loads(r.read().decode("utf-8"))
    return True


def _inject_extra_blocks(data):
    """把央视频/网络直播等外部源放在订阅最前面；失败时原样返回。"""
    try:
        from extra_source import get_extra_blocks
        blocks = get_extra_blocks()
    except Exception as ex:  # noqa: BLE001
        print(f"外部源注入失败: {ex}")
        return data
    if not blocks:
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", "replace")
    lines = text.splitlines()
    if not lines or not lines[0].upper().startswith("#EXTM3U"):
        return data
    out = [lines[0]]
    # 合并外部块 + 原有条目，统一按 group_order.txt 排序
    pairs = [(e, u) for e, u in blocks]
    pend = None
    for ln in lines[1:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            pend = s
            continue
        if s.startswith("#"):
            continue
        if pend is not None:
            pairs.append((pend, s))
            pend = None
    try:
        from group_sort import sort_pairs
        pairs = sort_pairs(pairs)
    except Exception as ex:  # noqa: BLE001
        print(f"分组排序跳过: {ex}")
    for extinf, url in pairs:
        out.append(extinf)
        out.append(url)
    return ("\n".join(out) + "\n").encode("utf-8")


def sync_user_files(repo, token, base_url=""):
    """为每个用户生成专属订阅文件并推送 GitHub；到期的用户文件自动删除。

    专属文件：output/users/<用户名>_<到期日期>.m3u（内容 = 当前 live.m3u）
    订阅地址：https://<域名>/output/users/<用户名>_<到期日期>.m3u（以 .m3u 结尾）
    到期：文件从 GitHub 删除，播放器 404 打不开。
    续期/改名：自动删除旧文件（用 users.json 里记录的 last_file）。
    """
    import time as _t
    users_f = os.path.join(BASE_DIR, "users.json")
    if not os.path.exists(users_f):
        return
    try:
        users = json.load(open(users_f, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    # 注意：即使用户为空也要继续，后面的孤儿清理需要执行
    live_path = os.path.join(OUTPUT_DIR, "live.m3u")
    if not os.path.exists(live_path):
        print("output/live.m3u 不存在，跳过用户专属文件同步。")
        return
    data = _inject_extra_blocks(open(live_path, "rb").read())
    base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
    now = _t.time()
    users_dir = os.path.join(OUTPUT_DIR, "users")
    os.makedirs(users_dir, exist_ok=True)
    print(f"== 多人订阅（{len(users)} 个用户）==")
    active = 0
    changed = False
    wanted = set()
    for name, info in sorted(users.items()):
        expire_date = str(info.get("expire", ""))
        e = str(info.get("ts", ""))
        token_id = str(info.get("token", "") or "")
        if not expire_date or not e or not token_id:
            continue
        cur_file = f"u_{token_id}.m3u"
        gh_path = f"output/users/{cur_file}"
        if int(e) < now:
            # 已到期 -> 删除专属文件，订阅立即失效
            try:
                if delete_file(repo, gh_path, token):
                    print(f"  [{name}] 已到期（{expire_date}），{cur_file} 已删除 -> 订阅失效")
                else:
                    print(f"  [{name}] 已到期（{expire_date}），GitHub 无该文件")
            except Exception as ex:  # noqa: BLE001
                print(f"  [{name}] 到期删除失败: {ex}")
            continue
        # 清理历史命名（name_日期.m3u）留下的旧文件
        legacy = str(info.get("last_file", "") or "")
        if legacy and legacy != cur_file:
            try:
                delete_file(repo, f"output/users/{legacy}", token)
            except Exception:
                pass
        local = os.path.join(users_dir, cur_file)
        with open(local, "wb") as f:
            f.write(data)
        try:
            push_file(repo, gh_path, data, token)
            active += 1
            wanted.add(cur_file)
            info["last_file"] = cur_file
            changed = True
            print(f"  [{name}] 到期 {expire_date} -> {base_url}/output/users/{cur_file}")
        except Exception as ex:  # noqa: BLE001
            print(f"  [{name}] 推送失败: {ex}")

    # 清理孤儿文件：用户被删除 / 换了 token 后遗留的 .m3u
    try:
        import urllib.request as _u
        url = f"https://api.github.com/repos/{repo}/contents/output/users"
        req = _u.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "push-nas"})
        with _u.urlopen(req, timeout=20) as r:
            items = json.loads(r.read().decode("utf-8"))
        for it in items:
            fname = it.get("name", "")
            if fname.endswith(".m3u") and fname not in wanted:
                try:
                    delete_file(repo, f"output/users/{fname}", token)
                    print(f"  [清理] 删除孤儿订阅 {fname}")
                except Exception:
                    pass
    except Exception:
        pass

    if changed:
        try:
            with open(users_f, "w", encoding="utf-8") as _f:
                json.dump(users, _f, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass
    print(f"多人订阅同步完成：生效 {active} 个用户。")


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
        print(f"  你的域名：https://zengzifeng.ccwu.cc/output/live.m3u")
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
        # 只编码路径中的非 ASCII（保留 / : ? & = % # 等；冒号必须保留——ghproxy 内嵌 URL）
        from urllib.parse import quote
        scheme, rest = url.split("://", 1)
        host, path = rest.split("/", 1)
        path = quote(path, safe="/:?&=%#~+:")  # 冒号必须保留（ghproxy 内嵌 https:// URL）
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


def build_logo_index(token=None, force=False):
    """从台标仓库构建索引 {文件名: 镜像下载URL}。

    结果缓存到本地 .logo_index.json（24 小时有效），避免每次推送都请求 GitHub API。
    """
    import time
    if not force and os.path.exists(LOGO_INDEX_CACHE):
        try:
            age = time.time() - os.path.getmtime(LOGO_INDEX_CACHE)
            if age < LOGO_INDEX_TTL:
                with open(LOGO_INDEX_CACHE, encoding="utf-8") as f:
                    cached = json.load(f)
                if cached:
                    print(f"图标索引：{len(cached)} 个文件名（本地缓存，{int(age // 60)} 分钟前更新）")
                    return cached
        except Exception:  # noqa: BLE001
            pass
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
    try:
        with open(LOGO_INDEX_CACHE, "w", encoding="utf-8") as f:
            json.dump(idx, f, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass
    print(f"图标索引：{len(idx)} 个文件名（Meroser/Media_Coll + fanmingming/live + Meroser/IPTV + alantang1977）")
    return idx


def norm_logo_key(s):
    """台标文件名规范化：与频道名清洗对齐（繁转简/去括号/去空格等）。"""
    base = os.path.splitext(s)[0]
    return norm_channel(base).lower()


def match_fuzzy_logo(norm_name, idx_keys_set, min_len=4):
    """模糊匹配：纯中文频道名逐步去尾字，命中台标索引（避免数字台误配）。"""
    n = norm_name
    if re.search(r"[A-Za-z0-9]", n):
        return None  # 含英文/数字的走精确候选，避免 CCTV5+ 误配到 CCTV5
    for L in range(len(n), min_len - 1, -1):
        if n[:L] in idx_keys_set:
            return n[:L]
    return None


def download_http_multi(urls, timeout=90, token=None):
    """多通道下载：依次尝试 urls，返回 bytes 或 None。

    通道示例：jsdelivr（gz）→ fastly → ghproxy → GitHub blob API（带 token）。
    """
    for url in urls:
        if "api.github.com" in url:
            continue  # API 通道走下面的专用逻辑
        data = download_http(url, timeout=timeout)
        if data:
            return data
    # GitHub API 兜底：contents 拿 sha → blob raw 下载（带 token，避免限流；raw 省 base64 膨胀）
    if token:
        m2 = re.match(r"https://api\.github\.com/repos/([^/]+/[^/]+)/contents/(.+)", urls[-1]) if urls else None
        if m2:
            try:
                from urllib.parse import quote
                repo = m2.group(1)
                path = quote(m2.group(2), safe="/")
                cj = download_github_api(f"https://api.github.com/repos/{repo}/contents/{path}", token, timeout=60)
                sha = (cj or {}).get("sha")
                if sha:
                    for attempt in range(3):
                        data = download_github_raw(
                            f"https://api.github.com/repos/{repo}/git/blobs/{sha}", token, timeout=180)
                        if data:
                            return data
            except Exception:  # noqa: BLE001
                pass
    return None


def download_github_api(url, token, timeout=60):
    """GitHub API JSON 请求（带 token）。"""
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "User-Agent": "push-nas",
                     "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None


def download_github_raw(url, token, timeout=180):
    """GitHub API blob 原始字节下载（Accept raw，带重试）。"""
    import time
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {token}",
                         "User-Agent": "push-nas",
                         "Accept": "application/vnd.github.raw+json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            print(f"  blob 下载重试 {attempt + 1}: {e}")
            time.sleep(3)
    return None


def merge_epg_files(src_paths, out_path):
    """合并多个 EPG XML：channel id 统一为主 display-name（优先中文），节目按频道合并去重。

    src_paths: 本地的 EPG 文件路径列表（可能含 .gz，自动解压）。
    返回合并后的频道数；失败返回 0。
    """
    import gzip

    channels = {}   # name -> True（去重）
    progs = {}      # name -> { (start, title): attrs+body }
    src_count = 0
    for p in src_paths:
        if not p or not os.path.exists(p):
            continue
        try:
            raw = open(p, "rb").read()
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            text = raw.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            print(f"  读取 EPG 失败 {p}: {e}")
            continue
        src_count += 1
        # channel: id -> display-names
        id2names = {}
        for m in re.finditer(r'<channel\s+id="([^"]+)"[^>]*>(.*?)</channel>', text, re.S):
            cid = m.group(1)
            dns = re.findall(r"<display-name[^>]*>([^<]+)</display-name>", m.group(2))
            if dns:
                id2names[cid] = dns
        # programme: 按 channel 属性归到主名
        prog_blocks = re.findall(r"<programme\s+([^>]*)>(.*?)</programme>", text, re.S)
        for attrs, body in prog_blocks:
            cm = re.search(r'channel="([^"]*)"', attrs)
            if not cm or cm.group(1) not in id2names:
                continue
            nm = main_display_name(id2names[cm.group(1)])
            if not nm:
                continue
            channels[nm] = True
            sm = re.search(r'start="([^"]*)"', attrs)
            tm = re.search(r'<title[^>]*>([^<]*)</title>', body)
            key = (sm.group(1) if sm else "", tm.group(1) if tm else "")
            progs.setdefault(nm, {})[key] = (attrs, body)
    if not channels:
        print("合并 EPG：没有任何可用的节目单数据。")
        return 0
    # 写合并文件
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<tv generator-info-name="china-iptv-sources merged EPG">']
    for nm in sorted(channels):
        out.append(f'  <channel id="{esc_xml(nm)}">')
        out.append(f'    <display-name lang="zh">{esc_xml(nm)}</display-name>')
        out.append("  </channel>")
    for nm in sorted(progs):
        for key in sorted(progs[nm]):
            attrs, body = progs[nm][key]
            attrs2 = re.sub(r'channel="[^"]*"', f'channel="{esc_xml(nm)}"', attrs, count=1)
            out.append(f"  <programme {attrs2}>{body}</programme>")
    out.append("</tv>")
    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out))
    print(f"合并 EPG 完成：{len(channels)} 个频道、{sum(len(v) for v in progs.values())} 条节目"
          f"（来自 {src_count} 个源）-> {out_path}")
    return len(channels)


def main_display_name(dns):
    """取频道主显示名：优先中文名，否则第一个。"""
    for d in dns:
        if re.search(r"[\u4e00-\u9fff]", d):
            return d
    return dns[0] if dns else ""


def esc_xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def download_epg(epg_source, token=None):
    """下载并合并多源 EPG 节目单到 output/epg.xml。

    源 1：alantang（101 频道，51zmt 免费同源）
    源 2：CCSH 聚合（505 频道，央视/卫视/地方台/港澳台）
    合并后 channel id 统一为主显示名（中文优先），与订阅 tvg-id 精确一致。
    """
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    tmp_dir = os.path.join(BASE_DIR, ".epg_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    local = []
    # 源 1：alantang（ghproxy → GitHub API 兜底）
    d1 = download_http_multi([epg_source, EPG_ALANTANG_API], timeout=90, token=token)
    if d1:
        p1 = os.path.join(tmp_dir, "alantang.xml")
        with open(p1, "wb") as f:
            f.write(d1)
        local.append(p1)
        print(f"已下载 EPG 源1 alantang（{len(d1)} bytes）")
    else:
        print("EPG 源1 alantang 下载失败。")
    # 源 2：CCSH（多通道，gz 自动解压）
    d2 = download_http_multi(EPG_CCSH_CHANNELS, timeout=120, token=token)
    if d2:
        p2 = os.path.join(tmp_dir, "ccsh.xml.gz" if d2[:2] == b"\x1f\x8b" else "ccsh.xml")
        with open(p2, "wb") as f:
            f.write(d2)
        local.append(p2)
        print(f"已下载 EPG 源2 CCSH（{len(d2)} bytes）")
    else:
        print("EPG 源2 CCSH 下载失败，将只使用源1。")
    if not local:
        print("EPG 全部下载失败，跳过（不影响订阅）。")
        return None
    out = os.path.join(OUTPUT_DIR, "epg.xml")
    n = merge_epg_files(local, out)
    if not n:
        print("EPG 合并失败，跳过。")
        return None
    return out


def norm_channel(name):
    """频道名清洗：繁转简、删括号、去前后缀、统一央视/CGTN 写法（CCTV-1 -> CCTV1）。"""
    n = name.strip().replace(" ", "").replace("　", "")
    n = n.translate(_TRAD_MAP)
    n = re.sub(r"[（(][^（()）]*[)）]", "", n)  # 删除括号及其内容（翡翠综合台(北美) -> 翡翠综合台）
    for p in _NAME_PREFIXES:
        if n.startswith(p):
            n = n[len(p):]
            break
    for s in _NAME_SUFFIXES:
        if n.endswith(s):
            n = n[: -len(s)]
            break
    n = re.sub(r"[•·]*(线路\d*|源\d*)$", "", n)  # 尾部 线路1/源2 等编号
    n = re.sub(r"^\d+(?=[A-Za-z\u4e00-\u9fff])", "", n)  # 前导编号（200TVBS -> TVBS；2026-10-19 不动）
    n = re.sub(r"CCTV[-－]0*(\d+)", r"CCTV\1", n)  # CCTV-01 -> CCTV1
    n = re.sub(r"央视0*(\d+)", r"CCTV\1", n)
    n = re.sub(r"^CGT(?!N)", "CGTN", n)  # CGT法语 -> CGTN法语
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
    if norm.startswith("TVBS"):
        cands += ["TVBS.png", "TVBS新闻台.png", "TVBS欢乐台.png", "TVBS亚洲台.png"]
    cands.append(f"logos{name.strip()}.png")   # 原样兜底
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def sync_logos(entries, workers=20, token=None):
    """下载频道图标到 output/logo/，返回 {频道名: 图标文件名}。

    匹配链：精确候选（含 CCTV 变体）→ 规范化精确（空格/大小写/繁简）→
           子串包含（如 Now剧集 -> Now剧集台）→ 纯中文去尾字模糊。
    """
    os.makedirs(LOGO_DIR, exist_ok=True)
    names = list(dict.fromkeys(n for _c, n, _u in entries))
    print(f"== 同步频道图标（{len(names)} 个唯一频道，并发 {workers}）==")
    idx = build_logo_index(token)
    idx_norm = {norm_logo_key(k): k for k in idx}  # 规范化文件名 -> 原始文件名
    idx_norm_items = list(idx_norm.items())
    result = {}

    def _save(name, orig):
        fname = safe_logo_name(orig)
        local = os.path.join(LOGO_DIR, fname)
        if os.path.exists(local) and os.path.getsize(local) > 300:
            return fname
        data = download_http(idx[orig], timeout=15)
        if data and len(data) > 300 and (
                data[:8] == b"\x89PNG\r\n\x1a\n" or data[:2] == b"\xff\xd8"
                or data[:4] == b"GIF8"):
            with open(local, "wb") as f:
                f.write(data)
            return fname
        return None

    def work(name):
        # 1) 精确候选（频道名清洗后直配，含 CCTV/卫视等变体）
        for cand in channel_logo_candidates(name):
            if cand in idx:
                r = _save(name, cand)
                if r:
                    return r
        # 2) 规范化精确（TVB DRAMA -> tvbdrama；繁简/大小写/空格差异）
        nk = norm_logo_key(name)
        if len(nk) >= 3 and nk in idx_norm:
            r = _save(name, idx_norm[nk])
            if r:
                return r
        # 3) 子串包含（长度差 <= 3，防误配）：Now剧集 -> Now剧集台 / HBO原创 -> HBO原创巨献
        if len(nk) >= 4:
            for kk, orig in idx_norm_items:
                if kk == nk:
                    continue
                if (kk in nk or nk in kk) and abs(len(kk) - len(nk)) <= 3:
                    r = _save(name, orig)
                    if r:
                        return r
        # 4) 纯中文去尾字模糊（湖南卫视高清 -> 湖南卫视）
        hit = match_fuzzy_logo(nk, set(idx_norm))
        if hit:
            r = _save(name, idx_norm[hit])
            if r:
                return r
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
    """解析 EPG 的 channel 列表。

    返回 (map, norm_set)：
      map: {规范化频道名: 原 display-name}（与合并后 epg.xml 的 channel id 一致）
      norm_set: 规范化名集合（供匹配链使用）
    """
    try:
        text = open(xml_path, encoding="utf-8", errors="ignore").read()
    except Exception:  # noqa: BLE001
        return {}, set()
    ch = {}
    for m in re.finditer(r'<channel\s+id="([^"]+)"[^>]*>(.*?)</channel>', text, re.S):
        cid = m.group(1)
        dm = re.search(r"<display-name[^>]*>([^<]+)</display-name>", m.group(2))
        if dm:
            dn = dm.group(1)
            key = norm_channel(dn)
            if key:
                ch.setdefault(key, dn)
    print(f"EPG 解析出 {len(ch)} 个频道的节目单标识（合并后频道 id 统一为频道名）")
    return ch, set(ch)


def match_epg_id(channel_name, epg_map, epg_norm_set):
    """频道名 -> EPG channel id（display-name 原样）的四级匹配。

    1) 规范化精确（CCTV-1 高清 -> CCTV1；繁简/括号/前后缀已清洗）
    2) 别名映射（CCTVNEWS -> CGTN；BRTV影视 -> 北京影视）
    3) 子串包含（长度差 <= 3，防误配）：Now剧集 -> Now剧集台
    4) 纯中文去尾字模糊（湖南卫视高清 -> 湖南卫视）
    未命中返回 None（调用方回退为规范化频道名）。
    """
    n = norm_channel(channel_name)
    if not n:
        return None
    if n in epg_norm_set:
        return epg_map[n]
    # 别名（整名 + 前缀展开：BRTV体育休闲 -> 北京体育休闲）
    if n in _EPG_ALIAS:
        a = norm_channel(_EPG_ALIAS[n])
        if a in epg_norm_set:
            return epg_map[a]
    # 去掉"台/频道/卫视"尾字再试（BRTV新闻HD -> BRTV新闻）
    for tail in ("频道", "电视台", "卫视", "台"):
        if n.endswith(tail) and len(n) > len(tail):
            t = n[: -len(tail)]
            if t in epg_norm_set:
                return epg_map[t]
    # 子串包含
    if len(n) >= 4:
        for k in epg_norm_set:
            if k == n:
                continue
            if (k in n or n in k) and abs(len(k) - len(n)) <= 3:
                return epg_map[k]
    # 纯中文去尾字模糊
    hit = match_fuzzy_logo(n, epg_norm_set)
    if hit:
        return epg_map[hit]
    return None


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


def load_group_logos(base_url, output_dir=None, config=None):
    """构建 {分组名: group-logo URL}。

    分组图标文件名用英文/拼音（GitHub Pages 对中文文件名访问不稳定会 404），
    文件名映射见 config.json 的 group_icons。图标由部署包自带（output/logo/groups/）。"""
    config = config or {}
    mapping = config.get("group_icons") or {}
    mapping = {k: v for k, v in mapping.items() if not k.startswith("_")}
    gl = {}
    for group_name, fname in mapping.items():
        gl[group_name] = f"{base_url}/output/logo/groups/{urllib.parse.quote(fname)}.png"
    return gl


def write_all_outputs(all_entries, updated, logo_map=None, tvgid_map=None, epg_url=None, up_url=None, group_logo_map=None):
    """把全量原样版本写入 output/all.txt、output/all.m3u（校验时的兜底订阅）。"""
    txt = build_txt(all_entries, up_url=up_url, updated=updated)
    m3u = build_m3u(all_entries, logo_map=logo_map, tvgid_map=tvgid_map, updated=updated, epg_url=epg_url, up_url=up_url, group_logo_map=group_logo_map)
    with open(os.path.join(OUTPUT_DIR, "all.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    with open(os.path.join(OUTPUT_DIR, "all.m3u"), "w", encoding="utf-8", newline="\n") as f:
        f.write(m3u)
    print(f"已生成 output/all.txt、all.m3u（全量原样 {len(all_entries)} 条，校验时兜底订阅）")


def run_import(src_file, validate, category, logo_map=None, epg_map=None, epg_url=None, up_url=None, group_logo_map=None):
    # 订阅里的 live_url 需要对外域名（原来只在 main() 里定义，导致 NameError）
    base_url = (load_config().get("base_url") or DEFAULT_BASE_URL).rstrip("/")
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
        http_items = [(i, u) for i, (c, n, u) in enumerate(entries)
                      if u.lower().startswith(("http://", "https://"))]
        urls = list(dict.fromkeys(u for _, u in http_items))  # 去重：同一地址可能挂在多个频道下
        print(f"== 有效性检测（--validate，去重后 {len(urls)} 个唯一地址，48 并发，每条超时 6 秒）==")
        non_http_count = len(entries) - len(urls)
        print(f"待检测 http(s) 源 {len(urls)} 条；组播/rtsp 等 {non_http_count} 条自动保留（不误删）")
        best_map = check_stream_best_many(urls, timeout=10, workers=64, retries=1)
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
    # tvg-id：频道名四级匹配到合并 EPG 的 channel id（display-name 原样），
    # 未命中回退为规范化频道名。EPG 覆盖到的频道播放器 100% 能对上节目单。
    if isinstance(epg_map, tuple) and len(epg_map) == 2:
        epg_map_norm, epg_norm_set = epg_map
    else:
        epg_map_norm = epg_map or {}
        epg_norm_set = set(epg_map_norm)
    tvgid_map = {}
    for _c, n, _u in entries:
        hit = match_epg_id(n, epg_map_norm, epg_norm_set)
        tvgid_map[n] = hit or norm_channel(n) or n
    write_outputs(entries, OUTPUT_DIR, logo_map=logo_map, tvgid_map=tvgid_map,
                  updated=updated, stats=stats, epg_url=epg_url, up_url=up_url,
                  group_logo_map=group_logo_map, live_url=f"{base_url}/output/live.m3u")
    print(f"已生成 output/live.txt、live.m3u、uptime.txt、stats.json（订阅版 {len(entries)} 条）")
    # 并入外部源（电信组播/网络直播）并按 group_order_main.txt 排序
    try:
        from group_sort import merge_live_files
        _c, _n = merge_live_files(OUTPUT_DIR)
        print(f"主列表归并：外部源 {_n} 条，改写 {_c} 个文件")
    except Exception as _e:  # noqa: BLE001
        print(f"主列表归并跳过：{_e}")
    if validate:
        write_all_outputs(all_entries, updated, logo_map=logo_map, tvgid_map=tvgid_map, epg_url=epg_url, up_url=up_url, group_logo_map=group_logo_map)
    return entries


def main():
    ap = argparse.ArgumentParser(description="飞牛 NAS 直播源自动推送 GitHub")
    ap.add_argument("--file", dest="src_file", metavar="FILE", help="NAS 上直播源文件路径（m3u/txt）")
    ap.add_argument("--latest-dir", dest="src_dir", metavar="DIR",
                    help="自动取目录中最新的 m3u/txt 源文件（文件名字会变时用这个）")
    ap.add_argument("--validate", action="store_true",
                    help="推送前做有效性检测：http(s) 源去重后 48 并发逐个探测，组播/rtsp 自动保留，"
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

    # 1) 同步 EPG（多源合并：alantang + CCSH；生成 m3u 需要节目单频道映射）
    epg_map = None
    if args.sync_epg:
        ep = download_epg(epg_source, token=token)
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
        # 转成完整 URL（台标地址指向你自己的域名；文件名含中文时做百分号编码，
        # 否则播放器请求中文 URL 失败会导致台标不显示）
        logo_map = {k: f"{base_url}/output/logo/{urllib.parse.quote(v)}" for k, v in logo_map.items()}

    # 3) 生成（EPG 映射用于 tvg-id，自建合并节目单挂载头部）
    entries = None
    if src:
        epg_urls = []
        if epg_map or os.path.exists(os.path.join(OUTPUT_DIR, "epg.xml")):
            epg_urls.insert(0, f"{base_url}/output/epg.xml")  # 自托管合并 EPG（channel id 已统一为频道名）
        if not epg_urls:
            epg_urls = [EPG_SOURCE]  # 兜底
        group_logo_map = load_group_logos(base_url, config=cfg)
        entries = run_import(src, args.validate, args.category,
                             logo_map=logo_map, epg_map=epg_map,
                             epg_url=epg_urls, up_url="http://116.230.212.78:8222/rtp/233.18.204.168:5140",
                             group_logo_map=group_logo_map)
    elif not args.push_only:
        print("请指定 --file <源文件路径> 或 --latest-dir <源文件目录>，或使用 --push-only。")
        sys.exit(1)

    if args.no_push:
        print("已跳过推送。")
        return

    push_outputs(repo, token)
    sync_user_files(repo, token, base_url)
    if args.sync_logo:
        push_logos(repo, token)


if __name__ == "__main__":
    main()

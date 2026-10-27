#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions 版订阅管线 —— 替代 NAS 上的 sync_users + extra_source + local_sub。

每轮执行：
  1. 拉 IPTVZB 巡检列表 → 每频道≤20 裁剪 → output/top20.m3u
  2. 探测广东中转池 → 选能连通的最快 N 台 → output/nas.m3u（每频道 N 条）
  3. 合并三源（全量组播 + 网络直播 + 广东电信）→ 分组排序/网络直播重组/每频道封顶
     → 7 个用户订阅 output/users/u_*.m3u
  4. 批量提交（一次 commit 推全部文件，减少 Pages 构建）

环境变量：
  GH_TOKEN  GitHub token（Actions secrets 注入，或本地运行时读取）
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = "Mozilla/5.0 (compatible; github-pipeline/1.0)"
CST = timezone(timedelta(hours=8))
BASE = os.path.dirname(os.path.abspath(__file__))   # .../github
ROOT = os.path.dirname(BASE)                          # repo root
OUT = os.path.join(ROOT, "output")

sys.path.insert(0, ROOT)
import logo_source  # noqa: E402  台标统一查 ZZF 仓库的台标库

REPO = "zengzifeng520/china-iptv-sources"
API = "https://api.github.com/repos/" + REPO

SRC_CHECKED = "https://zzf.new.tg/checked/live.m3u"
SRC_NETLIVE = "https://mpimg.cn/down.php/8a7c70ba5d709b2876b54c63d0acda08.txt"

GROUP_ORDER = [
    "订阅信息", "推送时间", "咪咕直播", "电信组播", "央视频道", "卫视频道",
    "地方频道", "4K节目", "广东频道", "深圳频道", "上海节目", "浙江节目",
    "重庆节目", "湖南节目", "港台节目", "四川节目", "北京节目", "江苏节目",
    "福建节目", "中数传媒", "IPTV", "其他频道",
    "经典轮播", "明星剧场", "虎牙一起看", "YY轮播", "历届春晚",
]
PROMO_URL = "https://zengzifeng.ccwu.cc/output/media/promo.mp4"
TOP_PER_CHANNEL = 20
LINES = 3
NL_CAT = {"央视": 0, "卫视": 1, "广东": 2, "深圳": 3, "地区": 4}
GROUP_RE = re.compile(r'group-title="([^"]*)"')
HOST_RE = re.compile(r"https?://([^/\s]+)/(rtp|udp)/", re.I)

GROUP_EMOJI = {
    "订阅信息": "ℹ️", "推送时间": "🕒", "咪咕直播": "🎯", "经典轮播": "🎞️",
    "明星剧场": "⭐", "虎牙一起看": "🐯", "YY轮播": "🎤", "历届春晚": "🎆", "电信组播": "🌐",
    "央视频道": "📺", "卫视频道": "📡", "地方频道": "🏙️", "地区频道": "🗺️",
    "4K节目": "🎬", "中数传媒": "💠", "其他频道": "📌", "IPTV": "🌍",
    "广东频道": "🌴", "深圳频道": "🌆", "上海节目": "🌃", "浙江节目": "🌊",
    "江苏节目": "🌸", "四川节目": "🐼", "重庆节目": "🏔️", "山东节目": "⛰️",
    "湖南节目": "🎤", "福建节目": "🏝️", "港台节目": "🎌", "北京节目": "🏯",
    "系统": "⚙️",
}


def with_emoji(extinf_line):
    """给 group-title 加 Emoji 前缀（按基础组名匹配，已加过则跳过）。"""
    m = GROUP_RE.search(extinf_line)
    if not m:
        return extinf_line
    g = m.group(1)
    for base, emoji in GROUP_EMOJI.items():
        if g == base or g.startswith(base):
            if g.startswith(emoji):
                return extinf_line
            return extinf_line.replace('group-title="%s"' % g,
                                       'group-title="%s %s"' % (emoji, g), 1)
    return extinf_line


def log(msg):
    print(msg, flush=True)


def fetch(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def load_json(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def natkey(s):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


GUESS_LOGO = "https://gcore.jsdelivr.net/gh/yuanzl77/TVlogo@master/png/%s.png"


def extinf(group, name):
    """统一生成 EXTINF 行，自动带 tvg-id/tvg-name/tvg-logo（便于 EPG 匹配）。

    台标优先查 ZZF 的台标库（logo_source），查不到才退回旧的猜文件名
    —— 那个猜法实测绝大多数 404，只能当兜底。
    """
    nm = (name or "").strip()
    logo = logo_source.resolve(nm)
    # 查不到就不写 tvg-logo：以前这里是拼 yuanzl77 的文件名，实测几乎全是 404，
    # 播放器拿到的是裂图，还不如让它显示默认图标。
    attr = f' tvg-logo="{logo}"' if logo else ""
    return f'#EXTINF:-1 tvg-id="{nm}" tvg-name="{nm}"{attr} group-title="{group}",{nm}'


def name_of(extinf):
    return (extinf or "").rsplit(",", 1)[-1].strip()


# 轮播类独立订阅（并入主订阅，但来源不进巡检）：txt 的 name,url 格式
EXTRA_LISTS = [
    ("crawl/extra/jingdian.txt", "经典轮播"),
    ("crawl/extra/mingxing.txt", "明星剧场"),
    ("crawl/extra/huya.txt", "虎牙一起看"),
    ("crawl/extra/yy.txt", "YY轮播"),
    ("crawl/extra/chunwan.txt", "历届春晚"),
]


def load_extra_blocks():
    """读 crawl/extra/*.txt，生成 (extinf, url) 块；分组名见 EXTRA_LISTS。"""
    blocks = []
    for rel, group in EXTRA_LISTS:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        for raw in open(path, encoding="utf-8", errors="replace"):
            s = raw.strip()
            if not s or s.endswith("#genre#") or "://" not in s:
                continue
            name, url = s.rsplit(",", 1)
            name, url = name.strip(), url.strip()
            if name and url.startswith("http"):
                blocks.append((extinf(group, name), url))
    return blocks


def rank_of(group):
    g = group.strip()
    for i, k in enumerate(GROUP_ORDER):
        if g == k or g.startswith(k):
            return i
    return len(GROUP_ORDER)


# ---------- 解析 ----------
def parse_m3u(text):
    """[(extinf, url)]"""
    out, pend = [], None
    for ln in text.replace("\r", "").split("\n"):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            pend = s
            continue
        if s.startswith("#") or not s.startswith("http"):
            continue
        if pend:
            out.append((pend, s))
            pend = None
    return out


def parse_netlive_txt(text):
    """网络直播源是 txt：name,url，按行；忽略 #genre# 分组行。"""
    out = []
    for ln in text.replace("\r", "").split("\n"):
        s = ln.strip()
        if not s or s.startswith("#") or "," not in s:
            continue
        name, _, url = s.partition(",")
        name = name.strip()
        url = url.strip()
        if name and url.startswith("http"):
            out.append((extinf("网络直播", name), url))
    return out


def trim_top(blocks, n):
    """每频道最多 n 条（保序）。"""
    seen, out = {}, []
    for e, u in blocks:
        nm = name_of(e)
        if seen.get(nm, 0) >= n:
            continue
        seen[nm] = seen.get(nm, 0) + 1
        out.append((e, u))
    return out


# ---------- 网络直播重组 ----------
def _nl_cat(name):
    u = name.upper()
    if "CCTV" in u:
        return "央视"
    if "深圳" in name:
        return "深圳"
    if "广东" in name or name.startswith("珠江") or name.startswith("南方"):
        return "广东"
    if "卫视" in name:
        return "卫视"
    return "地区"


def _norm_name(name):
    n = name.strip()
    for op, cl in (("(", ")"), ("（", "）"), ("【", "】")):
        if n.endswith(cl):
            i = n.rfind(op)
            if i > 0:
                n = n[:i].strip()
    return n


def reorganize_netlive(blocks):
    """网络直播：同名（去后缀）归并成多线路，组名保持「网络直播」。"""
    net, rest = [], []
    for e, u in blocks:
        (net if 'group-title="网络直播"' in e else rest).append((e, u))
    if not net:
        return blocks
    merged, order = {}, []
    for e, u in net:
        nm = _norm_name(name_of(e))
        if not nm:
            rest.append((e, u))
            continue
        if nm not in merged:
            merged[nm] = []
            order.append(nm)
        merged[nm].append(u)
    out = []
    for nm in order:
        for u in merged[nm]:
            out.append((extinf("网络直播", nm), u))
    return rest + out


def reorder_netlive_run(blocks):
    """把连续的「网络直播」块按 央视→卫视→广东→深圳→地区（再按名字）重排。"""
    out, i, n = [], 0, len(blocks)
    while i < n:
        if 'group-title="网络直播"' in blocks[i][0]:
            j = i
            while j < n and 'group-title="网络直播"' in blocks[j][0]:
                j += 1
            run = sorted(blocks[i:j],
                         key=lambda p: (NL_CAT.get(_nl_cat(_norm_name(name_of(p[0]))), 9),
                                        natkey(_norm_name(name_of(p[0])))))
            out += run
            i = j
        else:
            out.append(blocks[i])
            i += 1
    return out


# ---------- 广东中转探测 ----------
def _probe(host, mode, mcast, secs=5):
    url = "http://%s/%s/%s" % (host, mode, mcast)
    try:
        r = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time", str(secs),
                            "-w", "%{size_download}", "-A", UA, url],
                           capture_output=True, text=True, timeout=secs + 6)
        return int(r.stdout.strip() or 0)
    except Exception:
        return 0


def build_gd(channels, relays):
    """探测所有中转，把 ok/kbps/last_check 写回 relays；返回 (nas_blocks, alive_list)。"""
    samples = [channels[0]["mcast"], channels[len(channels) // 2]["mcast"]]
    scored = []
    now = datetime.now(CST).strftime("%m-%d %H:%M")
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_probe, r["host"], r.get("mode", "udp"), samples[0]): r
                   for r in relays}
        for f in futures:
            r = futures[f]
            b1 = f.result()
            b2 = _probe(r["host"], r.get("mode", "udp"), samples[1])
            r["last_check"] = now
            if b1 > 20000 and b2 > 20000:
                r["ok"] = True
                r["kbps"] = int((b1 + b2) * 8 / (2 * 5) / 1000)
                scored.append((b1 + b2, r))
            else:
                r["ok"] = False
                r["kbps"] = 0
    scored.sort(key=lambda x: -x[0])
    alive = [r for _b, r in scored][:LINES]
    if not alive:
        # 美国节点测不通国内 udpxy 时，回退用 relays.json 里最后一次判定 ok 的（按 kbps）
        fb = sorted([r for r in relays if r.get("ok")], key=lambda r: -r.get("kbps", 0))[:LINES]
        if not fb:
            return [], []
        alive = fb
    # 标记当前在用的线路 + 地区（池子都是广东电信组播 239.77.x，所以都是广东电信）
    for r in relays:
        r["in_use"] = (r in alive)
        if not r.get("region"):
            r["region"] = "广东电信"
    blocks = []
    seen = set()
    for c in channels:
        mc = c.get("mcast")
        if not mc or mc in seen:
            continue
        seen.add(mc)
        for r in alive:
            blocks.append((extinf("电信组播", c.get("name")),
                           "http://%s/%s/%s" % (r["host"], r.get("mode", "udp"), mc)))
    return blocks, alive


# ---------- 生成 ----------
def build_user(blocks, expire, created, push_time, ts=None):
    info_url = next((u for _e, u in blocks if u.startswith("http")), "https://zengzifeng.ccwu.cc")
    now = datetime.now(CST)
    try:
        if ts:
            exp = datetime.fromtimestamp(int(ts), CST)
        else:
            exp = datetime.strptime(str(expire).replace("年", "-").replace("月", "-").replace("日", ""),
                                    "%Y-%m-%d").replace(tzinfo=CST)
    except Exception:
        exp = now + timedelta(days=365)
    days = max(0, (exp - now).days)
    info_extinf = (f'#EXTINF:-1 tvg-id="订阅" tvg-name="订阅" '
                   f'group-title="订阅信息 到期 {expire}",有效期 {created} ~ {expire}（剩余 {days} 天）')
    push_extinf = (f'#EXTINF:-1 tvg-id="推送" tvg-name="推送" '
                   f'group-title="推送时间 {push_time}",{push_time} 推送')
    allb = [(info_extinf, PROMO_URL), (push_extinf, PROMO_URL)] + list(blocks)
    # 丢弃「更新时间」分组
    allb = [b for b in allb if 'group-title="更新时间' not in b[0]]
    # 网络直播组内排序
    allb = reorder_netlive_run(allb)
    # 全局排序：分组顺序 + 组内自然序
    allb.sort(key=lambda b: (rank_of(GROUP_RE.search(b[0]).group(1) if GROUP_RE.search(b[0]) else ""),
                             natkey(name_of(b[0]))))
    # 每频道封顶
    cnt, out = {}, ["#EXTM3U url-tvg=\"https://zengzifeng.ccwu.cc/output/epg.xml\""]
    for e, u in allb:
        nm = name_of(e)
        if cnt.get(nm, 0) >= TOP_PER_CHANNEL:
            continue
        cnt[nm] = cnt.get(nm, 0) + 1
        out.append(with_emoji(e))
        out.append(u)
    return "\n".join(out) + "\n"


def build_shared(blocks):
    """共享订阅（网关 UPSTREAM 用）：三源合并、排序、封顶，但不带每用户的订阅信息头。"""
    allb = [b for b in blocks if 'group-title="更新时间' not in b[0]]
    allb = reorder_netlive_run(allb)
    allb.sort(key=lambda b: (rank_of(GROUP_RE.search(b[0]).group(1) if GROUP_RE.search(b[0]) else ""),
                             natkey(name_of(b[0]))))
    cnt, out = {}, ["#EXTM3U url-tvg=\"https://zengzifeng.ccwu.cc/output/epg.xml\""]
    for e, u in allb:
        nm = name_of(e)
        if cnt.get(nm, 0) >= TOP_PER_CHANNEL:
            continue
        cnt[nm] = cnt.get(nm, 0) + 1
        out.append(with_emoji(e))
        out.append(u)
    return "\n".join(out) + "\n"


# ---------- GitHub 推送 ----------
def _git_blob_sha(data):
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def _gh(method, url, token, payload=None, timeout=120):
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "User-Agent": "github-pipeline",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
    return json.loads(raw) if raw.strip() else {}


def cleanup_orphans(token, users, keep_tokens):
    """删除 output/users 里已不存在于 users.json 的孤儿订阅文件。"""
    keep = {("output/users/u_%s.m3u" % tok) for tok in keep_tokens}
    try:
        listing = _gh("GET", "%s/contents/output/users" % API, token)
    except Exception:
        return 0
    EMPTY = "#EXTM3U\n# 订阅已过期或已被删除\n".encode("utf-8")
    n = 0
    for it in listing:
        fname = it.get("name", "")
        path = "output/users/" + fname
        if fname.endswith(".m3u") and path not in keep:
            _gh("PUT", "%s/contents/%s" % (API, urllib.parse.quote(path)), token,
                {"message": "chore: 清空已删除用户订阅 " + fname,
                 "content": base64.b64encode(EMPTY).decode("ascii"), "sha": it.get("sha")})
            n += 1
            log("  [清理] 清空 " + fname)
    return n


def push_batch(token, files, message="chore: 更新订阅"):
    """files: [(path, bytes)] 合并成一次 commit，内容没变就不提交。"""
    if not files:
        return 0
    for attempt in range(3):
        try:
            ref = _gh("GET", "%s/git/ref/heads/main" % API, token)
            base = ref["object"]["sha"]
            base_tree = _gh("GET", "%s/git/commits/%s" % (API, base), token)["tree"]["sha"]
            existing = {}
            d = os.path.dirname(files[0][0])
            try:
                for it in _gh("GET", "%s/contents/%s" % (API, urllib.parse.quote(d, safe="/")), token):
                    existing[it.get("name")] = it.get("sha")
            except Exception:
                pass
            entries = []
            for path, data in files:
                if existing.get(os.path.basename(path)) == _git_blob_sha(data):
                    continue
                blob = _gh("POST", "%s/git/blobs" % API, token,
                           {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            if not entries:
                return 0
            tree = _gh("POST", "%s/git/trees" % API, token,
                       {"base_tree": base_tree, "tree": entries})
            commit = _gh("POST", "%s/git/commits" % API, token,
                         {"message": message, "tree": tree["sha"], "parents": [base]})
            _gh("PATCH", "%s/git/refs/heads/main" % API, token, {"sha": commit["sha"]})
            return len(entries)
        except Exception as exc:
            log("  推送第 %d 次失败: %s" % (attempt + 1, exc))
            if attempt == 2:
                raise
            time.sleep(3)
    return 0


# ---------- 主流程 ----------
def main():
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if not token:
        log("缺少 GH_TOKEN")
        return 1

    # 台标表：一次拉取，后面 extinf() 和组播来源行都靠它改写 tvg-logo
    logo_source.load_table()

    users = load_json(os.path.join(ROOT, "nas", "users.json"), {})
    relays = load_json(os.path.join(ROOT, "nas", "data", "relays.json"), {}).get("relays", [])
    channels = load_json(os.path.join(ROOT, "nas", "data", "channels_core.json"), [])
    channels = [c for c in channels if str(c.get("name", "")).strip() != "0"]

    push_time = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    files = []

    # 0) 咪咕直播：静态订阅，不探测（用户提供的 47.103 中转，group-title「咪咕直播」排最前）
    migu_blocks = []
    try:
        migu_path = os.path.join(ROOT, "crawl", "extra", "migu.m3u")
        if os.path.exists(migu_path) and os.path.getsize(migu_path) > 100:
            migu_text = open(migu_path, encoding="utf-8", errors="ignore").read()
            migu_blocks = parse_m3u(migu_text)
            migu_out = "#EXTM3U x-tvg-url=\"https://zzf.new.tg/epg.xml\"\n"
            migu_out += "\n".join(with_emoji(e) + "\n" + u for e, u in migu_blocks) + "\n"
            files.append(("output/migu.m3u", migu_out.encode("utf-8")))
            log("  咪咕直播 %d 条（静态，不巡检）" % len(migu_blocks))
    except Exception as exc:
        log("  咪咕直播读取失败: %s" % exc)
        migu_blocks = []

    # 1) 全量组播：优先读仓库本地 crawl/checked/live.m3u，失败回退远程 zzf.new.tg
    log("拉取全量组播巡检列表…")
    try:
        local_checked = os.path.join(ROOT, "crawl", "checked", "live.m3u")
        checked_text = ""
        if os.path.exists(local_checked) and os.path.getsize(local_checked) > 100:
            checked_text = open(local_checked, encoding="utf-8", errors="ignore").read()
            log("  使用本地 crawl/checked/live.m3u")
        if not checked_text.strip():
            checked_text = fetch(SRC_CHECKED)
            log("  本地为空，回退远程 %s" % SRC_CHECKED)
        checked = parse_m3u(checked_text)
        # 组播来源自带的 tvg-logo 是「猜文件名」的旧地址（多数 404），先统一换成台标库的
        checked, logo_hit = logo_source.apply(checked)
        log("  台标命中 %d/%d" % (logo_hit, len(checked)))
        full_blocks = trim_top(checked, TOP_PER_CHANNEL)
        top20 = "#EXTM3U\n# 每频道最多 %d 条 %s\n" % (TOP_PER_CHANNEL, push_time)
        top20 += "\n".join(e + "\n" + u for e, u in full_blocks) + "\n"
        files.append(("output/top20.m3u", top20.encode("utf-8")))
        log("  全量组播 %d 条" % len(full_blocks))
    except Exception as exc:
        log("  全量组播拉取失败: %s" % exc)
        full_blocks = []

    # 2) 轮播类独立订阅（经典轮播/明星剧场/虎牙一起看/YY轮播）：静态，不探测
    log("读取轮播类订阅（经典轮播/明星剧场/虎牙/YY）…")
    try:
        extra_blocks = load_extra_blocks()
        log("  轮播类 %d 条" % len(extra_blocks))
    except Exception as exc:
        log("  轮播类读取失败: %s" % exc)
        extra_blocks = []

    # 3) 广东电信：探测中转 → 最快 N 台 → nas.m3u
    log("探测广东中转池（%d 台）…" % len(relays))
    gd_blocks, alive = build_gd(channels, relays)
    if gd_blocks:
        nas = "#EXTM3U\n# 本地订阅 %s 中转 %s\n" % (push_time, alive[0]["host"])
        nas += "\n".join(e + "\n" + u for e, u in gd_blocks) + "\n"
        files.append(("output/nas.m3u", nas.encode("utf-8")))
        log("  广东 %d 条（%d 台中转）" % (len(gd_blocks), len(alive)))
    else:
        log("  广东中转全部探测失败，保留线上旧 nas.m3u")
    # 把探测结果写回 relays.json（供后台/状态页查看）
    pool_state = {"relays": relays, "active_host": alive[0]["host"] if alive else "",
                  "checked": push_time}
    files.append(("nas/data/relays.json",
                  json.dumps(pool_state, ensure_ascii=False, indent=1).encode("utf-8")))

    # 4) 用户订阅（咪咕直播 + 轮播类排最前，其次是组播/广东电信）
    blocks = migu_blocks + extra_blocks + full_blocks + gd_blocks
    if blocks:
        for name, info in sorted(users.items()):
            tok = info.get("token")
            if not tok:
                continue
            expire = info.get("expire", "")
            created = info.get("created", "")
            ts_val = info.get("ts")
            now_ts = int(time.time())
            if ts_val and int(ts_val) < now_ts:
                empty = "#EXTM3U\n# 订阅已过期，请联系管理员续期\n"
                files.append(("output/users/u_%s.m3u" % tok, empty.encode("utf-8")))
                log("  [%s] 已过期 -> 订阅清空" % name)
                continue
            text = build_user(blocks, expire, created, push_time, info.get("ts"))
            files.append(("output/users/u_%s.m3u" % tok, text.encode("utf-8")))
            log("  [%s] %d 条" % (name, text.count("#EXTINF")))

    # 5) 清理孤儿订阅（删掉的用户）
    keep_tokens = {u["token"] for u in users.values() if u.get("token")}
    if keep_tokens and gd_blocks:
        try:
            cleanup_orphans(token, users, keep_tokens)
        except Exception as exc:
            log("  孤儿清理跳过: %s" % exc)

    # 5.5) 共享订阅（网关 UPSTREAM）
    if blocks:
        files.append(("output/gateway.m3u", build_shared(blocks).encode("utf-8")))
        log("  共享订阅 gateway.m3u 已生成")

    # 6) 推送
    n = push_batch(token, files)
    log("推送完成：%d 个文件有变化" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

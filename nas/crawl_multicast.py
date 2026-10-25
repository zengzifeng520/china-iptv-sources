#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
crawl_multicast.py —— 在 NAS 上爬取 foodieguide 组播源，产出 push_nas.py 可直接吃的 m3u

    python3 crawl_multicast.py -o /tmp/multicast.m3u
    python3 crawl_multicast.py -o /tmp/merged.m3u --merge /vol1/.../live.m3u
    python3 crawl_multicast.py -o /tmp/multicast.m3u --emit rtp      # 输出 rtp:// 形式

只依赖标准库；接口细节见 IPTV组播API接口分析.md：
    GET http://www.foodieguide.com/iptvsearch/getall.php?ip=<IP[:PORT]>&p=1
    * 302 的响应体才是数据，禁止自动跳转
    * 有效响应里只有 "组播源" 才收录
"""
import argparse
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 某些 NAS/cron 环境 locale 不是 UTF-8，中文与 emoji 打印会抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = "http://www.foodieguide.com/iptvsearch/"
GETALL = BASE + "getall.php"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

HEADER_RE = re.compile(r"来自<b>([^<]*)</b>的(组播源|酒店源|代理源|秒播源)\(([^)]+)\)共有<b>(\d+)</b>个频道")
COPYTO_RE = re.compile(r"copyto\('([^']+)'\)")
NAME_RE = re.compile(r'data-title="Play with PC">\s*([^<]*?\S[^<]*?)\s*</div>')
REC_RE = re.compile(r'href="\?ip=([^"&]+)"\s*style="color:#000;">([^<]*)</a>')
FAIL_RE = re.compile(r"来自<b>([^<]*)</b>的源暂时失效")
MCAST_RE = re.compile(r"^(https?://[^/]+)/rtp/([0-9.]+:\d+)$", re.I)

SEEDS = [
    "1.192.242.239:8188", "171.122.197.67:8085", "171.117.0.98:8082",
    "113.205.89.156:4000", "guany.myds.me:25852", "yaofuming.top:8188",
    "www.rongrong.me:14022", "219.155.108.79:7000", "58.17.134.3:8001",
    "bigy.timwin.top:5400", "17782331118.top:8188",
    "1.1.1.1", "8.8.8.8", "114.114.114.114",
]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """接口用 302 返回数据体，必须禁止跟随。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        data = e.read()          # 302 的正文就是数据
    return data.decode("utf-8", "replace")


def probe(host_port, page=1, timeout=20):
    """返回 (kind, operator, host, count, channels, recs)。"""
    url = "%s?ip=%s&p=%d" % (GETALL, urllib.parse.quote(host_port, safe=""), page)
    try:
        html = fetch(url, timeout)
    except Exception as exc:                       # noqa: BLE001
        return ("error", "", host_port, 0, [], [], str(exc))

    m = HEADER_RE.search(html)
    if not m:
        fm = FAIL_RE.search(html)
        recs = [(a, b.strip()) for a, b in REC_RE.findall(html)]
        return ("fail", "", host_port, 0, [], recs, fm.group(1) if fm else "失效")

    operator, stype, host, declared = m.group(1), m.group(2), m.group(3), int(m.group(4))
    recs = [(a, b.strip()) for a, b in REC_RE.findall(html)]
    if stype != "组播源":
        return ("other", operator, host, declared, [], recs, stype)

    channels = []
    for block in html.split('<div class="result">')[1:]:
        u = COPYTO_RE.search(block)
        if not u:
            continue
        nm = NAME_RE.search(block)
        name = nm.group(1).strip() if nm else ""
        if name.startswith("<img"):      # 图标块，不是频道名
            continue
        channels.append((name or "未命名", u.group(1)))
    return ("multicast", operator, host, declared, channels, recs, "")


def to_rtp(url):
    m = MCAST_RE.match(url)
    return "rtp://" + m.group(2) if m else url


def crawl(max_rounds, workers, interval, page, timeout):
    discovered, verified = {}, {}
    sources, pool = [], []
    for s in SEEDS:
        if s not in discovered:
            discovered[s] = ""
            pool.append(s)

    rounds = 0
    while rounds < max_rounds and pool:
        batch = []
        while pool and len(batch) < workers:
            hp = pool.pop(0)
            if hp not in verified:
                batch.append(hp)
        if not batch:
            batch = ["1.1.1.1"]
            rounds += 1

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(probe, hp, page, timeout): hp for hp in batch}
            for fut in as_completed(futs):
                hp = futs[fut]
                rounds += 1
                try:
                    kind, operator, host, declared, channels, recs, msg = fut.result()
                except Exception as exc:           # noqa: BLE001
                    print("[err] %s %s" % (hp, exc), flush=True)
                    continue
                verified[hp] = kind
                for cand, _label in recs:
                    if cand not in discovered:
                        discovered[cand] = _label
                        pool.append(cand)
                if kind == "multicast" and channels:
                    sources.append({"operator": operator, "host": host,
                                    "count": declared, "channels": channels})
                    print("[ok] %-22s %-10s %3d 频道（声明 %d）"
                          % (host, operator, len(channels), declared), flush=True)
                if rounds >= max_rounds:
                    break
        time.sleep(interval)
        print("-- 轮次 %d/%d  已验证 %d  待爬 %d  组播源 %d"
              % (rounds, max_rounds, len(verified), len(pool), len(sources)), flush=True)
    return sources


def read_entries(path):
    """读取已有 m3u/txt，返回 [(group, name, url)]。"""
    out = []
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError:
        return out
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return out

    group, name = "", ""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            m = re.search(r'group-title="([^"]*)"', line)
            group = m.group(1) if m else ""
            name = line.rsplit(",", 1)[-1].strip() if "," in line else ""
        elif line.startswith("#") or "," in line and line.split(",")[0].startswith("#"):
            continue
        elif line.startswith(("http://", "https://", "rtp://", "udp://", "rtsp://", "rtmp://")):
            out.append((group, name, line))
            name = ""
            continue
        elif "," in line:                       # txt: 分类,#genre# / 名称,url
            a, _, b = line.partition(",")
            if b.strip().startswith(("http", "rtp", "udp", "rtsp", "rtmp")):
                out.append((group, a.strip(), b.strip()))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="multicast.m3u", help="输出 m3u 路径")
    ap.add_argument("--merge", metavar="M3U", help="合并进已有源文件（保留原条目）")
    ap.add_argument("--max-rounds", type=int, default=150, help="最多请求次数（默认 150）")
    ap.add_argument("--workers", type=int, default=3, help="并发（站点限频，建议 2-4）")
    ap.add_argument("--interval", type=float, default=0.8, help="每批之间休眠秒数")
    ap.add_argument("--page", type=int, default=1, help="接口 p 参数（默认 1）")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--emit", choices=("http", "rtp"), default="http",
                    help="http=保留 udpxy 网关地址；rtp=转成 rtp://组播IP:端口")
    ap.add_argument("--group-prefix", default="组播", help="分组名前缀")
    args = ap.parse_args()

    print("== 开始爬取（上限 %d 次请求，并发 %d，间隔 %.1fs）=="
          % (args.max_rounds, args.workers, args.interval), flush=True)
    sources = crawl(args.max_rounds, args.workers, args.interval, args.page, args.timeout)

    existing = read_entries(args.merge) if args.merge else []
    seen = {u for _g, _n, u in existing}
    lines = ["#EXTM3U"]
    for g, n, u in existing:
        lines.append('#EXTINF:-1 group-title="%s",%s' % (g, n) if g else "#EXTINF:-1,%s" % n)
        lines.append(u)

    added = 0
    for s in sources:
        group = ("%s %s" % (args.group_prefix, s["operator"])).strip()
        for name, url in s["channels"]:
            u = to_rtp(url) if args.emit == "rtp" else url
            if u in seen:
                continue
            seen.add(u)
            safe = name.replace('"', "'")
            lines.append('#EXTINF:-1 tvg-name="%s" group-title="%s",%s' % (safe, group, safe))
            lines.append(u)
            added += 1

    with open(args.out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    print("== 完成：组播源 %d 个，新增频道 %d 条，合并后共 %d 条 -> %s =="
          % (len(sources), added, len(seen), args.out))
    if not added:
        print("!! 没爬到达新条目：站点可能限频/失效，稍后重试或加大 --max-rounds")


if __name__ == "__main__":
    main()

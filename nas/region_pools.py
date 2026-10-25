#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""region_pools.py —— 把 IPTVZB 巡检好的源按「频道组」拆开，各配一套可用中转。

思路：中转是否可用取决于「它能不能拉到那组频道」，而不是它物理上属于哪个省
（实测：上海组和重庆组共用 8 个中转）。所以按频道组建池，每组独立验证。

产物：
    output/regions/<slug>.json   该组可用的中转（按速度排序）
    output/regions/<slug>.m3u    用该组最快中转拼好的订阅

用法：
    python3 region_pools.py --only 四川节目        # 只做一组（试点）
    python3 region_pools.py                        # 全部
    python3 region_pools.py --max-relays 6 --dry   # 只验证不写文件
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output", "regions")
SRC = os.environ.get("REGION_SRC", "https://zzf.new.tg/checked/live.m3u")
UA = "Mozilla/5.0 (compatible; region-pools/1.0)"
GROUP_RE = re.compile(r'group-title="([^"]*)"')
HOST_RE = re.compile(r"https?://([^/\s]+)/(rtp|udp)/", re.I)

# 不参与拆分的组
SKIP = ("更新时间", "订阅信息")


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read().decode("utf-8", "ignore")


def parse(text):
    """{组名: {"relays": {host: mode}, "items": [(name, mcast)]}}"""
    out = collections.OrderedDict()
    g, pend = None, None
    for ln in text.replace("\r", "").split("\n"):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            m = GROUP_RE.search(s)
            g = (m.group(1) if m else "其他").strip()
            pend = s.rsplit(",", 1)[-1].strip()
            continue
        if s.startswith("#") or not s.startswith("http"):
            continue
        m = HOST_RE.search(s)
        if not m:
            continue
        d = out.setdefault(g, {"relays": collections.OrderedDict(), "items": []})
        d["relays"].setdefault(m.group(1), m.group(2))
        # 组播路径（去掉中转部分）
        rest = s[m.end():]
        d["items"].append((pend or "未命名", rest))
        pend = None
    return out


def probe(host, mode, mcast, secs=3):
    url = f"http://{host}/{mode}/{mcast}"
    try:
        r = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time", str(secs),
                            "-w", "%{size_download}", "-A", UA, url],
                           capture_output=True, text=True, timeout=secs + 6)
        return int(r.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


def verify_relay(host, mode, items, samples=2):
    """用该组 2 个频道试拉，都出流才算可用；返回 kbps"""
    picks = [it[1] for it in items[:samples]]
    if not picks:
        return 0
    total = 0
    for mcast in picks:
        size = probe(host, mode, mcast)
        if size < 20000:
            return 0
        total += size
    return int(total * 8 / (3 * len(picks)) / 1000)


def slug(name):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", name).strip("_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只处理某一组（子串匹配）")
    ap.add_argument("--max-relays", type=int, default=8)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    print("拉取 %s" % SRC)
    data = parse(fetch(SRC))
    groups = [g for g in data if g not in SKIP]
    if a.only:
        groups = [g for g in groups if a.only in g]
    if not groups:
        print("没有匹配的分组")
        return 1

    if not a.dry:
        os.makedirs(OUT, exist_ok=True)
    total_ok = 0

    for g in groups:
        d = data[g]
        relays = list(d["relays"].items())
        items = d["items"]
        print(f"\n== {g}：中转 {len(relays)} 个，频道 {len(items)} 条 ==")
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            ks = list(ex.map(lambda rm: verify_relay(rm[0], rm[1], items), relays))
        good = sorted([(k, h, m) for (h, m), k in zip(relays, ks) if k > 0],
                      key=lambda x: -x[0])[:a.max_relays]
        print(f"   可用 {len(good)} / {len(relays)}，耗时 {time.time()-t0:.0f}s")
        for k, h, m in good:
            print(f"   √ {h:<30} {m}  {k} kbps")
        total_ok += len(good)

        if a.dry or not good:
            continue
        best_host, best_mode, best_kbps = good[0][1], good[0][2], good[0][0]
        s = slug(g)
        json.dump({"group": g, "checked": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "active": {"host": best_host, "kbps": best_kbps},
                   "relays": [{"host": h, "mode": m, "kbps": k} for k, h, m in good]},
                  open(os.path.join(OUT, f"{s}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        lines = ["#EXTM3U", f"# {g} 中转 {best_host} {time.strftime('%Y-%m-%d %H:%M')}"]
        seen = set()
        for name, mcast in items:
            if (name, mcast) in seen:
                continue
            seen.add((name, mcast))
            lines.append(f'#EXTINF:-1 group-title="{g}",{name}')
            lines.append(f"http://{best_host}/{best_mode}/{mcast}")
        open(os.path.join(OUT, f"{s}.m3u"), "w", encoding="utf-8", newline="\n").write(
            "\n".join(lines) + "\n")
        print(f"   已写 {s}.json / {s}.m3u（{len(seen)} 条）")

    print(f"\n合计可用中转 {total_ok} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())

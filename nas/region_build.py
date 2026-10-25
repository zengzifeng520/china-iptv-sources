#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""region_build.py —— 按地区生成独立订阅（每个地区一套频道 + 自己的中转）。

数据源：组播提取 API（与 IPTVZB 同一个）
    GET https://api.809899.xyz/extract_multicast?keyword=广东
每个 result 是一台中转节点，带该地区完整频道表。

流程：拉节点 → 用节点自己的频道实测挑最快 → 合并该地区所有节点的频道（去重）
      → 生成 output/regions/<地区>.m3u（地址统一指向选中的那台中转）

用法：
    python3 region_build.py                 # 默认几个地区
    python3 region_build.py --only 重庆
    python3 region_build.py --regions 广东,上海,重庆
"""
import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output", "regions")
API = "https://api.809899.xyz/extract_multicast"
AUTH = "iptv-tools-official-v1"
UA = "Mozilla/5.0 (compatible; region-build/1.0)"
HOST_RE = re.compile(r"https?://([^/]+)/(rtp|udp)/", re.I)
MCAST_RE = re.compile(r"/(rtp|udp)/([0-9.]+:\d+)", re.I)

DEFAULT_REGIONS = ["广东", "上海", "重庆", "北京", "河南", "福建", "湖南"]


def api(keyword):
    url = f"{API}?keyword={urllib.parse.quote(keyword)}"
    req = urllib.request.Request(url, headers={"X-App-Auth": AUTH, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:  # noqa: BLE001
        print(f"  接口失败：{e}")
        return {}


def nodes(keyword):
    out = []
    for r in (api(keyword).get("results") or []):
        chs = r.get("channels") or []
        if not chs:
            continue
        m = HOST_RE.search(chs[0].get("url", ""))
        if not m:
            continue
        out.append(dict(host=m.group(1), mode=m.group(2),
                        region=r.get("region", ""), updated=r.get("time", ""),
                        channels=[(c.get("name", ""), MCAST_RE.search(c.get("url", "")))
                                  for c in chs if c.get("url")]))
    return out


def play(host, mode, mcast, secs=4):
    url = f"http://{host}/{mode}/{mcast}"
    try:
        r = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time", str(secs),
                            "-w", "%{size_download}", "-A", UA, url],
                           capture_output=True, text=True, timeout=secs + 6)
        return int(r.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


def check_node(n):
    """用节点自己的前 2 个频道测，都出流才算活"""
    picks = [m.group(2) for _nm, m in n["channels"][:2] if m]
    if not picks:
        return 0
    total = 0
    for mc in picks:
        size = play(n["host"], n["mode"], mc)
        if size < 20000:
            # 失败重试一次，避免瞬时抖动误杀
            size = play(n["host"], n["mode"], mc)
            if size < 20000:
                return 0
        total += size
    return int(total * 8 / (4 * len(picks)) / 1000)


def slug(s):
    return re.sub(r"[^\w\u4e00-\u9fff]+", "_", s).strip("_")


def build(keyword):
    ns = nodes(keyword)
    if not ns:
        print(f"  {keyword}：无节点")
        return None

    scored = []
    for n in ns:
        k = check_node(n)
        if k:
            scored.append((k, n))
    if not scored:
        print(f"  {keyword}：{len(ns)} 个节点，无可用")
        return None
    scored.sort(key=lambda x: -x[0])
    best_k, best = scored[0]

    # 合并该地区所有节点（含不可用的）的频道，去重
    seen, items = set(), []
    for n in ns:
        for name, m in n["channels"]:
            if not m:
                continue
            key = (name, m.group(2))
            if key in seen:
                continue
            seen.add(key)
            items.append((name, m.group(2)))

    rel = [dict(host=n["host"], mode=n["mode"], kbps=k, region=n["region"])
           for k, n in scored]
    return dict(keyword=keyword, best=best, best_kbps=best_k,
                nodes=len(ns), usable=len(scored), items=items, relays=rel)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default=",".join(DEFAULT_REGIONS))
    ap.add_argument("--only", default="")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    kws = [k.strip() for k in a.regions.split(",") if k.strip()]
    if a.only:
        kws = [a.only]

    os.makedirs(OUT, exist_ok=True)
    results = []
    for kw in kws:
        print(f"\n== {kw} ==")
        r = build(kw)
        if not r:
            continue
        print(f"  节点 {r['nodes']} 个，可用 {r['usable']} 个")
        for x in r["relays"][:5]:
            print(f"    √ {x['host']:<26} {x['mode']}  {x['kbps']} kbps  {x['region']}")
        print(f"  频道 {len(r['items'])} 条，选用 {r['best']['host']} ({r['best_kbps']} kbps)")
        results.append(r)

        if a.dry:
            continue
        s = slug(kw)
        m3u = ["#EXTM3U", f"# {kw} 中转 {r['best']['host']} {r['best_kbps']}kbps"]
        for name, mc in r["items"]:
            m3u.append(f'#EXTINF:-1 group-title="{kw}",{name}')
            m3u.append(f"http://{r['best']['host']}/{r['best']['mode']}/{mc}")
        open(os.path.join(OUT, f"{s}.m3u"), "w", encoding="utf-8", newline="\n").write(
            "\n".join(m3u) + "\n")
        json.dump({"region": kw, "checked": __import__("time").strftime("%Y-%m-%d %H:%M:%S"),
                   "active": {"host": r["best"]["host"], "mode": r["best"]["mode"],
                              "kbps": r["best_kbps"]},
                   "relays": r["relays"], "channels": len(r["items"])},
                  open(os.path.join(OUT, f"{s}.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)

    print("\n=== 汇总 ===")
    total = 0
    for r in results:
        print(f"  {r['keyword']:<6} 可用中转 {r['usable']}/{r['nodes']}  频道 {len(r['items'])}")
        total += len(r["items"])
    print(f"  合计频道 {total} 条，地区 {len(results)} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())

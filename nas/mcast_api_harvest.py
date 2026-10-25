#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mcast_api_harvest.py —— 从组播提取 API 获取各地区中转节点，验证后入池。

接口（IPTVZB 用的同一个）：
    GET https://api.809899.xyz/extract_multicast?keyword=广东
    Header: X-App-Auth: iptv-tools-official-v1
    返回 {"results": [{"ip", "region", "channel_count", "channels": [{"name","url"}]}]}

每个 result 就是一台中转节点，自带该地区的频道表。
本脚本用它来补充「广东电信」中转池：拿节点 → 用我们自己的 167 个频道实测 → 能出流才收。

用法：
    python3 mcast_api_harvest.py                 # 只做广东
    python3 mcast_api_harvest.py --keyword 广东 --dry
    python3 mcast_api_harvest.py --list          # 只列节点，不验证
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
API = "https://api.809899.xyz/extract_multicast"
AUTH = "iptv-tools-official-v1"
POOL = "/vol1/1000/iptv-sub/data/relays.json"
CORE = "/vol1/1000/iptv-sub/data/channels_core.json"
UA = "Mozilla/5.0 (compatible; mcast-api-harvest/1.0)"
MAX_RELAYS = 16
HOST_RE = re.compile(r"https?://([^/]+)/(rtp|udp)/", re.I)


def query(keyword):
    url = f"{API}?keyword={urllib.parse.quote(keyword)}"
    req = urllib.request.Request(url, headers={"X-App-Auth": AUTH, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def nodes(keyword):
    """[(host, mode, region, channels)]"""
    out = []
    for r in (query(keyword).get("results") or []):
        chs = r.get("channels") or []
        if not chs:
            continue
        m = HOST_RE.search(chs[0].get("url", ""))
        if not m:
            continue
        out.append((m.group(1), m.group(2), r.get("region", ""), chs))
    return out


def probe(host, mode, mcast, secs=4):
    url = f"http://{host}/{mode}/{mcast}"
    try:
        r = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time", str(secs),
                            "-w", "%{size_download}", "-A", UA, url],
                           capture_output=True, text=True, timeout=secs + 6)
        return int(r.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


def verify(host, mode, mcasts):
    total = 0
    for mc in mcasts:
        n = probe(host, mode, mc)
        if n < 20000:
            return 0
        total += n
    return int(total * 8 / (4 * len(mcasts)) / 1000)


def load_pool():
    if os.path.exists(POOL):
        try:
            return json.load(open(POOL, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"relays": []}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keyword", default="广东")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    ns = nodes(a.keyword)
    if not ns:
        print("没有拿到节点（关键词可能不支持）")
        return 1

    print(f"关键词「{a.keyword}」共 {len(ns)} 个节点：")
    for h, m, rg, chs in ns:
        print(f"  {h:<24} {m}  {rg:<14} {len(chs)} 频道")
    if a.list:
        return 0

    try:
        core = json.load(open(CORE, encoding="utf-8"))
        mcasts = [core[0]["mcast"], core[len(core) // 2]["mcast"]]
    except Exception:  # noqa: BLE001
        mcasts = ["239.77.0.185:5146", "239.77.1.195:5146"]
    print(f"\n用我们的频道验证：{mcasts}")

    good = []
    for h, m, rg, _chs in ns:
        k = verify(h, m, mcasts)
        print(f"  {'√' if k else '×'} {h:<24} {m}  {k or '—'} kbps  {rg}")
        if k:
            good.append((k, h, m, rg))

    if not good:
        print("\n没有可用节点")
        return 0
    good.sort(reverse=True)
    if a.dry:
        print("\n--dry，不写入")
        return 0

    data = load_pool()
    pool = data.get("relays", [])
    known = {r["host"] for r in pool}
    added = 0
    for k, h, m, rg in good:
        if h in known:
            continue
        pool.append(dict(host=h, mode=m, ok=None, kbps=k, last_check="", region=rg))
        added += 1
    if len(pool) > MAX_RELAYS:
        # 保留主力（池序靠前）与新加的，淘汰已判定失效的
        alive = [r for r in pool if r.get("ok") is not False]
        dead = [r for r in pool if r.get("ok") is False]
        pool = (alive + dead)[:MAX_RELAYS]
    data["relays"] = pool
    json.dump(data, open(POOL, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n新增 {added} 台，中转池现有 {len(pool)} 台（服务 ≤10 分钟自动纳入）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

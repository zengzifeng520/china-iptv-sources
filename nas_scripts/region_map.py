#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""region_map.py —— 用组播提取 API 建立 IP → 地区 映射，并解析域名。

产物 /vol1/1000/iptv-sub/data/region_map.json：
{"by_ip": {"125.94.173.145": "广东省 广东电信", ...}, "by_host": {"yanshifen.top": "...", ...}}
"""
import json
import os
import socket
import sys
import urllib.parse
import urllib.request

API = "https://api.809899.xyz/extract_multicast"
AUTH = "iptv-tools-official-v1"
UA = "Mozilla/5.0 (compatible; region-map/1.0)"
OUT = "/vol1/1000/iptv-sub/data/region_map.json"
POOL = "/vol1/1000/iptv-sub/data/relays.json"
PROVINCES = ["广东", "上海", "重庆", "北京", "河南", "福建", "湖南"]


def query(keyword):
    url = "%s?keyword=%s" % (API, urllib.parse.quote(keyword))
    req = urllib.request.Request(url, headers={"X-App-Auth": AUTH, "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:  # noqa: BLE001
        print("  %s 查询失败 %s" % (keyword, e))
        return {}


def main():
    by_ip = {}
    for kw in PROVINCES:
        d = query(kw)
        n = 0
        for r in (d.get("results") or []):
            ip = (r.get("ip") or "").strip()
            rg = (r.get("region") or "").strip()
            if ip and rg and ip not in by_ip:
                by_ip[ip] = rg
                n += 1
        print("  %s: 累计 %d 个 IP" % (kw, len(by_ip)))

    by_host = {}
    try:
        pool = json.load(open(POOL, encoding="utf-8")).get("relays", [])
    except Exception:  # noqa: BLE001
        pool = []
    for r in pool:
        h = (r.get("host") or "")
        host = h.split(":")[0]
        if host in by_host:
            continue
        if not host.replace(".", "").isdigit():          # 域名 → 解析成 IP 再查
            try:
                ip = socket.gethostbyname(host)
            except Exception:  # noqa: BLE001
                continue
            rg = by_ip.get(ip)
            if rg:
                by_host[host] = rg
                print("  域名 %s -> %s -> %s" % (h, ip, rg))
        elif host in by_ip:
            by_host[host] = by_ip[host]

    json.dump({"by_ip": by_ip, "by_host": by_host},
              open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("已写 %s：by_ip %d 条，by_host %d 条" % (OUT, len(by_ip), len(by_host)))


if __name__ == "__main__":
    sys.exit(main())

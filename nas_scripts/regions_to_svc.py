#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""regions_to_svc.py —— 把 output/regions/*.m3u + *.json 转成服务端 data/regions.json。

服务端格式（region_svc.py 读这个）：
{"regions":[{"key","name","group","channels":[{name,mcast,group,mode}],
             "relays":[{host,mode,ok,kbps,last_check}],"active":{host,mode}}]}
"""
import glob
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(BASE, "output", "regions")
DST = "/vol1/1000/iptv-sub/data/regions.json"
GROUP_RE = re.compile(r'group-title="([^"]*)"')
MODE_RE = re.compile(r"https?://[^/]+/(rtp|udp)/", re.I)


def group_name(key):
    return "电信组播" if key == "广东" else f"{key}节目"


def read_m3u(path, key):
    chans, pend = [], None
    for ln in open(path, encoding="utf-8", errors="ignore"):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            pend = s.rsplit(",", 1)[-1].strip()
            continue
        if s.startswith("#") or not s.startswith("http"):
            continue
        m = MODE_RE.search(s)
        mc = s.split(f"/{m.group(1) if m else 'udp'}/")[-1] if m else ""
        if pend and mc:
            chans.append({"name": pend, "mcast": mc,
                          "group": group_name(key), "mode": m.group(1) if m else "udp"})
        pend = None
    return chans


def main():
    out = []
    for jf in sorted(glob.glob(os.path.join(SRC, "*.json"))):
        key = os.path.splitext(os.path.basename(jf))[0]
        if key == "广东":
            print("  广东由主服务负责（167 频道 + 13 台中转池），跳过")
            continue
        mf = os.path.splitext(jf)[0] + ".m3u"
        if not os.path.exists(mf):
            print(f"跳过 {key}（没有 m3u）")
            continue
        try:
            meta = json.load(open(jf, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            print(f"跳过 {key}（json 坏了）")
            continue
        chans = read_m3u(mf, key)
        if not chans:
            print(f"跳过 {key}（没有频道）")
            continue
        relays = [{"host": r["host"], "mode": r.get("mode", "udp"),
                   "ok": None, "kbps": r.get("kbps", 0), "last_check": ""}
                  for r in meta.get("relays", [])]
        act = meta.get("active") or {}
        item = {
            "key": key, "name": key, "group": group_name(key),
            "channels": chans, "relays": relays,
            "active": {"host": act.get("host", ""), "mode": act.get("mode", "udp")},
        }
        out.append(item)
        print(f"  {key:<6} 频道 {len(chans):>4}  中转 {len(relays)}  "
              f"当前 {item['active']['host']}")

    if not out:
        print("没有任何地区数据")
        return 1
    os.makedirs(os.path.dirname(DST), exist_ok=True)
    json.dump({"regions": out}, open(DST, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\n已写入 {DST}：{len(out)} 个地区，合计频道 {sum(len(x['channels']) for x in out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""filter_top.py —— 把 IPTVZB 巡检产物按频道裁剪：每个频道最多留 N 条（不测速）。

实测该列表 5781 条 / 1178 频道，多数频道只有 1–5 条，
只有 65 个频道超过 20 条（最多 30）。所以裁剪只是去掉超出部分，保留原有顺序。

裁剪后的列表作为「巡检列表」的数据源，由 sync_users.py 做失效性检测后再推送，
本脚本只负责裁剪 + 推送原始文件，不做任何测速。

用法：
    python3 filter_top.py            # 裁剪并推送
    python3 filter_top.py --dry      # 只统计不推送
"""
import argparse
import collections
import json
import os
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
SRC = os.environ.get("TOP_SRC", "https://zzf.new.tg/checked/live.m3u")
UA = "Mozilla/5.0 (compatible; top-n/1.0)"
TOP_N = int(os.environ.get("TOP_N", "20"))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "ignore")


def parse(text):
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


def name_of(extinf):
    return (extinf or "").rsplit(",", 1)[-1].strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--top", type=int, default=TOP_N)
    a = ap.parse_args()

    print(f"拉取 {SRC}")
    pairs = parse(fetch(SRC))
    if not pairs:
        print("没有拿到线路")
        return 1
    print(f"原始 {len(pairs)} 条")

    groups = collections.OrderedDict()
    for e, u in pairs:
        groups.setdefault(name_of(e), []).append((e, u))

    over = {n: v for n, v in groups.items() if len(v) > a.top}
    trimmed = sum(len(v) - a.top for v in over.values())
    print(f"频道 {len(groups)} 个，超过 {a.top} 条的有 {len(over)} 个，裁掉 {trimmed} 条")

    lines = ["#EXTM3U",
             f"# 每频道最多 {a.top} 条 {time.strftime('%Y-%m-%d %H:%M')}"]
    total = 0
    for n, v in groups.items():
        for e, u in v[: a.top]:
            lines.append(e)
            lines.append(u)
        total += min(len(v), a.top)

    stats = {"built": time.strftime("%Y-%m-%d %H:%M:%S"),
             "src_total": len(pairs), "out_total": total,
             "channels": len(groups), "trimmed_channels": len(over), "top": a.top}
    print(f"输出 {total} 条（省下 {len(pairs) - total}）")

    if a.dry:
        return 0

    os.makedirs(OUT, exist_ok=True)
    p = os.path.join(OUT, "top20.m3u")
    open(p, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")
    json.dump(stats, open(os.path.join(OUT, "top20.stats.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    try:
        from push_nas import DEFAULT_REPO, load_token, push_file
        push_file(DEFAULT_REPO, "output/top20.m3u", open(p, "rb").read(), load_token())
        print("已推送 output/top20.m3u")
    except Exception as ex:  # noqa: BLE001
        print(f"推送失败：{ex}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

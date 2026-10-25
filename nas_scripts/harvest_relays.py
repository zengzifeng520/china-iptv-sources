#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""harvest_relays.py —— 从自家巡检产物里捞中转，自动扩充中转池。

原理：`output/live.txt` / `all.txt` / `checked/live.txt` 里已有的中转，
凡是能服务广东组播段（239.77.x / 239.253.43.x）的，对我们那 167 个频道同样有效。
把它们收进 relays.json，主服务下一轮体检（≤10 分钟）就会自动纳入比较。

用法：
    python3 harvest_relays.py            # 收录
    python3 harvest_relays.py --dry      # 只看有哪些候选
"""
import json
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
POOL = "/vol1/1000/iptv-sub/data/relays.json"
MAX_RELAYS = int(os.environ.get("MAX_RELAYS", "16"))

SOURCES = [
    os.path.join(BASE, "output", "live.txt"),
    os.path.join(BASE, "output", "all.txt"),
    os.path.join(BASE, "output", "checked", "live.txt"),
]
# 远程候选源：可在 harvest_sources.txt 里增删（每行一个 URL，# 开头为注释）
SOURCES_FILE = os.path.join(BASE, "harvest_sources.txt")
REMOTE_DEFAULT = [
    "https://zzf.new.tg/checked/live.m3u",
    "https://zzf.new.tg/guangdong.txt",
]


def load_remote():
    if os.path.exists(SOURCES_FILE):
        urls = []
        with open(SOURCES_FILE, encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if t and not t.startswith("#"):
                    urls.append(t)
        if urls:
            return urls
    return list(REMOTE_DEFAULT)


REMOTE = load_remote()
# 我们关心的组播段前缀
PREFIXES = ("239.77.", "239.253.43.")
URL = re.compile(r"https?://([^/\s]+)/(rtp|udp)/+(\d{1,3}(?:\.\d{1,3}){3}):(\d{1,5})")
UA = "Mozilla/5.0 (compatible; nas-harvest/1.0)"


def _extract(text, found):
    for m in URL.finditer(text):
        if m.group(3).startswith(PREFIXES):
            found.setdefault(m.group(1), m.group(2))


def scan():
    """返回 {host: mode}，只收服务于目标组播段的中转"""
    found = {}
    for path in SOURCES:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as f:
                _extract(f.read(), found)
        except OSError:
            continue
    # 远程源（失败就算了，不影响本地结果）
    import urllib.request
    for url in REMOTE:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as resp:
                _extract(resp.read(4 * 1024 * 1024).decode("utf-8", "ignore"), found)
        except Exception:  # noqa: BLE001
            continue
    return found


def load_pool():
    if os.path.exists(POOL):
        try:
            return json.load(open(POOL, encoding="utf-8")).get("relays", [])
        except Exception:  # noqa: BLE001
            pass
    return []


def main():
    dry = "--dry" in sys.argv
    cand = scan()
    if not cand:
        print("产物里没找到候选（可能 output/live.txt 还没生成）")
        return 0

    pool = load_pool()
    known = {r["host"] for r in pool}
    new = [(h, m) for h, m in cand.items() if h not in known]

    print("候选 %d 个（已收录 %d，新增 %d）" % (len(cand), len(cand) - len(new), len(new)))
    for h, m in cand.items():
        mark = "已有" if h in known else "新增"
        print("  [%s] %-30s %s" % (mark, h, m))
    if dry or not new:
        return 0

    for h, m in new:
        pool.append(dict(host=h, mode=m, ok=None, kbps=0, last_check=""))

    # 超量时优先淘汰已经判定失效的、且最久没检查的
    dropped = 0
    if len(pool) > MAX_RELAYS:
        alive = [r for r in pool if r.get("ok") is not False]
        dead = sorted([r for r in pool if r.get("ok") is False],
                      key=lambda r: r.get("last_check") or "")
        keep = alive[:MAX_RELAYS]
        room = MAX_RELAYS - len(keep)
        pool = keep + dead[:max(0, room)]
        dropped = len(known | {h for h, _ in new}) - len(pool)
        print("超过上限 %d 台，淘汰 %d 台失效中转" % (MAX_RELAYS, max(0, dropped)))

    os.makedirs(os.path.dirname(POOL), exist_ok=True)
    json.dump(dict(relays=pool), open(POOL, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("中转池现有 %d 台" % len(pool))
    return 0


if __name__ == "__main__":
    sys.exit(main())

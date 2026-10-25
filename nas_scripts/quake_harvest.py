#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""quake_harvest.py —— 通过 360 网络空间测绘（Quake）发现新的 udpxy 中转。

**这不是主动扫描。** Quake 已经长期扫描并索引全网，
这里只是用它的搜索 API 查「某个省某个运营商的 udpxy」，再逐个验证能否拉到我们自己的频道。

用法：
    echo "<你的 Quake Token>" > quake_token.txt
    python3 quake_harvest.py                 # 默认：广东 + 电信
    python3 quake_harvest.py --pages 5 --size 20
    python3 quake_harvest.py --dry           # 只查不写入
    python3 quake_harvest.py --query 'udpxy AND province_cn:"广东"'

验证两关：
  1) /status 是否像 udpxy
  2) 用我们的真实频道试拉 3 秒，能出 20KB 以上才算可用（这一步最关键，直接过滤掉拉不到广东组播的）
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

BASE = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(BASE, "quake_token.txt")
POOL = "/vol1/1000/iptv-sub/data/relays.json"
CORE = "/vol1/1000/iptv-sub/data/channels_core.json"

API = "https://quake.360.net/api/v3/search/quake_service"
DEFAULT_QUERY = 'udpxy AND country_cn: "中国" AND province_cn: "广东" AND isp: "电信"'
UA = "Mozilla/5.0 (compatible; nas-quake-harvest/1.0)"
MCAST = "239.77.0.185:5146"   # 默认验证频道，main() 会按骨架覆盖
MAX_RELAYS = 12


def load_token():
    tok = os.environ.get("QUAKE_TOKEN", "").strip()
    if tok:
        return tok
    if os.path.exists(TOKEN_FILE):
        return open(TOKEN_FILE, encoding="utf-8").read().strip()
    return ""


def quake_page(token, query, start, size):
    body = json.dumps({
        "query": query,
        "start": start,
        "size": size,
        "ignore_cache": False,
        "latest": True,
        "include": ["ip", "port", "location.city_cn"],
    }).encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "X-QuakeToken": token,
        "Content-Type": "application/json",
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def candidates(token, query, pages, size):
    out, seen = [], set()
    for p in range(pages):
        start = p * size
        try:
            d = quake_page(token, query, start, size)
        except Exception as e:  # noqa: BLE001
            print("  查询失败：%s" % e)
            break
        if d.get("code") != 0:
            print("  接口返回：%s" % (d.get("message") or d.get("code")))
            break
        items = d.get("data") or []
        if not items:
            break
        for it in items:
            ip, port = it.get("ip"), it.get("port")
            if not ip or not port:
                continue
            host = f"{ip}:{port}"
            if host in seen:
                continue
            seen.add(host)
            city = ((it.get("location") or {}).get("city_cn") or "")
            out.append((host, city))
        total = ((d.get("meta") or {}).get("pagination") or {}).get("total", 0)
        print("  第 %d 页：%d 条（累计 %d / 共 %s）" % (p + 1, len(items), len(out), total))
        if len(out) >= total:
            break
    return out


def is_udpxy(host):
    try:
        req = urllib.request.Request(f"http://{host}/status", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read(8192).decode("utf-8", "ignore").lower()
        return ("udpxy" in body) or ("<table" in body and "client" in body)
    except Exception:  # noqa: BLE001
        return False


def can_play(host, mcast, mode, secs=3):
    url = f"http://{host}/{mode}/{mcast}"
    try:
        r = subprocess.run(["curl", "-s", "-o", os.devnull, "--max-time", str(secs),
                            "-w", "%{size_download}", "-A", UA, url],
                           capture_output=True, text=True, timeout=secs + 6)
        return int(r.stdout.strip() or 0)
    except Exception:  # noqa: BLE001
        return 0


def verify(item):
    host, city = item
    if not is_udpxy(host):
        return None
    for mode in ("rtp", "udp"):
        size = can_play(host, MCAST, mode)
        if size > 20000:
            return dict(host=host, mode=mode, ok=None, kbps=int(size * 8 / 3 / 1000),
                        last_check="", city=city)
    return None


def load_pool():
    if os.path.exists(POOL):
        try:
            return json.load(open(POOL, encoding="utf-8")).get("relays", [])
        except Exception:  # noqa: BLE001
            pass
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--pages", type=int, default=5)
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    global MCAST
    try:
        core = json.load(open(CORE, encoding="utf-8"))
        MCAST = core[0]["mcast"]
    except Exception:  # noqa: BLE001
        MCAST = "239.77.0.185:5146"
    print("验证频道：%s" % MCAST)

    token = load_token()
    if not token:
        print("\n没有 Quake Token。获取方式：")
        print("  1) 注册 https://quake.360.net")
        print("  2) 个人中心拿 API Token（有免费额度，超出需付费）")
        print("  3) echo \"<token>\" > %s" % TOKEN_FILE)
        return 1

    print("查询：%s" % a.query)
    cand = candidates(token, a.query, a.pages, a.size)
    if not cand:
        print("没有候选")
        return 0
    print("\n候选 %d 个，开始验证（先看 /status，再用真实频道试拉）" % len(cand))

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(verify, cand))

    good = [r for r in results if r]
    print("可用 %d / %d" % (len(good), len(cand)))
    for g in good:
        print("  √ %-24s %-4s %6d kbps  %s" % (g["host"], g["mode"], g["kbps"], g["city"]))

    if a.dry or not good:
        return 0

    pool = load_pool()
    known = {r["host"] for r in pool}
    added = 0
    for g in good:
        if g["host"] in known:
            continue
        g.pop("city", None)
        pool.append(g)
        added += 1
    if len(pool) > MAX_RELAYS:
        pool = pool[:MAX_RELAYS]
    os.makedirs(os.path.dirname(POOL), exist_ok=True)
    json.dump(dict(relays=pool), open(POOL, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("新增 %d 台，中转池现有 %d 台（服务 ≤10 分钟内自动纳入）" % (added, len(pool)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

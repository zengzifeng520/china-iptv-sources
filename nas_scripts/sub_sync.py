# -*- coding: utf-8 -*-
"""
站点订阅地址同步工具
====================
你在浏览器里拿到带 token 的订阅地址后，把它交给这个脚本，
它会负责：拉取 → 校验 → 存成本地播放列表 → 定时刷新 → token 失效时提醒你。

用法：
    python sub_sync.py -u "https://taoiptv.com/xxx.m3u?token=abcdef"
    python sub_sync.py                  # 用上次保存的地址重新拉一次
    python sub_sync.py --loop 30        # 每 30 分钟自动刷一次
    python sub_sync.py --info           # 看当前保存的地址与状态

产物：
    output/site/订阅.m3u       直接丢进播放器用
    output/site/订阅.txt       同内容文本版
    sub.json                   记住地址和上次结果
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output", "site")
CONF = os.path.join(HERE, "sub.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def load_conf():
    if os.path.exists(CONF):
        try:
            return json.load(open(CONF, encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_conf(c):
    json.dump(c, open(CONF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def fetch(url):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://taoiptv.com/"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.read()


def looks_blocked(raw: bytes):
    head = raw[:600].decode("utf-8", "ignore").lower()
    if "<!doctype" in head or "<html" in head:
        if "turnstile" in head or "challenges.cloudflare" in head:
            return "被 Cloudflare 人机验证拦截（token 失效或地址需在浏览器里重新生成）"
        if "登录" in raw[:2000].decode("utf-8", "ignore") or "login" in head:
            return "返回了登录页（token 失效）"
        return "返回了 HTML 而不是播放列表（token 失效或地址不对）"
    return None


def parse(raw: bytes):
    """返回 [(name, url)]"""
    txt = raw.decode("utf-8", "ignore").replace("\r", "")
    out, pending = [], None
    for line in txt.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            m = re.search(r",(.+?)\s*$", s)
            pending = m.group(1).strip() if m else ""
            continue
        if s.startswith("#"):
            continue
        if "://" in s:
            name = pending or s.rsplit("/", 1)[-1]
            if "," in s and "://" in s.split(",", 1)[1]:
                name, s = s.split(",", 1)
                name = name.strip()
            out.append((name.strip(), s.strip()))
            pending = None
    return out


def harvest_relays(items):
    """把列表里出现的中转服务器收进本地中转池，池子越用越厚"""
    hosts = {}
    for _, u in items:
        m = re.search(r"https?://([^/\s]+)/(rtp|udp)/", u)
        if m:
            hosts[m.group(1)] = m.group(2)
    if not hosts:
        return []
    pool_file = os.path.join(HERE, "relays.json")
    pool = []
    if os.path.exists(pool_file):
        try:
            pool = json.load(open(pool_file, encoding="utf-8")).get("relays", [])
        except Exception:
            pool = []
    known = {r["host"] for r in pool}
    added = []
    for h, mode in hosts.items():
        if h not in known:
            pool.append(dict(host=h, mode=mode, ok=None, kbps=0, last_check=""))
            added.append(h)
    json.dump(dict(relays=pool), open(pool_file, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    return added


def report(status, raw, conf):
    why = looks_blocked(raw)
    if why:
        print(f"[失败] HTTP {status} — {why}")
        conf["last_error"] = why
        conf["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        save_conf(conf)
        return False

    items = parse(raw)
    if not items:
        print(f"[失败] HTTP {status} 但没解析出频道（返回 {len(raw)} 字节）")
        return False

    os.makedirs(OUT, exist_ok=True)
    m3u = ["#EXTM3U"] + [f'#EXTINF:-1,{n}\n{u}' for n, u in items]
    open(os.path.join(OUT, "订阅.m3u"), "w", encoding="utf-8").write("\n".join(m3u))
    open(os.path.join(OUT, "订阅.txt"), "w", encoding="utf-8").write(
        "\n".join(f"{n},{u}" for n, u in items))

    schemes = {}
    for _, u in items:
        schemes[u.split("://")[0]] = schemes.get(u.split("://")[0], 0) + 1

    conf.update(last_ok=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                last_error="", count=len(items), size=len(raw),
                schemes=schemes)
    save_conf(conf)

    added = harvest_relays(items)

    print(f"[成功] HTTP {status}　{len(items)} 个频道　{len(raw)/1024:.0f} KB")
    print(f"       协议分布 {schemes}")
    if added:
        print(f"       中转池新增 {len(added)} 台：{', '.join(added)}")
    print(f"       前 3 个：")
    for n, u in items[:3]:
        print(f"         {n}  ->  {u}")
    print(f"\n播放列表：{os.path.join(OUT, '订阅.m3u')}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-u", "--url", default="", help="浏览器里复制到的完整订阅地址")
    ap.add_argument("--loop", type=int, default=0, help="每 N 分钟刷新一次")
    ap.add_argument("--info", action="store_true")
    a = ap.parse_args()

    conf = load_conf()
    if a.url:
        conf["url"] = a.url
        save_conf(conf)

    if a.info or not conf.get("url"):
        if not conf.get("url"):
            print("还没有保存订阅地址。\n")
            print("获取步骤：")
            print("  1) 浏览器打开 https://taoiptv.com ，等验证自动通过")
            print("  2) 选择你所在地区的宽带线路")
            print("  3) 页面上会给出地址和 token，拼起来复制")
            print("  4) 运行： python sub_sync.py -u \"<完整地址>\"")
            return
        print(f"地址      {conf['url'][:110]}")
        print(f"上次成功  {conf.get('last_ok') or '—'}")
        print(f"上次失败  {conf.get('last_error') or '—'}")
        print(f"频道数    {conf.get('count') or '—'}")

    if a.info:
        return

    url = conf["url"]
    print(f"[拉取] {url[:100]}{'...' if len(url) > 100 else ''}")
    try:
        st, raw = fetch(url)
        ok = report(st, raw, conf)
    except Exception as e:
        print(f"[异常] {e}")
        conf["last_error"] = str(e)
        save_conf(conf)
        ok = False

    if a.loop and a.loop > 0:
        print(f"\n每 {a.loop} 分钟自动刷新，Ctrl+C 退出")
        while True:
            time.sleep(a.loop * 60)
            print(f"\n[{datetime.now():%H:%M:%S}] 刷新中")
            try:
                st, raw = fetch(conf["url"])
                report(st, raw, conf)
            except Exception as e:
                print(f"[异常] {e}")


if __name__ == "__main__":
    main()

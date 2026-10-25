#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每小时巡检：复检 output/live.m3u / live.txt 的每条线路，连续 2 次失败才剔除。

结果写入 output/checked/（独立订阅地址），不改动 live.*：
  output/checked/live.m3u
  output/checked/live.txt
  output/checked/stats.json
  output/checked/state.json   （失败计数，本地状态）

只有内容变化时才推送，避免每小时产生噪音提交。
"""
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
CHK = os.path.join(OUT, "checked")
STATE = os.path.join(CHK, "state.json")

TIMEOUT = float(os.environ.get("CHECK_TIMEOUT", "6"))
WORKERS = int(os.environ.get("CHECK_WORKERS", "48"))
MAX_FAILS = int(os.environ.get("MAX_FAILS", "2"))
MIN_ALIVE = float(os.environ.get("MIN_ALIVE_RATIO", "0.3"))

# 按分组排除巡检：check_exclude.txt 每行一个分组名（# 开头为注释），
# 也可用环境变量 CHECK_EXCLUDE_GROUPS（逗号分隔）。
# 被排除的分组仍保留在订阅里，只是不再检测、不会被剔除。
_EXCLUDE_FILE = os.path.join(BASE, "check_exclude.txt")


def _load_exclude():
    groups = set()
    if os.path.exists(_EXCLUDE_FILE):
        with open(_EXCLUDE_FILE, encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if t and not t.startswith("#"):
                    groups.add(t)
    for g in os.environ.get("CHECK_EXCLUDE_GROUPS", "").split(","):
        if g.strip():
            groups.add(g.strip())
    return groups


EXCLUDE_GROUPS = _load_exclude()

UA = "Mozilla/5.0 (compatible; nas-hourly-check/1.0)"
SUFFIX = re.compile(r"\$LR.*$")


def base_url(s):
    return SUFFIX.sub("", (s or "").strip())


def is_http(u):
    return u.lower().startswith(("http://", "https://"))


def probe(url):
    if not is_http(url):
        return True  # rtp/rtsp 等无法公网探测，保留
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Range": "bytes=0-2047", "Accept": "*/*"}
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            if r.status not in (200, 206):
                return False
            data = r.read(2048)
            if data:
                return True
            ct = (r.headers.get("Content-Type") or "").lower()
            return "mpegurl" in ct
    except Exception:
        return False


def parse_m3u(path):
    header, entries, pending = "#EXTM3U", [], None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            up = s.upper()
            if up.startswith("#EXTM3U"):
                header = s
            elif s.startswith("#EXTINF"):
                pending = s
            elif s.startswith("#"):
                continue
            else:
                entries.append((pending, s))
                pending = None
    return header, entries


def parse_txt(path):
    groups = []
    cur = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            if "#genre#" in s:
                cur = re.sub(r"^#\s*", "", s.split(",")[0]).strip()
                groups.append((cur, []))
                continue
            if cur is None:
                continue
            name, sep, url = s.partition(",")
            if sep and url.strip():
                groups[-1][1].append((name.strip(), url.strip()))
    return groups


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def main():
    started = time.time()
    m3u_path = os.path.join(OUT, "live.m3u")
    txt_path = os.path.join(OUT, "live.txt")
    if not os.path.exists(m3u_path):
        print("找不到 output/live.m3u，退出")
        return 0

    header, m3u_entries = parse_m3u(m3u_path)
    txt_groups = parse_txt(txt_path) if os.path.exists(txt_path) else []

    # 分组排除：只属于被排除分组的地址本轮不检测（仍保留在订阅中）
    url_groups = {}
    for _g, items in txt_groups:
        for _n, url in items:
            url_groups.setdefault(base_url(url), set()).add(_g)
    in_txt = set(url_groups)

    excluded_urls, keep_urls = set(), set()
    for b, gs in url_groups.items():
        if gs and gs <= EXCLUDE_GROUPS:
            excluded_urls.add(b)
        else:
            keep_urls.add(b)
    # live.m3u 里额外出现、txt 中没有的地址照常检测
    for _extinf, url in m3u_entries:
        b = base_url(url)
        if b not in in_txt:
            keep_urls.add(b)

    if excluded_urls:
        print("按分组跳过巡检 %d 个地址（%s）"
              % (len(excluded_urls), "、".join(sorted(EXCLUDE_GROUPS))))

    targets = {}
    for b in keep_urls:
        targets[b] = True

    urls = list(targets.keys())
    if not urls:
        print("源文件为空，退出")
        return 0

    print("待检 %d 个唯一地址，并发 %d，超时 %ss，最多 %d 次失败后剔除" % (len(urls), WORKERS, TIMEOUT, MAX_FAILS))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        results = list(ex.map(probe, urls))
    alive = sum(1 for r in results if r)
    ratio = alive / len(urls)
    print("存活 %d/%d（%.1f%%），耗时 %.0fs" % (alive, len(urls), ratio * 100, time.time() - t0))

    if len(urls) >= 50 and ratio < MIN_ALIVE:
        print("存活率低于 %.0f%%，疑似网络异常，本轮不写入、不推送" % (MIN_ALIVE * 100))
        return 2

    now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    state = load_state()
    new_state = {}
    dead = set()
    for url, ok in zip(urls, results):
        if ok:
            continue
        fails = int((state.get(url) or {}).get("fails", 0)) + 1
        new_state[url] = {"fails": fails, "last_fail": now}
        if fails >= MAX_FAILS:
            dead.add(url)

    kept_m3u = [(e, u) for e, u in m3u_entries if base_url(u) not in dead]
    kept_txt = []
    for g, items in txt_groups:
        kept = [(n, u) for n, u in items if base_url(u) not in dead]
        if kept:
            kept_txt.append((g, kept))

    # 统一分组顺序 + 组内排序（失败不影响巡检）
    try:
        from group_sort import sort_pairs, sort_txt_groups, MAIN_ORDER
        kept_m3u = sort_pairs(kept_m3u, MAIN_ORDER)
        kept_txt = sort_txt_groups(kept_txt, MAIN_ORDER)
    except Exception as exc:  # noqa: BLE001
        print("分组排序跳过：%s" % exc)

    os.makedirs(CHK, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    m3u_out = header + "\n# 更新时间：%s（北京时间）\n" % stamp
    for extinf, url in kept_m3u:
        if extinf:
            m3u_out += extinf + "\n"
        m3u_out += url + "\n"

    txt_out = "# 更新时间：%s（北京时间）\n" % stamp
    for g, items in kept_txt:
        txt_out += "# %s,#genre#\n" % g
        for n, u in items:
            txt_out += "%s,%s\n" % (n, u)

    stats = {
        "updated": now,
        "checked": len(urls),
        "alive": alive,
        "alive_ratio": round(ratio, 4),
        "max_fails": MAX_FAILS,
        "dead_now": len(dead),
        "kept_m3u": len(kept_m3u),
        "kept_txt": sum(len(v) for _g, v in kept_txt),
        "duration_s": round(time.time() - started, 1),
    }

    def write_if_changed(path, content):
        def strip_time(text):
            return "\n".join(l for l in text.splitlines() if not l.startswith("# 更新时间"))

        try:
            with open(path, encoding="utf-8") as f:
                if strip_time(f.read()) == strip_time(content):
                    return False
        except Exception:
            pass
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        return True

    changed_m3u = write_if_changed(os.path.join(CHK, "live.m3u"), m3u_out)
    changed_txt = write_if_changed(os.path.join(CHK, "live.txt"), txt_out)
    with open(os.path.join(CHK, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
        f.write("\n")
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")

    print("剔除 %d 个连续失败地址；m3u %d -> %d 条，txt 分组 %d -> %d 条"
          % (len(dead), len(m3u_entries), len(kept_m3u), len(txt_groups), stats["kept_txt"]))

    if not (changed_m3u or changed_txt):
        print("验证列表无变化，跳过推送")
        return 0

    try:
        sys.path.insert(0, BASE)
        from push_nas import DEFAULT_REPO, load_token, push_file
        token = load_token()
        for name in ("live.m3u", "live.txt", "stats.json"):
            with open(os.path.join(CHK, name), "rb") as f:
                data = f.read()
            push_file(DEFAULT_REPO, "output/checked/" + name, data, token)
            print("已推送 output/checked/%s (%d bytes)" % (name, len(data)))
    except Exception as e:  # noqa: BLE001
        print("推送失败: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

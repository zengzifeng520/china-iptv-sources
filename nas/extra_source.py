#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""extra_source.py —— 外部节目源：拉取 + 巡检 + 按分组放在订阅最前面。

支持多个源、多个分组，按 extra_sources.json 中的顺序输出。
默认顺序：央视频（NAS 本地接口）→ 网络直播。
EXTRA_MIN_MBPS > 0 时才做测速过滤；默认 0 = 只判能不能连。
"""
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(BASE, "output", "extra_source.m3u")
CONFIG_FILE = os.path.join(BASE, "extra_sources.json")
CACHE_TTL = int(os.environ.get("EXTRA_CACHE_TTL", "3600"))
GROUP_NAME = os.environ.get("EXTRA_GROUP", "网络直播")
MIN_MBPS = float(os.environ.get("EXTRA_MIN_MBPS", "0"))
DEFAULT_SOURCES = [
    {
        "name": "央视频",
        "group": "央视频",
        "url": "http://192.168.3.21:8866/playlist.m3u",
    },
    {
        "name": "网络直播",
        "group": "网络直播",
        "url": "https://mpimg.cn/down.php/8a7c70ba5d709b2876b54c63d0acda08.txt",
    },
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_GROUP_RE = re.compile(r'group-title="([^"]*)"')

# 频道归类排序规则（按频道名关键词，用于"广东电信"等需组内排序的源）
# 顺序即排序优先级：央视 -> 卫视 -> 广东频道 -> 深圳频道 -> 地区频道
_CATEGORY_RULES = [
    ("央视", lambda n: n.startswith("CCTV") or n.startswith("CGTN")),
    ("卫视", lambda n: n.endswith("卫视")),
    ("广东频道", lambda n: n.startswith("广东")),
    ("深圳频道", lambda n: n.startswith("深圳")),
]


def _category_rank(name):
    """返回频道名的类别序号：央视0 / 卫视1 / 广东2 / 深圳3 / 地区4（兜底）。"""
    name = name or ""
    if name == "0":
        return 999  # 异常频道名"0"排最后
    for i, (_cat, rule) in enumerate(_CATEGORY_RULES):
        if rule(name):
            return i
    return len(_CATEGORY_RULES)


# 全局分组显示顺序（用户指定；前缀匹配，未列出的分组排最后）
_GROUP_ORDER = [
    "更新时间", "广东电信", "网络直播", "央视频道", "卫视频道", "地方频道",
    "4K节目", "广东频道", "上海节目", "浙江节目", "重庆节目", "湖南节目",
    "港台节目", "四川节目", "北京节目", "江苏节目", "福建节目", "中数传媒",
    "IPTV", "其他", "系统",
]


def _group_sort_key(group):
    for i, key in enumerate(_GROUP_ORDER):
        if group.startswith(key):
            return i
    return len(_GROUP_ORDER)


def _reorder_groups(blocks):
    """按 _GROUP_ORDER 重排分组顺序（组内保持原顺序）。"""
    by_group = {}
    for e_, u, g in blocks:
        by_group.setdefault(g, []).append((e_, u, g))
    ordered = sorted(by_group.keys(), key=_group_sort_key)
    out = []
    for g in ordered:
        out.extend(by_group[g])
    return out


def _load_specs():
    raw_env = os.environ.get("EXTRA_SOURCES", "").strip()
    if raw_env:
        return [
            {"name": u.strip(), "group": GROUP_NAME, "url": u.strip()}
            for u in raw_env.split(",") if u.strip()
        ]

    items = None
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                items = data.get("sources") or data.get("extra_sources") or []
            elif isinstance(data, list):
                items = data
        except Exception as exc:  # noqa: BLE001
            print("外部源：读取 %s 失败：%s" % (CONFIG_FILE, exc), flush=True)
    if not items:
        items = DEFAULT_SOURCES

    specs = []
    for item in items:
        if isinstance(item, str):
            name, group, url, enabled = "", GROUP_NAME, item, True
        else:
            name = str(item.get("name") or "")
            group = str(item.get("group") or GROUP_NAME).strip()
            url = str(item.get("url") or "").strip()
            enabled = bool(item.get("enabled", True))
        if not url or not enabled:
            continue
        cat = bool(item.get("categorize")) if isinstance(item, dict) else False
        specs.append({"name": name or group, "group": group or GROUP_NAME, "url": url, "categorize": cat})
    return specs


def _fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _parse(data):
    text = None
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return []
    out, pend = [], None
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            pend = s
        elif s.startswith("#"):
            continue
        elif s.lower().startswith(("http://", "https://", "rtp://", "udp://", "rtsp://")):
            if "," in s and "://" in s.split(",", 1)[0]:
                pend = None
                continue
            name = pend.rsplit(",", 1)[-1].strip() if pend else "未命名"
            out.append((name, s, pend))
            pend = None
        elif "," in s:
            a, _, b = s.partition(",")
            b = b.strip()
            if b.lower().startswith(("http://", "https://", "rtp://", "udp://", "rtsp://")):
                out.append((a.strip() or "未命名", b, None))
    return out


def _alive(url, timeout=6):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status not in (200, 206):
                return False
            return bool(r.read(4096))
    except Exception:  # noqa: BLE001
        return False


def _speed(url, secs=3.0):
    t0 = time.time()
    n = 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=6) as r:
            while time.time() - t0 < secs:
                b = r.read(65536)
                if not b:
                    break
                n += len(b)
    except Exception:  # noqa: BLE001
        return 0.0
    return n * 8 / max(0.3, time.time() - t0) / 1e6


def _extinf(name, group):
    safe = name.replace('"', "'")
    return '#EXTINF:-1 tvg-name="%s" group-title="%s",%s' % (safe, group, safe)


def _name_of(extinf):
    return (extinf or "").rsplit(",", 1)[-1].strip() or "未命名"


def _load_cache(allow_stale=False):
    if not os.path.exists(CACHE):
        return None
    if not allow_stale and time.time() - os.path.getmtime(CACHE) > CACHE_TTL:
        return None
    blocks = []
    pend = None
    for raw in open(CACHE, encoding="utf-8", errors="replace"):
        s = raw.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            pend = s
        elif s.startswith("#"):
            continue
        else:
            m = _GROUP_RE.search(pend or "")
            group = m.group(1) if m else GROUP_NAME
            blocks.append((pend, s, group))
            pend = None
    return blocks or None


def _write_cache(blocks):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE, "w", encoding="utf-8", newline="\n") as f:
        for e_, u, _g in blocks:
            f.write(e_ + "\n" + u + "\n")


def _group_items(specs, verbose=True):
    grouped = {}
    order = []
    categorized = set()
    for spec in specs:
        default_group = spec["group"]
        try:
            got = _parse(_fetch(spec["url"]))
            if verbose:
                print("外部源[%s]：%s -> %d 条" % (default_group, spec["url"], len(got)), flush=True)
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print("外部源[%s]：拉取失败 %s (%s)" % (default_group, spec["url"], exc), flush=True)
            got = []
        if spec.get("categorize"):
            # 单独分组：全部归入 spec["group"]，组内按 央视→卫视→广东→深圳→地区 排序
            categorized.add(default_group)
            items = []
            for n, u, ext in got:
                if ext and "group-title=" in ext:
                    ext = _GROUP_RE.sub('group-title="%s"' % default_group, ext)
                items.append((n, u, ext))
            items.sort(key=lambda x: _category_rank(x[0]))
            if default_group not in grouped:
                grouped[default_group] = []
                order.append(default_group)
            grouped[default_group].extend(items)
        else:
            for n, u, ext in got:
                # M3U 源保留原始 group-title 分组；TXT 源用 spec 里的 group
                g = None
                if ext:
                    m = _GROUP_RE.search(ext)
                    if m:
                        g = m.group(1)
                g = g or default_group
                if g not in grouped:
                    grouped[g] = []
                    order.append(g)
                grouped[g].append((n, u, ext))
    return grouped, order, categorized


def get_extra_blocks(workers=32, verbose=True, force=None):
    """返回 [(EXTINF, url), ...]，顺序 = extra_sources.json 中的分组顺序。"""
    if force is None:
        force = os.environ.get("EXTRA_FORCE", "").strip().lower() not in ("", "0", "false", "no")
    cached = _load_cache()
    if cached and not force:
        if verbose:
            groups = []
            for _e, _u, g in cached:
                if g not in groups:
                    groups.append(g)
            print("外部源：使用缓存 %d 条（%s）" % (len(cached), " → ".join(groups)), flush=True)
        return [(e_, u) for e_, u, _g in cached]

    cached_all = _load_cache(allow_stale=True) or []
    specs = _load_specs()
    if not specs:
        return [(e_, u) for e_, u, _g in cached_all]

    grouped, order, categorized = _group_items(specs, verbose=verbose)

    # 全局按 URL 去重，保留分组顺序；categorize 单独分组（如广东电信）跳过去重，保留完整双线路
    seen = set()
    for group in order:
        uniq = []
        for n, u, ext in grouped[group]:
            if group not in categorized and u in seen:
                continue
            if group not in categorized:
                seen.add(u)
            uniq.append((n, u, ext))
        grouped[group] = uniq

    all_items = [(n, u, g) for g in order for n, u, _ext in grouped[g]]
    if not all_items:
        return [(e_, u) for e_, u, _g in cached_all]

    # 轻量模式：默认跳过逐条频道连通性检测，只拉取订阅源并合并（巡检秒级完成，降低 NAS 负载）。
    # 如需恢复逐条检测，设环境变量 EXTRA_SKIP_CHECK=0 即可。
    skip_check = os.environ.get("EXTRA_SKIP_CHECK", "1").strip().lower() not in ("", "0", "false", "no")
    if skip_check:
        ok = {u for _n, u, _g in all_items}
    else:
        ok = set()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_alive, u): u for _n, u, _g in all_items}
            for f in as_completed(futs):
                u = futs[f]
                try:
                    if f.result():
                        ok.add(u)
                except Exception:  # noqa: BLE001
                    pass

    if verbose:
        total = len(all_items)
        print("外部源：巡检 %d 条，存活 %d 条" % (total, len(ok)), flush=True)

    if MIN_MBPS > 0 and ok:
        speeds = {}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_speed, u): u for u in ok}
            for f in as_completed(futs):
                u = futs[f]
                try:
                    speeds[u] = f.result()
                except Exception:  # noqa: BLE001
                    speeds[u] = 0.0
        ok = {u for u, mb in speeds.items() if mb >= MIN_MBPS}
        if verbose:
            print("外部源：测速 >= %.1f Mbps 的 %d 条" % (MIN_MBPS, len(ok)), flush=True)

    blocks = []
    for group in order:
        alive = [(n, u, ext) for n, u, ext in grouped[group] if u in ok]
        if not alive:
            # 某一组整体失败时保留上一轮该组的缓存，避免整组消失
            alive = [(_name_of(e_), u, e_) for e_, u, g in cached_all if g == group]
            if alive and verbose:
                print("外部源[%s]：本轮无存活，沿用上次缓存 %d 条" % (group, len(alive)), flush=True)
        for n, u, ext in alive:
            e_ = ext if ext else _extinf(n, group)
            blocks.append((e_, u, group))

    if not blocks:
        return [(e_, u) for e_, u, _g in cached_all]

    # 按 _GROUP_ORDER 重排分组顺序
    blocks = _reorder_groups(blocks)
    _write_cache(blocks)
    if verbose:
        counts = []
        for group in order:
            counts.append("%s=%d" % (group, sum(1 for _e, _u, g in blocks if g == group)))
        print("外部源：写入缓存 %d 条（%s）" % (len(blocks), "，".join(counts)), flush=True)
    return [(e_, u) for e_, u, _g in blocks]


if __name__ == "__main__":
    bs = get_extra_blocks()
    print("外部源合计：%d 条" % len(bs))
    for e_, u in bs[:8]:
        print("  ", e_.rsplit(",", 1)[-1], u[:70])

# -*- coding: utf-8 -*-
"""group_sort.py —— 分组顺序 + 组内频道排序 + 外部源合流。

两套顺序：
  group_order.txt       用户订阅（订阅信息 / 央视频道 优先）
  group_order_main.txt  主列表 live.txt / live.m3u（电信组播 / 网络直播 优先）

分组名可能带 emoji（🌃 上海节目），统一归一化后再匹配。
"""
import os
import re

BASE = os.path.dirname(os.path.abspath(__file__))
ORDER_FILE = os.path.join(BASE, "group_order.txt")
ORDER_MAIN_FILE = os.path.join(BASE, "group_order_main.txt")

DEFAULT_USER = [
    "订阅信息", "更新时间", "央视频道", "卫视频道", "地区频道",
    "4K节目", "电信组播", "网络直播", "广东频道", "上海节目",
    "浙江节目", "重庆节目", "湖南节目", "港台节目", "四川节目",
    "北京节目", "江苏节目", "福建节目", "山东节目", "中数传媒",
    "IPTV", "其他", "系统",
]

DEFAULT_MAIN = [
    "更新时间", "电信组播", "网络直播", "央视频道", "卫视频道",
    "地区频道", "4K节目", "广东频道", "上海节目", "浙江节目",
    "重庆节目", "湖南节目", "港台节目", "四川节目", "北京节目",
    "江苏节目", "福建节目", "中数传媒", "IPTV", "其他", "系统",
]

ALIAS = {
    "地方频道": "地区频道",
    "其他频道": "其他",
    "IPTViptv": "IPTV",
    "iptv": "IPTV",
}

_LEAD = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]+")
GROUP_RE = re.compile(r'group-title="([^"]*)"')


def load_order(path, default):
    if os.path.exists(path):
        out = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                t = line.strip()
                if t and not t.startswith("#"):
                    out.append(t)
        if out:
            return out
    return list(default)


USER_ORDER = load_order(ORDER_FILE, DEFAULT_USER)
MAIN_ORDER = load_order(ORDER_MAIN_FILE, DEFAULT_MAIN)


def norm(name):
    return _LEAD.sub("", (name or "").strip()).strip()


def key(name):
    s = norm(name)
    if s.startswith("订阅信息"):
        return "订阅信息"
    if s.startswith("更新时间"):
        return "更新时间"
    return ALIAS.get(s, s)


def natkey(s):
    """自然排序：CCTV2 排在 CCTV10 前面"""
    parts = re.split(r"(\d+)", s or "")
    return tuple((1, int(p)) if p.isdigit() else (0, p) for p in parts)


def _pos(order):
    return {g: i for i, g in enumerate(order)}


def rank(name, order=None):
    p = _pos(order or USER_ORDER)
    return p.get(key(name), len(p))


def name_of(extinf):
    return (extinf or "").rsplit(",", 1)[-1].strip()


def group_of(extinf):
    m = GROUP_RE.search(extinf or "")
    return m.group(1) if m else ""


# ---------- 文本解析 ----------
def split_m3u(text):
    lines = text.replace("\r", "").split("\n")
    head = lines[0] if lines and lines[0].upper().startswith("#EXTM3U") else "#EXTM3U"
    extra_heads = []
    pairs, pend = [], None
    for ln in lines[1:]:
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#EXTINF"):
            pend = s
            continue
        if s.startswith("#"):
            extra_heads.append(s)     # 如 # 更新时间：xxx
            continue
        if pend is not None:
            pairs.append((pend, s))
            pend = None
    return head, extra_heads, pairs


def split_txt(text):
    groups, cur, items = [], None, []
    for ln in text.replace("\r", "").split("\n"):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("#"):
            if "#genre#" in s:
                if cur is not None:
                    groups.append((cur, items))
                cur = s.lstrip("#").replace(",#genre#", "").strip()
                items = []
            continue
        if "," in s and cur is not None:
            n, u = s.split(",", 1)
            items.append((n.strip(), u.strip()))
    if cur is not None:
        groups.append((cur, items))
    return groups


# ---------- 排序 ----------
def sort_pairs(pairs, order=None):
    pos = _pos(order or USER_ORDER)

    def k(item):
        e = item[0]
        return (pos.get(key(group_of(e)), len(pos)), natkey(name_of(e)))

    return sorted(pairs, key=k)


def sort_txt_groups(groups, order=None):
    """同名分组合并 + 按顺序排列（保留原始分组名）"""
    pos = _pos(order or USER_ORDER)
    merged = {}
    for g, items in groups:
        k = key(g)
        if k not in merged:
            merged[k] = [norm(g) or g, []]
        merged[k][1].extend(items)

    out = []
    for k in sorted(merged, key=lambda x: (pos.get(x, len(pos)), x)):
        disp, items = merged[k]
        items = sorted(items, key=lambda nv: natkey(nv[0]))
        if items:
            out.append((disp, items))
    return out


# ---------- 外部源合流 ----------
def get_extras():
    try:
        from extra_source import get_extra_blocks
        return get_extra_blocks(verbose=False)
    except Exception:  # noqa: BLE001
        return []


def merge_m3u(text, blocks, order=None):
    head, extra_heads, pairs = split_m3u(text)
    seen = {u for _e, u in pairs}
    add = [(e, u) for e, u in blocks if u not in seen]
    pairs = sort_pairs(add + pairs, order)
    out = [head] + extra_heads
    for e, u in pairs:
        out.append(e)
        out.append(u)
    return "\n".join(out) + "\n"


def merge_txt(text, blocks, order=None):
    groups = split_txt(text)
    extra_groups = [(group_of(e) or "其他", [(name_of(e), u)]) for e, u in blocks]
    sg = sort_txt_groups(groups + extra_groups, order)
    out = []
    for g, items in sg:
        out.append("# %s,#genre#" % g)
        for n, u in items:
            out.append("%s,%s" % (n, u))
    return "\n".join(out) + "\n"


def merge_live_files(out_dir, order=None):
    """把外部源并入 output/live.txt 与 live.m3u，并按主列表顺序排序。"""
    order = order or MAIN_ORDER
    blocks = get_extras()
    changed = 0
    p_m3u = os.path.join(out_dir, "live.m3u")
    p_txt = os.path.join(out_dir, "live.txt")
    if os.path.exists(p_m3u):
        old = open(p_m3u, encoding="utf-8", errors="ignore").read()
        new = merge_m3u(old, blocks, order)
        if new != old:
            open(p_m3u, "w", encoding="utf-8", newline="\n").write(new)
            changed += 1
    if os.path.exists(p_txt):
        old = open(p_txt, encoding="utf-8", errors="ignore").read()
        new = merge_txt(old, blocks, order)
        if new != old:
            open(p_txt, "w", encoding="utf-8", newline="\n").write(new)
            changed += 1
    return changed, len(blocks)

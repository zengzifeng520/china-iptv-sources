#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台标来源：统一查 zengzifeng520/ZZF 仓库的台标库。

为什么不再自己拼地址
--------------------
原来这里（run_pipeline / crawl/main）是直接猜文件名：
    https://gcore.jsdelivr.net/gh/yuanzl77/TVlogo@master/png/{频道名}.png
频道名对不上文件名就是 404 —— 实测随机抽 12 个，只有 1 个能打开。
播放器里表现就是「大部分台没台标」。

现在改成查表
------------
ZZF 站点每次生成订阅后会导出一张表：
    https://zzf.new.tg/logos.json
内容是 {频道名(含常用变体): 台标地址}，台标图片本体放在
    https://zzf.new.tg/data/logos/
表和图片都出自同一个仓库，覆盖央视/卫视/地方/港澳台/国外 + 自制补齐。

用法
----
    from logo_source import load_table, resolve, rewrite_extinf

    load_table()                       # 每轮跑一次，失败就返回空表（不阻塞）
    logo = resolve("湖南卫视高清")      # -> url or None
    line = rewrite_extinf(extinf, ...)  # 原地替换 tvg-logo
"""
from __future__ import annotations

import json
import re
import urllib.request

LOGO_TABLE_URL = "https://zzf.new.tg/logos.json"
UA = "Mozilla/5.0 (compatible; china-iptv-sources/1.0)"

# 匹配前剥掉的清晰度/规格后缀（可叠加，如 "CCTV-5 高清 4K50p"）
_SUFFIX = [
    "蓝光高清", "超高清", "高清版", "超清版", "标清版", "高清", "高请",
    "超清", "标清", "蓝光", "FHD", "UHD", "4K50P", "4KHDR", "4K", "8K",
    "HDR", "HD", "SD", "50P", "720", "1080",
]
_TRAIL = re.compile(r"(?:[\s\-_]*(?:" + "|".join(_SUFFIX) + r"))+$", re.I)

# 台名里的品牌前缀 -> 中文台名（ZZF 库里存的是裸名：北京卫视 / 国际频道）
_BRAND = {"BRTV": "北京", "BTV": "北京"}

_TABLE: dict[str, str] = {}


def load_table(url: str = LOGO_TABLE_URL, timeout: int = 30) -> dict[str, str]:
    """拉取台标表；任何异常都返回空表（调用方按「没查到」处理）。"""
    global _TABLE
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        table = data.get("logos") if isinstance(data, dict) else None
        _TABLE = table if isinstance(table, dict) else {}
        print("  台标表 %d 条（%s）" % (len(_TABLE), data.get("updated", "?")))
    except Exception as exc:  # noqa: BLE001
        print("  台标表拉取失败：%s（沿用原 tvg-logo）" % exc)
        _TABLE = {}
    return _TABLE


def set_table(table: dict[str, str]) -> None:
    global _TABLE
    _TABLE = table or {}


def _norm(s: str) -> str:
    s = (s or "").strip()
    s = (s.replace("（", "(").replace("）", ")")
          .replace("＋", "+").replace("／", "/"))
    return re.sub(r"[\s·\-_.—–\[\]【】《》]", "", s).lower()


def _strip_suffix(s: str) -> str:
    prev = None
    s = (s or "").strip()
    while prev != s:
        prev = s
        s = _TRAIL.sub("", s).strip()
    return s


_CCTV_RE = re.compile(r"^(CCTV|CGTN)[\s\-]?(\d+\+?)(.+)$", re.I)


def _candidates(name: str) -> list[str]:
    """按优先级给出候选键：原样 → 规范化 → 剥后缀 → 剥品牌前缀。

    另外补一条央视专用规则：CCTV10科教 / CCTV-1综合 / CCTV5+体育赛事
    这类「台号 + 语义后缀」的写法，退化成台号本身（CCTV10 / CCTV-1 / CCTV5+），
    库里存的就是裸台号。
    """
    out: list[str] = []
    for base in (name, _strip_suffix(name)):
        out += [base, _norm(base)]
        m0 = _CCTV_RE.match(base.strip())
        if m0:
            bare = m0.group(1).upper() + m0.group(2)
            out += [bare, bare.replace("-", ""), _norm(bare), _norm(bare.replace("-", ""))]
    m = re.match(r"^([A-Za-z]{2,6})[\s\-_]?(.+)$", _strip_suffix(name))
    if m and re.search(r"[\u4e00-\u9fff]", m.group(2)):
        tail = m.group(2)
        out += [tail, _norm(tail)]
        brand = _BRAND.get(m.group(1).upper())
        if brand:
            out += [brand + tail, _norm(brand + tail)]
    return [x for x in dict.fromkeys(out) if x]


def resolve(name: str) -> str | None:
    """频道名 -> 台标地址；查不到返回 None（不猜）。"""
    if not _TABLE:
        return None
    for key in _candidates(name):
        url = _TABLE.get(key)
        if url:
            return url
    # 频道名带逗号（如「人狠话不多,王宝强经典电影！」）时，逐段再查一次：
    # 源数据的标题经常是「正片名, 一句话简介」，而库里存的是干净的片名。
    if "," in name or "，" in name or "、" in name:
        parts = [p.strip() for p in re.split(r"[,，、]", name) if p.strip()]
        for part in reversed(parts):
            for key in _candidates(part):
                url = _TABLE.get(key)
                if url:
                    return url
    return None


_LOGO_RE = re.compile(r'\stvg-logo="[^"]*"')
_NAME_RE = re.compile(r',([^,]*)$')


# 「猜文件名」时代的死链特征：yuanzl77/TVlogo@master/png/<频道名>.png
# 这种地址绝大多数 404，播放器拿到的是一张裂图，不如直接不写这个属性。
_DEAD_GUESS = re.compile(r'^https?://[^\s"]*TVlogo@master/png/')


def rewrite_extinf(line: str) -> tuple[str, bool]:
    """把一行 #EXTINF 的 tvg-logo 换成台标库里的地址。

    返回 (新行, 是否命中)。
    - 命中：换成台标库地址
    - 没命中但原来是「猜文件名」的死链：删掉 tvg-logo，别留裂图
    - 其它情况：原样返回（不删人家已有的可用台标）
    """
    if not line.startswith("#EXTINF"):
        return line, False
    m = _NAME_RE.search(line)
    if not m:
        return line, False
    name = m.group(1).strip()
    url = resolve(name)
    if not url:
        cur = _LOGO_RE.search(line)
        if cur and _DEAD_GUESS.match(cur.group(0).split('"')[1]):
            return _LOGO_RE.sub("", line), False
        return line, False
    new = _LOGO_RE.sub(' tvg-logo="%s"' % url, line)
    if 'tvg-logo="' not in new:            # 原本没有这个属性 -> 插在 group-title 前
        if 'group-title="' in new:
            new = new.replace(' group-title="', ' tvg-logo="%s" group-title="' % url, 1)
        else:
            new = new + ' tvg-logo="%s"' % url if ',' in new else new
    return new, True


def apply(blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """批量改写 [(extinf, url)]，返回 (新列表, 命中数)。"""
    out, hit = [], 0
    for extinf, url in blocks:
        new, ok = rewrite_extinf(extinf)
        out.append((new, url))
        hit += 1 if ok else 0
    return out, hit

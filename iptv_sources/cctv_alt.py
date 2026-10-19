# -*- coding: utf-8 -*-
"""央视备选源采集：聚合源候选 + 已知可播服务器模板生成，三级校验后补全频道。

官方静态直链（liveplayer.js 的 CDN 模板）目前只对 CCTV-1 / CCTV-13 稳定
可用；其余频道从两类来源取候选：
  1) 聚合源：fanmingming / iptv-org / 279458179 / BurningC4 等公开维护的
     央视列表（抓取后解析出 CCTV 条目）；
  2) 服务器模板：历史上验证过可播的公网直播服务器（电信 tsfile、电信 hls、
     海外镜像等），按"服务器 × 频道"批量生成候选地址。
所有候选经三级校验（master→变体→分片全通过）才写入订阅。
"""
import logging
import re

from . import cctv
from .fetcher import fetch_text_with_fallback
from .parser import parse_auto
from .validator import check_stream_best_many

log = logging.getLogger("cctv_alt")

CATEGORY = "央视"

_CCTV_NAME = {ch_id: name for ch_id, name in cctv.CCTV_CHANNELS}
# 官方频道表用 cctv5plus/cctvjilu/cctvchild 等 ID，聚合源标题 CCTV5/CCTV9/CCTV14
# 对应主频道，单独映射到展示名
_ALIAS_NAME = {
    "cctv5": "CCTV-5 体育",
    "cctv9": "CCTV-9 纪录",
    "cctv14": "CCTV-14 少儿",
}

# 服务器模板：{展示频道名: 候选地址模板函数或列表}
# 均为历史上在本项目/社区验证过可播的公网服务器；三级校验会过滤失效项。
_CHANNEL_LIST = [
    ("cctv1", "CCTV-1 综合"), ("cctv2", "CCTV-2 财经"), ("cctv3", "CCTV-3 综艺"),
    ("cctv4", "CCTV-4 中文国际"), ("cctv5plus", "CCTV-5+ 体育赛事"), ("cctv6", "CCTV-6 电影"),
    ("cctv7", "CCTV-7 国防军事"), ("cctv8", "CCTV-8 电视剧"), ("cctv9", "CCTV-9 纪录"),
    ("cctv10", "CCTV-10 科教"), ("cctv11", "CCTV-11 戏曲"), ("cctv12", "CCTV-12 社会与法"),
    ("cctv13", "CCTV-13 新闻"), ("cctv14", "CCTV-14 少儿"), ("cctv15", "CCTV-15 音乐"),
    ("cctv16", "CCTV-16 奥林匹克"), ("cctv17", "CCTV-17 农业农村"), ("cctv4k", "CCTV-4K 超高清"),
]
_TEMPLATE_SERVERS = {
    # 云南电信 tsfile（0001~0015 = CCTV1~15）
    ("cctv1", "cctv2", "cctv3", "cctv4", "cctv5", "cctv6", "cctv7",
     "cctv8", "cctv9", "cctv10", "cctv11", "cctv12", "cctv13", "cctv14", "cctv15"):
        lambda ch: f"http://124.165.251.82:85/tsfile/live/{ch[4:]:0>4}_1.m3u8?key=txiptv&playlive=1&authid=0",
    # 吉林电信 tsfile（0001~0016）
    ("cctv1", "cctv2", "cctv3", "cctv5", "cctv8", "cctv9", "cctv10", "cctv11",
     "cctv12", "cctv13", "cctv14", "cctv15", "cctv16"):
        lambda ch: f"http://61.136.172.236:9901/tsfile/live/{ch[4:]:0>4}_1.m3u8?key=txiptv&playlive=1&authid=0",
    # 黑龙江电信 hls/N（N = 频道数字）
    ("cctv3", "cctv4", "cctv6", "cctv11", "cctv13", "cctv14", "cctv15", "cctv16", "cctv17"):
        lambda ch: f"http://112.46.85.60:8009/hls/{ch[4:]}/index.m3u8",
    # 海外镜像（cctvN 命名）
    ("cctv2", "cctv3", "cctv4", "cctv16", "cctv17"):
        lambda ch: f"http://173.208.212.130:8181/1080p/cctv{ch[4:]}.m3u8",
    # 海外镜像（cctvNhd 命名）
    ("cctv1", "cctv3", "cctv5plus", "cctv9", "cctv12"):
        lambda ch: f"http://74.91.26.218:82/live/cctv{ch[4:]}.m3u8",
}
# 特例：74.91 的 5+ 路径是 cctv5p
_TEMPLATE_EXTRA = [
    ("CCTV-5 体育", "http://120.76.248.139/live/bfgd/4200000246.m3u8"),
    ("CCTV-6 电影", "http://8.138.7.223/tv/1905.php?id=LIVE3YPYZ30E7V1R0"),
    ("CCTV-5+ 体育赛事", "http://74.91.26.218:82/live/cctv5p.m3u8"),
    ("CCTV-9 纪录", "http://112.46.85.60:8009/hls/509/index.m3u8"),
    ("CCTV-13 新闻", "https://event.pull.hebtv.com/jishi/cp1.m3u8"),
    ("CCTV-14 少儿", "https://event.pull.hebtv.com/jishi/cp2.m3u8"),
]

# 聚合源中已知错标/可靠性差的服务器，直接排除（如 223.78.65.165 的 0006
# 实为 CCTV-6 却常被标为 CCTV-5）
_BLOCKED_HOSTS = {"223.78.65.165"}


def _normalize_name(title):
    """把聚合源里的标题（如 CCTV1 / CCTV5PLUS / CCTV-4K / CCTV-1 (720p)）
    归一化为标准频道名；无法识别的标题原样返回。"""
    t = title.strip()
    t = re.sub(r"[\s\-_]", "", t).upper()
    # 去掉 (720p) / [235ms] / (600p) 等后缀，只认纯频道标识
    t = re.sub(r"[(（\[【].*$", "", t)
    m = re.match(r"^CCTV(\d{1,2}|5PLUS|4K|NEWS|WORLD|EUROPE|AMERICA|CHILD|JILU|RECORD)$", t)
    if not m:
        return title.strip()
    num = m.group(1)
    alias = {
        "5PLUS": "cctv5plus",
        "4K": "cctv4k",
        "NEWS": "cctvnews",
        "WORLD": "cctvworld",
        "EUROPE": "cctveurope",
        "AMERICA": "cctvamerica",
        "CHILD": "cctvchild",
        "JILU": "cctvjilu",
        "RECORD": "cctvjilu",
    }
    ch_id = alias.get(num, f"cctv{num.lower()}")
    if ch_id in _CCTV_NAME:
        return _CCTV_NAME[ch_id]
    if ch_id in _ALIAS_NAME:
        return _ALIAS_NAME[ch_id]
    return title.strip()


def build_template_candidates():
    """生成服务器模板候选：[(category, name, url), ...]。"""
    out = []
    for chans, factory in _TEMPLATE_SERVERS.items():
        for ch_id in chans:
            name = _CCTV_NAME.get(ch_id)
            if not name:
                continue
            url = factory(ch_id)
            out.append((CATEGORY, name, url))
    for name, url in _TEMPLATE_EXTRA:
        out.append((CATEGORY, name, url))
    return out


def collect_cctv_alt_sources(
    sources,
    max_per_channel=1,
    timeout=8,
    workers=40,
    retries=1,
    max_lines_per_source=4000,
):
    """采集央视频道候选（聚合源 + 服务器模板）并三级校验。

    Args:
        sources: config 中 cctv_alt_sources 列表，[{"name":..., "urls":[...]}, ...]
        max_per_channel: 每频道最多保留的备选源条数
    Returns:
        (entries, stats)
        entries: [(category, name, url), ...]
        stats:   {源名称: {"ok": bool, "count": 条目数, "added": 新增条数}}
    """
    candidates = {}
    stats = {}
    # 0) 服务器模板候选先行（频道对应关系可控、经过验证；聚合源条目排其后，
    #    避免个别聚合源错标（如把 CCTV-6 的流标成 CCTV-5）影响结果）
    before = len(candidates)
    for cat, name, url in build_template_candidates():
        key = (name, url)
        if key not in candidates:
            candidates[key] = (cat, name, url)
    stats["服务器模板(内置)"] = {"ok": True, "count": len(candidates) - before, "added": len(candidates) - before}
    log.info("服务器模板候选 %d 条", len(candidates) - before)
    # 1) 聚合源候选
    for src in sources:
        name = src.get("name", "unknown")
        urls = src.get("urls", [])
        if not urls:
            stats[name] = {"ok": False, "count": 0, "error": "no urls"}
            continue
        try:
            text = fetch_text_with_fallback(name, urls)
        except Exception as e:  # noqa: BLE001
            stats[name] = {"ok": False, "count": 0, "error": str(e)}
            log.error("跳过央视备选源 %s: %s", name, e)
            continue
        lines = text.splitlines()
        if len(lines) > max_lines_per_source:
            log.warning("源 %s 共 %d 行，超过上限 %d，截断", name, len(lines), max_lines_per_source)
            text = "\n".join(lines[:max_lines_per_source])
        parsed = parse_auto(text)
        before = len(candidates)
        for _cat, raw_name, url in parsed:
            if not re.search(r"CCTV|央视", raw_name):
                continue
            if any(h in url for h in _BLOCKED_HOSTS):
                continue
            disp = _normalize_name(raw_name)
            key = (disp, url)
            if key not in candidates:
                candidates[key] = (CATEGORY, disp, url)
        stats[name] = {"ok": True, "count": len(parsed), "added": len(candidates) - before}
        log.info("聚合源 %s: 解析 %d 条，央视条目新增 %d 条", name, len(parsed), len(candidates) - before)

    cand_list = list(candidates.values())
    if not cand_list:
        return [], stats
    log.info("央视备选候选共 %d 条，开始三级校验...", len(cand_list))
    urls = [u for _, _, u in cand_list]
    best_map = check_stream_best_many(urls, timeout=timeout, workers=workers, retries=retries)

    by_channel = {}
    for cat, ch_name, url in cand_list:
        best = best_map.get(url)
        if not best:
            continue
        by_channel.setdefault(ch_name, []).append(best)

    entries = []
    for ch_name in sorted(by_channel):
        for url in by_channel[ch_name][:max_per_channel]:
            entries.append((CATEGORY, ch_name, url))
    log.info("央视备选源有效 %d 条（覆盖 %d 个频道）", len(entries), len(by_channel))
    return entries, stats

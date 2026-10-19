# -*- coding: utf-8 -*-
"""央视官方直播源采集。

原理：tv.cctv.com/live/ 页面通过 https://js.player.cntv.cn/creator/liveplayer.js
播放直播流。该播放器内置 12 个 CDN 域名，直播流地址遵循固定模式：

    http://{cdn_host}/{cdn_path}{channel_id}{suffix}/index.m3u8

本模块按「频道 ID × CDN 模式」构建候选地址，再逐条并发校验，保留可用源。
频道 ID 取自央视播放器与 tv.cctv.com/live 频道表（如 cctv13 新闻、cctvjilu 纪录、
cctvchild 少儿、cctv5plus 体育赛事等）。
"""
import logging

from .validator import check_stream_many

log = logging.getLogger("cctv")

# 频道 ID -> 展示名（央视官方直播频道的 ID 与展示名）
CCTV_CHANNELS = [
    ("cctv1", "CCTV-1 综合"),
    ("cctv2", "CCTV-2 财经"),
    ("cctv3", "CCTV-3 综艺"),
    ("cctv4", "CCTV-4 中文国际"),
    ("cctv5plus", "CCTV-5+ 体育赛事"),
    ("cctv6", "CCTV-6 电影"),
    ("cctv7", "CCTV-7 国防军事"),
    ("cctv8", "CCTV-8 电视剧"),
    ("cctvjilu", "CCTV-9 纪录"),
    ("cctv10", "CCTV-10 科教"),
    ("cctv11", "CCTV-11 戏曲"),
    ("cctv12", "CCTV-12 社会与法"),
    ("cctv13", "CCTV-13 新闻"),
    ("cctvchild", "CCTV-14 少儿"),
    ("cctv15", "CCTV-15 音乐"),
    ("cctv16", "CCTV-16 奥林匹克"),
    ("cctv17", "CCTV-17 农业农村"),
    ("cctvnews", "CCTV-新闻"),
    ("cctveurope", "CCTV-4 欧洲"),
    ("cctvamerica", "CCTV-4 美洲"),
    ("cctv4k", "CCTV-4K 超高清"),
]

# 频道 ID -> 台标文件名（使用 fanmingming/live 提供的台标 CDN）
CCTV_LOGO = {
    "cctv1": "CCTV1.png",
    "cctv2": "CCTV2.png",
    "cctv3": "CCTV3.png",
    "cctv4": "CCTV4.png",
    "cctv5plus": "CCTV5+.png",
    "cctv6": "CCTV6.png",
    "cctv7": "CCTV7.png",
    "cctv8": "CCTV8.png",
    "cctvjilu": "CCTV9.png",
    "cctv10": "CCTV10.png",
    "cctv11": "CCTV11.png",
    "cctv12": "CCTV12.png",
    "cctv13": "CCTV13.png",
    "cctvchild": "CCTV14.png",
    "cctv15": "CCTV15.png",
    "cctv16": "CCTV16.png",
    "cctv17": "CCTV17.png",
    "cctvnews": "CCTV13.png",
    "cctveurope": "CCTV4欧洲.png",
    "cctvamerica": "CCTV4美洲.png",
    "cctv4k": "CCTV4K.png",
}

# CDN 域名与路径模式（提取自央视播放器 liveplayer.js）
CCTV_CDNS = [
    ("ldncctvwbcdks.v.kcdnvip.com", "ldncctvwbcd/cdrmld", "_1"),
    ("ldncctvwbcdbd.a.bdydns.com", "ldncctvwbcd/cdrmld", "_1"),
    ("ldncctvwbcdali.v.myalicdn.com", "ldncctvwbcd/cdrmld", "_1"),
    ("ldncctvwbcdcnc.v.wscdns.com", "ldncctvwbcd/cdrmld", "_1"),
    ("ldncctvwbcdtxy.liveplay.myqcloud.com", "ldncctvwbcd/cdrmld", "_1"),
    ("ldncctvwbcdhwy.cntv.myhwcdn.cn", "ldncctvwbcd/cdrmld", "_1"),
    ("ldncctvwbndhwy.cntv.myhwcdn.cn", "ldncctvwbnd/ldc", "_2"),
    ("ldncctvwbndbd.a.bdydns.com", "ldncctvwbnd/ldc", "_2"),
    ("ldncctvwbndtxy.liveplay.myqcloud.com", "ldncctvwbnd/ldc", "_2"),
    ("ldncctvwbndks.v.kcdnvip.com", "ldncctvwbnd/ldc", "_2"),
    ("ldncctvwbndali.v.myalicdn.com", "ldncctvwbnd/ldc", "_2"),
    ("ldncctvwbndcnc.v.wscdns.com", "ldncctvwbnd/ldc", "_2"),
]

CATEGORY = "央视官方"
LOGO_BASE = "https://live.fanmingming.com/tv/"


def build_candidates():
    """为每个频道生成全部候选地址 [(channel_id, name, url), ...]。"""
    out = []
    for ch_id, ch_name in CCTV_CHANNELS:
        for host, path, suf in CCTV_CDNS:
            out.append((ch_id, ch_name, f"http://{host}/{path}{ch_id}{suf}/index.m3u8"))
    return out


def logo_url_for(ch_id):
    fname = CCTV_LOGO.get(ch_id)
    return f"{LOGO_BASE}{fname}" if fname else ""


def collect_cctv_sources(max_urls_per_channel=3, timeout=8, workers=40, retries=1):
    """采集央视官方直播源：并发校验所有候选 CDN 地址，每频道保留若干可用源。

    Returns:
        (entries, stats, logo_map)
        entries:  [(category, name, url), ...]，按频道顺序排列。
        stats:    {"channels_total": 频道总数, "channels_ok": 至少 1 个可用源的频道数}
        logo_map: {name: 台标 URL}
    """
    candidates = build_candidates()
    all_urls = [u for _, _, u in candidates]
    ok_map = check_stream_many(all_urls, timeout=timeout, workers=workers, retries=retries)

    entries = []
    logo_map = {}
    stats = {"channels_total": 0, "channels_ok": 0}

    per_channel = {}
    for ch_id, ch_name, url in candidates:
        per_channel.setdefault(ch_id, []).append((ch_name, url))

    for ch_id, ch_name in CCTV_CHANNELS:
        stats["channels_total"] += 1
        pool = per_channel.get(ch_id, [])
        valid = [u for _, u in pool if ok_map.get(u)]
        if not valid:
            log.warning("频道 %s (%s) 无可用源，跳过", ch_id, ch_name)
            continue
        stats["channels_ok"] += 1
        logo_map[ch_name] = logo_url_for(ch_id)
        for url in valid[:max_urls_per_channel]:
            entries.append((CATEGORY, ch_name, url))
        log.info("频道 %s (%s): %d/%d 个候选可用", ch_id, ch_name, len(valid), len(pool))

    return entries, stats, logo_map

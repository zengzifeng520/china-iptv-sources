# -*- coding: utf-8 -*-
"""酒店直播源采集：从配置的多数据源抓取、解析、合并、去重。"""
import logging

from .fetcher import fetch_text_with_fallback
from .parser import parse_auto

log = logging.getLogger("hotel")


def collect_hotel_sources(sources, max_lines_per_source=2000):
    """从配置的数据源采集酒店直播源。

    Args:
        sources: config.json 中 hotel_sources 列表，每一项为
                 {"name": 源名称, "urls": [主地址, 镜像地址...]}
        max_lines_per_source: 单个源的最大行数上限（防御异常大文件）。
    Returns:
        (entries, stats)
        entries: [(category, name, url), ...]，已跨源去重（按 频道名+url）。
        stats:   {源名称: {"ok": bool, "count": 解析条数, "added": 新增条数}}
    """
    merged = {}  # (name, url) -> (category, name, url)
    stats = {}
    for src in sources:
        name = src.get("name", "unknown")
        urls = src.get("urls", [])
        if not urls:
            log.warning("源 %s 未配置地址，跳过", name)
            stats[name] = {"ok": False, "count": 0, "error": "no urls"}
            continue
        try:
            text = fetch_text_with_fallback(name, urls)
        except Exception as e:  # noqa: BLE001
            stats[name] = {"ok": False, "count": 0, "error": str(e)}
            log.error("跳过源 %s: %s", name, e)
            continue

        lines = text.splitlines()
        if len(lines) > max_lines_per_source:
            log.warning(
                "源 %s 共 %d 行，超过上限 %d，截断处理",
                name, len(lines), max_lines_per_source,
            )
            text = "\n".join(lines[:max_lines_per_source])

        parsed = parse_auto(text)
        before = len(merged)
        for cat, ch_name, url in parsed:
            key = (ch_name, url)
            if key not in merged:
                merged[key] = (cat, ch_name, url)
        added = len(merged) - before
        stats[name] = {"ok": True, "count": len(parsed), "added": added}
        log.info("源 %s: 解析 %d 条，新增 %d 条", name, len(parsed), added)

    entries = list(merged.values())
    return entries, stats

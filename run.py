# -*- coding: utf-8 -*-
"""入口脚本：采集酒店源 + 央视源 → 并发校验 → 生成订阅文件。

用法:
    python run.py               # 读取同目录 config.json（缺失则用内置默认配置）
    python run.py -c my.json    # 指定配置文件
    python run.py --verbose     # 输出调试日志

退出码: 0 成功（生成了有效订阅）；1 失败（没有任何可用源）。
"""
import argparse
import json
import logging
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from iptv_sources.cctv import collect_cctv_sources  # noqa: E402
from iptv_sources.cctv_alt import collect_cctv_alt_sources  # noqa: E402
from iptv_sources.generator import now_str, write_outputs  # noqa: E402
from iptv_sources.hotel import collect_hotel_sources  # noqa: E402
from iptv_sources.validator import check_stream_many  # noqa: E402

log = logging.getLogger("run")

DEFAULT_CONFIG = {
    "hotel_sources": [
        {
            "name": "ssili126/tv 酒店源",
            "urls": [
                "https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt",
                "https://ghproxy.net/https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt",
                "https://gh-proxy.com/https://raw.githubusercontent.com/ssili126/tv/main/itvlist.txt",
                "https://cdn.jsdelivr.net/gh/ssili126/tv@main/itvlist.txt",
            ],
        },
        {
            "name": "fanmingming/live ITV",
            "urls": [
                "https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/itv.m3u",
                "https://ghproxy.net/https://raw.githubusercontent.com/fanmingming/live/main/tv/m3u/itv.m3u",
                "https://cdn.jsdelivr.net/gh/fanmingming/live@main/tv/m3u/itv.m3u",
                "https://live.fanmingming.com/tv/m3u/itv.m3u",
            ],
        },
    ],
    "cctv": {"max_urls_per_channel": 3},
    "validator": {"timeout": 8, "workers": 40, "retries": 1},
    "output_dir": "output",
    "max_lines_per_source": 2000,
    "max_hotel_candidates": 3000,
}


def load_config(path):
    candidates = [path] if path else []
    candidates.append(os.path.join(BASE_DIR, "config.json"))
    for p in candidates:
        if p and os.path.exists(p):
            log.info("使用配置文件: %s", p)
            with open(p, encoding="utf-8") as f:
                return json.load(f)
    log.info("未找到配置文件，使用内置默认配置")
    return DEFAULT_CONFIG


def main():
    ap = argparse.ArgumentParser(description="采集并生成 IPTV 直播源订阅")
    ap.add_argument("-c", "--config", help="配置文件路径（默认 config.json）")
    ap.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    val = cfg["validator"]
    out_dir = cfg.get("output_dir", "output")
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(BASE_DIR, out_dir)
    os.makedirs(out_dir, exist_ok=True)

    timeout = val.get("timeout", 8)
    workers = val.get("workers", 40)
    retries = val.get("retries", 1)
    max_urls_per_channel = cfg["cctv"].get("max_urls_per_channel", 3)

    # 1) 央视官方源（内部已校验）
    log.info("===== 采集央视官方源 =====")
    cctv_entries, cctv_stats, logo_map = collect_cctv_sources(
        max_urls_per_channel=max_urls_per_channel,
        timeout=timeout,
        workers=workers,
        retries=retries,
    )
    log.info("央视源: %d 个频道可用，共 %d 条流", cctv_stats["channels_ok"], len(cctv_entries))

    # 2) 央视备选源（官方直链缺失的频道，从聚合源补全；内部已三级校验）
    log.info("===== 采集央视备选源 =====")
    cctv_alt_cfg = cfg.get("cctv_alt", {})
    cctv_alt_sources = cctv_alt_cfg.get("sources", [])
    cctv_alt_entries, cctv_alt_stats = ({}, {})
    if cctv_alt_sources:
        cctv_alt_entries, cctv_alt_stats = collect_cctv_alt_sources(
            cctv_alt_sources,
            max_per_channel=cctv_alt_cfg.get("max_per_channel", 1),
            timeout=timeout,
            workers=workers,
            retries=retries,
        )
        log.info("央视备选源: %d 条", len(cctv_alt_entries))
    else:
        log.info("未配置央视备选源，跳过")

    # 3) 酒店直播源
    log.info("===== 采集酒店直播源 =====")
    hotel_entries, hotel_stats = collect_hotel_sources(
        cfg["hotel_sources"],
        max_lines_per_source=cfg.get("max_lines_per_source", 2000),
    )
    max_candidates = cfg.get("max_hotel_candidates", 1200)
    if len(hotel_entries) > max_candidates:
        log.warning(
            "酒店源候选 %d 条超过上限 %d，截断（可用 max_hotel_candidates 调大）",
            len(hotel_entries), max_candidates,
        )
        hotel_entries = hotel_entries[:max_candidates]
    log.info("酒店源: 去重后共 %d 条候选", len(hotel_entries))

    # 3) 校验酒店源
    log.info("===== 校验酒店源（%d 条，并发 %d）=====", len(hotel_entries), workers)
    hotel_valid = []
    if hotel_entries:
        ok_map = check_stream_many(
            [u for _, _, u in hotel_entries],
            timeout=timeout,
            workers=workers,
            retries=retries,
        )
        hotel_valid = [
            (c, n, u) for c, n, u in hotel_entries if ok_map.get(u)
        ]
        log.info("酒店源有效 %d / %d", len(hotel_valid), len(hotel_entries))

    # 4) 汇总排序：央视官方在前；备选其次；酒店源按分类分组（组内保持原顺序）
    hotel_valid.sort(key=lambda x: x[0])
    all_entries = cctv_entries + cctv_alt_entries + hotel_valid

    # 5) 生成输出
    updated = now_str()
    stats = {
        "updated_at": updated,
        "total": len(all_entries),
        "cctv": cctv_stats,
        "cctv_alt": cctv_alt_stats,
        "hotel": hotel_stats,
        "hotel_candidates": len(hotel_entries),
        "hotel_valid": len(hotel_valid),
        "categories": {},
    }
    for c, _n, _u in all_entries:
        stats["categories"][c] = stats["categories"].get(c, 0) + 1

    write_outputs(all_entries, out_dir, logo_map=logo_map, updated=updated, stats=stats)

    print()
    print("=" * 56)
    print(f"更新完成: {updated}")
    print(f"输出目录: {out_dir}")
    print(f"可用频道/流总数: {len(all_entries)}")
    print(f"  央视官方: {len(cctv_entries)} 条 / 央视备选: {len(cctv_alt_entries)} 条 / 酒店源有效: {len(hotel_valid)} 条（候选 {len(hotel_entries)} 条）")
    for cat, cnt in stats["categories"].items():
        print(f"  [{cat}] {cnt} 条")
    print("已生成: live.txt, live.m3u, stats.json")
    print("=" * 56)

    sys.exit(0 if all_entries else 1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「独立订阅」：crawl/extra/*.txt -> output/*.m3u

现在管两条：
    crawl/extra/jingdian.txt  ->  output/jingdian.m3u   group「经典轮播」
    crawl/extra/mingxing.txt  ->  output/mingxing.m3u   group「明星剧场」
    crawl/extra/huya.txt      ->  output/huya.m3u       group「虎牙一起看」
    crawl/extra/yy.txt        ->  output/yy.m3u         group「YY轮播」

为什么单独一条
--------------
这些地址是第三方房间号 / 直连 flv，跟运营商组播完全两套来源：
不进每小时巡检（crawl/checked），也不并入用户订阅——单独一个文件，谁要谁订阅。

所以本脚本只做格式转换：读 txt（name,url，支持 #genre# 分组行）→ 写 m3u，
台标照旧查 ZZF 台标库（logo_source），不加任何探测逻辑。

用法
----
  python github/gen_extra.py                 # 全部生成
  python github/gen_extra.py --only mingxing # 只生成明星剧场
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
import logo_source  # noqa: E402

CST = timezone(timedelta(hours=8))

# key -> (源文件, 输出文件, 分组名)
LISTS = {
    "jingdian": ("crawl/extra/jingdian.txt", "output/jingdian.m3u", "经典轮播"),
    "mingxing": ("crawl/extra/mingxing.txt", "output/mingxing.m3u", "明星剧场"),
    "huya": ("crawl/extra/huya.txt", "output/huya.m3u", "虎牙一起看"),
    "yy": ("crawl/extra/yy.txt", "output/yy.m3u", "YY轮播"),
    "chunwan": ("crawl/extra/chunwan.txt", "output/chunwan.m3u", "历届春晚"),
}

# 分组 emoji（与 run_pipeline.py 的 GROUP_EMOJI 风格一致）
GROUP_EMOJI = {
    "经典轮播": "🎞️",
    "明星剧场": "⭐",
    "虎牙一起看": "🐯",
    "YY轮播": "🎤",
    "历届春晚": "🎆",
}


def parse_src(path: str) -> list[tuple[str, str]]:
    """读 name,url 列表；同一地址只留第一条（避免播放器里同一路出现两次）。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.endswith("#genre#") or "://" not in s:
                continue
            name, url = s.rsplit(",", 1)
            name, url = name.strip(), url.strip()
            if not name or url in seen:
                continue
            seen.add(url)
            out.append((name, url))
    return out


def build(key: str) -> int:
    src_rel, out_rel, group = LISTS[key]
    src, out = os.path.join(ROOT, src_rel), os.path.join(ROOT, out_rel)
    if not os.path.exists(src):
        print(f"[{key}] 缺少源文件 {src_rel}")
        return 1
    entries = parse_src(src)
    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    disp = f"{GROUP_EMOJI.get(group, '')} {group}".strip()
    lines = ['#EXTM3U x-tvg-url="https://zzf.new.tg/epg.xml"',
             f"# 更新时间：{stamp}（北京时间）",
             f"# {disp} {len(entries)} 条（独立订阅，不参与巡检）"]
    hit = 0
    for name, url in entries:
        logo = logo_source.resolve(name)
        hit += 1 if logo else 0
        attr = f' tvg-logo="{logo}"' if logo else ""
        lines.append(f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}"{attr} '
                     f'group-title="{disp}",{name}')
        lines.append(url)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[{key}] {out_rel}：{len(entries)} 条，台标 {hit} 条，分组「{disp}」")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=sorted(LISTS), default=None)
    args = ap.parse_args()
    logo_source.load_table()
    keys = [args.only] if args.only else list(LISTS)
    rc = 0
    for k in keys:
        rc |= build(k)
    return rc


if __name__ == "__main__":
    sys.exit(main())

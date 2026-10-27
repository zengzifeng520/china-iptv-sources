#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成「经典轮播」独立订阅：crawl/extra/jingdian.txt -> output/jingdian.m3u

为什么单独一条
--------------
这批地址是第三方房间号跳转（/huya/xxx、/yy/xxx），跟运营商组播完全两套来源，
不进每小时巡检（crawl/checked），也不并入用户订阅——单独一个文件，谁要谁订阅。

所以本脚本只做格式转换：读 txt（name,url 或 #genre# 分组）→ 写 m3u，
台标照旧查 ZZF 台标库（logo_source），不加任何探测逻辑。

用法
----
  python github/gen_jingdian.py
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
sys.path.insert(0, ROOT)
import logo_source  # noqa: E402

SRC = os.path.join(ROOT, "crawl", "extra", "jingdian.txt")
OUT = os.path.join(ROOT, "output", "jingdian.m3u")
GROUP = "经典轮播"
CST = timezone(timedelta(hours=8))


def main() -> int:
    if not os.path.exists(SRC):
        print(f"缺少源文件 {SRC}")
        return 1
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    with open(SRC, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s or s.endswith("#genre#") or "://" not in s:
                continue
            name, url = s.rsplit(",", 1)
            name, url = name.strip(), url.strip()
            if not name or url in seen:
                continue
            seen.add(url)
            entries.append((name, url))

    logo_source.load_table()
    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    lines = ['#EXTM3U x-tvg-url="https://zzf.new.tg/epg.xml"',
             f"# 更新时间：{stamp}（北京时间）",
             f"# 经典轮播 {len(entries)} 条（独立订阅，不参与巡检）"]
    hit = 0
    for name, url in entries:
        logo = logo_source.resolve(name)
        hit += 1 if logo else 0
        attr = f' tvg-logo="{logo}"' if logo else ""
        lines.append(f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}"{attr} '
                     f'group-title="{GROUP}",{name}')
        lines.append(url)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    print(f"生成 {OUT}：{len(entries)} 条，台标命中 {hit} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())

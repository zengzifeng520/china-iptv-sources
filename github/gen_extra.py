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

# 推广页：订阅里「推送时间」那条指向它（与 run_pipeline.py 保持一致）
PROMO_URL = "https://zengzifeng.ccwu.cc/output/media/promo.mp4"
# 订阅头部的 EPG 地址（统一走本站，不再指向 zzf.new.tg）
EPG_URL = "https://zengzifeng.ccwu.cc/output/epg.xml"

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


def render(key: str, stamp: str | None = None):
    """生成一份独立订阅的文本（不写盘）。

    返回 (m3u_text, txt_text, entries, logo_hits, 显示分组名)。
    """
    src_rel, out_rel, group = LISTS[key]
    src = os.path.join(ROOT, src_rel)
    if not os.path.exists(src):
        raise FileNotFoundError(src_rel)
    entries = parse_src(src)
    stamp = stamp or datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    disp = f"{GROUP_EMOJI.get(group, '')} {group}".strip()
    lines = ['#EXTM3U url-tvg="%s"' % EPG_URL,
             f"# 更新时间：{stamp}（北京时间）",
             f"# {disp} {len(entries)} 条（独立订阅，不参与巡检）",
             # 推送时间组：所有订阅统一带，方便判断源是否还在更新
             '#EXTINF:-1 tvg-id="推送时间" tvg-name="推送时间" '
             'group-title="🕒 推送时间",%s 推送' % stamp,
             PROMO_URL]
    hit = 0
    for name, url in entries:
        logo = logo_source.resolve(name)
        hit += 1 if logo else 0
        attr = f' tvg-logo="{logo}"' if logo else ""
        lines.append(f'#EXTINF:-1 tvg-id="{name}" tvg-name="{name}"{attr} '
                     f'group-title="{disp}",{name}')
        lines.append(url)
    # 同名 .txt（DIYP / 酷9 直接读 txt）：分组,#genre# + 频道名,地址
    tlines = (["🕒 推送时间,#genre#", f"{stamp} 推送,{PROMO_URL}", f"{disp},#genre#"]
              + [f"{n},{u}" for n, u in entries])
    return ("\n".join(lines) + "\n", "\n".join(tlines) + "\n", entries, hit, disp)


def build(key: str, stamp: str | None = None) -> int:
    """写盘版（命令行单独跑时用）。"""
    src_rel, out_rel, group = LISTS[key]
    src = os.path.join(ROOT, src_rel)
    if not os.path.exists(src):
        print(f"[{key}] 缺少源文件 {src_rel}")
        return 1
    m3u, txt, entries, hit, disp = render(key, stamp)
    out = os.path.join(ROOT, out_rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(m3u)
    with open(os.path.splitext(out)[0] + ".txt", "w", encoding="utf-8", newline="\n") as f:
        f.write(txt)
    print(f"[{key}] {out_rel} + .txt：{len(entries)} 条，台标 {hit} 条，分组「{disp}」")
    return 0


def build_batch(push_time: str | None = None):
    """给 run_pipeline 用：返回 [(仓库相对路径, bytes)]，随每轮巡检一起提交。

    推送时间统一用这一轮巡检的时间，播放器里看到的就是「几点巡检过」。
    """
    if not logo_source._TABLE:
        logo_source.load_table()
    out: list[tuple[str, bytes]] = []
    for key in LISTS:
        try:
            m3u, txt, entries, hit, disp = render(key, push_time)
        except FileNotFoundError as exc:
            print(f"[{key}] 跳过：缺少源文件 {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"[{key}] 生成失败：{exc}")
            continue
        base_rel = os.path.splitext(LISTS[key][1])[0]
        out.append((base_rel + ".m3u", m3u.encode("utf-8")))
        out.append((base_rel + ".txt", txt.encode("utf-8")))
        print(f"[{key}] {len(entries)} 条，台标 {hit}，分组「{disp}」")
    return out


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

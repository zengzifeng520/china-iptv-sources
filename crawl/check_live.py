#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按小时巡检 live.m3u / live.txt，剔除「连续失效」的线路，输出到 checked/。

设计要点：
- 只做 HTTP 探测（快），不跑 ffprobe；深度检测留给每天的全量生成任务。
- 同一轮内失败会重试一次；仍失败才计入 state 的失败次数。
- 连续 MAX_FAILS 次（默认 2）失败才真正剔除，避免网络抖动误杀。
- 恢复正常的线路会自动从 state 中移除，失败计数归零。
- 输出独立文件（checked/live.m3u、checked/live.txt），不覆盖每日生成的 live.*。
- 存活率过低（默认 <30%）时判定为网络异常，直接退出且不写文件，防止把列表清空。
"""

import asyncio
import json
import os
import re
import sys
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import aiohttp

BASE_DIR = os.environ.get("CHECK_DIR") or os.path.dirname(os.path.abspath(__file__))
SRC_M3U = os.path.join(BASE_DIR, "live.m3u")
SRC_TXT = os.path.join(BASE_DIR, "live.txt")
OUT_DIR = os.environ.get("CHECK_OUT") or os.path.join(BASE_DIR, "checked")
STATE_FILE = os.path.join(OUT_DIR, "state.json")
STATS_FILE = os.path.join(OUT_DIR, "stats.json")

TIMEOUT = float(os.environ.get("CHECK_TIMEOUT", "6"))
CONCURRENCY = max(1, int(os.environ.get("CHECK_CONCURRENCY", "100")))
MAX_FAILS = max(1, int(os.environ.get("MAX_FAILS", "2")))
MIN_ALIVE_RATIO = float(os.environ.get("MIN_ALIVE_RATIO", "0.3"))
ATTEMPTS = max(1, int(os.environ.get("CHECK_ATTEMPTS", "2")))
BODY_WAIT = float(os.environ.get("CHECK_BODY_WAIT", "4"))
# 组播转单播（udpxy）代理：能否出数据取决于探测点所在运营商，
# 云端 runner 天然判不准，因此允许「200 + udpxy 响应」算存活。
LENIENT_UDPXY = os.environ.get("LENIENT_UDPXY", "0") == "1"

CST = timezone(timedelta(hours=8))

SUFFIX_RE = re.compile(r"\$LR.*$")


def base_url(line):
    """去掉 $LR—IPV4【线路N】… 这类后缀，得到可探测的原始地址。"""
    return SUFFIX_RE.sub("", (line or "").strip())


def probe_target(line):
    u = base_url(line)
    return u.replace("&amp;", "&").replace("&#38;", "&")


def is_probeable(url):
    return url.startswith("http://") or url.startswith("https://")


def parse_m3u(path):
    header = "#EXTM3U"
    entries = []
    pending = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            if s.startswith("#EXTM3U"):
                header = s
            elif s.startswith("#EXTINF"):
                pending = s
            elif s.startswith("#"):
                continue
            else:
                entries.append({"extinf": pending, "url_line": s})
                pending = None
    return header, entries


def parse_txt(path):
    groups = OrderedDict()
    current = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for raw in f:
            s = raw.strip()
            if not s:
                continue
            if "#genre#" in s:
                current = s.split(",")[0].strip()
                groups.setdefault(current, [])
                continue
            if current is None:
                continue
            name, sep, url = s.partition(",")
            if sep and url.strip():
                groups[current].append((name.strip(), url.strip()))
    return groups


async def probe(session, sem, url):
    """返回 True/False。

    判定顺序：
    1) 收到前 1KB 数据 -> 存活；
    2) LENIENT_UDPXY=1 时，udpxy 代理返回 200 -> 视为存活（组播源出流依赖运营商，
       云端无法判断）；
    3) rtp/udp 等非 HTTP 协议无法探测，保留。
    """
    if not is_probeable(url):
        return True
    async with sem:
        for attempt in range(ATTEMPTS):
            try:
                timeout = aiohttp.ClientTimeout(total=TIMEOUT + BODY_WAIT)
                async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        raise RuntimeError("HTTP %s" % resp.status)
                    server = (resp.headers.get("Server") or "").lower()
                    # udpxy 先回头再出流，宽松模式下见到响应头即可判定
                    if LENIENT_UDPXY and "udpxy" in server:
                        return True
                    try:
                        chunk = await asyncio.wait_for(resp.content.read(1024), timeout=BODY_WAIT)
                    except asyncio.TimeoutError:
                        chunk = b""
                    if chunk.strip():
                        return True
            except Exception:
                pass
            if attempt + 1 < ATTEMPTS:
                await asyncio.sleep(1.0)
        return False


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def main():
    started = time.time()
    now = datetime.now(CST).isoformat(timespec="seconds")

    header, m3u_entries = (parse_m3u(SRC_M3U) if os.path.exists(SRC_M3U) else ("#EXTM3U", []))
    txt_groups = parse_txt(SRC_TXT) if os.path.exists(SRC_TXT) else OrderedDict()

    targets = OrderedDict()
    for e in m3u_entries:
        t = probe_target(e["url_line"])
        if t:
            targets[t] = True
    for entries in txt_groups.values():
        for _, url in entries:
            t = probe_target(url)
            if t:
                targets[t] = True

    if not targets:
        print("源文件为空，退出")
        return 0

    urls = list(targets.keys())
    print(
        "待检 %d 个地址，并发 %d，超时 %ss，每轮最多 %d 次，udpxy 宽松模式 %s"
        % (len(urls), CONCURRENCY, TIMEOUT, ATTEMPTS, "开" if LENIENT_UDPXY else "关")
    )

    async def run():
        connector = aiohttp.TCPConnector(limit=CONCURRENCY, ssl=False, force_close=True)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; playlist-check/1.0)"}
        async with aiohttp.ClientSession(connector=connector, headers=headers) as session:
            sem = asyncio.Semaphore(CONCURRENCY)
            return await asyncio.gather(*(probe(session, sem, u) for u in urls))

    results = asyncio.run(run())
    alive_map = dict(zip(urls, results))
    alive_cnt = sum(1 for v in results if v)
    ratio = alive_cnt / len(urls)
    print(f"存活 {alive_cnt}/{len(urls)}（{ratio:.1%}），耗时 {time.time() - started:.0f}s")

    if len(urls) >= 50 and ratio < MIN_ALIVE_RATIO:
        print(f"::error::存活率 {ratio:.1%} 低于阈值 {MIN_ALIVE_RATIO:.0%}，疑似网络异常，本轮不写入")
        return 2

    state = load_state()
    new_state = {}
    dead = set()
    for url, ok in alive_map.items():
        if ok:
            continue
        prev = state.get(url) or {}
        fails = int(prev.get("fails", 0)) + 1
        new_state[url] = {
            "fails": fails,
            "last_fail": now,
            "first_fail": prev.get("first_fail") or now,
        }
        if fails >= MAX_FAILS:
            dead.add(url)

    kept_m3u = [e for e in m3u_entries if probe_target(e["url_line"]) not in dead]
    removed_m3u = len(m3u_entries) - len(kept_m3u)

    kept_txt = OrderedDict()
    for group, entries in txt_groups.items():
        kept = [(n, u) for n, u in entries if probe_target(u) not in dead]
        if kept:
            kept_txt[group] = kept
    removed_txt = sum(len(v) for v in txt_groups.values()) - sum(len(v) for v in kept_txt.values())

    os.makedirs(OUT_DIR, exist_ok=True)

    stamp = datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")
    m3u_lines = [header, f"# 更新时间：{stamp}（北京时间）"]
    for e in kept_m3u:
        if e["extinf"]:
            m3u_lines.append(e["extinf"])
        m3u_lines.append(e["url_line"])
    m3u_content = "\n".join(m3u_lines) + "\n"

    txt_lines = [f"# 更新时间：{stamp}（北京时间）"]
    for group, entries in kept_txt.items():
        txt_lines.append(f"{group},#genre#")
        for name, url in entries:
            txt_lines.append(f"{name},{url}")
    txt_content = "\n".join(txt_lines) + "\n"

    def _strip_time(text):
        return "\n".join(l for l in text.splitlines() if not l.startswith("# 更新时间"))

    for path, content in (
        (os.path.join(OUT_DIR, "live.m3u"), m3u_content),
        (os.path.join(OUT_DIR, "live.txt"), txt_content),
    ):
        try:
            with open(path, encoding="utf-8") as f:
                old = f.read()
        except Exception:
            old = None
        if old is not None and _strip_time(old) == _strip_time(content):
            continue
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")

    stats = {
        "updated": now,
        "checked": len(urls),
        "alive": alive_cnt,
        "alive_ratio": round(ratio, 4),
        "max_fails": MAX_FAILS,
        "dead_now": len(dead),
        "removed_m3u_lines": removed_m3u,
        "removed_txt_lines": removed_txt,
        "kept_m3u_entries": len(kept_m3u),
        "kept_txt_entries": sum(len(v) for v in kept_txt.values()),
        "duration_s": round(time.time() - started, 1),
    }
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
        f.write("\n")

    print(
        f"剔除 {len(dead)} 个连续失败地址；m3u 条目 {len(m3u_entries)} -> {len(kept_m3u)}，"
        f"txt 条目 {sum(len(v) for v in txt_groups.values())} -> {stats['kept_txt_entries']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
